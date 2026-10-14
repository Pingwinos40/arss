#!/usr/bin/env python3
"""
LCR meter recorder for the HALO dashboard (/daq/* routes in app.py).

Drives the BK894 through the pyvisa-py driver in instruments.py and records
on a fixed time grid into a per-run folder:

    <data_dir>/<UTC stamp>_<run name>/lcr.csv
    <data_dir>/<UTC stamp>_<run name>/manifest.json

data_dir comes from the ARSS_DATA_DIR environment variable (default
~/arss-data). The run name is validated (no path separators) and an existing
run folder is never overwritten (issue #15). The manifest is written when
the run starts and rewritten when it ends, including when it ends on an
error, so every CSV has its settings, read-back and counts next to it
(issue #5, LCR side).

Concurrency (issues #6, #39): one lock guards the instrument handle and the
start/stop/connect/disconnect transitions. connect() refuses while
recording and closes the old handle before opening a new one; disconnect()
stops recording first; start() refuses while a recording thread is alive.
Instrument I/O itself is serialized by the driver's per-instrument lock, so
/daq/measure and the recording thread never cross replies.
"""

import csv
import json
import math
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timezone

from instruments import BK894
from lcr_format import display_reading, json_safe_raw


DATA_DIR_ENV = "ARSS_DATA_DIR"
DEFAULT_DATA_DIR = os.path.join("~", "arss-data")
DEFAULT_RUN_NAME = "lcr"
RUN_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
CSV_NAME = "lcr.csv"
MANIFEST_NAME = "manifest.json"
CSV_COLUMNS = ["sample_index", "utc_iso", "t_mono_s",
               "primary", "secondary", "status"]
MANIFEST_SCHEMA = 1

# Stop the run after this many failed measurements in a row (issue #41;
# Digital_Multitool drops a source after 5 consecutive failures too).
MAX_CONSECUTIVE_ERRORS = 5
INTERVAL_MIN_S = 0.05
INTERVAL_MAX_S = 3600.0
NOTE_MAX_LEN = 500
# A measurement can take one VISA timeout (2 s) plus a drain (20 x 60 ms);
# give the thread a comfortable margin before reporting it as stuck.
STOP_JOIN_TIMEOUT_S = 10.0

STATE_IDLE = "idle"
STATE_RECORDING = "recording"
STATE_ERROR = "error"


# ---------------------------------------------------------------------------
# Errors, each carrying the HTTP status the route should answer with
# ---------------------------------------------------------------------------
class LcrError(Exception):
    """Instrument or recorder failure (HTTP 500)."""
    http_status = 500


class LcrInputError(LcrError, ValueError):
    """Bad request parameter (HTTP 400)."""
    http_status = 400


class LcrConflictError(LcrError):
    """Operation refused in the current state (HTTP 409)."""
    http_status = 409


def http_status_for(exc):
    """HTTP status for an exception raised by LCRRecorder."""
    if isinstance(exc, LcrError):
        return exc.http_status
    if isinstance(exc, ValueError):
        return 400
    return 500


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def next_tick(t_first, interval_s, slot, now):
    """When the next sample starts: (next_slot, due_time, skipped_slots).

    Ported from Digital_Multitool commit f22527f (continuous_log.py).

    Samples are due on the grid t_first + k * interval_s, k = 0, 1, 2...
    `slot` is the k of the tick that just finished and `now` the time it
    finished (same clock as t_first). Normally the next tick is slot + 1.
    If the reads took so long that one or more later slots have already
    passed, those slots are SKIPPED and counted, never replayed: a slow
    tick is followed by idle time up to the next slot still ahead, not by a
    burst of catch-up reads. So the instrument is never polled more often
    than the cadence asked for, and a source slower than the cadence costs
    samples, not extra traffic.

    An interval too short for the grid arithmetic (the grid index stops
    being finite) degrades to "start now".
    """
    nxt = slot + 1
    due = t_first + nxt * interval_s
    if due >= now:
        return nxt, due, 0
    behind = (now - t_first) / interval_s
    if not math.isfinite(behind) or behind > 2 ** 52:
        return nxt, now, 0
    late = int(math.ceil(behind))
    if late <= nxt:
        return nxt, max(due, now), 0
    return late, t_first + late * interval_s, late - nxt


def validate_run_name(name):
    """Return a safe run name or raise LcrInputError (issue #15).

    Empty or missing means DEFAULT_RUN_NAME. Only letters, digits, '.', '_'
    and '-' (1 to 64 characters) are accepted, so a name can never contain a
    path separator; all-dot names are refused as well."""
    if name is None:
        return DEFAULT_RUN_NAME
    name = str(name).strip()
    if not name:
        return DEFAULT_RUN_NAME
    if not RUN_NAME_RE.match(name) or set(name) == {"."}:
        raise LcrInputError(
            "run name must be 1-64 characters of A-Z a-z 0-9 . _ - "
            "(no path separators)")
    return name


def validate_interval(interval):
    try:
        interval = float(interval)
    except (TypeError, ValueError):
        raise LcrInputError("interval must be numeric")
    if not math.isfinite(interval) or not (
            INTERVAL_MIN_S <= interval <= INTERVAL_MAX_S):
        raise LcrInputError(
            f"interval must be {INTERVAL_MIN_S} to {INTERVAL_MAX_S} s")
    return interval


def utc_iso(dt):
    """datetime (UTC) -> '2026-10-14T15:30:12.345Z'."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def utc_now():
    return datetime.now(timezone.utc)


def software_revision(repo_dir=None):
    """(git revision, dirty flag) of the code that is running, or
    (None, None) when git or the repository is not available."""
    repo_dir = repo_dir or os.path.dirname(os.path.abspath(__file__))
    try:
        rev = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo_dir, capture_output=True,
            text=True, timeout=3)
        if rev.returncode != 0:
            return None, None
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=repo_dir, capture_output=True, text=True, timeout=3)
        dirty = bool(status.stdout.strip()) if status.returncode == 0 else None
        return rev.stdout.strip() or None, dirty
    except (OSError, subprocess.SubprocessError):
        return None, None


# ---------------------------------------------------------------------------
# Recorder
# ---------------------------------------------------------------------------
class LCRRecorder:
    """Manages the BK894 connection, configuration and background recording."""

    def __init__(self, data_dir=None, driver_factory=None):
        if data_dir is None:
            data_dir = os.environ.get(DATA_DIR_ENV) or DEFAULT_DATA_DIR
        self.data_dir = os.path.abspath(os.path.expanduser(data_dir))
        self._driver_factory = driver_factory or BK894

        # _lock guards the handle and every state transition. It is
        # reentrant because disconnect() calls stop() and start() calls
        # connect() while holding it.
        self._lock = threading.RLock()
        # _state_lock guards the counters and the last sample, which the
        # recording thread updates and status()/get_last_data() read. It is
        # never held across instrument I/O, so status polls never block.
        self._state_lock = threading.Lock()

        self._lcr = None
        self._thread = None
        self._stop_event = threading.Event()

        self._state = STATE_IDLE
        self._last_error = None
        self._config = None          # last read-back {'mode', 'frequency', 'voltage'}
        self._run = None             # dict describing the current/last run
        self._last_sample = None
        self._sample_count = 0
        self._skipped_count = 0
        self._error_count = 0
        self._consecutive_errors = 0

    # ------------------------------------------------------------------
    # Properties used by the routes
    # ------------------------------------------------------------------
    @property
    def connected(self):
        return self._lcr is not None

    @property
    def recording(self):
        t = self._thread
        return t is not None and t.is_alive()

    @property
    def mode(self):
        cfg = self._config
        return cfg.get("mode") if cfg else None

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------
    def connect(self):
        """Open the BK894 found by USB VID/PID. Returns the *IDN? string.

        Refused while recording (the running thread owns the handle). Any
        previous handle is closed BEFORE the new one is opened, so repeated
        connects never leak a session (issue #39)."""
        with self._lock:
            if self.recording:
                raise LcrConflictError(
                    "already recording; stop the recording before reconnecting")
            return self._connect_locked()

    def _connect_locked(self):
        old, self._lcr = self._lcr, None
        if old is not None:
            self._close_driver(old)
        self._config = None
        try:
            lcr = self._driver_factory()
        except Exception as exc:
            self._set_error(f"connect failed: {exc}")
            raise LcrError(f"connect failed: {exc}") from exc
        # Learn the meter's current settings so readings taken before any
        # configure are labeled with the mode actually in effect.
        try:
            cfg = lcr.get_config()
            mode = (cfg.get("mode") or "").strip().upper() or None
            self._config = {"mode": mode, "frequency": cfg.get("frequency"),
                            "voltage": cfg.get("voltage")}
        except Exception as exc:
            self._close_driver(lcr)
            self._set_error(f"connect failed reading the configuration: {exc}")
            raise LcrError(
                f"connect failed reading the configuration: {exc}") from exc
        self._lcr = lcr
        with self._state_lock:
            self._state = STATE_IDLE
            self._last_error = None
        return lcr.idn

    def disconnect(self):
        """Stop any recording, return the meter to local, close the session."""
        with self._lock:
            self.stop()
            old, self._lcr = self._lcr, None
            self._config = None
            if old is not None:
                self._close_driver(old)

    @staticmethod
    def _close_driver(lcr):
        try:
            lcr.go_local()
        except Exception:
            pass
        try:
            lcr.close()
        except Exception:
            pass

    def _require_driver_locked(self):
        if self._lcr is None:
            self._connect_locked()
        return self._lcr

    # ------------------------------------------------------------------
    # Single measurement (independent of recording)
    # ------------------------------------------------------------------
    def measure_once(self):
        """Take one measurement. Connects first if needed.

        Returns the API dict (raw values, display-safe values, units)."""
        with self._lock:
            lcr = self._require_driver_locked()
        # The handle lock is released before the I/O so status polls do not
        # wait on the meter; the driver's own I/O lock serializes this with
        # the recording thread.
        try:
            primary, secondary, status = lcr.measure()
        except Exception as exc:
            try:
                lcr._drain()
            except Exception:
                pass
            raise LcrError(f"measurement failed: {exc}") from exc
        return self._reading_dict(primary, secondary, status, self.mode)

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------
    def start(self, run_name=None, interval=0.5, mode="RX", freq=1000,
              voltage=1.0, note=None):
        """Connect if needed, configure with read-back, create the run folder,
        write the start manifest and start the recording thread.

        Raises LcrInputError (400), LcrConflictError (409, already recording
        or run folder exists) or LcrError (500, instrument failure). Returns
        a dict describing the run."""
        name = validate_run_name(run_name)
        interval = validate_interval(interval)
        try:
            mode, freq, voltage = BK894.check_settings(mode, freq, voltage)
        except (TypeError, ValueError) as exc:
            raise LcrInputError(str(exc)) from exc
        note = "" if note is None else str(note)
        if len(note) > NOTE_MAX_LEN:
            raise LcrInputError(f"note must be at most {NOTE_MAX_LEN} characters")

        with self._lock:
            if self.recording:
                raise LcrConflictError("already recording")
            lcr = self._require_driver_locked()
            try:
                readback = lcr.configure_and_verify(mode, freq, voltage)
            except Exception as exc:
                try:
                    lcr._drain()
                except Exception:
                    pass
                self._set_error(f"configure failed: {exc}")
                raise LcrError(f"configure failed: {exc}") from exc
            self._config = dict(readback)

            started = utc_now()
            run_id = f"{started.strftime('%Y%m%dT%H%M%SZ')}_{name}"
            run_dir = os.path.join(self.data_dir, run_id)
            # The folder name is one path component by construction; check
            # anyway that it resolves inside data_dir.
            if os.path.dirname(os.path.realpath(run_dir)) != os.path.realpath(
                    self.data_dir):
                raise LcrInputError("run folder would fall outside the data directory")
            try:
                os.makedirs(self.data_dir, exist_ok=True)
                os.mkdir(run_dir)
            except FileExistsError as exc:
                raise LcrConflictError(
                    f"run folder {run_id} already exists; pick another name "
                    f"or wait a second") from exc
            except OSError as exc:
                raise LcrError(f"cannot create run folder: {exc}") from exc

            rev, dirty = software_revision()
            run = {
                "run_id": run_id,
                "run_dir": run_dir,
                "name": name,
                "note": note,
                "start_utc": utc_iso(started),
                "end_utc": None,
                "idn": getattr(lcr, "idn", None),
                "resource": getattr(lcr, "resource", None),
                "requested": {"mode": mode, "frequency_hz": freq,
                              "voltage_v": voltage},
                "readback": {"mode": readback.get("mode"),
                             "frequency_hz": readback.get("frequency"),
                             "voltage_v": readback.get("voltage")},
                "interval_s": interval,
                "software_git_rev": rev,
                "software_git_dirty": dirty,
                "stop_reason": None,
            }
            with self._state_lock:
                self._run = run
                self._last_sample = None
                self._sample_count = 0
                self._skipped_count = 0
                self._error_count = 0
                self._consecutive_errors = 0
                self._last_error = None
                self._state = STATE_RECORDING
            try:
                self._write_manifest(run)
            except OSError as exc:
                with self._state_lock:
                    self._state = STATE_ERROR
                    self._last_error = f"cannot write manifest: {exc}"
                raise LcrError(f"cannot write manifest: {exc}") from exc

            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._record_loop, args=(lcr, run, interval),
                name="LCRRecorder", daemon=True)
            self._thread.start()
            return {"run_id": run_id, "run_dir": run_dir,
                    "interval_s": interval, "requested": run["requested"],
                    "readback": run["readback"], "idn": run["idn"]}

    def stop(self):
        """Stop the recording thread (if any) and clear the last sample.

        Returns True when no recording thread is left running."""
        with self._lock:
            self._stop_event.set()
            t = self._thread
            if t is not None and t.is_alive() and t is not threading.current_thread():
                t.join(timeout=STOP_JOIN_TIMEOUT_S)
            stopped = not (t is not None and t.is_alive())
            with self._state_lock:
                self._last_sample = None
                if not stopped:
                    self._state = STATE_ERROR
                    self._last_error = (
                        f"recording thread did not stop within "
                        f"{STOP_JOIN_TIMEOUT_S:g} s (instrument I/O stuck?)")
                else:
                    # A finished run (or a failed connect) leaves the
                    # recorder idle; last_error stays visible in status().
                    self._state = STATE_IDLE
            return stopped

    def _record_loop(self, lcr, run, interval):
        """Background loop: fixed-grid :FETC? into the run CSV."""
        csv_path = os.path.join(run["run_dir"], CSV_NAME)
        fh = None
        stop_reason = "stopped by user"
        try:
            fh = open(csv_path, "x", newline="", encoding="utf-8")
            writer = csv.writer(fh)
            writer.writerow(CSV_COLUMNS)
            fh.flush()
            t_first = time.monotonic()
            slot = 0
            due = t_first
            while True:
                delay = due - time.monotonic()
                if delay > 0:
                    if self._stop_event.wait(delay):
                        break
                elif self._stop_event.is_set():
                    break
                # Timestamps mark when :FETC? was sent.
                t_mono = time.monotonic()
                t_utc = utc_now()
                try:
                    primary, secondary, status = lcr.measure()
                except Exception as exc:
                    msg = f"measurement failed: {exc}"
                    with self._state_lock:
                        self._error_count += 1
                        self._consecutive_errors += 1
                        self._last_error = msg
                        n_bad = self._consecutive_errors
                    # Realign a desynced reply stream before the next tick
                    # (issue #37).
                    try:
                        lcr._drain()
                    except Exception:
                        pass
                    if n_bad >= MAX_CONSECUTIVE_ERRORS:
                        stop_reason = (f"{n_bad} consecutive measurement "
                                       f"errors; last: {msg}")
                        with self._state_lock:
                            self._state = STATE_ERROR
                        break
                else:
                    writer.writerow([slot, utc_iso(t_utc),
                                     f"{t_mono - t_first:.6f}",
                                     repr(primary), repr(secondary), status])
                    fh.flush()
                    with self._state_lock:
                        self._sample_count += 1
                        self._consecutive_errors = 0
                        self._last_sample = {
                            "sample_index": slot,
                            "utc_iso": utc_iso(t_utc),
                            "t_mono_s": t_mono - t_first,
                            "mono": t_mono,
                            "primary": primary,
                            "secondary": secondary,
                            "status": status,
                        }
                slot, due, skipped = next_tick(t_first, interval, slot,
                                               time.monotonic())
                if skipped:
                    with self._state_lock:
                        self._skipped_count += skipped
        except Exception as exc:
            # Open or write failure: the run cannot continue.
            stop_reason = f"recording failed: {exc}"
            with self._state_lock:
                self._state = STATE_ERROR
                self._last_error = stop_reason
        finally:
            if fh is not None:
                try:
                    fh.close()
                except OSError:
                    pass
            with self._state_lock:
                run["end_utc"] = utc_iso(utc_now())
                run["stop_reason"] = stop_reason
                if self._state == STATE_RECORDING:
                    self._state = STATE_IDLE
            try:
                self._write_manifest(run)
            except Exception as exc:
                with self._state_lock:
                    self._state = STATE_ERROR
                    self._last_error = f"cannot write final manifest: {exc}"

    # ------------------------------------------------------------------
    # Manifest
    # ------------------------------------------------------------------
    def _manifest_dict(self, run):
        with self._state_lock:
            run = dict(run)
            counts = {
                "sample_count": self._sample_count,
                "skipped_count": self._skipped_count,
                "error_count": self._error_count,
                "last_error": self._last_error,
            }
        return {
            "schema": MANIFEST_SCHEMA,
            "run_id": run["run_id"],
            "note": run["note"],
            "start_utc": run["start_utc"],
            "end_utc": run["end_utc"],
            "stop_reason": run["stop_reason"],
            "instrument": {"idn": run["idn"], "resource": run["resource"]},
            "requested": run["requested"],
            "readback": run["readback"],
            "interval_s": run["interval_s"],
            **counts,
            "csv": CSV_NAME,
            "csv_columns": CSV_COLUMNS,
            "csv_notes": (
                "sample_index is the grid slot k (due at k * interval_s after "
                "the first sample); missing k are skipped or failed slots. "
                "utc_iso and t_mono_s mark when :FETC? was sent. primary, "
                "secondary and status are the raw meter values, unfiltered."),
            "software_git_rev": run["software_git_rev"],
            "software_git_dirty": run["software_git_dirty"],
            "written_utc": utc_iso(utc_now()),
        }

    def _write_manifest(self, run):
        manifest = self._manifest_dict(run)
        path = os.path.join(run["run_dir"], MANIFEST_NAME)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
        return manifest

    # ------------------------------------------------------------------
    # State for the routes
    # ------------------------------------------------------------------
    def _set_error(self, msg):
        with self._state_lock:
            self._state = STATE_ERROR
            self._last_error = msg

    @staticmethod
    def _reading_dict(primary, secondary, status, mode):
        return {
            "primary_raw": json_safe_raw(primary),
            "secondary_raw": json_safe_raw(secondary),
            "status": status,
            "display": display_reading(mode, primary, secondary, status),
            "mode": mode,
        }

    def status(self):
        lcr = self._lcr
        cfg = self._config
        with self._state_lock:
            run = dict(self._run) if self._run else None
            out = {
                "state": self._state,
                "recording": self.recording,
                "connected": lcr is not None,
                "idn": getattr(lcr, "idn", None) if lcr is not None else None,
                "resource": getattr(lcr, "resource", None) if lcr is not None else None,
                "config": dict(cfg) if cfg else None,
                "mode": cfg.get("mode") if cfg else None,
                "sample_count": self._sample_count,
                "skipped_count": self._skipped_count,
                "error_count": self._error_count,
                "consecutive_errors": self._consecutive_errors,
                "last_error": self._last_error,
                "data_dir": self.data_dir,
            }
        if run:
            out.update({
                "run_id": run["run_id"],
                "run_dir": run["run_dir"],
                "interval_s": run["interval_s"],
                "start_utc": run["start_utc"],
                "end_utc": run["end_utc"],
                "stop_reason": run["stop_reason"],
            })
        else:
            out.update({"run_id": None, "run_dir": None, "interval_s": None,
                        "start_utc": None, "end_utc": None,
                        "stop_reason": None})
        return out

    def get_last_data(self):
        """Latest sample for the live display, with its age, or a dict of
        Nones when there is no sample (none yet, or cleared by start/stop)."""
        with self._state_lock:
            sample = dict(self._last_sample) if self._last_sample else None
            state = self._state
        mode = self.mode
        if sample is None:
            return {"sample_index": None, "utc_iso": None, "t_mono_s": None,
                    "age_s": None, "primary_raw": None,
                    "secondary_raw": None, "status": None,
                    "display": display_reading(mode, None, None, None),
                    "mode": mode, "state": state}
        out = self._reading_dict(sample["primary"], sample["secondary"],
                                 sample["status"], mode)
        out.update({
            "sample_index": sample["sample_index"],
            "utc_iso": sample["utc_iso"],
            "t_mono_s": sample["t_mono_s"],
            "age_s": max(0.0, time.monotonic() - sample["mono"]),
            "state": state,
        })
        return out
