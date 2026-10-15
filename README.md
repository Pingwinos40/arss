# HALO / ARSS Code

Code and scripts for the **Hexagonal Automated Load Oscillator (HALO)** and its **Automated Radial Stretcher System (ARSS)** — a test rig for dynamically stretching dielectric elastomer actuators (DEAs) and elastomer-electrode composites while simultaneously characterizing their electrical properties via an LCR meter.

## System Overview

The ARSS is a two-node embedded system connected through a local router. A user joins the router's Wi-Fi network and controls the entire experiment from a browser-based dashboard.

**Raspberry Pi 5 (Supervisor)** — runs a Flask web server that serves the interactive dashboard, proxies serial commands to the Arduino, streams video from an Arducam Quad-Camera HAT, and manages background LCR meter data acquisition.

**Arduino Mega 2560 (Motor Controller)** — generates hardware-timed stepper pulses via Timer1 CTC mode on pin D11 (OC1A). Accepts a text-based serial command protocol for frequency, RPM, direction, and step-count control. Also reads a DHT22 sensor for environmental monitoring.

**BK Precision 894 LCR Meter** — connected to the Pi via USB-TMC, driven through PyVISA with the pure-Python pyvisa-py backend (libusb via pyusb; the kernel `usbtmc` driver is blacklisted). Controlled through SCPI commands for impedance, capacitance, inductance, and related measurements across configurable test frequencies (100 Hz to 500 kHz) and AC levels (0.01 to 2.0 V).

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
|   |-- instruments.py        BK894 driver (pyvisa-py), ported from Digital_Multitool
|   |-- lcr_format.py         LCR units per mode, overload-safe display values
|   |-- lcr_controller.py     Background LCR recording manager (run folders + manifest)
|   |-- halodashboard.service  systemd unit for auto-start
|   |-- start_dashboard.sh     Shell launcher
|
|-- ArduinoFirmwares/
|   |-- ArduinoMegaControl.ino   Active firmware (Timer1 CTC, DHT22, serial commands)
|   |-- ArduinoUnoControl.ino    Legacy: Uno R4 WiFi with onboard HTTP server
|   |-- ArduinoNanoControl.ino   Legacy: Nano Timer2 PWM generator
|
|-- deploy/
|   |-- 99-bk894.rules           udev rule: non-root access to the BK894 (USB 0471:2827)
|   |-- blacklist-usbtmc.conf    modprobe blacklist for the kernel usbtmc driver
|
|-- bench/
|   |-- bk894_smoke.py           Hand-run BK894 check on the Pi (IDN, :VOLT read-back, fetches)
|
|-- tests/                       Hardware-free unit tests (python -m unittest discover -s tests -v)
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

- Connect to the BK Precision 894 over USB-TMC (found by USB VID/PID `0471:2827`; the `*IDN?` reply must name model 894)
- Configure all 14 measurement modes (CPD, CSRS, RX, ZTD, LSRS, etc.), test frequency (100 Hz to 500 kHz), and AC level (0.01 to 2.0 V, sent as `:VOLT`); mode, frequency and level are read back from the meter and a mismatch stops the start
- Background recording on a fixed time grid; overrun slots are skipped and counted, and the run stops after 5 consecutive measurement errors
- Each run writes `<data dir>/<UTC stamp>_<run name>/lcr.csv` (sample index, UTC ISO 8601 time, monotonic time, raw primary/secondary/status) and `manifest.json` (IDN, requested and read-back settings, interval, counts, last error, software git revision, note). The data dir is `$ARSS_DATA_DIR`, default `~/arss-data`; run names allow only `A-Z a-z 0-9 . _ -` and existing runs are never overwritten
- Single-shot measurements
- Live display with SI-prefixed units per mode, overload readings shown as OVL, and sample age (values older than 3 intervals show as stale)

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

`instruments.py` is a port of the bench-proven BK894 driver from the Digital_Multitool repository (commit f22527f). It talks to the meter through `pyvisa.ResourceManager('@py')` (pyvisa-py over libusb), with a 2 s I/O timeout, a device clear on open, and a per-instrument lock around every write/read/query.

**BK894** — B&K Precision 894 LCR Meter. 14 measurement modes, frequency/AC-level configuration with read-back (`configure_and_verify`), desync recovery (drain and retry), and `measure()` returning `(primary, secondary, status)`.

Known BK894 quirks (from Digital_Multitool, bench-verified there on its meter, not yet on the rig meter):

- The AC test level command is plain `:VOLT` (query `:VOLT?`). `:LEV:VOLT` and `:VOLT:LEV` are silently ignored.
- The meter clamps the level at 2.0 V and the frequency at 500 kHz.
- A late reply can shift every later query by one answer; the driver drains the input buffer and retries.
- The USB serial-number descriptor is garbage; the real serial is in `*IDN?`.

PC-side bench work with the LCR meter and the oscilloscope lives in Digital_Multitool; the old `SCPI/` folder (a third copy of the driver and a tkinter GUI) was removed.

## Setup

### Pi 5

```bash
# Clone and set up venv
cd /home/admin
git clone <repo-url> HALODashboard
cd HALODashboard/RaspberryPiWebserver
python3 -m venv venv
source venv/bin/activate
sudo apt install libusb-1.0-0   # libusb runtime for pyusb / pyvisa-py
pip install -r ../requirements.txt

# Copy the systemd service
sudo cp halodashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable halodashboard
sudo systemctl start halodashboard
```

The service auto-starts on boot, sets the camera mux to channel 0, and serves the dashboard on port 5000.

#### LCR meter access (BK894)

The dashboard reaches the meter through libusb, so the kernel `usbtmc` driver must not claim it and the service user needs access to the USB device node. From the repository root:

```bash
sudo cp deploy/blacklist-usbtmc.conf /etc/modprobe.d/
sudo modprobe -r usbtmc
sudo cp deploy/99-bk894.rules /etc/udev/rules.d/
sudo udevadm control --reload
# unplug and replug the meter so the rule applies
```

The rule grants mode 0666 on the meter only (VID 0471, PID 2827), mirroring the Digital_Multitool setup; the rule file shows a stricter `plugdev` variant. These two files have not been tested on the Pi yet. To check the whole chain, stop the dashboard (`sudo systemctl stop halodashboard`) and run `python3 bench/bk894_smoke.py`: it prints `*IDN?`, sets CPD / 1 kHz / 0.5 V, prints the `:VOLT?` read-back, takes 10 readings, and exits nonzero on a mismatch.

Run data goes to `$ARSS_DATA_DIR` (default `~/arss-data` of the service user).

### Tests

```bash
python -m unittest discover -s tests -v
```

The tests use fake transports and need neither hardware nor pyvisa.

### Arduino Mega

Flash `ArduinoFirmwares/ArduinoMegaControl.ino` using the Arduino IDE or PlatformIO. Requires the `DHT` library.

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
