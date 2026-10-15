#!/usr/bin/env python3
# Ported from Digital_Multitool commit f22527f (instruments.py / lcr_format.py)
#
# Kept from the source: _io_lock, get_resource_manager, list_usb_instruments,
# VisaInstrument (open sequence, write/read/ask, _drain, go_local) and the
# BK894 core (MODES, set_mode, set_frequency, set_voltage, measure,
# get_config). Dropped because nothing in arss uses them: TekMSO24, BK4055B,
# BK9174B, BK5493C, the raw-write helpers, and the BK894 bias / aperture /
# fixture-correction methods. Added for arss (marked "arss:" below): an
# optional rm argument to list_usb_instruments, the *IDN? model check
# (issue #7) and BK894.configure_and_verify (read-back guard, issue #31).
"""
BK Precision 894 LCR meter driver, PyVISA-py backed (USB-TMC over libusb).

This replaces the old kernel-usbtmc transport (os.open('/dev/usbtmc*')),
whose read timeout never fired on a blocking descriptor (issue #38). The
kernel `usbtmc` module must be blacklisted so libusb (pyusb, used by the
pyvisa-py '@py' backend) can claim the meter, and a udev rule must grant the
service user access to the USB device node. See deploy/ and the README.

The meter is discovered by USB VID/PID through PyVISA's resource list, so
callers never pass device paths:

    lcr = BK894()

Pass an explicit resource string only to override discovery (for example
when two meters are attached):

    lcr = BK894(resource='USB0::1137::10279::SOMESERIAL::0::INSTR')
"""
import math
import re
import threading


# --------------------------------------------------------------------------
# Shared PyVISA ResourceManager
# --------------------------------------------------------------------------

_RM = None
_RM_LOCK = threading.Lock()


def _io_lock(obj):
    """Per-instrument reentrant I/O lock, created lazily.

    SCPI is write-then-read: two threads interleaving on one session cross
    their replies (each one parses the other's answer, and the reply that
    was owed stays in the buffer for the next query). In arss the recording
    thread and the /daq/measure route share one session. Lazy creation keeps
    test fakes that skip __init__ working. RLock so a compound driver method
    can hold it across its whole transaction while the primitives it calls
    re-acquire."""
    lock = obj.__dict__.get('_io_rlock')
    if lock is None:
        with _RM_LOCK:
            lock = obj.__dict__.get('_io_rlock')
            if lock is None:
                lock = obj.__dict__['_io_rlock'] = threading.RLock()
    return lock


def get_resource_manager():
    """Process-wide PyVISA ResourceManager (pyvisa-py backend).

    pyvisa is imported lazily so that merely importing this module (the Flask
    app at startup, the unit tests) does not require pyvisa to be installed.
    It is only needed to actually open a device.
    """
    global _RM
    with _RM_LOCK:
        if _RM is None:
            import pyvisa
            _RM = pyvisa.ResourceManager('@py')
        return _RM


def list_usb_instruments(rm=None):
    """Return [(resource_string, vid_int, pid_int)] for all USB-TMC resources.

    arss: rm may be passed in (tests use a fake); default is the shared one.
    """
    rm = rm if rm is not None else get_resource_manager()
    found = []
    for r in rm.list_resources():
        if not r.startswith('USB'):
            continue
        parts = r.split('::')
        try:
            found.append((r, int(parts[1]), int(parts[2])))
        except (IndexError, ValueError):
            continue
    return found


# --------------------------------------------------------------------------
# USB-TMC base class
# --------------------------------------------------------------------------

class InstrumentIdentityError(RuntimeError):
    """arss: *IDN? did not name the expected model (issue #7)."""


class VisaInstrument:
    """Base class for USB-TMC instruments accessed through pyvisa-py.

    Subclasses set:
      VID, PID    int   USB vendor/product IDs (hex literals OK)
      TIMEOUT_MS  int   default PyVISA I/O timeout in milliseconds
      IDN_MODEL   str   arss: text that must appear in the manufacturer or
                        model field of *IDN? (None skips the check)
    """
    VID = None
    PID = None
    TIMEOUT_MS = 2000
    IDN_MODEL = None

    def __init__(self, resource=None, rm=None):
        rm = rm if rm is not None else get_resource_manager()
        if resource is None:
            resource = self._discover(rm)
            if resource is None:
                raise RuntimeError(
                    f"{self.__class__.__name__}: no device found with "
                    f"VID=0x{self.VID:04x} PID=0x{self.PID:04x}. "
                    f"Check that the instrument is powered and enumerated "
                    f"(lsusb | grep -i {self.VID:04x}:{self.PID:04x})."
                )
        self.resource = resource
        self.inst = rm.open_resource(resource)
        # If anything after the open fails, CLOSE the session before
        # propagating: a leaked open claims the USB interface for the process
        # lifetime, making every reconnect fail until app restart (issue #39).
        try:
            self.inst.timeout = self.TIMEOUT_MS
            self.inst.read_termination = '\n'
            self.inst.write_termination = '\n'
            # Flush any stale device output (an unread reply from a previous
            # session) so the first query does not read garbage (issue #37).
            try:
                self.inst.clear()
            except Exception:
                pass
            self.idn = self.inst.query('*IDN?').strip()
            self._check_idn(self.idn)
            self._post_open()
        except Exception:
            try:
                self.inst.close()
            except Exception:
                pass
            raise

    def _post_open(self):
        """Subclass hook for additional setup after open + IDN."""
        pass

    def _check_idn(self, idn):
        """arss: refuse to drive an instrument that is not the expected model.

        IEEE 488.2 *IDN? is 'manufacturer,model,serial,firmware'. Only the
        first two fields are searched so a serial number that happens to
        contain the model digits cannot pass the check."""
        if not self.IDN_MODEL:
            return
        fields = [f.strip() for f in (idn or '').split(',')]
        if not any(self.IDN_MODEL in f for f in fields[:2]):
            raise InstrumentIdentityError(
                f"{self.__class__.__name__}: *IDN? reply {idn!r} does not "
                f"name model {self.IDN_MODEL!r}; refusing to drive "
                f"{self.resource!r}.")

    @classmethod
    def _discover(cls, rm):
        """Return the first USB resource string matching cls.VID/cls.PID, or None."""
        for r, vid, pid in list_usb_instruments(rm):
            if vid == cls.VID and pid == cls.PID:
                return r
        return None

    def write(self, command):
        with _io_lock(self):
            self.inst.write(command)

    def read(self):
        with _io_lock(self):
            return self.inst.read().strip()

    def ask(self, command):
        """Write a command and return its response (newline-stripped)."""
        with _io_lock(self):
            return self.inst.query(command).strip()

    # Alias matching pyvisa convention; both ask() and query() are used here.
    query = ask

    def close(self):
        try:
            self.inst.close()
        except Exception:
            pass

    def _drain(self):
        """Discard any pending/stale bytes in the input buffer so the next
        query reads its OWN reply. More forceful than inst.clear() (which
        does not reliably flush every meter): reads with a short timeout until
        the buffer is empty. Silent."""
        with _io_lock(self):
            old = getattr(self.inst, 'timeout', None)
            try:
                self.inst.timeout = 60
                for _ in range(20):
                    self.inst.read()      # discard a stale reply
            except Exception:
                pass                      # timeout means the buffer is empty
            finally:
                if old is not None:
                    try:
                        self.inst.timeout = old
                    except Exception:
                        pass

    def go_local(self):
        """Best-effort return-to-local so the front panel works again after
        the app lets go of the meter. pyvisa-py ASSERTS remote on USB-TMC
        connect (and any command puts a box in remote), so without this the
        panel stays locked until a power cycle. Sends SCPI SYSTem:LOCal and
        then the transport's go-to-local (USB488 GO_TO_LOCAL). Silent:
        shutdown must never raise."""
        try:
            self.write('SYST:LOC')
        except Exception:
            pass
        try:
            self._visa_go_local()
        except Exception:
            pass

    def _visa_go_local(self):
        """Issue the transport-level go-to-local via the pyvisa-py session
        (USB488 GO_TO_LOCAL for USB-TMC). All reaches are guarded by the
        caller."""
        sess = self.inst.visalib.sessions[self.inst.session]
        iface = getattr(sess, 'interface', None)
        if iface is None:
            return
        usb_dev = getattr(iface, 'usb_dev', None)
        if usb_dev is not None:                         # USB-TMC (USB488)
            import usb.util as uu
            intf = getattr(iface, 'usb_intf', None)
            usb_dev.ctrl_transfer(
                uu.build_request_type(uu.CTRL_IN, uu.CTRL_TYPE_CLASS,
                                      uu.CTRL_RECIPIENT_INTERFACE),
                161,                                    # USBTMC GO_TO_LOCAL
                0x0000, intf.index if intf is not None else 0, 0x0001,
                timeout=1000)


# --------------------------------------------------------------------------
# B&K Precision 894 LCR Meter (USB-TMC)
# --------------------------------------------------------------------------

class ConfigReadbackError(RuntimeError):
    """arss: the meter did not apply the requested settings (issue #31)."""


class BK894(VisaInstrument):
    """B&K Precision 894 LCR Meter.

    NOTE: this meter's USB serial-number descriptor is malformed (returns
    non-ASCII garbage). PyVISA enumerates it anyway since pyvisa-py addresses
    by bus/device under the hood. The true serial number is exposed only
    through `*IDN?`.
    """
    VID = 0x0471
    PID = 0x2827
    TIMEOUT_MS = 2000
    # arss: the model field of *IDN? must contain this (issue #7). The exact
    # IDN string of the rig meter is not recorded anywhere yet; the bench
    # smoke script (bench/bk894_smoke.py) prints it.
    IDN_MODEL = '894'

    # arss: limits in one place (issue #34). The meter clamps the AC level at
    # 2.0 V and the frequency at 500 kHz (Digital_Multitool README, "Known
    # instrument quirks").
    FREQ_MIN_HZ = 100
    FREQ_MAX_HZ = 500000
    VOLT_MIN_V = 0.01
    VOLT_MAX_V = 2.0

    # arss: read-back tolerance. The meter answers numeric queries with six
    # significant digits ('1.00000e+03', captured from the real meter in the
    # source repo's tests), so a relative 1e-4 only absorbs that formatting.
    # A meter that rounds the value to a coarser step will fail the check,
    # which is the point: the CSV must record what was applied.
    READBACK_REL_TOL = 1e-4

    MODES = {
        'CPD':  'Capacitance + Dissipation',
        'CPQ':  'Capacitance + Q Factor',
        'CPG':  'Capacitance + Conductance',
        'CPRP': 'Capacitance + Parallel Resistance',
        # Series-capacitance pairs: the bench meter reported itself in
        # 'csrs' on 2026-07-10, so these exist on this unit.
        'CSD':  'Capacitance (series) + Dissipation',
        'CSQ':  'Capacitance (series) + Q Factor',
        'CSRS': 'Capacitance (series) + Series Resistance',
        'LSRS': 'Inductance (series) + Resistance (series)',
        'LSRD': 'Inductance (series) + Resistance (DC)',
        'LPRS': 'Inductance (parallel) + Resistance (series)',
        'LPRP': 'Inductance (parallel) + Resistance (parallel)',
        'RX':   'Resistance + Reactance',
        'ZTD':  'Impedance + Phase (degrees)',
        'ZTR':  'Impedance + Phase (radians)',
    }

    def set_mode(self, mode):
        """Set measurement mode (e.g. 'CPD', 'LSRS'). See MODES dict."""
        if mode.upper() not in self.MODES:
            raise ValueError(f"Invalid mode. Choose from: {list(self.MODES.keys())}")
        self.write(f':FUNC:IMP {mode.upper()}')

    def set_frequency(self, freq_hz):
        """Set test frequency (100 Hz to 500 kHz)."""
        if not 100 <= freq_hz <= 500000:
            raise ValueError("Frequency must be 100 Hz to 500 kHz")
        self.write(f':FREQ {freq_hz}')

    def set_voltage(self, voltage):
        """Set AC test voltage (0.01 to 2.0 V).

        The BK 894's AC test-level command is plain ``:VOLT`` (set and query),
        bench-verified 2026-06-16 in Digital_Multitool. NOT ``:LEV:VOLT`` nor
        the E4980A-style ``:VOLT:LEV``; both are silently rejected on this
        unit and the level just stays put. The meter clamps the level at
        2.0 V, so higher values are out of range.
        """
        if not 0.01 <= voltage <= 2.0:
            raise ValueError("Voltage must be 0.01 to 2.0 V")
        self.write(f':VOLT {voltage}')

    def measure(self):
        """Return (primary, secondary, status). status 0 = good."""
        with _io_lock(self):
            result = self.ask(':FETC?')
        primary, secondary, status = result.split(',')
        return float(primary), float(secondary), int(status)

    def get_config(self):
        """Read mode/frequency/voltage, resilient to a stale/desynced reply
        (the mode string 'CSRS' bleeding into a numeric field: an off-by-one
        where every query returns the previous query's answer). On a bad
        numeric field it DRAINS the input buffer (which realigns the meter;
        inst.clear() alone does not reliably flush this unit) and re-reads, up
        to 3 tries. Never raises on a bad reply: a field that still will not
        parse comes back None. Transport errors (timeouts) do propagate."""
        mode = freq = volt = None
        with _io_lock(self):
            for attempt in range(3):
                if attempt:
                    self._drain()
                mode = self.ask(':FUNC:IMP?')
                freq = self._try_float(':FREQ?')
                volt = self._try_float(':VOLT?')
                if freq is not None and volt is not None:
                    break
        return {'mode': mode, 'frequency': freq, 'voltage': volt}

    def _try_float(self, cmd):
        raw = self.ask(cmd)
        m = re.match(r'[-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?',
                     (raw or '').strip())
        return float(m.group()) if m else None

    # arss additions (not in the source driver)

    @classmethod
    def check_settings(cls, mode, freq_hz, voltage):
        """arss: validate settings without touching the instrument.

        Returns (MODE, freq_hz, voltage) normalized; raises ValueError."""
        mode = str(mode or '').strip().upper()
        if mode not in cls.MODES:
            raise ValueError(
                f"Invalid mode {mode!r}. Choose from: {list(cls.MODES)}")
        freq_hz = float(freq_hz)
        voltage = float(voltage)
        if not math.isfinite(freq_hz) or not (
                cls.FREQ_MIN_HZ <= freq_hz <= cls.FREQ_MAX_HZ):
            raise ValueError(
                f"Frequency must be {cls.FREQ_MIN_HZ} Hz to "
                f"{cls.FREQ_MAX_HZ} Hz")
        if not math.isfinite(voltage) or not (
                cls.VOLT_MIN_V <= voltage <= cls.VOLT_MAX_V):
            raise ValueError(
                f"Voltage must be {cls.VOLT_MIN_V} to {cls.VOLT_MAX_V} V")
        return mode, freq_hz, voltage

    @staticmethod
    def _format_number(value):
        """Plain decimal for SCPI: 1000.0 -> '1000', 0.5 -> '0.5'."""
        return f'{value:.10g}'

    def configure_and_verify(self, mode, freq_hz, voltage):
        """arss: apply mode, frequency and AC level, then read all three back.

        Holds the I/O lock for the whole transaction so no other thread's
        query can land between a set and its read-back. The read-back goes
        through get_config(), which drains and retries on a desynced reply.
        Raises ConfigReadbackError when a field cannot be read or does not
        match the request (issue #31: the old ':LEV:VOLT' was silently
        ignored and nothing noticed). Returns the read-back dict:
        {'mode': 'CPD', 'frequency': 1000.0, 'voltage': 0.5}.
        """
        mode, freq_hz, voltage = self.check_settings(mode, freq_hz, voltage)
        with _io_lock(self):
            self.set_mode(mode)
            self.write(f':FREQ {self._format_number(freq_hz)}')
            self.write(f':VOLT {self._format_number(voltage)}')
            cfg = self.get_config()
        got_mode = (cfg.get('mode') or '').strip().upper()
        problems = []
        if got_mode != mode:
            problems.append(f"mode: requested {mode}, meter reports "
                            f"{cfg.get('mode')!r}")
        for key, want, unit in (('frequency', freq_hz, 'Hz'),
                                ('voltage', voltage, 'V')):
            got = cfg.get(key)
            if got is None:
                problems.append(f"{key}: no numeric read-back (desynced "
                                f"reply after 3 tries)")
            elif not math.isclose(got, want, rel_tol=self.READBACK_REL_TOL,
                                  abs_tol=1e-12):
                problems.append(f"{key}: requested {want:g} {unit}, meter "
                                f"reports {got:g} {unit}")
        if problems:
            raise ConfigReadbackError(
                "BK894 did not apply the requested settings: "
                + "; ".join(problems))
        return {'mode': got_mode, 'frequency': cfg['frequency'],
                'voltage': cfg['voltage']}
