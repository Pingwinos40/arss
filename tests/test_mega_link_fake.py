# test_mega_link_fake.py
# A fake pyserial port that emulates ArduinoFirmwares/ArduinoMegaControl.ino:
# same reply lines, same in-order single-threaded processing, configurable
# per-command reply delays, dropped replies, ERR? injection, unplug and
# firmware reset. Shared by test_mega_link.py and test_app_motor.py.

import json
import os
import queue
import sys
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER_DIR = os.path.join(os.path.dirname(HERE), "RaspberryPiWebserver")
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

from serial.serialutil import SerialException  # noqa: E402

import mega_link  # noqa: E402


def FmtFloat(value, places):
    return "%.*f" % (places, value)


class FakeMega:
    """pyserial-like object: read(n), write(b), in_waiting, close()."""

    def __init__(self, Delays=None, ReadyDelayS=0.01, SendReady=True,
                 DropTokens=None, ErrTokens=None, MaxRpm=1200, Ppr=200):
        # Delays: token -> seconds, or token -> list of seconds (one per call;
        # the last entry repeats).
        self.Delays = dict(Delays or {})
        self.DropTokens = dict(DropTokens or {})  # token -> how many to drop
        self.ErrTokens = set(ErrTokens or ())
        self.Cond = threading.Condition()
        self.OutBuf = bytearray()
        self.InLines = queue.Queue()
        self.Written = []          # (monotonic time, line) as received
        self.Unplugged = False
        self.Closed = False
        self.timeout = mega_link.READ_POLL_S
        self.Running = False
        self.Hz = 1000.0
        self.TargetHz = 1000.0
        self.Ppr = Ppr
        self.Dir = 0
        self.MaxRpm = MaxRpm
        self.RampUp = 0.0
        self.RampDown = 0.0
        self.TempC = 23.4
        self.Rh = 45.6
        self._InBuf = bytearray()
        self._Worker = threading.Thread(target=self._Run, daemon=True)
        self._Worker.start()
        if SendReady:
            threading.Timer(ReadyDelayS, lambda: self._Emit("READY")).start()

    # Pyserial surface
    @property
    def in_waiting(self):
        with self.Cond:
            if self.Unplugged:
                raise SerialException("device disconnected")
            return len(self.OutBuf)

    def read(self, n=1):
        deadline = time.monotonic() + self.timeout
        with self.Cond:
            while not self.OutBuf:
                if self.Unplugged:
                    raise SerialException("device reports readiness to read but returned no data")
                if self.Closed:
                    return b""
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return b""
                self.Cond.wait(remaining)
            if self.Unplugged:
                raise SerialException("device disconnected")
            out = bytes(self.OutBuf[:n])
            del self.OutBuf[:n]
            return out

    def write(self, data):
        now = time.monotonic()
        with self.Cond:
            if self.Unplugged or self.Closed:
                raise SerialException("write failed: port gone")
            self._InBuf.extend(data)
            while b"\n" in self._InBuf:
                idx = self._InBuf.index(b"\n")
                line = self._InBuf[:idx].decode("ascii").replace("\r", "")
                del self._InBuf[:idx + 1]
                self.Written.append((now, line))
                self.InLines.put(line)
        return len(data)

    def close(self):
        with self.Cond:
            self.Closed = True
            self.Cond.notify_all()

    # Test controls
    def Unplug(self):
        with self.Cond:
            self.Unplugged = True
            self.Cond.notify_all()

    def EmitReady(self):
        self._Emit("READY")

    def WrittenLines(self):
        with self.Cond:
            return [line for _t, line in self.Written]

    def WriteTime(self, text):
        with self.Cond:
            for t, line in self.Written:
                if line == text:
                    return t
        return None

    # Firmware emulation
    def _Emit(self, line):
        with self.Cond:
            if self.Unplugged or self.Closed:
                return
            self.OutBuf.extend((line + "\r\n").encode("ascii"))
            self.Cond.notify_all()

    def _DelayFor(self, token):
        d = self.Delays.get(token, 0.0)
        if isinstance(d, list):
            if len(d) > 1:
                return d.pop(0)
            return d[0] if d else 0.0
        return d

    def _Run(self):
        while True:
            with self.Cond:
                if self.Closed or self.Unplugged:
                    return
            try:
                line = self.InLines.get(timeout=0.05)
            except queue.Empty:
                continue
            token = line.strip().split(" ", 1)[0]
            delay = self._DelayFor(token)
            if delay:
                time.sleep(delay)
            if self.DropTokens.get(token, 0) > 0:
                self.DropTokens[token] -= 1
                continue
            if token in self.ErrTokens:
                self._Emit("ERR?")
                continue
            reply = self._Handle(line.strip())
            if reply is not None:
                self._Emit(reply)

    def StatusLine(self):
        rpm = self.Hz * 60.0 / self.Ppr if self.Ppr > 0 else 0.0
        targetRpm = self.TargetHz * 60.0 / self.Ppr if self.Ppr > 0 else 0.0
        return ("{\"running\":%s,\"hz\":%s,\"rpm\":%s,\"dir\":%d,\"ppr\":%d,"
                "\"steps_remaining\":0,\"max_rpm\":%d,\"ramping\":false,"
                "\"ramp_up\":%s,\"ramp_down\":%s,\"target_rpm\":%s,"
                "\"env\":{\"temp_c\":%s,\"humidity\":%s}}"
                % ("true" if self.Running else "false", FmtFloat(self.Hz, 3),
                   FmtFloat(rpm, 2), self.Dir, self.Ppr, self.MaxRpm,
                   FmtFloat(self.RampUp, 1), FmtFloat(self.RampDown, 1),
                   FmtFloat(targetRpm, 2), FmtFloat(self.TempC, 1), FmtFloat(self.Rh, 1)))

    def _Handle(self, line):
        if line == "PING":
            return "PONG"
        if line == "STATUS":
            return self.StatusLine()
        if line == "START":
            self.Running = True
            return "OK START"
        if line in ("STOP", "KILL"):
            self.Running = False
            return "OK STOP"
        if line.startswith("SETFREQ"):
            hz = min(max(float(line[7:] or 0), 0.5), 50000.0)
            self.TargetHz = self.Hz = hz
            return "OK SETFREQ " + FmtFloat(hz, 3)
        if line.startswith("SETRPM"):
            rpm = max(0.0, float(line[6:] or 0))
            hz = min(max(rpm * self.Ppr / 60.0, 0.5), 50000.0)
            self.TargetHz = self.Hz = hz
            return "OK SETRPM %s Hz=%s" % (FmtFloat(rpm, 2), FmtFloat(hz, 3))
        if line.startswith("SETDIR"):
            p = line[6:].strip()
            val = 1 if (p[:2].upper() == "CW" or p[:1] in ("1", "+")) else 0
            self.Dir = val
            return "OK SETDIR %d" % val
        if line.startswith("SETSTEPS"):
            n = max(0, int(line[8:] or 0))
            if n <= 0:
                self.Running = False
                return "OK SETSTEPS 0"
            self.Running = True
            return "OK SETSTEPS %d" % n
        if line.startswith("SETMAXRPM"):
            self.MaxRpm = min(max(int(line[9:] or 0), 1), 100000)
            return "OK SETMAXRPM %d" % self.MaxRpm
        if line.startswith("SETRAMP"):
            parts = line[7:].split()
            self.RampUp = max(0.0, float(parts[0])) if parts else 0.0
            self.RampDown = max(0.0, float(parts[1])) if len(parts) > 1 else 0.0
            return "OK SETRAMP UP=%s DOWN=%s" % (FmtFloat(self.RampUp, 1), FmtFloat(self.RampDown, 1))
        if line == "SENSOR?":
            return "ENV %s, %s %%RH" % (FmtFloat(self.TempC, 1), FmtFloat(self.Rh, 1))
        return "ERR?"


class FakeBench:
    """Port finder plus opener. Every open yields a fresh FakeMega, as a real
    open resets the Mega through DTR."""

    def __init__(self, **fakeKwargs):
        self.FakeKwargs = fakeKwargs
        self.Plugged = True
        self.Opened = []
        self.Lock = threading.Lock()

    def Find(self):
        return "/dev/ttyACM-fake" if self.Plugged else None

    def Open(self, portName):
        with self.Lock:
            if not self.Plugged:
                raise SerialException("could not open port %s" % portName)
            fake = FakeMega(**self.FakeKwargs)
            self.Opened.append(fake)
            return fake

    @property
    def Current(self):
        with self.Lock:
            return self.Opened[-1] if self.Opened else None

    def Unplug(self):
        with self.Lock:
            self.Plugged = False
            fake = self.Opened[-1] if self.Opened else None
        if fake is not None:
            fake.Unplug()

    def Replug(self, **fakeKwargs):
        with self.Lock:
            if fakeKwargs:
                self.FakeKwargs = fakeKwargs
            self.Plugged = True


def MakeLink(bench, **kwargs):
    opts = dict(PollPeriodS=0.1, StaleAfterS=2.0, CommandTimeoutS=1.0,
                QueueWaitS=3.0, ResetWaitS=1.0, BackoffMinS=0.02,
                BackoffMaxS=0.1, BackoffJitterS=0.01)
    opts.update(kwargs)
    return mega_link.MegaLink(PortFinder=bench.Find, Opener=bench.Open, **opts)


def WaitFor(predicate, timeoutS=3.0, stepS=0.01):
    deadline = time.monotonic() + timeoutS
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(stepS)
    return predicate()


class FakeSelfTest(unittest.TestCase):
    """The fake must speak the firmware's exact reply formats."""

    def test_reply_formats(self):
        fake = FakeMega(SendReady=False)
        try:
            self.assertEqual(fake._Handle("SETRPM 30"), "OK SETRPM 30.00 Hz=100.000")
            self.assertEqual(fake._Handle("SETDIR CCW"), "OK SETDIR 0")
            self.assertEqual(fake._Handle("SETDIR CW"), "OK SETDIR 1")
            self.assertEqual(fake._Handle("SENSOR?"), "ENV 23.4, 45.6 %RH")
            status = json.loads(fake._Handle("STATUS"))
            self.assertEqual(status["max_rpm"], 1200)
            self.assertIn("env", status)
        finally:
            fake.close()


if __name__ == "__main__":
    unittest.main()
