# app.py
# Flask dashboard that talks to the Arduino Mega over USB serial
# and serves an HTML dashboard at "/".

from flask import Flask, request, jsonify, Response, render_template, redirect, url_for
import os, time, glob, json, subprocess, threading, contextlib
import serial
from serial.serialutil import SerialException
from lcr_controller import LCRRecorder

# ---------------------------
# Config
# ---------------------------
ARDUINO_BAUD = 115200
ARDUINO_PORT_HINTS = ["/dev/ttyACM*", "/dev/ttyUSB*"]  # Mega usually shows as ttyACM*
SERIAL_OPEN_TIMEOUT_S = 3.0
FIRST_OPEN_RESET_DELAY_S = 2.0
READ_TIMEOUT_S = 0.5
WRITE_LOCK = threading.Lock()

LIBCAM_CMD = [
    "libcamera-vid",
    "-t", "0",
    "--inline",
    "--width", "1280",
    "--height", "720",
    "--framerate", "20",
    "-o", "-",
    "-n"
]

app = Flask(__name__)
lcr = LCRRecorder()

# ---------------------------
# Arduino serial manager
# ---------------------------
class MegaSerial:
    def __init__(self):
        self.Port = None
        self.Ser = None
        self.LastOpenAttempt = 0.0

    def _FindPort(self):
        for pat in ARDUINO_PORT_HINTS:
            for p in sorted(glob.glob(pat)):
                return p
        return None

    def _OpenIfNeeded(self):
        if self.Ser and self.Ser.is_open:
            return
        now = time.time()
        if now - self.LastOpenAttempt < 0.5:
            return
        self.LastOpenAttempt = now

        port = self._FindPort()
        if not port:
            raise SerialException("Arduino Mega serial port not found")

        self.Port = port
        self.Ser = serial.Serial(
            port=self.Port,
            baudrate=ARDUINO_BAUD,
            timeout=READ_TIMEOUT_S
        )
        time.sleep(FIRST_OPEN_RESET_DELAY_S)
        self.Ser.reset_input_buffer()

    def SendLine(self, line):
        with WRITE_LOCK:
            self._OpenIfNeeded()
            if not self.Ser or not self.Ser.is_open:
                raise SerialException("Serial not open")
            self.Ser.write((line.strip() + "\n").encode("ascii"))
            self.Ser.flush()
            time.sleep(0.15)
            lines = []
            while True:
                try:
                    raw = self.Ser.readline()
                except SerialException:
                    break
                if not raw:
                    break
                s = raw.decode("ascii", errors="ignore").strip()
                if s:
                    lines.append(s)
            return lines

    def QueryJsonStatus(self):
        lines = self.SendLine("STATUS")
        for s in lines:
            if s.startswith("{") and s.endswith("}"):
                try:
                    return json.loads(s)
                except json.JSONDecodeError:
                    pass
        return {"raw": lines}

Mega = MegaSerial()

# ---------------------------
# MJPEG stream
# ---------------------------
def GenerateMjpeg():
    proc = subprocess.Popen(
        LIBCAM_CMD,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=0
    )

    def FindJpegFrames(b):
        frames = []
        i = 0
        while True:
            soi = b.find(b"\xff\xd8", i)
            if soi < 0: break
            eoi = b.find(b"\xff\xd9", soi + 2)
            if eoi < 0: break
            frames.append(b[soi:eoi+2])
            i = eoi + 2
        return frames, b[i:]

    buffer = b""
    try:
        while True:
            chunk = proc.stdout.read(4096)
            if not chunk: break
            buffer += chunk
            frames, buffer = FindJpegFrames(buffer)
            for f in frames:
                yield (b"--frame\r\n"
                       b"Content-Type: image/jpeg\r\n"
                       b"Content-Length: " + str(len(f)).encode() + b"\r\n\r\n" +
                       f + b"\r\n")
    finally:
        with contextlib.suppress(Exception): proc.terminate()
        with contextlib.suppress(Exception): proc.kill()

# ---------------------------
# Web UI routes
# ---------------------------
@app.route("/")
def Root():
    # Serve the HTML dashboard instead of "OK"
    return render_template("index.html")

@app.route("/video.mjpg")
def VideoStream():
    return Response(GenerateMjpeg(),
                    mimetype="multipart/x-mixed-replace; boundary=frame")

# ---------------------------
# API routes (compatible names)
# ---------------------------
@app.route("/ping")
def Ping():
    try:
        return jsonify({"response": Mega.SendLine("PING")})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/status")
def Status():
    try:
        return jsonify(Mega.QueryJsonStatus())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/start")
def Start():
    try:
        return jsonify({"response": Mega.SendLine("START")})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/stop")
def Stop():
    try:
        return jsonify({"response": Mega.SendLine("STOP")})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/kill")
def Kill():
    try:
        return jsonify({"response": Mega.SendLine("KILL")})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/setFreq")
def SetFreq():
    try:
        hz = float(request.args.get("hz", ""))
    except ValueError:
        return jsonify({"error": "hz must be a number"}), 400
    autoStart = request.args.get("start", "1") in ("1", "true", "True")
    try:
        r1 = Mega.SendLine(f"SETFREQ {hz}")
        r2 = Mega.SendLine("START") if autoStart else []
        return jsonify({"setfreq": r1, "start": r2})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/setRpm")
def SetRpm():
    try:
        rpm = int(float(request.args.get("rpm", "")))
    except ValueError:
        return jsonify({"error": "rpm must be numeric"}), 400
    autoStart = request.args.get("start", "0") in ("1", "true", "True")
    try:
        r1 = Mega.SendLine(f"SETRPM {rpm}")
        r2 = Mega.SendLine("START") if autoStart else []
        return jsonify({"setrpm": r1, "start": r2})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/setDirection")
def SetDirection():
    val = request.args.get("value", "")
    if not val:
        return jsonify({"error": "value required"}), 400
    try:
        return jsonify({"response": Mega.SendLine(f"SETDIR {val}")})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/sensor")
def Sensor():
    try:
        r = Mega.SendLine("SENSOR?")
        out = {"raw": r}
        for line in r:
            if line.startswith("ENV "):
                rest = line[4:]
                parts = rest.replace(" C", "").replace(" %RH", "").split(",")
                if len(parts) >= 2:
                    t = float(parts[0].strip()); h = float(parts[1].strip())
                    out = {"temp_c": t, "humidity": h}
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
        return jsonify({"response": Mega.SendLine(f"SETSTEPS {n}")})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/setMaxRpm")
def SetMaxRpm():
    try:
        n = int(request.args.get("value", ""))
    except ValueError:
        return jsonify({"error": "value must be integer"}), 400
    try:
        return jsonify({"response": Mega.SendLine(f"SETMAXRPM {n}")})
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
        return jsonify({"response": Mega.SendLine(f"SETRAMP {step} {ms}")})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ---------------------------
# Main
# ---------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
