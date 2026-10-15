#!/usr/bin/env python3
"""Headless tests for LCRRecorder (fake driver, temp data dir, no hardware).

Run from the repository root: python -m unittest discover -s tests -v
"""
import csv
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "RaspberryPiWebserver"))

import lcr_controller  # noqa: E402
from instruments import BK894, ConfigReadbackError  # noqa: E402
from lcr_controller import (CSV_COLUMNS, LCRRecorder,  # noqa: E402
                            LcrConflictError, LcrError, LcrInputError,
                            MAX_CONSECUTIVE_ERRORS, http_status_for,
                            next_tick, validate_run_name)

UTC_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


class FakeDriver:
    """Stands in for BK894: records lifecycle calls, scripted measure()."""

    instances = []

    def __init__(self, measure=None, fail_configure=False, log=None):
        self.idn = "B&K Precision,894,SN123,1.0.5"
        self.resource = "USB0::1137::10279::X::0::INSTR"
        self._measure = measure
        self.fail_configure = fail_configure
        self.closed = False
        self.local = False
        self.drains = 0
        self.n_measure = 0
        self.log = log if log is not None else []
        self.log.append(("open", id(self)))
        FakeDriver.instances.append(self)

    def get_config(self):
        return {"mode": "cpd", "frequency": 1000.0, "voltage": 1.0}

    def configure_and_verify(self, mode, freq_hz, voltage):
        if self.fail_configure:
            raise ConfigReadbackError("BK894 did not apply the requested "
                                      "settings: voltage: requested 0.5 V, "
                                      "meter reports 1 V")
        return {"mode": mode, "frequency": float(freq_hz),
                "voltage": float(voltage)}

    def measure(self):
        self.n_measure += 1
        if self._measure is not None:
            return self._measure(self.n_measure)
        return (1.0e-9 * self.n_measure, 0.001, 0)

    def _drain(self):
        self.drains += 1

    def go_local(self):
        self.local = True

    def close(self):
        self.closed = True
        self.log.append(("close", id(self)))


def wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


class RecorderCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="arss-test-")
        self.data_dir = os.path.join(self.tmp, "data")
        self.log = []
        self.measure_fn = None
        self.fail_configure = False
        self.factory_calls = 0

        def factory():
            self.factory_calls += 1
            return FakeDriver(measure=self.measure_fn,
                              fail_configure=self.fail_configure,
                              log=self.log)
        self.rec = LCRRecorder(data_dir=self.data_dir, driver_factory=factory)

    def tearDown(self):
        try:
            self.rec.disconnect()
        finally:
            shutil.rmtree(self.tmp, ignore_errors=True)

    def start(self, **kw):
        args = dict(run_name="t1", interval=0.05, mode="CPD", freq=1000,
                    voltage=0.5)
        args.update(kw)
        return self.rec.start(**args)

    def read_run(self, run_dir):
        with open(os.path.join(run_dir, "manifest.json"), encoding="utf-8") as fh:
            manifest = json.load(fh)
        with open(os.path.join(run_dir, "lcr.csv"), newline="",
                  encoding="utf-8") as fh:
            rows = list(csv.reader(fh))
        return manifest, rows


class TestSingleWorker(RecorderCase):
    def test_second_start_is_refused(self):
        self.start()
        with self.assertRaises(LcrConflictError) as cm:
            self.start(run_name="t2")
        self.assertIn("already recording", str(cm.exception))
        self.assertEqual(http_status_for(cm.exception), 409)
        workers = [t for t in threading.enumerate() if t.name == "LCRRecorder"]
        self.assertEqual(len(workers), 1)
        self.assertTrue(self.rec.stop())
        self.assertFalse(self.rec.recording)

    def test_connect_refused_while_recording(self):
        self.start()
        n_open = self.factory_calls
        with self.assertRaises(LcrConflictError):
            self.rec.connect()
        self.assertEqual(self.factory_calls, n_open, "no new session opened")
        self.rec.stop()

    def test_reconnect_closes_old_handle_first(self):
        self.rec.connect()
        first = FakeDriver.instances[-1]
        self.rec.connect()
        second = FakeDriver.instances[-1]
        self.assertTrue(first.closed)
        self.assertFalse(second.closed)
        self.assertEqual(self.log, [("open", id(first)), ("close", id(first)),
                                    ("open", id(second))])

    def test_disconnect_stops_recording(self):
        self.start()
        drv = FakeDriver.instances[-1]
        self.rec.disconnect()
        self.assertFalse(self.rec.recording)
        self.assertFalse(self.rec.connected)
        self.assertTrue(drv.closed)
        self.assertTrue(drv.local)

    def test_connect_failure_is_reported(self):
        def boom():
            raise RuntimeError("BK894: no device found")
        rec = LCRRecorder(data_dir=self.data_dir, driver_factory=boom)
        with self.assertRaises(LcrError) as cm:
            rec.connect()
        self.assertEqual(http_status_for(cm.exception), 500)
        st = rec.status()
        self.assertEqual(st["state"], "error")
        self.assertIn("no device found", st["last_error"])
        self.assertFalse(st["connected"])


class TestStartValidation(RecorderCase):
    def test_bad_settings_are_400_and_touch_nothing(self):
        for kw in ({"mode": "NOPE"}, {"freq": 600000}, {"freq": 50},
                   {"voltage": 2.5}, {"interval": 0.0},
                   {"interval": float("nan")}, {"note": "x" * 501}):
            with self.assertRaises(LcrInputError, msg=repr(kw)) as cm:
                self.start(**kw)
            self.assertEqual(http_status_for(cm.exception), 400)
        self.assertEqual(self.factory_calls, 0)
        self.assertFalse(os.path.exists(self.data_dir))

    def test_readback_mismatch_fails_start(self):
        self.fail_configure = True
        with self.assertRaises(LcrError) as cm:
            self.start()
        self.assertEqual(http_status_for(cm.exception), 500)
        self.assertIn("did not apply", str(cm.exception))
        self.assertFalse(self.rec.recording)
        self.assertEqual(self.rec.status()["state"], "error")
        self.assertFalse(os.path.exists(self.data_dir), "no run folder")


class TestRunNames(RecorderCase):
    def test_valid_names(self):
        for name in ("lcr", "run_01", "DEA-3.sample", "a" * 64,
                     "measurements.csv"):
            self.assertEqual(validate_run_name(name), name)
        self.assertEqual(validate_run_name(None), "lcr")
        self.assertEqual(validate_run_name(""), "lcr")
        self.assertEqual(validate_run_name("  "), "lcr")

    def test_invalid_names(self):
        for name in ("../x", "..", ".", "...", "a/b", "a\\b", "/etc/passwd",
                     "C:\\x", "a b", "x" * 65, "run;rm", "caf\u00e9",
                     "..\\..\\x", "a\x00b"):
            with self.assertRaises(LcrInputError, msg=repr(name)):
                validate_run_name(name)

    def test_traversal_is_rejected_by_start(self):
        with self.assertRaises(LcrInputError):
            self.start(run_name="../../evil")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "evil")))
        self.assertFalse(os.path.exists(self.data_dir))
        self.assertEqual(self.factory_calls, 0)

    def test_existing_run_folder_is_not_overwritten(self):
        fixed = datetime(2026, 10, 14, 15, 30, 12, 345000, tzinfo=timezone.utc)
        with mock.patch.object(lcr_controller, "utc_now", return_value=fixed):
            run = self.start(run_name="same")
            self.assertTrue(wait_for(
                lambda: self.rec.status()["sample_count"] >= 1))
            self.rec.stop()
            with open(os.path.join(run["run_dir"], "lcr.csv"), "rb") as fh:
                before = fh.read()
            with self.assertRaises(LcrConflictError) as cm:
                self.start(run_name="same")
            self.assertEqual(http_status_for(cm.exception), 409)
        self.assertEqual(run["run_id"], "20261014T153012Z_same")
        with open(os.path.join(run["run_dir"], "lcr.csv"), "rb") as fh:
            self.assertEqual(fh.read(), before)


class TestRecordLoop(RecorderCase):
    def test_manifest_and_csv_contents(self):
        with mock.patch.object(lcr_controller, "software_revision",
                               return_value=("abc123", False)):
            run = self.start(note="sample A, 20 % strain")
        self.assertTrue(run["run_dir"].startswith(self.data_dir))
        self.assertTrue(os.path.exists(os.path.join(run["run_dir"],
                                                    "manifest.json")))
        self.assertTrue(wait_for(
            lambda: self.rec.status()["sample_count"] >= 3))
        self.assertTrue(self.rec.stop())
        manifest, rows = self.read_run(run["run_dir"])

        self.assertEqual(manifest["run_id"], run["run_id"])
        self.assertEqual(manifest["note"], "sample A, 20 % strain")
        self.assertRegex(manifest["start_utc"], UTC_ISO_RE)
        self.assertRegex(manifest["end_utc"], UTC_ISO_RE)
        self.assertEqual(manifest["instrument"]["idn"],
                         "B&K Precision,894,SN123,1.0.5")
        self.assertEqual(manifest["requested"],
                         {"mode": "CPD", "frequency_hz": 1000.0,
                          "voltage_v": 0.5})
        self.assertEqual(manifest["readback"],
                         {"mode": "CPD", "frequency_hz": 1000.0,
                          "voltage_v": 0.5})
        self.assertEqual(manifest["interval_s"], 0.05)
        self.assertEqual(manifest["software_git_rev"], "abc123")
        self.assertIs(manifest["software_git_dirty"], False)
        self.assertEqual(manifest["error_count"], 0)
        self.assertIsNone(manifest["last_error"])
        self.assertEqual(manifest["stop_reason"], "stopped by user")
        self.assertIn("skipped_count", manifest)
        self.assertEqual(manifest["csv_columns"], CSV_COLUMNS)

        self.assertEqual(rows[0], CSV_COLUMNS)
        data = rows[1:]
        self.assertEqual(len(data), manifest["sample_count"])
        for i, row in enumerate(data):
            self.assertRegex(row[1], UTC_ISO_RE)
            self.assertEqual(row[5], "0")
            self.assertGreaterEqual(float(row[2]), 0.0)
        # raw values are written unchanged (repr round-trips the float)
        self.assertEqual(float(data[0][3]), 1.0e-9)
        self.assertEqual(int(data[0][0]), 0)

    def test_overload_is_raw_in_csv_but_none_for_display(self):
        self.measure_fn = lambda n: (9.9e37, 9.9e37, 1)
        run = self.start()
        self.assertTrue(wait_for(
            lambda: self.rec.status()["sample_count"] >= 1))
        data = self.rec.get_last_data()
        self.assertEqual(data["primary_raw"], 9.9e37)
        self.assertIsNone(data["display"]["primary"])
        self.assertEqual(data["display"]["primary_text"], "OVL")
        self.assertTrue(data["display"]["overload"])
        self.rec.stop()
        _, rows = self.read_run(run["run_dir"])
        self.assertEqual(float(rows[1][3]), 9.9e37)

    def test_error_cutoff_stops_run(self):
        def always_fail(n):
            raise TimeoutError("VI_ERROR_TMO (-1073807339)")
        self.measure_fn = always_fail
        run = self.start()
        self.assertTrue(wait_for(lambda: not self.rec.recording))
        st = self.rec.status()
        self.assertEqual(st["state"], "error")
        self.assertEqual(st["error_count"], MAX_CONSECUTIVE_ERRORS)
        self.assertIn("VI_ERROR_TMO", st["last_error"])
        self.assertEqual(st["sample_count"], 0)
        drv = FakeDriver.instances[-1]
        self.assertEqual(drv.drains, MAX_CONSECUTIVE_ERRORS,
                         "drain after every failed read")
        manifest, rows = self.read_run(run["run_dir"])
        self.assertEqual(manifest["error_count"], MAX_CONSECUTIVE_ERRORS)
        self.assertIn("VI_ERROR_TMO", manifest["last_error"])
        self.assertIn("consecutive", manifest["stop_reason"])
        self.assertIsNotNone(manifest["end_utc"])
        self.assertEqual(rows, [CSV_COLUMNS])

    def test_isolated_errors_do_not_stop_run(self):
        def flaky(n):
            if n % 2 == 0:
                raise ValueError("could not convert string to float: 'CSRS'")
            return (1e-9, 0.001, 0)
        self.measure_fn = flaky
        self.start()
        self.assertTrue(wait_for(
            lambda: self.rec.status()["error_count"] >= MAX_CONSECUTIVE_ERRORS + 1))
        st = self.rec.status()
        self.assertTrue(self.rec.recording)
        self.assertEqual(st["state"], "recording")
        self.assertIn("CSRS", st["last_error"])
        self.rec.stop()

    def test_slow_reads_skip_and_count_slots(self):
        def slow(n):
            time.sleep(0.13)
            return (1e-9, 0.001, 0)
        self.measure_fn = slow
        run = self.start(interval=0.05)
        self.assertTrue(wait_for(
            lambda: self.rec.status()["sample_count"] >= 3))
        self.rec.stop()
        manifest, rows = self.read_run(run["run_dir"])
        self.assertGreaterEqual(manifest["skipped_count"], 2)
        idx = [int(r[0]) for r in rows[1:]]
        self.assertEqual(idx, sorted(set(idx)))
        self.assertTrue(any(b - a > 1 for a, b in zip(idx, idx[1:])),
                        f"grid gaps expected in {idx}")

    def test_last_sample_cleared_on_stop_and_start(self):
        self.start()
        self.assertTrue(wait_for(
            lambda: self.rec.get_last_data()["status"] is not None))
        data = self.rec.get_last_data()
        self.assertIsNotNone(data["age_s"])
        self.assertGreaterEqual(data["age_s"], 0.0)
        self.assertRegex(data["utc_iso"], UTC_ISO_RE)
        self.assertEqual(data["display"]["primary_unit"], "F")
        self.rec.stop()
        data = self.rec.get_last_data()
        self.assertIsNone(data["status"])
        self.assertIsNone(data["age_s"])
        st = self.rec.status()
        self.assertEqual(st["state"], "idle")
        self.assertFalse(st["recording"])

    def test_open_failure_is_reported(self):
        # Pre-create lcr.csv as a directory so open(..., 'x') fails in the
        # thread; the run must end in the error state with a manifest.
        real_mkdir = os.mkdir

        def mkdir_and_block(path, *a, **kw):
            real_mkdir(path, *a, **kw)
            real_mkdir(os.path.join(path, "lcr.csv"))
        with mock.patch.object(lcr_controller.os, "mkdir", mkdir_and_block):
            run = self.start()
        self.assertTrue(wait_for(lambda: not self.rec.recording))
        st = self.rec.status()
        self.assertEqual(st["state"], "error")
        self.assertIn("recording failed", st["last_error"])
        with open(os.path.join(run["run_dir"], "manifest.json"),
                  encoding="utf-8") as fh:
            manifest = json.load(fh)
        self.assertIn("recording failed", manifest["stop_reason"])

    def test_measure_once_auto_connects_and_is_display_safe(self):
        self.measure_fn = lambda n: (float("nan"), 0.5, 0)
        out = self.rec.measure_once()
        self.assertEqual(self.factory_calls, 1)
        self.assertEqual(out["primary_raw"], "nan")
        self.assertIsNone(out["display"]["primary"])
        self.assertEqual(out["mode"], "CPD")
        json.dumps(out, allow_nan=False)   # must be strict-JSON safe


class TestNextTick(unittest.TestCase):
    """Fixed-grid scheduler, ported from Digital_Multitool continuous_log."""

    def test_on_time(self):
        self.assertEqual(next_tick(0.0, 1.0, 0, 0.3), (1, 1.0, 0))

    def test_exactly_due(self):
        self.assertEqual(next_tick(0.0, 1.0, 0, 1.0), (1, 1.0, 0))

    def test_overrun_skips_and_counts(self):
        # tick 0 took 2.5 s at a 1 s cadence: slots 1 and 2 are gone
        self.assertEqual(next_tick(0.0, 1.0, 0, 2.5), (3, 3.0, 2))

    def test_slight_overrun_starts_now_without_skip(self):
        # behind = 1.2 -> ceil 2 > nxt 1 -> slot 2, one skipped
        self.assertEqual(next_tick(10.0, 1.0, 0, 11.2), (2, 12.0, 1))

    def test_no_drift(self):
        # grid stays anchored to t_first regardless of read time
        slot, due, _ = next_tick(5.0, 0.5, 7, 8.9)
        self.assertEqual((slot, due), (8, 9.0))

    def test_degenerate_interval(self):
        slot, due, skipped = next_tick(0.0, 1e-300, 0, 1.0)
        self.assertEqual((slot, due, skipped), (1, 1.0, 0))


class TestHttpMapping(unittest.TestCase):
    def test_status_codes(self):
        self.assertEqual(http_status_for(LcrInputError("x")), 400)
        self.assertEqual(http_status_for(LcrConflictError("x")), 409)
        self.assertEqual(http_status_for(LcrError("x")), 500)
        self.assertEqual(http_status_for(ValueError("x")), 400)
        self.assertEqual(http_status_for(RuntimeError("x")), 500)

    def test_default_data_dir_from_env(self):
        with mock.patch.dict(os.environ, {"ARSS_DATA_DIR": "/srv/arss"}):
            rec = LCRRecorder(driver_factory=lambda: None)
        self.assertEqual(rec.data_dir, os.path.abspath("/srv/arss"))
        with mock.patch.dict(os.environ):
            os.environ.pop("ARSS_DATA_DIR", None)
            rec = LCRRecorder(driver_factory=lambda: None)
        self.assertTrue(rec.data_dir.endswith("arss-data"))

    def test_real_driver_is_default_factory(self):
        rec = LCRRecorder(data_dir=tempfile.gettempdir())
        self.assertIs(rec._driver_factory, BK894)


if __name__ == "__main__":
    unittest.main()
