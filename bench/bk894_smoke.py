#!/usr/bin/env python3
"""BK894 bench smoke test for the Pi (run by hand, needs the meter).

Checks the pyvisa-py transport and the :VOLT fix (issue #31) on the rig
meter:

  1. discover the meter by USB VID/PID and print *IDN?
  2. configure CPD, 1 kHz, 0.5 V with the driver's set_* methods
  3. print the raw :FUNC:IMP?, :FREQ? and :VOLT? replies
  4. take 10 :FETC? readings and print them
  5. return the front panel to local and close the session

Exit codes: 0 all good, 1 read-back mismatch, 2 connect failed,
3 a fetch failed.

Stop the dashboard first (sudo systemctl stop halodashboard): only one
process can claim the meter's USB interface at a time.

Run from the repository root:
    python3 bench/bk894_smoke.py
"""
import math
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "RaspberryPiWebserver"))

from instruments import BK894  # noqa: E402

MODE = "CPD"
FREQ_HZ = 1000
VOLT_V = 0.5
N_FETCH = 10
FETCH_SPACING_S = 0.2


def ParseNumber(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def main():
    try:
        lcr = BK894()
    except Exception as exc:
        print(f"CONNECT FAILED: {exc}")
        print("Checks: meter powered and on USB (lsusb | grep 0471:2827), "
              "usbtmc blacklisted (lsmod | grep usbtmc shows nothing), udev "
              "rule installed and meter replugged, dashboard stopped.")
        return 2
    exitCode = 0
    try:
        print(f"Resource : {lcr.resource}")
        print(f"*IDN?    : {lcr.idn}")

        lcr.set_mode(MODE)
        lcr.set_frequency(FREQ_HZ)
        lcr.set_voltage(VOLT_V)
        print(f"Sent     : :FUNC:IMP {MODE}, :FREQ {FREQ_HZ}, :VOLT {VOLT_V}")

        rawMode = lcr.ask(":FUNC:IMP?")
        rawFreq = lcr.ask(":FREQ?")
        rawVolt = lcr.ask(":VOLT?")
        print(f":FUNC:IMP? -> {rawMode!r}")
        print(f":FREQ?     -> {rawFreq!r}")
        print(f":VOLT?     -> {rawVolt!r}")

        freq = ParseNumber(rawFreq)
        volt = ParseNumber(rawVolt)
        mismatches = []
        if rawMode.strip().upper() != MODE:
            mismatches.append(f"mode {rawMode!r} != {MODE}")
        if freq is None or not math.isclose(freq, FREQ_HZ, rel_tol=1e-4):
            mismatches.append(f"frequency {rawFreq!r} != {FREQ_HZ}")
        if volt is None or not math.isclose(volt, VOLT_V, rel_tol=1e-4):
            mismatches.append(f"AC level {rawVolt!r} != {VOLT_V}")
        if mismatches:
            print("READ-BACK MISMATCH: " + "; ".join(mismatches))
            exitCode = 1
        else:
            print("Read-back matches the request.")

        print(f"{N_FETCH} fetches (primary, secondary, status):")
        for i in range(N_FETCH):
            try:
                primary, secondary, status = lcr.measure()
            except Exception as exc:
                print(f"  {i:2d}: FETCH FAILED: {exc}")
                return 3
            print(f"  {i:2d}: {primary!r}, {secondary!r}, {status}")
            time.sleep(FETCH_SPACING_S)
    finally:
        lcr.go_local()
        lcr.close()
    return exitCode


if __name__ == "__main__":
    sys.exit(main())
