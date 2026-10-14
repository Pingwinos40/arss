#!/usr/bin/env python3
"""Headless tests for the BK894 driver (fake transports, no hardware, no pyvisa).

Reply formats ('1.00000e+03', 'CSRS', the off-by-one desync) are the ones
captured from the real meter in Digital_Multitool commit f22527f,
tests/test_bk894_parse.py.

Run from the repository root: python -m unittest discover -s tests -v
"""
import os
import sys
import threading
import time
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "RaspberryPiWebserver"))

import instruments  # noqa: E402
from instruments import (BK894, ConfigReadbackError,  # noqa: E402
                         InstrumentIdentityError, _io_lock)

BK_RESOURCE = "USB0::1137::10279::GARBAGE::0::INSTR"


class FakeLCR(BK894):
    """BK894 with ask/write stubbed (skips VisaInstrument.__init__)."""

    def __init__(self, answers=None):
        self.answers = answers or {}
        self.sent = []
        self.inst = types.SimpleNamespace(timeout=2000)

    def ask(self, command):
        self.sent.append(command)
        return self.answers[command]

    def write(self, command):
        self.sent.append(command)


class MeterInst:
    """Fake pyvisa session that models the meter's settings: writes set
    them, queries read them back in the meter's reply format."""

    def __init__(self, idn="B&K Precision,894,SN123,1.0.5", ignore=()):
        self.idn = idn
        self.ignore = set(ignore)   # command headers silently ignored
        self.mode = "CPD"
        self.freq = 1000.0
        self.volt = 1.0
        self.timeout = None
        self.read_termination = None
        self.write_termination = None
        self.cleared = 0
        self.closed = False
        self.log = []

    def clear(self):
        self.cleared += 1
        self.log.append("clear")

    def close(self):
        self.closed = True

    def write(self, cmd):
        self.log.append(cmd)
        head, _, arg = cmd.partition(" ")
        if head in self.ignore:
            return
        if head == ":FUNC:IMP":
            self.mode = arg
        elif head == ":FREQ":
            self.freq = float(arg)
        elif head == ":VOLT":
            self.volt = float(arg)

    def query(self, cmd):
        self.log.append(cmd)
        if cmd == "*IDN?":
            return self.idn + "\n"
        if cmd == ":FUNC:IMP?":
            return self.mode.lower() + "\n"
        if cmd == ":FREQ?":
            return f"{self.freq:.5e}\n"
        if cmd == ":VOLT?":
            return f"{self.volt:.5e}\n"
        if cmd == ":FETC?":
            return "+1.00000e-09,+2.00000e-03,0\n"
        raise AssertionError(f"unexpected query {cmd}")

    def read(self):
        raise Exception("timeout")   # empty buffer ends a drain


class FakeRM:
    def __init__(self, resources, inst):
        self.resources = resources
        self.inst = inst
        self.opened = []

    def list_resources(self):
        return tuple(self.resources)

    def open_resource(self, resource):
        self.opened.append(resource)
        return self.inst


class TestDiscoveryAndOpen(unittest.TestCase):
    def test_vid_pid_values(self):
        self.assertEqual(BK894.VID, 0x0471)
        self.assertEqual(BK894.PID, 0x2827)

    def test_discovers_by_vid_pid_and_runs_open_sequence(self):
        inst = MeterInst()
        rm = FakeRM(["ASRL/dev/ttyACM0::INSTR",
                     "USB0::1689::261::C0001::0::INSTR",   # Tek scope
                     BK_RESOURCE], inst)
        lcr = BK894(rm=rm)
        self.assertEqual(rm.opened, [BK_RESOURCE])
        self.assertEqual(lcr.resource, BK_RESOURCE)
        self.assertEqual(inst.timeout, 2000)
        self.assertEqual(inst.read_termination, "\n")
        self.assertEqual(inst.write_termination, "\n")
        # clear() happens before the first query
        self.assertEqual(inst.log[:2], ["clear", "*IDN?"])
        self.assertEqual(lcr.idn, "B&K Precision,894,SN123,1.0.5")
        self.assertFalse(inst.closed)

    def test_no_device_raises_clear_error(self):
        rm = FakeRM(["USB0::1689::261::C0001::0::INSTR"], MeterInst())
        with self.assertRaises(RuntimeError) as cm:
            BK894(rm=rm)
        self.assertIn("0471", str(cm.exception))
        self.assertEqual(rm.opened, [])

    def test_idn_of_wrong_model_raises_and_closes(self):
        inst = MeterInst(idn="TEKTRONIX,MSO24,C012345,CF:91.1CT FV:1.0")
        rm = FakeRM([BK_RESOURCE], inst)
        with self.assertRaises(InstrumentIdentityError) as cm:
            BK894(rm=rm)
        self.assertIn("894", str(cm.exception))
        self.assertTrue(inst.closed, "session must be closed on IDN mismatch")

    def test_model_digits_in_serial_do_not_pass(self):
        inst = MeterInst(idn="ACME,LCR-100,SN894000,1.0")
        with self.assertRaises(InstrumentIdentityError):
            BK894(rm=FakeRM([BK_RESOURCE], inst))
        self.assertTrue(inst.closed)

    def test_idn_failure_closes_session(self):
        inst = MeterInst()

        def boom(cmd):
            raise TimeoutError("VI_ERROR_TMO")
        inst.query = boom
        with self.assertRaises(TimeoutError):
            BK894(rm=FakeRM([BK_RESOURCE], inst))
        self.assertTrue(inst.closed)

    def test_module_import_does_not_need_pyvisa(self):
        # The resource manager is created lazily; importing instruments and
        # opening through an injected rm never touched it.
        self.assertIsNone(instruments._RM)


class TestCommands(unittest.TestCase):
    def test_voltage_uses_plain_volt_command(self):
        lcr = FakeLCR()
        lcr.set_voltage(0.5)
        self.assertEqual(lcr.sent, [":VOLT 0.5"])
        self.assertFalse(any("LEV" in c for c in lcr.sent))

    def test_voltage_range(self):
        lcr = FakeLCR()
        for bad in (0.0, 0.009, 2.01, 5.0):
            with self.assertRaises(ValueError):
                lcr.set_voltage(bad)
        self.assertEqual(lcr.sent, [])

    def test_frequency_range_is_500k(self):
        lcr = FakeLCR()
        lcr.set_frequency(500000)
        lcr.set_frequency(100)
        self.assertEqual(lcr.sent, [":FREQ 500000", ":FREQ 100"])
        for bad in (99, 500001):
            with self.assertRaises(ValueError):
                lcr.set_frequency(bad)

    def test_fourteen_modes(self):
        self.assertEqual(len(BK894.MODES), 14)
        for m in ("CSD", "CSQ", "CSRS", "CPD", "ZTR"):
            self.assertIn(m, BK894.MODES)
        lcr = FakeLCR()
        lcr.set_mode("csrs")
        self.assertEqual(lcr.sent, [":FUNC:IMP CSRS"])
        with self.assertRaises(ValueError):
            lcr.set_mode("XYZ")

    def test_measure_parses_fetch(self):
        lcr = FakeLCR({":FETC?": "+1.23450e-09,+4.50000e-03,0"})
        self.assertEqual(lcr.measure(), (1.2345e-09, 0.0045, 0))

    def test_check_settings(self):
        self.assertEqual(BK894.check_settings("cpd", "1000", "0.5"),
                         ("CPD", 1000.0, 0.5))
        for args in (("NOPE", 1000, 1), ("RX", 50, 1), ("RX", 600000, 1),
                     ("RX", 1000, 3), ("RX", float("nan"), 1),
                     ("RX", 1000, float("inf"))):
            with self.assertRaises(ValueError):
                BK894.check_settings(*args)


class TestReadbackGuard(unittest.TestCase):
    def _lcr(self, inst):
        return BK894(rm=FakeRM([BK_RESOURCE], inst))

    def test_configure_and_verify_sends_volt_and_returns_readback(self):
        inst = MeterInst()
        lcr = self._lcr(inst)
        cfg = lcr.configure_and_verify("ZTD", 250000, 0.25)
        self.assertEqual(cfg, {"mode": "ZTD", "frequency": 250000.0,
                               "voltage": 0.25})
        self.assertIn(":VOLT 0.25", inst.log)
        self.assertIn(":FREQ 250000", inst.log)
        self.assertFalse(any(c.startswith(":LEV") for c in inst.log))

    def test_ignored_level_command_raises(self):
        # A meter that silently ignores the level write (the #31 symptom)
        # keeps its old level; the read-back must catch it.
        inst = MeterInst(ignore={":VOLT"})
        lcr = self._lcr(inst)
        with self.assertRaises(ConfigReadbackError) as cm:
            lcr.configure_and_verify("CPD", 1000, 0.5)
        self.assertIn("voltage", str(cm.exception))

    def test_frequency_mismatch_raises(self):
        inst = MeterInst(ignore={":FREQ"})
        lcr = self._lcr(inst)
        with self.assertRaises(ConfigReadbackError) as cm:
            lcr.configure_and_verify("CPD", 2000, 1.0)
        self.assertIn("frequency", str(cm.exception))

    def test_mode_mismatch_raises(self):
        inst = MeterInst(ignore={":FUNC:IMP"})
        lcr = self._lcr(inst)
        with self.assertRaises(ConfigReadbackError) as cm:
            lcr.configure_and_verify("RX", 1000, 1.0)
        self.assertIn("mode", str(cm.exception))

    def test_float_formatting_within_tolerance(self):
        inst = MeterInst()
        lcr = self._lcr(inst)
        # 0.123456 V prints as 1.23456e-01; 12345.67 Hz as 1.23457e+04
        cfg = lcr.configure_and_verify("CPD", 12345.67, 0.123456)
        self.assertAlmostEqual(cfg["frequency"], 12345.7, places=1)


class TestDesync(unittest.TestCase):
    def test_get_config_recovers_from_desynced_reply(self):
        # The mode string desyncs into the numeric field on the first read;
        # after a buffer DRAIN + retry the meter answers cleanly.
        seq = {":FUNC:IMP?": ["CSRS", "CSRS"],
               ":FREQ?": ["CSRS", "1.00000e+03"],   # desynced, then correct
               ":VOLT?": ["1.00000e+00", "1.00000e+00"]}

        class Retry(BK894):
            def __init__(self):
                self.calls = {}
                self.drained = 0
                self.inst = types.SimpleNamespace(timeout=2000, read=self._read)

            def _read(self):
                self.drained += 1
                raise Exception("buffer empty")     # ends the drain loop

            def ask(self, cmd):
                i = self.calls.get(cmd, 0)
                self.calls[cmd] = i + 1
                vals = seq[cmd]
                return vals[min(i, len(vals) - 1)]

        lcr = Retry()
        cfg = lcr.get_config()
        self.assertEqual(cfg["frequency"], 1000.0)
        self.assertEqual(cfg["voltage"], 1.0)
        self.assertEqual(cfg["mode"], "CSRS")
        self.assertGreaterEqual(lcr.drained, 1)     # it drained and retried
        self.assertEqual(lcr.inst.timeout, 2000)    # drain restored timeout

    def test_get_config_degrades_instead_of_raising(self):
        class Stuck(BK894):
            def __init__(self):
                self.inst = types.SimpleNamespace(timeout=2000, read=self._read)

            def _read(self):
                raise Exception("buffer empty")

            def ask(self, cmd):
                return "CSRS"

        cfg = Stuck().get_config()
        self.assertEqual(cfg["mode"], "CSRS")
        self.assertIsNone(cfg["frequency"])
        self.assertIsNone(cfg["voltage"])

    def test_configure_recovers_after_one_desync(self):
        # One stale reply in the buffer: every query is answered with the
        # previous answer until a drain discards it.
        class StaleInst(MeterInst):
            def __init__(self):
                super().__init__()
                self.stale = ["cpd\n"]
                self.drains = 0

            def query(self, cmd):
                fresh = super().query(cmd)
                if cmd == "*IDN?":
                    return fresh
                self.stale.append(fresh)
                return self.stale.pop(0)

            def read(self):
                if len(self.stale) > 0:
                    self.drains += 1
                    return self.stale.pop(0)
                raise Exception("timeout")

        inst = StaleInst()
        lcr = BK894(rm=FakeRM([BK_RESOURCE], inst))
        cfg = lcr.configure_and_verify("RX", 1000, 1.0)
        self.assertEqual(cfg, {"mode": "RX", "frequency": 1000.0,
                               "voltage": 1.0})
        self.assertGreaterEqual(inst.drains, 1)

    def test_persistent_desync_raises_on_configure(self):
        class Stuck(MeterInst):
            def query(self, cmd):
                if cmd == "*IDN?":
                    return super().query(cmd)
                return "cpd\n"

        lcr = BK894(rm=FakeRM([BK_RESOURCE], Stuck()))
        with self.assertRaises(ConfigReadbackError) as cm:
            lcr.configure_and_verify("CPD", 1000, 1.0)
        self.assertIn("no numeric read-back", str(cm.exception))


class TestLocks(unittest.TestCase):
    def test_configure_transactions_never_interleave(self):
        # The fake answers :FREQ? with the frequency most recently written
        # by ANY thread and sleeps to widen the race window. Without the
        # transaction lock, a thread reads back another thread's frequency
        # and configure_and_verify raises.
        class SlowInst(MeterInst):
            def write(self, cmd):
                time.sleep(0.0005)
                super().write(cmd)

            def query(self, cmd):
                time.sleep(0.0005)
                return super().query(cmd)

        lcr = BK894(rm=FakeRM([BK_RESOURCE], SlowInst()))
        errors = []

        def hammer(freq, n=40):
            for _ in range(n):
                try:
                    cfg = lcr.configure_and_verify("CPD", freq, 1.0)
                    if cfg["frequency"] != float(freq):
                        errors.append((freq, cfg))
                except Exception as exc:
                    errors.append((freq, repr(exc)))

        threads = [threading.Thread(target=hammer, args=(f,))
                   for f in (1000, 2000, 3000)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [], f"crossed transactions: {errors[:3]}")

    def test_lock_is_reentrant_lazy_and_per_instrument(self):
        a, b = FakeLCR(), FakeLCR()
        lk = _io_lock(a)
        self.assertIs(lk, _io_lock(a))
        self.assertIsNot(lk, _io_lock(b))
        with lk:
            with lk:
                self.assertTrue(True)


if __name__ == "__main__":
    unittest.main()
