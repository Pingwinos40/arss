# lcr_controller.py

import pyvisa, time, csv, threading

class LCRRecorder:
    def __init__(self, ip="192.168.0.10"):
        self.ip = ip
        # use the legacy connection string that worked on your laptop setup
        self.resource_string = f"TCPIP0::{self.ip}::inst0::INSTR"

        # force the pure-Python (pyvisa-py) backend
        self.rm = pyvisa.ResourceManager('@py')
        self.instrument = None
        self.thread = None
        self.stop_event = threading.Event()
        self.last_data = None  # holds latest measurement for live updates

    def connect(self):
        # open the VXI-11 resource with the legacy string
        self.instrument = self.rm.open_resource(self.resource_string)
        # prevent automatic clear/reset on open
        self.instrument.clear_on_open = False
        # SCPI commands over LAN expect LF-only
        self.instrument.write_termination = '\n'
        self.instrument.read_termination  = '\n'
        # allow up to 5 seconds per operation
        self.instrument.timeout = 5000

    def configure(self, mode, freq, voltage, speed):
        # apply function, frequency, voltage, and aperture with small pauses
        self.instrument.write(f"FUNC {mode}")
        time.sleep(0.1)
        self.instrument.write(f"FREQ {freq}HZ")
        time.sleep(0.1)
        self.instrument.write(f"VOLT {voltage}V")
        time.sleep(0.1)
        self.instrument.write(f"APER {speed.upper()}")
        time.sleep(0.1)

    def start(self, filename, interval, mode):
        # clear stop flag and spawn background logging thread
        self.stop_event.clear()
        self.thread = threading.Thread(
            target=self._record,
            args=(filename, interval, mode, time.time())
        )
        self.thread.daemon = True
        self.thread.start()

    def _record(self, filename, interval, mode, start_time):
        # open CSV and loop until stop() is called
        with open(filename, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["Timestamp", "Value"])
            while not self.stop_event.is_set():
                self.instrument.write("*TRG")   # trigger measurement
                time.sleep(0.2)                   # allow trigger to settle
                result = self.instrument.query("FETC?")
                timestamp = time.time() - start_time
                self.last_data = (timestamp, result)
                writer.writerow([f"{timestamp:.3f}", result])
                f.flush()
                time.sleep(interval)
        # clean up instrument on exit
        self.instrument.close()

    def stop(self):
        # signal thread loop to end
        self.stop_event.set()

    def get_last_data(self):
        # return latest measurement tuple (timestamp, value)
        return self.last_data
