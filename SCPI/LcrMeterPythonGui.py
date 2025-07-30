import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import pyvisa
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
import csv
from datetime import datetime
import time

class LCRMeterApp:
    def __init__(self, root):
        self.root = root
        self.root.title("B&K Precision LCR Meter Frequency Sweep")
        self.root.geometry("900x700")
        
        # Initialize variables
        self.rm = None
        self.instrument = None
        self.connected = False
        self.measurement_data = []
        
        # Create GUI
        self.create_widgets()
        
        # Initialize VISA
        self.initialize_visa()

    def initialize_visa(self):
        try:
            # Use pyvisa-py as backend if NI VISA is not available
            self.rm = pyvisa.ResourceManager('@py')  # Use pyvisa-py backend
            self.status_var.set(f"VISA Backend: {self.rm.visalib}")
            self.refresh_resources()
        except Exception as e:
            messagebox.showerror("VISA Error", f"Could not initialize VISA: {str(e)}\n"
                                            "Make sure NI-VISA or pyvisa-py is installed.")
            self.status_var.set("VISA initialization failed")

    def create_widgets(self):
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        main_frame.columnconfigure(1, weight=1)

        # Connection Frame
        conn_frame = ttk.LabelFrame(main_frame, text="Instrument Connection", padding="10")
        conn_frame.grid(row=0, column=0, columnspan=2, sticky=(tk.W, tk.E), pady=(0, 10))

        ttk.Label(conn_frame, text="Resource:").grid(row=0, column=0, sticky=tk.W)
        self.resource_var = tk.StringVar()
        self.resource_combo = ttk.Combobox(conn_frame, textvariable=self.resource_var, width=50)
        self.resource_combo.grid(row=0, column=1, padx=(10, 5))

        self.refresh_btn = ttk.Button(conn_frame, text="Refresh", command=self.refresh_resources)
        self.refresh_btn.grid(row=0, column=2, padx=(5, 0))

        self.connect_btn = ttk.Button(conn_frame, text="Connect", command=self.connect_instrument)
        self.connect_btn.grid(row=0, column=3, padx=(10, 0))

        self.status_label = ttk.Label(conn_frame, text="Not Connected", foreground="red")
        self.status_label.grid(row=0, column=4, padx=(10, 0))

        # Manual Entry
        ttk.Label(conn_frame, text="Or enter manually:").grid(row=1, column=0, sticky=tk.W, pady=(5,0))
        self.manual_resource = tk.StringVar()
        ttk.Entry(conn_frame, textvariable=self.manual_resource, width=50).grid(row=1, column=1, padx=(10,5), pady=(5,0), columnspan=2)
        ttk.Button(conn_frame, text="Use Manual", command=self.use_manual_resource).grid(row=1, column=3, padx=(10,0), pady=(5,0))

        # Parameters
        param_frame = ttk.LabelFrame(main_frame, text="Frequency Sweep Parameters", padding="10")
        param_frame.grid(row=1, column=0, columnspan=2, sticky=(tk.W, tk.E), pady=(0, 10))

        ttk.Label(param_frame, text="Start Frequency (Hz):").grid(row=0, column=0, sticky=tk.W)
        self.start_freq = tk.DoubleVar(value=100.0)
        ttk.Entry(param_frame, textvariable=self.start_freq, width=15).grid(row=0, column=1, padx=(10, 5))

        ttk.Label(param_frame, text="Stop Frequency (Hz):").grid(row=1, column=0, sticky=tk.W)
        self.stop_freq = tk.DoubleVar(value=100000.0)
        ttk.Entry(param_frame, textvariable=self.stop_freq, width=15).grid(row=1, column=1, padx=(10, 5))

        ttk.Label(param_frame, text="Number of Points:").grid(row=2, column=0, sticky=tk.W)
        self.num_points = tk.IntVar(value=50)
        ttk.Entry(param_frame, textvariable=self.num_points, width=15).grid(row=2, column=1, padx=(10, 5))

        ttk.Label(param_frame, text="Sweep Type:").grid(row=3, column=0, sticky=tk.W)
        self.sweep_type = tk.StringVar(value="Logarithmic")
        ttk.Radiobutton(param_frame, text="Logarithmic", variable=self.sweep_type, value="Logarithmic").grid(row=3, column=1, sticky=tk.W)
        ttk.Radiobutton(param_frame, text="Linear", variable=self.sweep_type, value="Linear").grid(row=3, column=2, sticky=tk.W, padx=(20, 0))

        ttk.Label(param_frame, text="Measurement Mode:").grid(row=4, column=0, sticky=tk.W)
        self.measurement_mode = tk.StringVar(value="ZTD")
        mode_frame = ttk.Frame(param_frame)
        mode_frame.grid(row=4, column=1, columnspan=2, sticky=tk.W)
        ttk.Radiobutton(mode_frame, text="ZTD (Z, θ°)", variable=self.measurement_mode, value="ZTD").pack(side=tk.LEFT)
        ttk.Radiobutton(mode_frame, text="ZTH (Z, θ°)", variable=self.measurement_mode, value="ZTH").pack(side=tk.LEFT, padx=(10, 0))

        ttk.Label(param_frame, text="Test Signal Level (V):").grid(row=5, column=0, sticky=tk.W)
        self.test_level = tk.DoubleVar(value=1.0)
        ttk.Combobox(param_frame, textvariable=self.test_level, values=[0.1, 0.3, 1.0, 1.5], width=10).grid(row=5, column=1, padx=(10, 5), sticky=tk.W)

        ttk.Label(param_frame, text="Measurement Speed:").grid(row=6, column=0, sticky=tk.W)
        self.measurement_speed = tk.StringVar(value="Medium")
        ttk.Combobox(param_frame, textvariable=self.measurement_speed, values=["Fast", "Medium", "Slow"], width=10).grid(row=6, column=1, padx=(10, 5), sticky=tk.W)

        # Control Buttons
        button_frame = ttk.Frame(main_frame)
        button_frame.grid(row=2, column=0, columnspan=2, pady=(0, 10))
        self.sweep_btn = ttk.Button(button_frame, text="Start Sweep", command=self.start_sweep, state="disabled")
        self.sweep_btn.pack(side=tk.LEFT, padx=(0, 10))
        self.save_btn = ttk.Button(button_frame, text="Save Data", command=self.save_data, state="disabled")
        self.save_btn.pack(side=tk.LEFT, padx=(0, 10))
        self.clear_btn = ttk.Button(button_frame, text="Clear Plot", command=self.clear_plot)
        self.clear_btn.pack(side=tk.LEFT)

        # Plot Frame
        result_frame = ttk.LabelFrame(main_frame, text="Results", padding="10")
        result_frame.grid(row=3, column=0, columnspan=2, sticky=(tk.W, tk.E, tk.N, tk.S))
        main_frame.rowconfigure(3, weight=1)

        self.fig, (self.ax1, self.ax2) = plt.subplots(2, 1, figsize=(8, 6))
        self.fig.tight_layout(pad=3.0)
        self.canvas = FigureCanvasTkAgg(self.fig, result_frame)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

        # Status Bar
        self.status_var = tk.StringVar(value="Ready")
        status_bar = ttk.Label(main_frame, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W)
        status_bar.grid(row=4, column=0, columnspan=2, sticky=(tk.W, tk.E), pady=(5, 0))

    def use_manual_resource(self):
        manual = self.manual_resource.get().strip()
        if manual:
            self.resource_var.set(manual)
            self.status_var.set(f"Manual resource set: {manual}")
        else:
            messagebox.showwarning("Input Error", "Please enter a valid resource string.")

    def refresh_resources(self):
        if not self.rm:
            self.status_var.set("VISA not initialized")
            return

        try:
            resources = self.rm.list_resources()
            self.resource_combo['values'] = resources
            if resources:
                self.resource_var.set(resources[0])
                self.status_var.set(f"Found {len(resources)} resources")
            else:
                self.resource_var.set("")
                self.status_var.set("No VISA resources found")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to list resources: {e}")
            self.status_var.set("Resource listing failed")

    def connect_instrument(self):
        try:
            resource_addr = self.resource_var.get().strip()
            if not resource_addr:
                messagebox.showwarning("Warning", "No resource address selected")
                return

            # Open connection
            self.instrument = self.rm.open_resource(resource_addr)
            
            # Critical: Set proper VISA I/O settings
            self.instrument.timeout = 10000  # 10 seconds
            self.instrument.write_termination = '\n'
            self.instrument.read_termination = '\n'

            # Reset and identify
            self.instrument.write("*RST")
            time.sleep(1)
            self.instrument.write("*CLS")
            time.sleep(0.1)

            idn = self.instrument.query("*IDN?").strip()
            self.status_label.config(text=f"Connected: {idn}", foreground="green")
            self.connect_btn.config(text="Disconnect", command=self.disconnect_instrument)
            self.sweep_btn.config(state="normal")
            self.connected = True

            # Configure
            self.configure_instrument()
            self.status_var.set(f"Connected to {idn}")

        except Exception as e:
            messagebox.showerror("Connection Error", f"Failed to connect: {str(e)}\n\n"
                                 "Common fixes:\n"
                                 "1. Ensure USB cable is secure\n"
                                 "2. Device not used by another program\n"
                                 "3. Try manual address: USB0::0x0471::0x2827::4à::INSTR")
            self.status_var.set("Connection failed")

    def disconnect_instrument(self):
        try:
            if self.instrument:
                self.instrument.close()
            self.instrument = None
            self.status_label.config(text="Not Connected", foreground="red")
            self.connect_btn.config(text="Connect", command=self.connect_instrument)
            self.sweep_btn.config(state="disabled")
            self.connected = False
            self.status_var.set("Disconnected")
        except Exception as e:
            messagebox.showerror("Error", f"Error disconnecting: {str(e)}")

    def configure_instrument(self):
        try:
            mode = self.measurement_mode.get()
            self.instrument.write(f"FUNC:{mode}")

            speed_map = {"Fast": "FAST", "Medium": "MED", "Slow": "SLOW"}
            self.instrument.write(f"APER {speed_map[self.measurement_speed.get()]}")

            self.instrument.write(f"VOLT {self.test_level.get()}")

            self.instrument.write("AVER ON")
            self.instrument.write("AVER:COUN 4")

        except Exception as e:
            messagebox.showerror("Config Error", f"Failed to configure instrument: {str(e)}")

    def start_sweep(self):
        if not self.connected:
            messagebox.showwarning("Warning", "Not connected to instrument")
            return

        try:
            start_freq = self.start_freq.get()
            stop_freq = self.stop_freq.get()
            num_points = self.num_points.get()
            sweep_type = self.sweep_type.get()

            if start_freq <= 0 or stop_freq <= 0:
                messagebox.showerror("Error", "Frequencies must be positive")
                return
            if start_freq >= stop_freq:
                messagebox.showerror("Error", "Start frequency must be < stop frequency")
                return
            if num_points < 2 or num_points > 500:
                messagebox.showerror("Error", "Points must be 2–500")
                return

            frequencies = (np.logspace(np.log10(start_freq), np.log10(stop_freq), num_points)
                           if sweep_type == "Logarithmic"
                           else np.linspace(start_freq, stop_freq, num_points))

            self.measurement_data = []
            self.status_var.set("Running sweep...")

            progress_window = tk.Toplevel(self.root)
            progress_window.title("Progress")
            progress_window.geometry("300x100")
            progress_label = ttk.Label(progress_window, text="Measuring...")
            progress_label.pack(pady=10)
            progress_bar = ttk.Progressbar(progress_window, mode='determinate', length=200)
            progress_bar.pack(pady=10)
            progress_bar['maximum'] = num_points

            impedances, phases, valid_freqs = [], [], []

            for i, freq in enumerate(frequencies):
                try:
                    self.instrument.write(f"FREQ {freq:.6f}")
                    time.sleep(0.2)  # Allow settling
                    data = self.instrument.query("FETCH?").strip()
                    vals = [float(x) for x in data.split(',')]

                    z = vals[0]
                    theta = vals[1]

                    impedances.append(z)
                    phases.append(theta)
                    valid_freqs.append(freq)
                    self.measurement_data.append({'frequency': freq, 'impedance': z, 'phase': theta})

                except Exception as e:
                    self.status_var.set(f"Error at {freq:.1f} Hz: {e}")
                    continue

                progress_bar['value'] = i + 1
                progress_label.config(text=f"{i+1}/{num_points}")
                self.root.update()
                progress_window.update()

            progress_window.destroy()

            if not self.measurement_data:
                messagebox.showwarning("No Data", "No measurements were successful.")
                return

            self.update_plot(valid_freqs, impedances, phases)
            self.save_btn.config(state="normal")
            self.status_var.set(f"Sweep complete: {len(self.measurement_data)} points")

        except Exception as e:
            messagebox.showerror("Error", f"Sweep failed: {str(e)}")
            self.status_var.set("Sweep failed")

    def update_plot(self, freqs, z, theta):
        self.ax1.clear(), self.ax2.clear()
        self.ax1.semilogx(freqs, z, 'b-o', markersize=3, linewidth=1)
        self.ax1.set_ylabel("Impedance (Ω)", color='b')
        self.ax1.set_title("Impedance vs Frequency")
        self.ax1.grid(True, alpha=0.3)

        self.ax2.semilogx(freqs, theta, 'r-s', markersize=3, linewidth=1)
        self.ax2.set_xlabel("Frequency (Hz)")
        self.ax2.set_ylabel("Phase (°)", color='r')
        self.ax2.set_title("Phase Angle vs Frequency")
        self.ax2.grid(True, alpha=0.3)

        self.fig.tight_layout()
        self.canvas.draw()

    def save_data(self):
        if not self.measurement_data:
            messagebox.showwarning("No Data", "No data to save.")
            return

        file = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV", "*.csv")])
        if not file: return

        try:
            with open(file, 'w', newline='') as f:
                w = csv.DictWriter(f, fieldnames=['frequency', 'impedance', 'phase'])
                w.writeheader()
                w.writerows(self.measurement_data)
            self.status_var.set(f"Saved to {file}")
            messagebox.showinfo("Saved", f"Data saved to:\n{file}")
        except Exception as e:
            messagebox.showerror("Save Error", str(e))

    def clear_plot(self):
        self.ax1.clear(), self.ax2.clear(), self.canvas.draw()
        self.measurement_data = []
        self.save_btn.config(state="disabled")
        self.status_var.set("Cleared")

    def cleanup(self):
        if self.instrument:
            try: self.instrument.close()
            except: pass
        if self.rm:
            try: self.rm.close()
            except: pass

def main():
    root = tk.Tk()
    app = LCRMeterApp(root)

    def on_close():
        app.cleanup()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()

if __name__ == "__main__":
    main()

    