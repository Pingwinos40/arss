import tkinter as tk
from tkinter import ttk, messagebox
import pyvisa
import time
import csv
import threading
from datetime import datetime

# Global event to stop recording
StopEvent = threading.Event()

# Mapping SCPI FUNC mode to header columns for CSV
HeaderMapping = {
    "C": ["Timestamp", "Capacitance (F)", "Dissipation Factor", "Status"],
    "L": ["Timestamp", "Inductance (H)", "Dissipation Factor", "Status"],
    "R-X": ["Timestamp", "Resistance (Ohm)", "Reactance (Ohm)", "Status"]
}

def StartRecording():
    mode = mode_var.get()
    interval = float(record_interval_entry.get())
    duration = float(duration_entry.get())
    filename = filename_entry.get().strip()
    if not filename.endswith(".csv"):
        filename += ".csv"

    StopEvent.clear()

    thread = threading.Thread(target=RecordData, args=(mode, interval, duration, filename))
    thread.daemon = True
    thread.start()
    status_label.config(text="Recording...")

def RecordData(mode, interval, duration, filename):
    rm = pyvisa.ResourceManager("@py")
    devices = rm.list_resources()
    target = next((d for d in devices if d.startswith("USB")), None)
    if not target:
        messagebox.showerror("Error", "No USB instrument found.")
        return

    try:
        instr = rm.open_resource(target, timeout=5000)
        instr.write_termination = '\n'
        instr.read_termination = '\n'

        print("IDN:", instr.query("*IDN?"))

        instr.write(f"FUNC {mode}")
        instr.write("TRIG:SOUR IMM")
        time.sleep(0.2)

        start_time = time.time()

        with open(filename, mode="w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(HeaderMapping.get(mode, ["Timestamp", "Val1", "Val2", "Status"]))

            while not StopEvent.is_set() and time.time() - start_time < duration:
                instr.write("*TRG")
                time.sleep(0.1)
                try:
                    result = instr.query("FETC?")
                    values = [v.strip() for v in result.split(",")]
                    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                    writer.writerow([timestamp] + values)
                    f.flush()
                except Exception as e:
                    print("Fetch error:", e)
                time.sleep(interval)
    finally:
        instr.close()
        status_label.config(text="Idle")
        print("Recording finished.")

def StopRecording():
    StopEvent.set()
    status_label.config(text="Stopping...")

# GUI Setup
root = tk.Tk()
root.title("BK Precision 894 LCR Recorder")

tk.Label(root, text="Measurement Mode:").grid(row=0, column=0, sticky="e")
mode_var = tk.StringVar(value="C")
ttk.Combobox(root, textvariable=mode_var, values=["C", "L", "R-X"]).grid(row=0, column=1)

tk.Label(root, text="Interval (s):").grid(row=1, column=0, sticky="e")
record_interval_entry = tk.Entry(root)
record_interval_entry.insert(0, "0.5")
record_interval_entry.grid(row=1, column=1)

tk.Label(root, text="Total Duration (s):").grid(row=2, column=0, sticky="e")
duration_entry = tk.Entry(root)
duration_entry.insert(0, "10")
duration_entry.grid(row=2, column=1)

tk.Label(root, text="CSV Filename:").grid(row=3, column=0, sticky="e")
filename_entry = tk.Entry(root)
filename_entry.insert(0, "lcr_output.csv")
filename_entry.grid(row=3, column=1)

tk.Button(root, text="Start Recording", command=StartRecording).grid(row=4, column=0)
tk.Button(root, text="Stop", command=StopRecording).grid(row=4, column=1)

status_label = tk.Label(root, text="Idle", fg="blue")
status_label.grid(row=5, column=0, columnspan=2)

root.mainloop()
