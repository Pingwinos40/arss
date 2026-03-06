#!/usr/bin/env python3
"""
LCR Meter recorder for HALO dashboard integration.

Uses the BK894 driver from instruments.py (raw usbtmc) instead of pyvisa.
Runs a background thread that logs measurements to CSV and exposes the
latest reading for the Flask /daq/* endpoints.
"""

import time
import csv
import threading
import glob
import os
from instruments import BK894


class LCRRecorder:
    """Manages BK894 connection, configuration, and background CSV recording."""

    # Default device path; override at construction or via connect()
    DEFAULT_DEVICE_HINTS = ["/dev/usbtmc*"]

    def __init__(self, device=None):
        self.device = device
        self.lcr = None          # BK894 instance (None until connect())
        self.thread = None
        self.stop_event = threading.Event()
        self.last_data = None    # (timestamp, primary, secondary, status)
        self.mode = "RX"         # default measurement mode
        self.lock = threading.Lock()

    # ------------------------------------------------------------------
    # Connection helpers
    # ------------------------------------------------------------------
    def _find_device(self):
        """Auto-detect first available usbtmc device."""
        for pattern in self.DEFAULT_DEVICE_HINTS:
            matches = sorted(glob.glob(pattern))
            if matches:
                return matches[0]
        return None

    def connect(self, device=None):
        """
        Open the BK894 over usbtmc.
        If device is None, tries self.device then auto-detect.
        """
        target = device or self.device or self._find_device()
        if not target:
            raise FileNotFoundError(
                "No usbtmc device found. Is the BK894 powered on and connected via USB?"
            )
        self.lcr = BK894(target)
        self.device = target
        return self.lcr.idn

    def disconnect(self):
        """Close the instrument handle."""
        with self.lock:
            if self.lcr:
                try:
                    self.lcr.close()
                except Exception:
                    pass
                self.lcr = None

    @property
    def connected(self):
        return self.lcr is not None

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------
    def configure(self, mode="RX", freq=1000, voltage=1.0):
        """
        Apply measurement settings to the BK894.
        mode:    one of BK894.MODES keys (e.g. 'RX', 'CPD', 'ZTD')
        freq:    test frequency in Hz (100 -- 200000)
        voltage: AC test signal in V (0.01 -- 2.0)
        """
        if not self.lcr:
            raise RuntimeError("LCR meter not connected. Call connect() first.")
        self.lcr.set_mode(mode)
        time.sleep(0.1)
        self.lcr.set_frequency(freq)
        time.sleep(0.1)
        self.lcr.set_voltage(voltage)
        time.sleep(0.1)
        self.mode = mode.upper()

    # ------------------------------------------------------------------
    # Single measurement (for non-recording use)
    # ------------------------------------------------------------------
    def measure_once(self):
        """
        Take a single measurement.
        Returns (primary, secondary, status).
        """
        if not self.lcr:
            raise RuntimeError("LCR meter not connected.")
        with self.lock:
            return self.lcr.measure()

    # ------------------------------------------------------------------
    # Background recording
    # ------------------------------------------------------------------
    def start(self, filename="measurements.csv", interval=0.5,
              mode="RX", freq=1000, voltage=1.0):
        """
        Connect (if needed), configure, and start background CSV recording.

        Parameters
        ----------
        filename : str   CSV output path.
        interval : float Seconds between measurements.
        mode     : str   BK894 measurement mode.
        freq     : float Test frequency in Hz.
        voltage  : float AC test signal in V.
        """
        # Connect if not already
        if not self.lcr:
            self.connect()

        # Apply configuration
        self.configure(mode=mode, freq=freq, voltage=voltage)

        # Clear stop flag, reset last_data
        self.stop_event.clear()
        self.last_data = None

        self.thread = threading.Thread(
            target=self._record_loop,
            args=(filename, interval, time.time()),
            daemon=True,
        )
        self.thread.start()

    def _record_loop(self, filename, interval, t0):
        """Background loop: trigger, fetch, write CSV, repeat."""
        # Build header from mode
        headers = BK894.HEADERS.get(self.mode, ["Primary", "Secondary"])
        csv_header = ["Timestamp (s)"] + headers + ["Status"]

        with open(filename, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(csv_header)

            while not self.stop_event.is_set():
                try:
                    with self.lock:
                        primary, secondary, status = self.lcr.measure()
                    ts = time.time() - t0
                    self.last_data = (ts, primary, secondary, status)
                    writer.writerow([f"{ts:.3f}", primary, secondary, status])
                    f.flush()
                except Exception as e:
                    # Log but do not crash the thread
                    print(f"[LCRRecorder] measurement error: {e}")

                self.stop_event.wait(timeout=interval)

    def stop(self):
        """Signal the recording thread to stop."""
        self.stop_event.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=3.0)

    def get_last_data(self):
        """
        Return the most recent measurement tuple for live dashboard updates.
        Returns (timestamp, primary, secondary, status) or None.
        """
        return self.last_data
