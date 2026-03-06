# HALO / ARSS Code

Code and scripts for the **Hexagonal Automated Load Oscillator (HALO)** and its **Automated Radial Stretcher System (ARSS)** — a test rig for dynamically stretching dielectric elastomer actuators (DEAs) and elastomer-electrode composites while simultaneously characterizing their electrical properties via an LCR meter.

## System Overview

The ARSS is a two-node embedded system connected through a local router. A user joins the router's Wi-Fi network and controls the entire experiment from a browser-based dashboard.

**Raspberry Pi 5 (Supervisor)** — runs a Flask web server that serves the interactive dashboard, proxies serial commands to the Arduino, streams video from an Arducam Quad-Camera HAT, and manages background LCR meter data acquisition.

**Arduino Mega 2560 (Motor Controller)** — generates hardware-timed stepper pulses via Timer1 CTC mode on pin D11 (OC1A). Accepts a text-based serial command protocol for frequency, RPM, direction, and step-count control. Also reads a DHT22 sensor for environmental monitoring.

**BK Precision 894 LCR Meter** — connected to the Pi via USB-TMC. Controlled through SCPI commands for impedance, capacitance, inductance, and related measurements across configurable test frequencies and voltages.

```
  [User Laptop]
       |
   Wi-Fi (router)
       |
  [Raspberry Pi 5]
    /     |     \
Serial  USB-TMC  CSI (x4)
  |       |        \
[Mega]  [BK894]  [Arducam Quad-HAT]
```

## Repository Structure

```
HALO_ARSS_Code/
|-- RaspberryPiWebserver/
|   |-- app.py               Flask dashboard + API server
|   |-- instruments.py        USB-TMC instrument drivers (BK894, TekMSO24)
|   |-- lcr_controller.py     Background LCR recording manager
|   |-- halodashboard.service  systemd unit for auto-start
|   |-- start_dashboard.sh     Shell launcher
|
|-- ArduinoFirmwares/
|   |-- ArduinoMegaControl.ino   Active firmware (Timer1 CTC, DHT22, serial commands)
|   |-- ArduinoUnoControl.ino    Legacy: Uno R4 WiFi with onboard HTTP server
|   |-- ArduinoNanoControl.ino   Legacy: Nano Timer2 PWM generator
|
|-- SCPI/
|   |-- instruments.py           Symlink to RaspberryPiWebserver/instruments.py
|   |-- instrument_gui.py        Standalone tkinter GUI for BK894 + TekMSO24
|   |-- LCR_Comm.py              Legacy: tkinter LCR recorder (network/VXI-11)
|   |-- LCR_COM2.py              Legacy: tkinter LCR recorder (USB, timed duration)
|   |-- LCR_USB_Test.py          Legacy: quick VISA enumeration test
|   |-- LcrMeterPythonGui.py     Legacy: frequency sweep GUI with matplotlib
|
|-- html/
|   |-- index.html               Motor + environment + camera dashboard
|   |-- daq.html                 LCR meter DAQ page
|
|-- rPiFirmware/
|   |-- config.txt               /boot/firmware/config.txt for Arducam Quad-HAT
```

## Dashboard Features

### Motor Control (`/`)

- Set RPM, frequency (Hz), or discrete step count
- CW / CCW direction toggle
- Start / Stop / Kill commands
- Live motor state badge (running / idle)

### Environment Monitoring (`/`)

- Real-time temperature and humidity from the DHT22
- Scrolling uPlot chart with configurable time window (15 s to 300 s)
- Zoom (scroll wheel), pan (drag), and smooth/marker toggles
- CSV export of the visible window or full session

### Camera (`/`)

- Live MJPEG stream from the Arducam Quad-Camera HAT (4 channels via GPIO mux)
- Snapshot download as JPEG

### LCR Meter DAQ (`/daq`)

- Connect to the BK Precision 894 over USB-TMC (auto-detects `/dev/usbtmc*`)
- Configure measurement mode (RX, CPD, ZTD, LSRS, etc.), test frequency, and AC voltage
- Start/stop background CSV recording at configurable intervals
- Single-shot measurements
- Live display with mode-aware unit formatting (nF, pF, uH, mH, Ohm)

## Serial Command Protocol (Arduino Mega)

| Command | Description |
|---|---|
| `PING` | Returns `PONG` |
| `STATUS` | Returns JSON with running state, Hz, RPM, direction, PPR, env data |
| `START` | Begin pulse output |
| `STOP` / `KILL` | Stop pulse output |
| `SETFREQ <hz>` | Set pulse frequency (0.5 to 50000 Hz) |
| `SETRPM <rpm>` | Set RPM (converted to Hz via PPR) |
| `SETDIR <CW\|CCW>` | Set stepper direction |
| `SETSTEPS <n>` | Move exactly N steps then stop |
| `SETPPR <n>` | Set pulses per revolution (default 200) |
| `SETMAXRPM <n>` | Store max RPM limit |
| `SETRAMP <rpm_step> <ms>` | Store ramp parameters |
| `SENSOR?` | Returns `ENV <temp_c>, <humidity> %RH` |

## Instrument Drivers

`instruments.py` provides lightweight Python classes for USB-TMC instruments using raw `/dev/usbtmc*` file I/O — no pyvisa or NI-VISA required.

**BK894** — B&K Precision 894 LCR Meter. Supports 11 measurement modes, frequency/voltage configuration, and `measure()` returning `(primary, secondary, status)`.

**TekMSO24** — Tektronix MSO24 Oscilloscope. Supports 4-channel vertical/horizontal/trigger configuration, automated measurements, and binary waveform capture.

## Setup

### Pi 5

```bash
# Clone and set up venv
cd /home/admin
git clone <repo-url> HALODashboard
cd HALODashboard/RaspberryPiWebserver
python3 -m venv venv
source venv/bin/activate
pip install flask pyserial

# Copy the systemd service
sudo cp halodashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable halodashboard
sudo systemctl start halodashboard
```

The service auto-starts on boot, sets the camera mux to channel 0, and serves the dashboard on port 5000.

### Arduino Mega

Flash `ArduinoFirmwares/ArduinoMegaControl.ino` using the Arduino IDE or PlatformIO. Requires the `DHT` library.

### Standalone GUI (no Pi needed)

```bash
cd SCPI/
python3 instrument_gui.py
```

Connects to the BK894 at `/dev/usbtmc1` and TekMSO24 at `/dev/usbtmc2`. Provides tabbed control for both instruments plus multi-instrument CSV data logging.

## Hardware

- Raspberry Pi 5 (4 GB+)
- Arduino Mega 2560
- BK Precision 894 LCR Meter (USB)
- Arducam Quad-Camera HAT V2.2 (CSI, 4-channel mux)
- DHT22 temperature/humidity sensor (on Mega pin D53)
- TB6600 or similar stepper driver (STEP on Mega D11, DIR on D33)
- Local Wi-Fi router (dedicated network for the test rig)

## AI Attribution

Portions of the code, documentation, and system architecture in this repository were developed with assistance from **Anthropic Claude** (Opus 4.6 and Sonnet 4.6). This includes the instrument driver library (`instruments.py`), the Flask dashboard and DAQ integration, the standalone instrument control GUI, Arduino firmware refinements, and this README. All AI-generated content was reviewed, tested, and validated by the author.

## Author

**Anatol M. Gogoj**
PhD Candidate, Mechanical Engineering
University of Connecticut — Duduta Lab (The Robot Incubator)

## License

This project is not currently under a formal open-source license. Contact the author for use or collaboration inquiries.
