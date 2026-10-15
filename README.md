# HALO / ARSS Code

Code and scripts for the **Hexagonal Automated Load Oscillator (HALO)** and its **Automated Radial Stretcher System (ARSS)**: a test rig for dynamically stretching dielectric elastomer actuators (DEAs) and elastomer-electrode composites while simultaneously characterizing their electrical properties via an LCR meter.

## System Overview

The ARSS is a two-node embedded system connected through a local router. A user joins the router's Wi-Fi network and controls the entire experiment from a browser-based dashboard.

**Raspberry Pi 5 (Supervisor)**: runs a Flask web server that serves the interactive dashboard, owns the serial link to the Arduino, streams video from the Arducam camera mux, and manages background LCR meter data acquisition.

**Arduino Mega 2560 (Motor Controller)**: generates hardware-timed stepper pulses via Timer1 CTC mode on pin D11 (OC1A). Accepts a text-based serial command protocol for frequency, RPM, direction, and step-count control. Also reads a DHT22 sensor for environmental monitoring.

**BK Precision 894 LCR Meter**: connected to the Pi via USB-TMC, driven through PyVISA with the pure-Python pyvisa-py backend (libusb via pyusb; the kernel `usbtmc` driver is blacklisted). Controlled through SCPI commands for impedance, capacitance, inductance, and related measurements across configurable test frequencies (100 Hz to 500 kHz) and AC levels (0.01 to 2.0 V).

```
  [User Laptop]
       |
   Wi-Fi (router)
       |
  [Raspberry Pi 5]
    /     |     \
Serial  USB-TMC  CSI (4 cameras behind a GPIO mux)
  |       |        \
[Mega]  [BK894]  [Arducam multi-camera board]
```

## Repository Structure

```
arss/
|-- RaspberryPiWebserver/
|   |-- app.py                  Flask dashboard + API server
|   |-- mega_link.py            Serial link to the Mega: one owner thread, cached status, priority STOP
|   |-- instruments.py          BK894 driver (pyvisa-py), ported from Digital_Multitool
|   |-- lcr_format.py           LCR units per mode, overload-safe display values
|   |-- lcr_controller.py       Background LCR recording manager (run folders + manifest)
|   |-- halodashboard.service   systemd unit for auto-start
|   |-- start_dashboard.sh      Shell launcher (same command as the service, for manual runs)
|
|-- ArduinoFirmwares/
|   |-- ArduinoMegaControl.ino  Active firmware (Timer1 CTC, DHT22, serial commands)
|   |-- ArduinoUnoControl.ino   Legacy: Uno R4 WiFi with onboard HTTP server
|   |-- ArduinoNanoControl.ino  Legacy: Nano Timer2 pulse generator
|
|-- html/
|   |-- index.html              Motor + environment + camera dashboard
|   |-- daq.html                LCR meter DAQ page
|
|-- static/vendor/uplot/        uPlot 1.6.17 (MIT), served locally so the chart works offline
|
|-- deploy/
|   |-- 99-bk894.rules          udev rule: non-root access to the BK894 (USB 0471:2827)
|   |-- blacklist-usbtmc.conf   modprobe blacklist for the kernel usbtmc driver
|
|-- bench/
|   |-- bk894_smoke.py          Hand-run BK894 check on the Pi (IDN, :VOLT read-back, fetches)
|
|-- tests/                      Hardware-free unit tests (see Tests below)
|
|-- rPiFirmware/
|   |-- config.txt              /boot/firmware/config.txt for the camera board
|
|-- requirements.txt            Python dependencies for the Pi app
|-- AI_ATTRIBUTION.md           Which AI models were used, for what, and what was verified
```

## Dashboard Features

### Motor Control (`/`)

- Set RPM, frequency (Hz), or a discrete step count
- CW / CCW direction
- Start, and a STOP button that is sent ahead of any queued command
- Ramp rates (RPM/s up and down) and a max-RPM limit; a request above the limit asks for confirmation, with Cancel as the default
- Link chip showing the Mega link as LIVE, STALE (no fresh status for more than 2 s) or DOWN, and a banner when the link is down
- Every indicator (motor running/idle, ramp, max RPM, environment) shows `---` or "unknown" when its data is not fresh, never a stale "idle"

### Environment Monitoring (`/`)

- Temperature and humidity from the DHT22, polled every 0.5 s through the cached status
- Scrolling uPlot chart with a selectable time window (15 s to 300 s)
- Zoom (scroll wheel), pan (drag), smooth and marker toggles, reset view
- CSV export of the visible window or the full session

### Camera (`/`)

- Live MJPEG stream from one of four cameras; the cameras sit behind a GPIO mux, so only one is live at a time
- Snapshot download as JPEG
- The camera backend (`rpicam-vid`, `libcamera-vid`, or `ffmpeg`) is picked on first use; the camera routes return 503 if none is installed

### LCR Meter DAQ (`/daq`)

- Connect to the BK Precision 894 over USB-TMC (found by USB VID/PID `0471:2827`; the `*IDN?` reply must name model 894)
- Configure all 14 measurement modes (CPD, CSRS, RX, ZTD, LSRS, etc.), test frequency (100 Hz to 500 kHz), and AC level (0.01 to 2.0 V, sent as `:VOLT`); mode, frequency and level are read back from the meter and a mismatch stops the start
- Background recording on a fixed time grid; overrun slots are skipped and counted, and the run stops after 5 consecutive measurement errors
- Each run writes `<data dir>/<UTC stamp>_<run name>/lcr.csv` (sample index, UTC ISO 8601 time, monotonic time, raw primary/secondary/status) and `manifest.json` (IDN, requested and read-back settings, interval, counts, last error, software git revision, note). The data dir is `$ARSS_DATA_DIR`, default `~/arss-data`; run names allow only `A-Z a-z 0-9 . _ -` and existing runs are never overwritten
- Single-shot measurements
- Live display with SI-prefixed units per mode and the sample age; values older than 3 recording intervals show as `---` (stale). Readings that carry no number (NaN, infinity, or the SCPI overload code 9.9e37) also show as `---` with an overload note, while the CSV keeps the raw values

## HTTP API (Pi, port 5000)

All routes are unauthenticated GET requests (see Safety Notes).

| Route | Purpose |
|---|---|
| `/`, `/daq` | Dashboard pages |
| `/status` | Cached Mega STATUS fields plus `link` (LIVE/STALE/DOWN), `age_s`, `updated_utc`; never touches the serial port |
| `/sensor` | Cached temperature and humidity, with the same freshness fields |
| `/ping` | Round trip PING/PONG through the link |
| `/start`, `/stop`, `/kill` | Motor start; STOP and KILL use the priority path |
| `/setRpm?rpm=`, `/setFreq?hz=` | Speed; above the max RPM they answer 409 unless `override=1`. `/setFreq` also starts the motor unless `start=0`; `/setRpm` starts it only with `start=1` |
| `/setDirection?value=`, `/setSteps?value=` | Direction (`CW`, `CCW`, or `1`/`0`, `+`/`-`); step move (starts the motor) |
| `/setMaxRpm?value=`, `/setRamp?up=&down=` | Limit and ramp rates (RPM/s) |
| `/camera/select/<ch>`, `/video.mjpg?ch=`, `/snapshot.jpg?ch=` | Camera mux, stream, still |
| `/daq/connect`, `/daq/status`, `/daq/start`, `/daq/stop`, `/daq/measure`, `/daq/data` | LCR meter |

Command routes answer 400 for bad input (non-finite or out-of-range numbers, unknown direction), 503 when the Mega link is DOWN, 504 on a command timeout, and 502 when the firmware answers `ERR?`.

## Serial Command Protocol (Arduino Mega)

115200 baud, one ASCII command per line. Lines longer than 63 characters are truncated by the firmware. The firmware prints `READY` once after reset; opening the serial port resets the Mega, so the motor stops and settings return to their defaults on every reconnect.

| Command | Reply | Notes |
|---|---|---|
| `PING` | `PONG` | |
| `STATUS` | one JSON line | `running`, `hz`, `rpm`, `dir` (1 = CW), `ppr`, `steps_remaining`, `max_rpm`, `ramping`, `ramp_up`, `ramp_down`, `target_rpm`, `env.temp_c`, `env.humidity` (null if the DHT22 read failed) |
| `START` | `OK START` | Begin pulse output |
| `STOP`, `KILL` | `OK STOP` | Both stop pulse output |
| `SETFREQ <hz>` | `OK SETFREQ <hz>` | Clamped to 0.5 to 50000 Hz |
| `SETRPM <rpm>` | `OK SETRPM <rpm> Hz=<hz>` | Converted to Hz via PPR |
| `SETDIR <token>` | `OK SETDIR 0\|1` | 1 (CW) if the token starts with `CW` (any case), `1` or `+`; anything else sets 0 (CCW) |
| `SETSTEPS <n>` | `OK SETSTEPS <n>` | Moves exactly n steps and **starts the motor**; n <= 0 stops |
| `SETPPR <n>` | `OK SETPPR <n>` | Pulses per revolution, default 200, clamped 1 to 200000; must match the driver's microstep setting |
| `SETMAXRPM <n>` | `OK SETMAXRPM <n>` | Stored and reported in STATUS; **not enforced by the firmware** (the Pi enforces it) |
| `SETRAMP <up> <down>` | `OK SETRAMP UP=<u> DOWN=<d>` | Ramp rates in RPM/s, 0 = instant |
| `SENSOR?` | `ENV <t>, <h> %RH` | `nan` if the DHT22 read failed |
| anything else | `ERR?` | |

## Instrument Drivers

`instruments.py` is a port of the bench-proven BK894 driver from the Digital_Multitool repository (commit f22527f). It talks to the meter through `pyvisa.ResourceManager('@py')` (pyvisa-py over libusb), with a 2 s I/O timeout, a device clear on open, and a per-instrument lock around every write/read/query.

**BK894**: B&K Precision 894 LCR Meter. 14 measurement modes, frequency/AC-level configuration with read-back (`configure_and_verify`), desync recovery (drain and retry), and `measure()` returning `(primary, secondary, status)`.

Known BK894 quirks (from Digital_Multitool, bench-verified there on its meter, not yet on the rig meter):

- The AC test level command is plain `:VOLT` (query `:VOLT?`). `:LEV:VOLT` and `:VOLT:LEV` are silently ignored.
- The meter clamps the level at 2.0 V and the frequency at 500 kHz.
- A late reply can shift every later query by one answer; the driver drains the input buffer and retries.
- The USB serial-number descriptor is garbage; the real serial is in `*IDN?`.

PC-side bench work with the LCR meter and the oscilloscope lives in Digital_Multitool; the old `SCPI/` folder (a third copy of the driver and a tkinter GUI) was removed.

## Setup

### Pi 5

The service expects the repository at `/home/admin/arss` with a virtual environment at `/home/admin/arss/venv`, run as user `admin`. If the Pi still has an older copy (for example `/home/admin/HALODashboard`), clone fresh instead of pulling: the repository history was rewritten on 2026-10-14.

```bash
# System packages: libusb for pyvisa-py, gpiod for the camera mux (gpioset),
# rpicam-apps for the cameras
sudo apt install python3-venv libusb-1.0-0 gpiod rpicam-apps

# Clone and set up the venv
cd /home/admin
git clone https://github.com/Pingwinos40/arss.git
cd arss
python3 -m venv venv
venv/bin/pip install -r requirements.txt

# Install and start the systemd service
sudo cp RaspberryPiWebserver/halodashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now halodashboard
```

The service starts on boot, sets the camera mux to channel 0, and serves the dashboard on port 5000. For a manual run (with the service stopped), use `RaspberryPiWebserver/start_dashboard.sh`. A different user or install folder means editing `User=` and the `/home/admin/arss` paths in the service file.

Not yet checked on the Pi: the service and `app.py` drive the camera mux with `gpioset gpiochip0`; confirm with `gpiodetect` that the 40-pin header is `gpiochip0` on the installed kernel.

#### LCR meter access (BK894)

The dashboard reaches the meter through libusb, so the kernel `usbtmc` driver must not claim it and the service user needs access to the USB device node. From the repository root:

```bash
sudo cp deploy/blacklist-usbtmc.conf /etc/modprobe.d/
sudo modprobe -r usbtmc
sudo cp deploy/99-bk894.rules /etc/udev/rules.d/
sudo udevadm control --reload
# unplug and replug the meter so the rule applies
```

The rule grants mode 0666 on the meter only (VID 0471, PID 2827), mirroring the Digital_Multitool setup; the rule file shows a stricter `plugdev` variant. These two files have not been tested on the Pi yet. To check the whole chain, stop the dashboard (`sudo systemctl stop halodashboard`) and run `venv/bin/python bench/bk894_smoke.py`: it prints `*IDN?`, sets CPD / 1 kHz / 0.5 V, prints the `:VOLT?` read-back, takes 10 readings, and exits nonzero on a mismatch.

Run data goes to `$ARSS_DATA_DIR` (default `~/arss-data` of the service user).

### Tests

```bash
python -m unittest discover -s tests -v
```

The tests use fake transports (a fake Mega firmware and a fake BK894) and need no hardware and no pyvisa. They do need Flask and pyserial from `requirements.txt`, because the route tests import `app.py`.

### Arduino Mega

Flash `ArduinoFirmwares/ArduinoMegaControl.ino` using the Arduino IDE or PlatformIO. Requires a library that provides `DHT.h` (the firmware is written against the Adafruit `DHT sensor library` API).

## Safety Notes

- **No firmware deadman yet (#9).** If the Pi, the dashboard, or the USB link dies while the motor runs, the motor keeps running. While the link is DOWN, STOP cannot reach the Mega.
- **Max RPM is enforced only on the Pi (#10).** Any other client on the serial port can exceed it.
- **No authentication (#14).** Every route, including motor control, is an unauthenticated GET on port 5000. Keep the rig on its own isolated network.

## Hardware

- Raspberry Pi 5 (4 GB+)
- Arduino Mega 2560
- BK Precision 894 LCR Meter (USB)
- Arducam multi-camera board, 4 cameras on one CSI port via a GPIO mux (SEL GPIO4, EN1 GPIO17, EN2 GPIO18)
- DHT22 temperature/humidity sensor (on Mega pin D53)
- TB6600 or similar stepper driver (STEP on Mega D11, DIR on D33); set `SETPPR` to match its microstep DIP setting (#28)
- Local Wi-Fi router (dedicated network for the test rig)

## Author

**Anatol M. Gogoj**
PhD Candidate, Mechanical Engineering
University of Connecticut, Duduta Lab (The Robot Incubator)

## License

This project is not currently under a formal open-source license. Contact the author for use or collaboration inquiries.
