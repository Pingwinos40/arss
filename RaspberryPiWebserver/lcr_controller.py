# lcr_controller.py
import pyvisa, time, csv, threading

class LCRRecorder:
    def __init__(self, ip="192.168.0.10"):
        self.ip = ip
        self.instrument = None
        self.thread = None
        self.stop_event = threading.Event()
        self.last_data = None  # For live status

    def connect(self):
        rm = pyvisa.ResourceManager()
        self.instrument = rm.open_resource(f"TCPIP0::{self.ip}::inst0::INSTR")

    def configure(self, mode, freq, voltage, speed):
        self.instrument.write(f"FUNC {mode}")
        self.instrument.write(f"FREQ {freq}HZ")
        self.instrument.write(f"VOLT {voltage}V")
        self.instrument.write(f"APER {speed.upper()}")

    def start(self, filename, interval, mode):
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._record, args=(filename, interval, mode, time.time()))
        self.thread.daemon = True
        self.thread.start()

    def _record(self, filename, interval, mode, start_time):
        with open(filename, "w", newline="") as f:
            writer = csv.writer(f)
            header = ["Timestamp", "Value"]  # Update based on mode if needed
            writer.writerow(header)
            while not self.stop_event.is_set():
                self.instrument.write("*TRG")
                time.sleep(0.2)
                result = self.instrument.query("FETC?")
                timestamp = time.time() - start_time
                self.last_data = (timestamp, result)
                writer.writerow([f"{timestamp:.3f}", result])
                f.flush()
                time.sleep(interval)
        self.instrument.close()

    def stop(self):
        self.stop_event.set()

    def get_last_data(self):
        return self.last_data
