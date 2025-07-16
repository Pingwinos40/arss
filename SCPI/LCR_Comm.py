import tkinter as tk
from tkinter import ttk, messagebox
import pyvisa
import time
import csv
import threading

# Global event to signal recording stop
StopEvent = threading.Event()

# Mapping from measurement mode to expected header (first column is always Timestamp)
HeaderMapping = {
    "Z-deg": ["Timestamp", "Impedance (Ohm)", "Phase Angle (deg)"],
    "R-X":  ["Timestamp", "Resistance (Ohm)", "Reactance (Ohm)"],
    "C":    ["Timestamp", "Capacitance (F)"],
    "L":    ["Timestamp", "Inductance (H)"],
    "Q":    ["Timestamp", "Quality Factor"],
    "D":    ["Timestamp", "Dissipation Factor"],
    "DCR":  ["Timestamp", "DC Resistance (Ohm)"]
}

def StartRecording():
    # Retrieve settings from GUI
    MeasurementMode = mode_var.get()
    
    try:
        SamplingFreq = float(sampling_freq_entry.get())
        if not (20 <= SamplingFreq <= 500000):
            raise ValueError
    except ValueError:
        messagebox.showerror("Input Error", "Sampling frequency must be between 20 and 500000 Hz.")
        return

    try:
        SampleLevel = float(sample_level_entry.get())
        if not (0.005 <= SampleLevel <= 2):
            raise ValueError
    except ValueError:
        messagebox.showerror("Input Error", "AC test signal voltage must be between 0.005 and 2 V.")
        return

    MeasurementSpeed = speed_var.get()  # "low", "med", "fast"
    
    try:
        RecordInterval = float(record_interval_entry.get())
        if RecordInterval <= 0:
            raise ValueError
    except ValueError:
        messagebox.showerror("Input Error", "Recording interval must be a positive number (in seconds).")
        return
    
    Filename = filename_entry.get().strip()
    if Filename == "":
        messagebox.showerror("Input Error", "Please provide a CSV filename.")
        return
    # Append .csv if not already provided
    if not Filename.lower().endswith(".csv"):
        Filename = Filename + ".csv"

    # Build SCPI commands (modify these as per your manual's specifications)
    ModeCommand = f"FUNC {MeasurementMode}"
    FreqCommand = f"FREQ {SamplingFreq}HZ"
    LevelCommand = f"VOLT {SampleLevel} V"
    SpeedCommand = f"APER {MeasurementSpeed.upper()}"  # expecting "LOW", "MED", "FAST"

    # Open VISA connection using default (pyvisa-py) backend
    rm = pyvisa.ResourceManager()
    ResourceStr = "TCPIP0::192.168.0.10::inst0::INSTR"
    try:
        instrument = rm.open_resource(ResourceStr)
    except Exception as e:
        messagebox.showerror("Connection Error", f"Error connecting to instrument: {e}")
        return

    try:
        # Send configuration commands to the instrument
        instrument.write(ModeCommand)
        time.sleep(0.1)
        instrument.write(FreqCommand)
        time.sleep(0.1)
        instrument.write(LevelCommand)
        time.sleep(0.1)
        instrument.write(SpeedCommand)
        time.sleep(0.1)
    except Exception as e:
        messagebox.showerror("Configuration Error", f"Error sending configuration commands: {e}")
        instrument.close()
        return

    # Record the start time for relative timestamps
    startTime = time.time()

    # Clear the stop event and start the measurement thread.
    StopEvent.clear()
    # Pass the MeasurementMode so we know what header and parsing to use.
    thread = threading.Thread(target=RecordMeasurements, args=(instrument, Filename, RecordInterval, MeasurementMode, startTime))
    thread.daemon = True
    thread.start()
    status_label.config(text="Recording started...")
    print("Recording started...")

def RecordMeasurements(instrument, filename, interval, measurement_mode, start_time):
    # Determine header based on measurement mode; default to one measurement value if not found.
    header = HeaderMapping.get(measurement_mode, ["Timestamp", "Measurement Value", "N/A"])
    expected_values = len(header) - 1  # excluding timestamp

    # Open CSV file for writing; overwrites any existing file.
    with open(filename, mode="w", newline="") as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(header)  # Write header row
        
        while not StopEvent.is_set():
            try:
                # Trigger measurement and wait briefly
                instrument.write("*TRG")
                time.sleep(0.2)
                data = instrument.query("FETC?")
                # Split returned measurement string by commas
                values = [val.strip() for val in data.strip().split(',')]
                # Uncomment below if you want to warn about mismatched number of values:
                # if len(values) != expected_values:
                #     print(f"Warning: Expected {expected_values} values, got {len(values)}. Data: {values}")
                # Compute relative timestamp (in seconds) with three decimals
                relativeTimestamp = "{:.3f}".format(time.time() - start_time)
                row = [relativeTimestamp] + values
                writer.writerow(row)
                csvfile.flush()
                print(f"{relativeTimestamp}: {values}")
            except Exception as e:
                print("Error during measurement:", e)
            time.sleep(interval)
    instrument.close()
    status_label.config(text="Recording stopped.")

    print("WriteTerm:", instrument.write_termination)
    print("ReadTerm: ", instrument.read_termination)


def StopRecording():
    StopEvent.set()
    status_label.config(text="Stopping recording...")
    print("Recording stopped.")

# Create tkinter GUI
root = tk.Tk()
root.title("BK Precision 894 Measurement Recorder")

# Measurement Mode Dropdown
tk.Label(root, text="Measurement Mode:").grid(row=0, column=0, sticky="w", padx=5, pady=5)
mode_var = tk.StringVar(value="R-X")
mode_options = ["R-X", "Z-deg", "C", "L", "Q", "D", "DCR"]
mode_dropdown = ttk.Combobox(root, textvariable=mode_var, values=mode_options, state="readonly")
mode_dropdown.grid(row=0, column=1, padx=5, pady=5)

# Sampling Frequency Entry
tk.Label(root, text="Sampling Frequency (Hz) [20 - 500000]:").grid(row=1, column=0, sticky="w", padx=5, pady=5)
sampling_freq_entry = tk.Entry(root)
sampling_freq_entry.insert(0, "1000")
sampling_freq_entry.grid(row=1, column=1, padx=5, pady=5)

# AC Test Signal Voltage Entry
tk.Label(root, text="AC Test Signal Voltage (V) [0.005 - 2]:").grid(row=2, column=0, sticky="w", padx=5, pady=5)
sample_level_entry = tk.Entry(root)
sample_level_entry.insert(0, "1")
sample_level_entry.grid(row=2, column=1, padx=5, pady=5)

# Measurement Speed Dropdown
tk.Label(root, text="Measurement Speed:").grid(row=3, column=0, sticky="w", padx=5, pady=5)
speed_var = tk.StringVar(value="med")
speed_options = ["low", "med", "fast"]
speed_dropdown = ttk.Combobox(root, textvariable=speed_var, values=speed_options, state="readonly")
speed_dropdown.grid(row=3, column=1, padx=5, pady=5)

# Recording Interval Entry (seconds between measurements)
tk.Label(root, text="Recording Interval (s):").grid(row=4, column=0, sticky="w", padx=5, pady=5)
record_interval_entry = tk.Entry(root)
record_interval_entry.insert(0, "0.01")
record_interval_entry.grid(row=4, column=1, padx=5, pady=5)

# CSV Filename Entry
tk.Label(root, text="CSV Filename:").grid(row=5, column=0, sticky="w", padx=5, pady=5)
filename_entry = tk.Entry(root)
filename_entry.insert(0, "measurements.csv")
filename_entry.grid(row=5, column=1, padx=5, pady=5)

# Start and Stop Buttons
start_button = tk.Button(root, text="Start Recording", command=StartRecording)
start_button.grid(row=6, column=0, padx=5, pady=10)

stop_button = tk.Button(root, text="Stop Recording", command=StopRecording)
stop_button.grid(row=6, column=1, padx=5, pady=10)

# Status Label
status_label = tk.Label(root, text="Idle", fg="blue")
status_label.grid(row=7, column=0, columnspan=2, padx=5, pady=5)

root.mainloop()