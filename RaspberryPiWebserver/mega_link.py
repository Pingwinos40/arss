# mega_link.py
# Single owner of the USB serial link to the Arduino Mega motor controller
# (ArduinoFirmwares/ArduinoMegaControl.ino). Issue #32.
#
# Structure (after the dea-characterization-board host link):
#   - The reader thread owns the port lifecycle: discovery, open, read, close,
#     and reconnect with exponential backoff. It splits bytes into lines and
#     routes each line to the command that is waiting for it.
#   - The writer thread drains a FIFO command queue with exactly one queued
#     command in flight. Each command carries a matcher derived from the
#     firmware's reply format and a timeout. There is no fixed sleep and no
#     read-until-timeout.
#   - STOP and KILL bypass the queue: the calling thread writes them under the
#     write lock immediately (the lock is held only for the write itself, never
#     while waiting for a reply). The Mega firmware has no deadman yet (#9), so
#     STOP must never wait behind other traffic.
#   - The poll thread issues STATUS and SENSOR? through the queue every
#     PollPeriodS and keeps timestamped snapshots. HTTP routes read those
#     snapshots and never touch the port. The poll is also the host keepalive.
#
# Firmware protocol (derived from ArduinoMegaControl.ino HandleLine/setup):
#   command              reply line
#   PING                 PONG
#   STATUS               {"running":b,"hz":f,"rpm":f,"dir":0|1,"ppr":n,
#                         "steps_remaining":n,"max_rpm":n,"ramping":b,
#                         "ramp_up":f,"ramp_down":f,"target_rpm":f,
#                         "env":{"temp_c":f|null,"humidity":f|null}}
#   START                OK START
#   STOP, KILL           OK STOP           (both tokens give the same reply)
#   SETFREQ <hz>         OK SETFREQ <target hz, 3 dp>   (clamped 0.5..50000)
#   SETRPM <rpm>         OK SETRPM <rpm, 2 dp> Hz=<target hz, 3 dp>
#   SETPPR <n>           OK SETPPR <n>                  (clamped 1..200000)
#   SETDIR <tok>         OK SETDIR <0|1>   (1 if tok starts with CW (any case),
#                                           '1' or '+'; anything else is 0)
#   SETSTEPS <n>         OK SETSTEPS <n>   (n <= 0 stops: OK SETSTEPS 0)
#   SETMAXRPM <n>        OK SETMAXRPM <n>               (clamped 1..100000)
#   SETRAMP <up> <down>  OK SETRAMP UP=<1 dp> DOWN=<1 dp>
#   SENSOR?              ENV <temp C, 1 dp | nan>, <RH, 1 dp | nan> %RH
#   anything else        ERR?
#   unsolicited          READY             (end of setup(), after every reset)
# The firmware has no correlation ids and no echo. It processes lines strictly
# in order, so replies arrive in the order the commands were written. Its line
# buffer is char[64] including the terminator; longer lines are silently
# truncated, so commands are limited to 63 characters here.

import collections
import glob
import json
import math
import queue
import random
import re
import threading
import time
from datetime import datetime, timezone

import serial
from serial.serialutil import SerialException, SerialTimeoutException

# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------
MEGA_BAUD = 115200
MEGA_PORT_HINTS = ["/dev/ttyACM*", "/dev/ttyUSB*"]  # Mega usually shows as ttyACM*
MAX_COMMAND_LEN = 63

READ_POLL_S = 0.1              # serial read timeout; bounds reader loop latency
WRITE_TIMEOUT_S = 0.5          # bounds a write if the USB endpoint stops draining
DEFAULT_POLL_PERIOD_S = 0.5
DEFAULT_STALE_AFTER_S = 2.0
DEFAULT_COMMAND_TIMEOUT_S = 1.0
DEFAULT_QUEUE_WAIT_S = 3.0     # how long a caller waits for its turn in the queue
DEFAULT_RESET_WAIT_S = 3.0     # wait for READY after open (bootloader plus setup)
DEFAULT_ORPHAN_TTL_S = 5.0     # how long a timed-out command may still drain its late reply
BACKOFF_MIN_S = 0.5
BACKOFF_MAX_S = 5.0
BACKOFF_JITTER_S = 0.25
STABLE_SESSION_S = 5.0         # a session this long resets the reconnect backoff

LINK_LIVE = "LIVE"
LINK_STALE = "STALE"
LINK_DOWN = "DOWN"


# -----------------------------------------------------------------------------
# Errors. Routes map these to HTTP codes (see app.py LinkErrorResponse).
# -----------------------------------------------------------------------------
class LinkError(Exception):
    """Base class for link failures."""


class LinkDown(LinkError):
    """Port not open, or it dropped while the command was pending."""


class LinkNotReady(LinkDown):
    """Port is open but the firmware state is not known yet (no STATUS since
    the last open or reset), so motion and configuration commands are held."""


class LinkTimeout(LinkError):
    """No matching reply inside the command timeout, or no turn in the queue."""


class FirmwareError(LinkError):
    """The firmware answered ERR? or sent a reply that cannot be parsed."""


class CommandRejected(ValueError):
    """The command text is not a single valid firmware command line."""


# -----------------------------------------------------------------------------
# Reply matchers and parsers
# -----------------------------------------------------------------------------
def MatchStatusLine(line):
    return line.startswith("{") and line.endswith("}")


def MatchEnvLine(line):
    return line.startswith("ENV ")


def _MatchExact(text):
    def Matcher(line):
        return line == text
    return Matcher


def _MatchPrefix(prefix):
    def Matcher(line):
        return line.startswith(prefix)
    return Matcher


# token -> (matcher, takes_arguments)
COMMAND_TABLE = {
    "PING": (_MatchExact("PONG"), False),
    "STATUS": (MatchStatusLine, False),
    "SENSOR?": (MatchEnvLine, False),
    "START": (_MatchExact("OK START"), False),
    "STOP": (_MatchExact("OK STOP"), False),
    "KILL": (_MatchExact("OK STOP"), False),
    "SETFREQ": (_MatchPrefix("OK SETFREQ "), True),
    "SETRPM": (_MatchPrefix("OK SETRPM "), True),
    "SETPPR": (_MatchPrefix("OK SETPPR "), True),
    "SETDIR": (_MatchPrefix("OK SETDIR "), True),
    "SETSTEPS": (_MatchPrefix("OK SETSTEPS "), True),
    "SETMAXRPM": (_MatchPrefix("OK SETMAXRPM "), True),
    "SETRAMP": (_MatchPrefix("OK SETRAMP "), True),
}
QUERY_TOKENS = ("PING", "STATUS", "SENSOR?")
STOP_TOKENS = ("STOP", "KILL")
ERROR_LINE = "ERR?"
READY_LINE = "READY"


def CheckCommand(text):
    """Validate one command line and return its token.

    Rejects CR, LF and every other non-printable or non-ASCII character, so a
    caller can never smuggle a second command onto the wire (#13). Only the
    tokens the firmware implements are accepted.
    """
    if not isinstance(text, str) or not text:
        raise CommandRejected("command must be a non-empty string")
    for ch in text:
        code = ord(ch)
        if code < 0x20 or code > 0x7E:
            raise CommandRejected("command contains a control or non-ASCII character: %r" % text)
    if len(text) > MAX_COMMAND_LEN:
        raise CommandRejected("command is %d chars; the firmware truncates lines over %d"
                              % (len(text), MAX_COMMAND_LEN))
    if text != text.strip():
        raise CommandRejected("command has leading or trailing whitespace: %r" % text)
    token = text.split(" ", 1)[0]
    if token not in COMMAND_TABLE:
        raise CommandRejected("unknown command token %r" % token)
    takesArgs = COMMAND_TABLE[token][1]
    if not takesArgs and text != token:
        raise CommandRejected("%s takes no arguments" % token)
    if takesArgs and text == token:
        raise CommandRejected("%s needs an argument" % token)
    return token


def _FiniteOrNone(value):
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: _FiniteOrNone(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_FiniteOrNone(v) for v in value]
    return value


def ParseStatusLine(line):
    """Parse the STATUS JSON line. Returns a dict, or None if unparseable."""
    try:
        data = json.loads(line, parse_constant=lambda _c: None)
    except ValueError:
        return None
    if not isinstance(data, dict) or "running" not in data:
        return None
    return _FiniteOrNone(data)


_ENV_RE = re.compile(r"^ENV\s+([^,\s]+)\s*,\s*([^\s]+)\s+%RH$")


def ParseEnvLine(line):
    """Parse 'ENV <t>, <h> %RH'. nan becomes None. Returns None if malformed."""
    m = _ENV_RE.match(line)
    if not m:
        return None
    out = {}
    for key, raw in (("temp_c", m.group(1)), ("humidity", m.group(2))):
        try:
            value = float(raw)
        except ValueError:
            return None
        out[key] = value if math.isfinite(value) else None
    return out


def UtcNowIso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# -----------------------------------------------------------------------------
# Port discovery and open (replaceable for tests)
# -----------------------------------------------------------------------------
def FindMegaPort():
    for pat in MEGA_PORT_HINTS:
        ports = sorted(glob.glob(pat))
        if ports:
            return ports[0]
    return None


def OpenMegaSerial(portName):
    # Opening the port asserts DTR, which resets the Mega 2560 through its
    # auto-reset circuit. The firmware restarts from setup(): output stopped,
    # 1 kHz, PPR 200, max RPM 1200, ramps 0. The link therefore treats every
    # open as a new firmware session, waits for READY, and reads STATUS before
    # it accepts motion or configuration commands.
    # exclusive=True takes a POSIX flock so a second dashboard process cannot
    # open the same port and interleave commands with this one.
    return serial.Serial(port=portName, baudrate=MEGA_BAUD, timeout=READ_POLL_S,
                         write_timeout=WRITE_TIMEOUT_S, exclusive=True)


# -----------------------------------------------------------------------------
# Internal records
# -----------------------------------------------------------------------------
class _Pending:
    """One command awaiting its reply line."""

    def __init__(self, text, token, timeoutS, priority, requireStatus):
        self.Text = text
        self.Token = token
        self.Matcher = COMMAND_TABLE[token][0]
        self.TimeoutS = timeoutS
        self.Priority = priority
        self.RequireStatus = requireStatus
        self.Lines = []
        self.Done = threading.Event()
        self.Error = None
        self.SentMono = None
        self.WriteDoneMono = None
        self.ReplyMono = None
        self.Cancelled = False
        self.TimedOut = False      # an orphan: still drains its late reply
        self.ExpireMono = None
        self.IsBarrier = False


class _Snapshot:
    def __init__(self, data, generation):
        self.Data = data
        self.Mono = time.monotonic()
        self.Utc = UtcNowIso()
        self.Generation = generation


# -----------------------------------------------------------------------------
# The link
# -----------------------------------------------------------------------------
class MegaLink:
    def __init__(self, PortFinder=None, Opener=None,
                 PollPeriodS=DEFAULT_POLL_PERIOD_S,
                 StaleAfterS=DEFAULT_STALE_AFTER_S,
                 CommandTimeoutS=DEFAULT_COMMAND_TIMEOUT_S,
                 QueueWaitS=DEFAULT_QUEUE_WAIT_S,
                 ResetWaitS=DEFAULT_RESET_WAIT_S,
                 OrphanTtlS=DEFAULT_ORPHAN_TTL_S,
                 BackoffMinS=BACKOFF_MIN_S, BackoffMaxS=BACKOFF_MAX_S,
                 BackoffJitterS=BACKOFF_JITTER_S, Poll=True):
        self._PortFinder = PortFinder or FindMegaPort
        self._Opener = Opener or OpenMegaSerial
        self.PollPeriodS = float(PollPeriodS)
        self.StaleAfterS = float(StaleAfterS)
        self.CommandTimeoutS = float(CommandTimeoutS)
        self.QueueWaitS = float(QueueWaitS)
        self.ResetWaitS = float(ResetWaitS)
        self.OrphanTtlS = float(OrphanTtlS)
        self.BackoffMinS = float(BackoffMinS)
        self.BackoffMaxS = float(BackoffMaxS)
        self.BackoffJitterS = float(BackoffJitterS)
        self._PollEnabled = bool(Poll)

        # Lock order: _WriteLock before _StateLock, never the reverse.
        # _WriteLock serializes bytes on the wire and the port open/close
        # swap. It is never held while waiting for a reply.
        self._WriteLock = threading.Lock()
        # _StateLock guards everything else below. Held only briefly.
        self._StateLock = threading.Lock()

        self._Port = None
        self._PortName = None
        self._Generation = 0
        self._AttachMono = 0.0
        self._ReadyEvt = threading.Event()
        self._StatusValid = False   # STATUS parsed since the last open or reset
        self._Reason = "not started"
        self._Queue = queue.Queue()
        self._Outstanding = []      # written and awaiting a reply, in write order
        self._Status = None
        self._Sensor = None

        self.Reconnects = 0
        self.Timeouts = 0
        self.LateReplies = 0
        self.FirmwareResets = 0
        self.RecentUnmatched = collections.deque(maxlen=20)

        self._StopEvt = threading.Event()
        self._StartLock = threading.Lock()
        self._Threads = []
        self._Started = False

    # Lifecycle
    def Start(self):
        """Start the reader, writer and poll threads. Idempotent."""
        with self._StartLock:
            if self._Started:
                return
            self._Started = True
            self._StopEvt.clear()
            with self._StateLock:
                self._Reason = "connecting"
            targets = [("MegaReader", self._ReaderLoop), ("MegaWriter", self._WriterLoop)]
            if self._PollEnabled:
                targets.append(("MegaPoller", self._PollLoop))
            self._Threads = []
            for name, fn in targets:
                th = threading.Thread(target=fn, name=name, daemon=True)
                th.start()
                self._Threads.append(th)

    def Close(self, JoinS=2.0):
        """Stop all threads and close the port."""
        with self._StartLock:
            if not self._Started:
                return
            self._StopEvt.set()
            self._ReadyEvt.set()
            for th in self._Threads:
                th.join(JoinS)
            self._Threads = []
            self._Started = False
        self._Detach("link closed")

    # Public commands
    def Request(self, Text, TimeoutS=None, RequireStatus=None):
        """Queue one command and wait for its matching reply line.

        Returns the list of reply lines (one line for every firmware command).
        Raises CommandRejected, LinkDown, LinkNotReady, LinkTimeout or
        FirmwareError. STOP and KILL are routed to the priority path.
        """
        token = CheckCommand(Text)
        if token in STOP_TOKENS:
            return self.Stop(Text, TimeoutS)["response"]
        if RequireStatus is None:
            RequireStatus = token not in QUERY_TOKENS
        timeoutS = self.CommandTimeoutS if TimeoutS is None else float(TimeoutS)
        self._FailFast(RequireStatus)
        pend = _Pending(Text, token, timeoutS, False, RequireStatus)
        self._Queue.put(pend)
        self._AwaitCaller(pend)
        if pend.Error is not None:
            raise pend.Error
        return list(pend.Lines)

    def Stop(self, Text="STOP", TimeoutS=None):
        """Priority STOP or KILL.

        Written immediately under the write lock, ahead of anything queued
        and in parallel with the command in flight. The reply 'OK STOP' is
        routed by content, so it can never complete the in-flight command.
        Afterwards a priority STATUS refreshes the cached state.
        """
        token = CheckCommand(Text)
        if token not in STOP_TOKENS:
            raise CommandRejected("Stop() only sends STOP or KILL")
        timeoutS = self.CommandTimeoutS if TimeoutS is None else float(TimeoutS)
        callMono = time.monotonic()
        pend = _Pending(Text, token, timeoutS, True, False)
        self._PriorityExchange(pend)
        result = {
            "response": list(pend.Lines),
            "write_latency_ms": round((pend.WriteDoneMono - callMono) * 1000.0, 2),
            "reply_ms": round((pend.ReplyMono - pend.SentMono) * 1000.0, 2),
            "status_refreshed": False,
        }
        try:
            statusPend = _Pending("STATUS", "STATUS", timeoutS, True, False)
            self._PriorityExchange(statusPend)
            result["status_refreshed"] = ParseStatusLine(statusPend.Lines[-1]) is not None
        except LinkError:
            # The STOP was acknowledged; a missing refresh only means the
            # cached snapshot ages into STALE until the next poll lands.
            pass
        return result

    # Cached state
    def LinkState(self, snapshot=None):
        """LIVE, STALE or DOWN for the given snapshot (default: STATUS)."""
        with self._StateLock:
            if snapshot is None:
                snapshot = self._Status
            return self._LinkStateLocked(snapshot)[0]

    def _LinkStateLocked(self, snap):
        if self._Port is None:
            age = None if snap is None else time.monotonic() - snap.Mono
            return LINK_DOWN, age
        if snap is None or snap.Generation != self._Generation:
            return LINK_STALE, None
        age = time.monotonic() - snap.Mono
        if age > self.StaleAfterS:
            return LINK_STALE, age
        return LINK_LIVE, age

    def _SnapshotView(self, snap):
        with self._StateLock:
            state, age = self._LinkStateLocked(snap)
            return {
                "link": state,
                "age_s": None if age is None else round(age, 3),
                "updated_utc": None if snap is None else snap.Utc,
                "reason": self._Reason,
                "port": self._PortName,
                "status_valid": self._StatusValid,
                "data": None if snap is None else dict(snap.Data),
            }

    def StatusSnapshot(self):
        with self._StateLock:
            snap = self._Status
        return self._SnapshotView(snap)

    def SensorSnapshot(self):
        with self._StateLock:
            snap = self._Sensor
        return self._SnapshotView(snap)

    def CurrentStatus(self):
        """Firmware STATUS dict from the current session, or None."""
        with self._StateLock:
            snap = self._Status
            if snap is None or self._Port is None or snap.Generation != self._Generation:
                return None
            return dict(snap.Data)

    def IsConnected(self):
        with self._StateLock:
            return self._Port is not None

    def IsReady(self):
        """Connected and firmware state known (STATUS seen this session)."""
        with self._StateLock:
            return self._Port is not None and self._StatusValid

    def Health(self):
        with self._StateLock:
            return {
                "connected": self._Port is not None,
                "port": self._PortName,
                "reason": self._Reason,
                "status_valid": self._StatusValid,
                "generation": self._Generation,
                "queue_depth": self._Queue.qsize(),
                "outstanding": len(self._Outstanding),
                "reconnects": self.Reconnects,
                "timeouts": self.Timeouts,
                "late_replies": self.LateReplies,
                "firmware_resets": self.FirmwareResets,
                "recent_unmatched": list(self.RecentUnmatched),
            }

    # Caller side helpers
    def _FailFast(self, requireStatus):
        with self._StateLock:
            if not self._Started:
                raise LinkDown("link not started")
            if self._Port is None:
                raise LinkDown("link down: %s" % self._Reason)
            if requireStatus and not self._StatusValid:
                raise LinkNotReady("firmware state unknown since (re)connect; waiting for STATUS")

    def _AwaitCaller(self, pend):
        if pend.Done.wait(self.QueueWaitS + pend.TimeoutS):
            return
        with self._StateLock:
            if pend.SentMono is None and not pend.Done.is_set():
                # Never sent: cancel it so the writer skips it (G-33). The
                # caller is told it failed, so it must not run later.
                pend.Cancelled = True
                pend.Error = LinkTimeout("%r not sent: command queue busy for %.1f s"
                                         % (pend.Text, self.QueueWaitS + pend.TimeoutS))
                pend.Done.set()
                return
            sentMono = pend.SentMono
        # Sent but not resolved yet: the writer resolves it at SentMono + TimeoutS.
        remaining = (sentMono or time.monotonic()) + pend.TimeoutS + 0.5 - time.monotonic()
        if not pend.Done.wait(max(0.0, remaining)):
            with self._StateLock:
                if not pend.Done.is_set():
                    pend.Error = LinkTimeout("no reply to %r" % pend.Text)
                    pend.Done.set()

    def _PriorityExchange(self, pend):
        """Write now from the calling thread and wait for the reply."""
        with self._StateLock:
            if not self._Started:
                raise LinkDown("link not started")
        self._Send(pend)
        if not pend.Done.wait(pend.TimeoutS):
            self._ResolveTimeout(pend)
        if pend.Error is not None:
            raise pend.Error

    # Wire
    def _Send(self, pend):
        """Write one command line. Raises LinkDown or LinkTimeout."""
        data = (pend.Text + "\n").encode("ascii")
        with self._WriteLock:
            with self._StateLock:
                if pend.Cancelled:
                    return False
                port = self._Port
                if port is None:
                    pend.Error = LinkDown("link down: %s" % self._Reason)
                    pend.Done.set()
                    raise pend.Error
                pend.SentMono = time.monotonic()
                self._Outstanding.append(pend)
            try:
                # No flush(): on POSIX it is tcdrain(), which has no timeout and
                # could hold the write lock (and so block STOP) indefinitely.
                port.write(data)
            except SerialTimeoutException as exc:
                err = LinkTimeout("write of %r timed out: %s" % (pend.Text, exc))
            except (SerialException, OSError) as exc:
                err = LinkDown("write of %r failed: %s" % (pend.Text, exc))
            else:
                err = None
            pend.WriteDoneMono = time.monotonic()
        if err is not None:
            with self._StateLock:
                if pend in self._Outstanding:
                    self._Outstanding.remove(pend)
                if not pend.Done.is_set():
                    pend.Error = err
                    pend.Done.set()
            raise err
        return True

    def _ResolveTimeout(self, pend):
        """Mark pend timed out unless the reader completed it first (G-32)."""
        with self._StateLock:
            if pend.Done.is_set():
                return False
            pend.TimedOut = True
            pend.ExpireMono = time.monotonic() + self.OrphanTtlS
            pend.Error = LinkTimeout("no reply to %r within %.2f s" % (pend.Text, pend.TimeoutS))
            self.Timeouts += 1
            pend.Done.set()
            # The pend stays in _Outstanding as an orphan so its late reply is
            # consumed there instead of completing a later command (G-33).
            return True

    # Writer thread
    def _WriterLoop(self):
        while not self._StopEvt.is_set():
            try:
                pend = self._Queue.get(timeout=0.2)
            except queue.Empty:
                self._ExpireOrphans()
                continue
            try:
                self._Dispatch(pend)
            except Exception as exc:  # keep the writer alive no matter what
                with self._StateLock:
                    if not pend.Done.is_set():
                        pend.Error = LinkError("dispatch failed: %s" % exc)
                        pend.Done.set()

    def _WaitReady(self, pend):
        """Hold queued commands until the firmware banner (or ResetWaitS)."""
        while not self._StopEvt.is_set():
            with self._StateLock:
                if pend.Cancelled:
                    return False
                if self._Port is None:
                    pend.Error = LinkDown("link down: %s" % self._Reason)
                    pend.Done.set()
                    return False
                if self._ReadyEvt.is_set():
                    return True
                remaining = self._AttachMono + self.ResetWaitS - time.monotonic()
                if remaining <= 0:
                    # No READY banner (auto-reset disabled, or it was missed).
                    # Proceed; STATUS will tell whether the firmware answers.
                    self._ReadyEvt.set()
                    self._Reason = "connected (no READY banner seen)"
                    return True
            self._ReadyEvt.wait(min(remaining, 0.2))
        with self._StateLock:
            if not pend.Done.is_set():
                pend.Error = LinkDown("link closed")
                pend.Done.set()
        return False

    def _Dispatch(self, pend):
        with self._StateLock:
            if pend.Cancelled or pend.Done.is_set():
                return
        if not self._WaitReady(pend):
            return
        with self._StateLock:
            if pend.RequireStatus and not self._StatusValid and not pend.Done.is_set():
                pend.Error = LinkNotReady("firmware state unknown since (re)connect; waiting for STATUS")
                pend.Done.set()
                return
        try:
            if not self._Send(pend):
                return
        except LinkError:
            return
        if pend.Done.wait(pend.TimeoutS):
            return
        if self._ResolveTimeout(pend) and not pend.IsBarrier:
            self._Resync()

    def _Resync(self):
        """After a timeout, send PING and wait for PONG as a barrier.

        The firmware answers in order, so once PONG arrives every reply to a
        command written before the barrier has already arrived. Orphans still
        waiting at that point were lost and are dropped, so they cannot eat
        the reply of a later command of the same kind.
        """
        barrier = _Pending("PING", "PING", self.CommandTimeoutS, False, False)
        barrier.IsBarrier = True
        try:
            if not self._Send(barrier):
                return
        except LinkError:
            return
        if not barrier.Done.wait(barrier.TimeoutS):
            self._ResolveTimeout(barrier)
            return
        if barrier.Error is not None:
            return
        with self._StateLock:
            self._Outstanding = [p for p in self._Outstanding
                                 if not (p.TimedOut and p.SentMono is not None
                                         and p.SentMono < barrier.SentMono)]

    def _ExpireOrphans(self):
        with self._StateLock:
            self._ExpireOrphansLocked(time.monotonic())

    def _ExpireOrphansLocked(self, now):
        if any(p.TimedOut and p.ExpireMono is not None and now >= p.ExpireMono
               for p in self._Outstanding):
            self._Outstanding = [p for p in self._Outstanding
                                 if not (p.TimedOut and p.ExpireMono is not None
                                         and now >= p.ExpireMono)]

    # Poll thread
    def _PollLoop(self):
        # Chained: the next poll starts only after this one finished, so a
        # slow firmware can never pile up STATUS requests in the queue.
        while not self._StopEvt.is_set():
            t0 = time.monotonic()
            if self.IsConnected():
                for cmd in ("STATUS", "SENSOR?"):
                    if self._StopEvt.is_set():
                        break
                    try:
                        self.Request(cmd, RequireStatus=False)
                    except LinkError:
                        pass
            elapsed = time.monotonic() - t0
            self._StopEvt.wait(max(0.05, self.PollPeriodS - elapsed))

    # Reader thread
    def _ReaderLoop(self):
        backoff = self.BackoffMinS
        while not self._StopEvt.is_set():
            try:
                portName = self._PortFinder()
                if not portName:
                    raise OSError("Arduino Mega serial port not found")
                port = self._Opener(portName)
            except (SerialException, OSError, ValueError) as exc:
                self._SetReason("open failed: %s" % exc)
                self._StopEvt.wait(backoff + random.uniform(0.0, self.BackoffJitterS))
                backoff = min(backoff * 2.0, self.BackoffMaxS)
                continue
            self._Attach(port, portName)
            attachedMono = time.monotonic()
            try:
                self._Pump(port)
                reason = "link closed"
            except (SerialException, OSError) as exc:
                reason = "serial error: %s" % exc
            self._Detach(reason)
            if self._StopEvt.is_set():
                break
            if time.monotonic() - attachedMono >= STABLE_SESSION_S:
                backoff = self.BackoffMinS
            self._StopEvt.wait(backoff + random.uniform(0.0, self.BackoffJitterS))
            backoff = min(backoff * 2.0, self.BackoffMaxS)

    def _SetReason(self, reason):
        with self._StateLock:
            self._Reason = reason

    def _Attach(self, port, portName):
        with self._WriteLock:
            with self._StateLock:
                self._Port = port
                self._PortName = portName
                self._Generation += 1
                if self._Generation > 1:
                    self.Reconnects += 1
                self._AttachMono = time.monotonic()
                self._StatusValid = False
                self._Status = None
                self._Sensor = None
                self._Outstanding = []
                self._ReadyEvt.clear()
                self._Reason = "connected to %s, waiting for firmware READY" % portName

    def _Detach(self, reason):
        with self._WriteLock:
            with self._StateLock:
                port = self._Port
                self._Port = None
                self._StatusValid = False
                self._ReadyEvt.clear()
                self._Reason = reason
                outstanding = self._Outstanding
                self._Outstanding = []
            if port is not None:
                try:
                    port.close()
                except Exception:
                    pass
        err = LinkDown("link down: %s" % reason)
        with self._StateLock:
            for pend in outstanding:
                if not pend.Done.is_set():
                    pend.Error = err
                    pend.Done.set()
            while True:
                try:
                    pend = self._Queue.get_nowait()
                except queue.Empty:
                    break
                if not pend.Done.is_set():
                    pend.Error = err
                    pend.Done.set()

    def _Pump(self, port):
        buf = bytearray()
        while not self._StopEvt.is_set():
            waiting = port.in_waiting
            chunk = port.read(waiting if waiting > 0 else 1)
            if not chunk:
                continue
            buf.extend(chunk)
            while True:
                idx = buf.find(b"\n")
                if idx < 0:
                    break
                raw = bytes(buf[:idx])
                del buf[:idx + 1]
                line = raw.decode("ascii", errors="replace").strip()
                if line:
                    self._HandleLine(line)
            if len(buf) > 4096:
                # Garbage without a newline (wrong baud rate, noise). Drop it.
                buf.clear()

    def _HandleLine(self, line):
        now = time.monotonic()

        # 1. Data side effects. STATUS and ENV lines describe the firmware at
        #    the moment they were printed, whoever asked for them, so the cache
        #    is updated even from a late or unmatched reply.
        if line == READY_LINE:
            self._OnFirmwareReady()
            return
        if MatchStatusLine(line):
            parsed = ParseStatusLine(line)
            if parsed is not None:
                with self._StateLock:
                    self._Status = _Snapshot(parsed, self._Generation)
                    if not self._StatusValid:
                        self._StatusValid = True
                        self._Reason = "connected to %s" % self._PortName
        elif MatchEnvLine(line):
            parsed = ParseEnvLine(line)
            if parsed is not None:
                with self._StateLock:
                    self._Sensor = _Snapshot(parsed, self._Generation)

        # 2. Route the line to the oldest outstanding command that accepts it.
        #    Matchers are content based, so 'OK STOP' never completes a queued
        #    SETRPM, and a timed-out orphan drains its own late reply first.
        with self._StateLock:
            self._ExpireOrphansLocked(now)
            target = None
            if line == ERROR_LINE:
                if self._Outstanding:
                    target = self._Outstanding[0]
            else:
                for pend in self._Outstanding:
                    if pend.Matcher(line):
                        target = pend
                        break
            if target is None:
                self.RecentUnmatched.append((UtcNowIso(), line))
                return
            self._Outstanding.remove(target)
            if target.TimedOut or target.Done.is_set():
                self.LateReplies += 1
                return
            target.Lines.append(line)
            target.ReplyMono = now
            if line == ERROR_LINE:
                target.Error = FirmwareError("firmware answered ERR? to %r" % target.Text)
            target.Done.set()

    def _OnFirmwareReady(self):
        with self._StateLock:
            if self._ReadyEvt.is_set():
                # READY while already running: the Mega reset on its own
                # (brown-out, watchdog, someone else toggled DTR). Its state is
                # back to defaults.
                self.FirmwareResets += 1
                self._Reason = "firmware reset detected; waiting for STATUS"
            else:
                self._Reason = "connected to %s, waiting for STATUS" % self._PortName
            # Anything written before READY went to the bootloader or to a
            # firmware that has since restarted, so it was not executed.
            for pend in self._Outstanding:
                if not pend.Done.is_set():
                    pend.Error = FirmwareError("firmware restarted while %r was pending; "
                                               "it was not executed" % pend.Text)
                    pend.Done.set()
            self._Outstanding = []
            self._StatusValid = False
            self._Status = None
            self._Sensor = None
            self._ReadyEvt.set()
