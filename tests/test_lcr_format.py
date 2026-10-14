#!/usr/bin/env python3
"""Headless tests for the LCR readout formatter and display-safe mapping.

Ported from Digital_Multitool commit f22527f (tests/test_lcr_format.py),
plus tests for as_number / display_reading (issue #40). Non-ASCII unit
symbols are written as escapes so this file stays ASCII.

Run from the repository root: python -m unittest discover -s tests -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "RaspberryPiWebserver"))

from lcr_format import (DEGREE, MICRO, MODE_UNITS, OHM, THETA,  # noqa: E402
                        as_number, display_reading, format_measurement,
                        format_si, json_safe_raw)


class TestFormatSi(unittest.TestCase):
    def test_si_scaling_capacitance(self):
        self.assertEqual(format_si(3.3e-9, "F"), "3.3 nF")
        self.assertEqual(format_si(4.7e-4, "F"), f"470 {MICRO}F")
        self.assertEqual(format_si(1.234e-12, "F"), "1.234 pF")
        # below the smallest prefix, keep it rather than inventing femto
        self.assertEqual(format_si(5e-14, "F"), "0.05 pF")

    def test_si_scaling_inductance_and_ohms(self):
        self.assertEqual(format_si(2.2e-6, "H"), f"2.2 {MICRO}H")
        self.assertEqual(format_si(0.15, "H"), "150 mH")
        self.assertEqual(format_si(4700.0, OHM), f"4.7 k{OHM}")
        self.assertEqual(format_si(2.2e6, OHM), f"2.2 M{OHM}")
        self.assertEqual(format_si(0.05, OHM), f"50 m{OHM}")

    def test_si_edge_cases(self):
        self.assertEqual(format_si(None, "F"), "--")
        self.assertEqual(format_si(0, "F"), "0 F")
        self.assertEqual(format_si(-3.3e-9, "F"), "-3.3 nF")
        self.assertEqual(format_si(0.00123, ""), "0.00123")
        self.assertEqual(format_si(57.3, DEGREE), f"57.3 {DEGREE}")
        self.assertEqual(format_si(1.0, "rad"), "1 rad")


class TestFormatMeasurement(unittest.TestCase):
    def test_measurement_pairs(self):
        self.assertEqual(format_measurement("CPD", 3.3e-9, 0.0012),
                         ("3.3 nF", "D: 0.0012"))
        self.assertEqual(format_measurement("LSRS", 2.2e-6, 0.5),
                         (f"2.2 {MICRO}H", f"Rs: 500 m{OHM}"))
        self.assertEqual(format_measurement("ZTD", 1.5e3, -89.9),
                         (f"1.5 k{OHM}", f"{THETA}: -89.9 {DEGREE}"))
        self.assertEqual(format_measurement("RX", 50.0, -12.5),
                         (f"50 {OHM}", f"X: -12.5 {OHM}"))

    def test_measurement_robustness(self):
        self.assertEqual(format_measurement("CPD", 3.3e-9, None),
                         ("3.3 nF", "D: --"))
        self.assertEqual(format_measurement("??", 1.0, 2.0), ("1", "2"))
        self.assertEqual(format_measurement(None, None, None), ("--", "--"))
        p, s = format_measurement("cpd", 1e-9, 0.1)
        self.assertEqual(p, "1 nF")
        self.assertTrue(s.startswith("D:"))

    def test_mode_table_matches_driver(self):
        from instruments import BK894
        self.assertEqual(set(BK894.MODES), set(MODE_UNITS))


class TestDisplaySafe(unittest.TestCase):
    def test_as_number_maps_no_number_to_none(self):
        for bad in (None, True, False, float("nan"), float("inf"),
                    float("-inf"), 9.9e37, -9.9e37, 9.91e37, 1e38, "abc", ""):
            self.assertIsNone(as_number(bad), repr(bad))
        self.assertEqual(as_number(1e-9), 1e-9)
        self.assertEqual(as_number("-3.5"), -3.5)
        self.assertEqual(as_number(0), 0.0)
        self.assertEqual(as_number(9.8e37), 9.8e37)

    def test_display_reading_good_value(self):
        d = display_reading("cpd", 3.3e-9, 0.0012, 0)
        self.assertEqual(d["mode"], "CPD")
        self.assertEqual(d["primary"], 3.3e-9)
        self.assertEqual(d["secondary"], 0.0012)
        self.assertEqual(d["primary_unit"], "F")
        self.assertEqual(d["secondary_name"], "D")
        self.assertEqual(d["secondary_unit"], "")
        self.assertEqual(d["primary_text"], "3.3 nF")
        self.assertEqual(d["secondary_text"], "D: 0.0012")
        self.assertEqual(d["status_text"], "OK")
        self.assertFalse(d["overload"])

    def test_display_reading_overload(self):
        d = display_reading("RX", 9.9e37, float("nan"), 2)
        self.assertIsNone(d["primary"])
        self.assertIsNone(d["secondary"])
        self.assertEqual(d["primary_text"], "OVL")
        self.assertEqual(d["secondary_text"], "X: OVL")
        self.assertEqual(d["primary_unit"], OHM)
        self.assertEqual(d["status_text"], "status 2")
        self.assertTrue(d["overload"])

    def test_display_reading_empty(self):
        d = display_reading(None, None, None, None)
        self.assertIsNone(d["primary"])
        self.assertEqual(d["primary_text"], "--")
        self.assertEqual(d["status_text"], "--")
        self.assertFalse(d["overload"])

    def test_json_safe_raw(self):
        self.assertEqual(json_safe_raw(9.9e37), 9.9e37)
        self.assertEqual(json_safe_raw(1.5), 1.5)
        self.assertEqual(json_safe_raw(float("nan")), "nan")
        self.assertEqual(json_safe_raw(float("inf")), "inf")
        self.assertIsNone(json_safe_raw(None))


if __name__ == "__main__":
    unittest.main()
