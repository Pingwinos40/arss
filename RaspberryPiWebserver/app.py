# app.py
# Flask dashboard that talks to the Arduino Mega over USB serial
# and serves an HTML dashboard at "/".

from flask import Flask, request, jsonify, Response, render_template
import os, time, glob, json, threading, contextlib, subprocess
import serial
from serial.serialutil import SerialException
from lcr_controller import LCRRecorder
from shutil import which

# -----------------------------------------------------------------------------
# Flask app
# -----------------------------------------------------------------------------
app = Flask(__name__)
lcr = LCRRecorder()  # used by /daq/* endpoints

# -----------------------------------------------------------------------------
# Arduino serial manager
# -----------------------------------------------------------------------------
ARDUINO_BAUD = 115200
ARDUINO_PORT_HINTS = ["/dev/ttyACM*", "/dev/ttyUSB*"]  # Mega usually shows as ttyACM*
SERIAL_OPEN_TIMEOUT_S = 3.0
FIRST_OPEN_RESET_DELAY_S = 2.0
READ_TIMEOUT_S = 0.5
WRITE_LOCK = threading.Lock()

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

# -----------------------------------------------------------------------------
# Camera backend + Arducam CamArray mux (Pi 5 / Bookworm)
# -----------------------------------------------------------------------------
def FindExe(name): return which(name)

def PickCameraBackend():
    if FindExe("rpicam-vid"):
        return ("rpicam", ["rpicam-vid","-t","0","--codec","mjpeg",
                           "--width","1280","--height","720","--framerate","20",
                           "-o","-"])
    if FindExe("libcamera-vid"):
        return ("libcamera", ["libcamera-vid","-t","0","--codec","mjpeg",
                              "--width","1280","--height","720","--framerate","20",
                              "-o","-","-n"])
    if FindExe("ffmpeg"):
        devs = sorted(glob.glob("/dev/video[0-9]")) or ["/dev/video0"]
        return ("ffmpeg", ["ffmpeg","-hide_banner","-loglevel","warning",
                           "-f","video4linux2","-input_format","mjpeg",
                           "-i", devs[0],
                           "-vf","scale=1280:720","-r","20",
                           "-f","mjpeg","-"])
    raise RuntimeError("No camera backend found (install rpicam-apps or ffmpeg).")

CAM_KIND, CAM_CMD_BASE = PickCameraBackend()

# Mux pins for Arducam CamArray V2.2 (GPIO4 = SEL, GPIO17 = EN1, GPIO18 = EN2)
MUX_PINS = {"sel": 4, "en1": 17, "en2": 18}
# Channels 0..3 mapping:
#   0: sel=0 en1=0 en2=1
#   1: sel=1 en1=0 en2=1
#   2: sel=0 en1=1 en2=0
#   3: sel=1 en1=1 en2=0
MUX_MAP = {
    0: {"sel":0, "en1":0, "en2":1},
    1: {"sel":1, "en1":0, "en2":1},
    2: {"sel":0, "en1":1, "en2":0},
    3: {"sel":1, "en1":1, "en2":0},
}

def SetMuxChannel(ch: int):
    if ch not in MUX_MAP:
        raise ValueError("bad channel")
    cfg = MUX_MAP[ch]
    # One gpioset call sets all three
    subprocess.check_call(["gpioset", "gpiochip0",
                           f"{MUX_PINS['sel']}={cfg['sel']}",
                           f"{MUX_PINS['en1']}={cfg['en1']}",
                           f"{MUX_PINS['en2']}={cfg['en2']}"])
    # settle a bit so ISP sees a stable source
    time.sleep(0.15)

def GenerateMjpeg(ch: int):
    # Switch mux, then launch camera backend; split stdout into JPEG frames
    SetMuxChannel(ch)
    proc = subprocess.Popen(CAM_CMD_BASE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
    buf = b""
    try:
        while True:
            chunk = proc.stdout.read(65536)
            if not chunk:
                break
            buf += chunk
            i = 0
            while True:
                soi = buf.find(b"\xff\xd8", i)
                if soi < 0:
                    buf = buf[i:] if i else buf
                    break
                eoi = buf.find(b"\xff\xd9", soi + 2)
                if eoi < 0:
                    buf = buf[soi:]
                    break
                frame = buf[soi:eoi + 2]
                i = eoi + 2
                yield (b"--frame\r\n"
                       b"Content-Type: image/jpeg\r\n"
                       b"Content-Length: " + str(len(frame)).encode() + b"\r\n\r\n" +
                       frame + b"\r\n")
            else:
                buf = b""
    finally:
        with contextlib.suppress(Exception): proc.terminate()
        with contextlib.suppress(Exception): proc.kill()

# -----------------------------------------------------------------------------
# Web UI pages
# -----------------------------------------------------------------------------
@app.route("/")
def Root():
    return render_template("index.html")

@app.route("/daq")
def DaqPage():
    return render_template("daq.html")

# -----------------------------------------------------------------------------
# Camera routes
# -----------------------------------------------------------------------------
@app.route("/camera/select/<int:ch>")
def CameraSelect(ch):
    try:
        SetMuxChannel(ch)
        return jsonify({"ok": True, "channel": ch})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/video.mjpg")
def VideoStream():
    ch = request.args.get("ch", default="0")
    try:
        ch = int(ch)
    except ValueError:
        ch = 0
    return Response(GenerateMjpeg(ch), mimetype="multipart/x-mixed-replace; boundary=frame")

@app.route("/snapshot.jpg")
def Snapshot():
    ch = int(request.args.get("ch", 0))
    w = int(request.args.get("w", 640))
    h = int(request.args.get("h", 480))
    try:
        SetMuxChannel(ch)
        if CAM_KIND == "rpicam" and FindExe("rpicam-still"):
            cmd = ["rpicam-still","-n","-o","-","--width",str(w),"--height",str(h)]
            data = subprocess.check_output(cmd, timeout=5)
        elif CAM_KIND == "libcamera" and FindExe("libcamera-jpeg"):
            # libcamera-jpeg may not support --width/--height; fall back to default size
            data = subprocess.check_output(["libcamera-jpeg","-n","-o","-"], timeout=5)
        else:
            dev = sorted(glob.glob("/dev/video[0-9]"))[0]
            data = subprocess.check_output([
                "ffmpeg","-y","-hide_banner","-loglevel","error",
                "-f","video4linux2","-i",dev,"-frames:v","1",
                "-vf", f"scale={w}:{h}",
                "-f","image2pipe","-"
            ], timeout=5)
        return Response(data, mimetype="image/jpeg")
    except Exception as e:
        return jsonify({"error": f"snapshot failed: {e}"}), 500


# -----------------------------------------------------------------------------
# Arduino API routes
# -----------------------------------------------------------------------------
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
    autoStart = request.args.get("start", "1").lower() in ("1","true","yes")
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
    autoStart = request.args.get("start", "0").lower() in ("1","true","yes")
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

# -----------------------------------------------------------------------------
# DAQ endpoints (simple wrappers; safe if methods exist)
# -----------------------------------------------------------------------------
@app.route("/daq/start")
def DaqStart():
    filename = request.args.get("file", "measurements.csv")
    try:
        interval = float(request.args.get("interval", "0.5"))
    except ValueError:
        return jsonify({"error":"interval must be numeric seconds"}), 400
    if hasattr(lcr, "start"):
        try:
            lcr.start(filename=filename, interval=interval)
            return jsonify({"ok": True, "file": filename, "interval": interval})
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    return jsonify({"error":"DAQ start not implemented"}), 501

@app.route("/daq/stop")
def DaqStop():
    if hasattr(lcr, "stop"):
        try:
            lcr.stop()
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    return jsonify({"error":"DAQ stop not implemented"}), 501

@app.route("/daq/data")
def DaqData():
    if hasattr(lcr, "get_last_data"):
        ts_val = lcr.get_last_data()
        if ts_val:
            ts, val = ts_val
            return jsonify({"timestamp": ts, "value": val})
        return jsonify({"timestamp": None, "value": None})
    return jsonify({"error":"DAQ data not implemented"}), 501

# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
