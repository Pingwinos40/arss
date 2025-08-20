# app.py
# Flask dashboard that talks to the Arduino Mega over USB serial
# and proxies camera MJPEG plus LCR DAQ endpoints.

from flask import Flask, request, jsonify, Response
import os, time, glob, json, subprocess, threading
import serial
from serial.serialutil import SerialException
from lcr_controller import LCRRecorder

# ---------------------------
# Config
# ---------------------------
ARDUINO_BAUD = 115200
ARDUINO_PORT_HINTS = ["/dev/ttyACM*", "/dev/ttyUSB*"]  # Mega usually shows as ttyACM*
SERIAL_OPEN_TIMEOUT_S = 3.0
FIRST_OPEN_RESET_DELAY_S = 2.0   # give the Mega time to reboot after port open
READ_TIMEOUT_S = 0.5
WRITE_LOCK = threading.Lock()

# Camera config for MJPEG pipeline (tune as needed)
LIBCAM_CMD = [
    "libcamera-vid",
    "-t", "0",               # run forever
    "--inline",              # place SPS/PPS in stream to help parsers
    "--width", "1280",
    "--height", "720",
    "--framerate", "20",
    "-o", "-",               # write to stdout
    "-n"                     # no preview
]

app = Flask(__name__)

# One global LCR controller (uses your existing module)
lcr = LCRRecorder()

# ---------------------------
# Arduino serial manager
# ---------------------------
class MegaSerial:
    def __init__(self):
        self.port = None
        self.ser = None
        self.last_open_attempt = 0.0

    def _FindPort(self):
        for pat in ARDUINO_PORT_HINTS:
            for p in sorted(glob.glob(pat)):
                return p
        return None

    def _OpenIfNeeded(self):
        if self.ser and self.ser.is_open:
            return
        now = time.time()
        if now - self.last_open_attempt < 0.5:
            return
        self.last_open_attempt = now

        port = self._FindPort()
        if not port:
            raise SerialException("Arduino Mega serial port not found")

        self.port = port
        self.ser = serial.Serial(
            port=self.port,
            baudrate=ARDUINO_BAUD,
            timeout=READ_TIMEOUT_S
        )
        # Mega resets on open; give it a moment
        time.sleep(FIRST_OPEN_RESET_DELAY_S)
        # flush any boot text
        self.ser.reset_input_buffer()

    def SendLine(self, line):
        """Write a command line and return list of response lines read quickly."""
        with WRITE_LOCK:
            self._OpenIfNeeded()
            if not self.ser or not self.ser.is_open:
                raise SerialException("Serial not open")
            # write
            self.ser.write((line.strip() + "\n").encode("ascii"))
            self.ser.flush()

            # small wait for device to react
            time.sleep(0.15)

            # slurp available lines
            lines = []
            while True:
                try:
                    raw = self.ser.readline()
                except SerialException:
                    break
                if not raw:
                    break
                s = raw.decode("ascii", errors="ignore").strip()
                if s:
                    lines.append(s)
            return lines

    def QueryJsonStatus(self):
        """Send STATUS and try to parse JSON; return dict."""
        lines = self.SendLine("STATUS")
        # Find the first JSON-looking line
        for s in lines:
            if s.startswith("{") and s.endswith("}"):
                try:
                    return json.loads(s)
                except json.JSONDecodeError:
                    pass
        # Fallback: return raw
        return {"raw": lines}

Mega = MegaSerial()

# ---------------------------
# MJPEG stream using libcamera-vid
# ---------------------------
def GenerateMjpeg():
    # Start libcamera-vid process
    proc = subprocess.Popen(
        LIBCAM_CMD,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=0
    )

    buffer = b""

    def FindJpegFrames(b):
        frames = []
        i = 0
        while True:
            soi = b.find(b"\xff\xd8", i)
            if soi < 0:
                break
            eoi = b.find(b"\xff\xd9", soi + 2)
            if eoi < 0:
                break
            frames.append(b[soi:eoi+2])
            i = eoi + 2
        return frames, b[i:]

    try:
        while True:
            chunk = proc.stdout.read(4096)
            if not chunk:
                break
            buffer += chunk
            frames, buffer = FindJpegFrames(buffer)
            for f in frames:
                yield (b"--frame\r\n"
                       b"Content-Type: image/jpeg\r\n"
                       b"Content-Length: " + str(len(f)).encode() + b"\r\n\r\n" +
                       f + b"\r\n")
    finally:
        with contextlib.suppress(Exception):
            proc.terminate()
        with contextlib.suppress(Exception):
            proc.kill()

# ---------------------------
# Routes: Arduino control
# ---------------------------
@app.route("/")
def Root():
    return "OK"

@app.route("/ping")
def Ping():
    try:
        resp = Mega.SendLine("PING")
        return jsonify({"response": resp})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/status")
def Status():
    try:
        data = Mega.QueryJsonStatus()
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/start")
def Start():
    try:
        resp = Mega.SendLine("START")
        return jsonify({"response": resp})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/stop")
def Stop():
    try:
        resp = Mega.SendLine("STOP")
        return jsonify({"response": resp})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/kill")
def Kill():
    try:
        resp = Mega.SendLine("KILL")
        return jsonify({"response": resp})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/setFreq")
def SetFreq():
    """Set frequency in Hz and (optionally) start."""
    try:
        hz = float(request.args.get("hz", ""))
    except ValueError:
        return jsonify({"error": "hz must be a number"}), 400
    auto_start = request.args.get("start", "1") in ("1", "true", "True")
    try:
        r1 = Mega.SendLine(f"SETFREQ {hz}")
        r2 = Mega.SendLine("START") if auto_start else []
        return jsonify({"setfreq": r1, "start": r2})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/setRpm")
def SetRpm():
    """Set target RPM (optionally start)."""
    try:
        rpm = int(float(request.args.get("rpm", "")))
    except ValueError:
        return jsonify({"error": "rpm must be numeric"}), 400
    auto_start = request.args.get("start", "0") in ("1", "true", "True")
    try:
        r1 = Mega.SendLine(f"SETRPM {rpm}")
        r2 = Mega.SendLine("START") if auto_start else []
        return jsonify({"setrpm": r1, "start": r2})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/setDirection")
def SetDirection():
    """value=CW|CCW|1|0"""
    val = request.args.get("value", "")
    if not val:
        return jsonify({"error": "value required"}), 400
    try:
        r = Mega.SendLine(f"SETDIR {val}")
        return jsonify({"response": r})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/sensor")
def Sensor():
    """Read DHT22 once."""
    try:
        r = Mega.SendLine("SENSOR?")
        # Try to parse: "ENV 23.45 C, 40.12 %RH"
        out = {"raw": r}
        for line in r:
            if line.startswith("ENV "):
                try:
                    # Split conservatively
                    rest = line[4:]
                    parts = rest.replace(" C", "").replace(" %RH", "").split(",")
                    t = float(parts[0].strip())
                    h = float(parts[1].strip())
                    out = {"temp_c": t, "humidity": h}
                except Exception:
                    pass
                break
        return jsonify(out)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/setSteps")
def SetSteps():
    try:
        n = int(request.args.get("value", ""))
    except ValueError:
        return jsonify({"error": "value must be integer"}), 400
    try:
        r = Mega.SendLine(f"SETSTEPS {n}")
        return jsonify({"response": r})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/setMaxRpm")
def SetMaxRpm():
    try:
        n = int(request.args.get("value", ""))
    except ValueError:
        return jsonify({"error": "value must be integer"}), 400
    try:
        r = Mega.SendLine(f"SETMAXRPM {n}")
        return jsonify({"response": r})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/setRamp")
def SetRamp():
    try:
        step = int(request.args.get("rpm_step", ""))
        ms   = int(request.args.get("ms", ""))
    except ValueError:
        return jsonify({"error": "rpm_step and ms must be integers"}), 400
    try:
        r = Mega.SendLine(f"SETRAMP {step} {ms}")
        return jsonify({"response": r})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ---------------------------
# Routes: camera MJPEG stream
# ---------------------------
@app.route("/video.mjpg")
def VideoStream():
    return Response(GenerateMjpeg(),
                    mimetype="multipart/x-mixed-replace; boundary=frame")

# ---------------------------
# Routes: LCR recorder hooks
# ---------------------------
@app.route("/daq/start")
def DaqStart():
    file_name = request.args.get("file", "lcr_log.csv")
    try:
        interval = float(request.args.get("interval", "1.0"))
    except ValueError:
        return jsonify({"error": "interval must be numeric seconds"}), 400
    # Kick off recorder thread if your class supports it
    try:
        # Expecting your LCRRecorder to have start(file_name, interval)
        # If your signature differs, adjust here.
        if getattr(lcr, "thread", None) and lcr.thread and lcr.thread.is_alive():
            return jsonify({"status": "already_running"})
        lcr.start(file_name, interval)  # type: ignore[attr-defined]
        return jsonify({"status": "started", "file": file_name, "interval": interval})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/daq/stop")
def DaqStop():
    try:
        lcr.stop()
        return jsonify({"status": "stopping"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/daq/data")
def DaqData():
    try:
        data = lcr.get_last_data()
        if data:
            return jsonify({"timestamp": data[0], "value": data[1]})
        return jsonify({})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ---------------------------
# Main
# ---------------------------
if __name__ == "__main__":
    # Ensure dialout membership so we can open /dev/ttyACM0 without sudo
    # On first run you might need: sudo usermod -aG dialout $USER; newgrp dialout
    app.run(host="0.0.0.0", port=5000, debug=True)
