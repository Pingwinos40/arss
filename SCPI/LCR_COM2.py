import tkinter as tk
from tkinter import ttk, messagebox
import pyvisa
import time
import csv
import threading

StopEvent = threading.Event()

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

    MeasurementSpeed = speed_var.get()
    
    try:
        Duration = float(duration_entry.get())
        if Duration <= 0:
            raise ValueError
    except ValueError:
        messagebox.showerror("Input Error", "Duration must be a positive number (in seconds).")
        return
    
    Filename = filename_entry.get().strip()
    if Filename == "":
        messagebox.showerror("Input Error", "Please provide a CSV filename.")
        return
    if not Filename.lower().endswith(".csv"):
        Filename += ".csv"

    ModeCommand = f"FUNC {MeasurementMode}"
    FreqCommand = f"FREQ {SamplingFreq}HZ"
    LevelCommand = f"VOLT {SampleLevel} V"
    SpeedCommand = f"APER {MeasurementSpeed.upper()}"
    interval = 0.01  # Fixed interval

    rm = pyvisa.ResourceManager("@py")
    try:
        devs = rm.list_resources()
        target = next((d for d in devs if d.startswith("USB")), None)
        if not target:
            raise Exception("No USB VISA device found.")
        instrument = rm.open_resource(target)
    except Exception as e:
        messagebox.showerror("Connection Error", f"Error connecting to instrument: {e}")
        return

    try:
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

    startTime = time.time()
    StopEvent.clear()
    thread = threading.Thread(target=RecordMeasurements, args=(instrument, Filename, interval, MeasurementMode, startTime, Duration))
    thread.daemon = True
    thread.start()
    status_label.config(text="Recording started...")

def RecordMeasurements(instrument, filename, interval, measurement_mode, start_time, duration):
    header = HeaderMapping.get(measurement_mode, ["Timestamp", "Measurement Value"])
    expected_values = len(header) - 1

    with open(filename, mode="w", newline="") as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(header)
        
        while not StopEvent.is_set():
            elapsed = time.time() - start_time
            if elapsed > duration:
                break
            try:
                instrument.write("*TRG")
                time.sleep(0.1)
                data = instrument.query("FETC?")
                values = [val.strip() for val in data.strip().split(',')]
                timestamp = "{:.3f}".format(elapsed)
                row = [timestamp] + values
                writer.writerow(row)
                csvfile.flush()
                print(f"{timestamp}: {values}")
            except Exception as e:
                print("Error during measurement:", e)
            time.sleep(interval)
    instrument.close()
    status_label.config(text="Recording stopped.")

def StopRecording():
    StopEvent.set()
    status_label.config(text="Stopping recording...")

# GUI
root = tk.Tk()
root.title("BK Precision 894 Measurement Recorder")

tk.Label(root, text="Measurement Mode:").grid(row=0, column=0, sticky="w", padx=5, pady=5)
mode_var = tk.StringVar(value="R-X")
ttk.Combobox(root, textvariable=mode_var, values=list(HeaderMapping.keys()), state="readonly").grid(row=0, column=1, padx=5, pady=5)

tk.Label(root, text="Sampling Frequency (Hz) [20 - 500000]:").grid(row=1, column=0, sticky="w", padx=5, pady=5)
sampling_freq_entry = tk.Entry(root)
sampling_freq_entry.insert(0, "1000")
sampling_freq_entry.grid(row=1, column=1, padx=5, pady=5)

tk.Label(root, text="AC Test Signal Voltage (V) [0.005 - 2]:").grid(row=2, column=0, sticky="w", padx=5, pady=5)
sample_level_entry = tk.Entry(root)
sample_level_entry.insert(0, "1")
sample_level_entry.grid(row=2, column=1, padx=5, pady=5)

tk.Label(root, text="Measurement Speed:").grid(row=3, column=0, sticky="w", padx=5, pady=5)
speed_var = tk.StringVar(value="med")
ttk.Combobox(root, textvariable=speed_var, values=["low", "med", "fast"], state="readonly").grid(row=3, column=1, padx=5, pady=5)

tk.Label(root, text="Recording Interval (s):").grid(row=4, column=0, sticky="w", padx=5, pady=5)
tk.Entry(root, state="readonly", justify="center", disabledforeground="black", textvariable=tk.StringVar(value="0.01")).grid(row=4, column=1, padx=5, pady=5)

tk.Label(root, text="Recording Duration (s):").grid(row=5, column=0, sticky="w", padx=5, pady=5)
duration_entry = tk.Entry(root)
duration_entry.insert(0, "5")
duration_entry.grid(row=5, column=1, padx=5, pady=5)

tk.Label(root, text="CSV Filename:").grid(row=6, column=0, sticky="w", padx=5, pady=5)
filename_entry = tk.Entry(root)
filename_entry.insert(0, "measurements.csv")
filename_entry.grid(row=6, column=1, padx=5, pady=5)

tk.Button(root, text="Start Recording", command=StartRecording).grid(row=7, column=0, padx=5, pady=10)
tk.Button(root, text="Stop Recording", command=StopRecording).grid(row=7, column=1, padx=5, pady=10)

status_label = tk.Label(root, text="Idle", fg="blue")
status_label.grid(row=8, column=0, columnspan=2, padx=5, pady=5)

root.mainloop()

