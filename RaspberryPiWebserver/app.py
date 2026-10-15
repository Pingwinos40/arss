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
# One MegaLink owns the port (issue #32). It runs a reader thread (port open,
# read, reconnect), a writer thread (command queue, one command in flight) and
# a poll thread (STATUS and SENSOR? every 0.5 s, also the host keepalive).
# Routes read its cached snapshots and never touch the port themselves.
# STOP and KILL bypass the queue (#8). The threads start on the first request,
# so importing this module (tests, tooling) opens nothing.
import math, re
from mega_link import (MegaLink, LinkError, LinkDown, LinkNotReady, LinkTimeout,
                       FirmwareError, CommandRejected)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE_DIR = os.path.join(REPO_ROOT, "html")
STATIC_DIR = os.path.join(REPO_ROOT, "static")

MEGA_POLL_PERIOD_S = 0.5
MEGA_STALE_AFTER_S = 2.0

Mega = MegaLink(PollPeriodS=MEGA_POLL_PERIOD_S, StaleAfterS=MEGA_STALE_AFTER_S)

@app.before_request
def EnsureMegaLinkStarted():
    Mega.Start()

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

# Resolved lazily on the first camera request, so the app (and its tests)
# start on a machine without rpicam or ffmpeg. Only a successful pick is
# cached; a failed one is retried on the next camera request.
_CamBackend = None
_CamBackendLock = threading.Lock()

def GetCameraBackend():
    """Return (kind, cmd_base) or raise RuntimeError if no backend exists."""
    global _CamBackend
    with _CamBackendLock:
        if _CamBackend is None:
            _CamBackend = PickCameraBackend()
        return _CamBackend

def CameraUnavailableResponse(e):
    return jsonify({"error": "camera unavailable", "detail": str(e)}), 503

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

def GenerateMjpeg(ch: int, camCmdBase):
    # Switch mux, then launch camera backend; split stdout into JPEG frames
    SetMuxChannel(ch)
    proc = subprocess.Popen(camCmdBase, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
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
# Issue #16: the templates live in <repo>/html and static assets in
# <repo>/static, not next to this file, so both are resolved from this file's
# location and work regardless of the working directory. Flask reads both
# attributes lazily (first template lookup, first static request), so
# assigning them here at import time is equivalent to passing them to Flask().
app.template_folder = TEMPLATE_DIR
app.static_folder = STATIC_DIR

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
    try:
        _camKind, camCmdBase = GetCameraBackend()
    except RuntimeError as e:
        return CameraUnavailableResponse(e)
    ch = request.args.get("ch", default="0")
    try:
        ch = int(ch)
    except ValueError:
        ch = 0
    return Response(GenerateMjpeg(ch, camCmdBase), mimetype="multipart/x-mixed-replace; boundary=frame")

@app.route("/snapshot.jpg")
def Snapshot():
    try:
        camKind, _camCmdBase = GetCameraBackend()
    except RuntimeError as e:
        return CameraUnavailableResponse(e)
    ch = int(request.args.get("ch", 0))
    w = int(request.args.get("w", 640))
    h = int(request.args.get("h", 480))
    try:
        SetMuxChannel(ch)
        if camKind == "rpicam" and FindExe("rpicam-still"):
            cmd = ["rpicam-still","-n","-o","-","--width",str(w),"--height",str(h)]
            data = subprocess.check_output(cmd, timeout=5)
        elif camKind == "libcamera" and FindExe("libcamera-jpeg"):
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
# Input validation (#13) and firmware limits (ArduinoMegaControl.ino)
# -----------------------------------------------------------------------------
FW_HZ_MIN = 0.5             # SetPulseHz/RequestHz clamp
FW_HZ_MAX = 50000.0
FW_STEPS_MAX = 2000000000   # SETSTEPS clamp; 0 means stop
FW_MAX_RPM_MIN = 1          # SETMAXRPM clamp
FW_MAX_RPM_MAX = 100000
# The firmware does not bound SETRAMP beyond >= 0. This host-side ceiling only
# keeps the command line short and the value physically meaningful.
HOST_RAMP_MAX_RPM_S = 100000.0
DEFAULT_MAX_RPM = 1200      # firmware default; used only before the first STATUS

# Accepted SETDIR tokens mapped to the canonical token we send. The firmware
# treats anything starting with CW (any case), '1' or '+' as 1 and everything
# else as 0, so only unambiguous spellings are accepted here.
DIRECTION_TOKENS = {"CW": "CW", "CCW": "CCW", "1": "CW", "0": "CCW", "+": "CW", "-": "CCW"}

class BadInput(ValueError):
    pass

def ParseFiniteFloat(name, default=None, lo=None, hi=None):
    raw = request.args.get(name, default)
    if raw is None or str(raw).strip() == "":
        raise BadInput(f"{name} is required")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise BadInput(f"{name} must be a number")
    if not math.isfinite(value):
        raise BadInput(f"{name} must be finite")
    if lo is not None and value < lo:
        raise BadInput(f"{name} must be >= {lo}")
    if hi is not None and value > hi:
        raise BadInput(f"{name} must be <= {hi}")
    return value

def ParseBoundedInt(name, lo, hi):
    raw = request.args.get(name, "")
    try:
        value = int(str(raw).strip())
    except ValueError:
        raise BadInput(f"{name} must be an integer")
    if value < lo or value > hi:
        raise BadInput(f"{name} must be between {lo} and {hi}")
    return value

def ParseFlag(name, default):
    return request.args.get(name, default).lower() in ("1", "true", "yes")

def FormatNumber(value):
    """Fixed-point text for a command argument (never exponent notation)."""
    text = ("%.4f" % value).rstrip("0").rstrip(".")
    return text if text not in ("", "-0") else "0"

def LinkErrorResponse(e):
    """Map link errors to HTTP: 400 bad command, 503 link down or not ready,
    504 no reply in time, 502 firmware error."""
    if isinstance(e, CommandRejected):
        code = 400
    elif isinstance(e, LinkDown):
        code = 503
    elif isinstance(e, LinkTimeout):
        code = 504
    elif isinstance(e, FirmwareError):
        code = 502
    else:
        code = 500
    return jsonify({"error": str(e), "link": Mega.LinkState()}), code

def BadInputResponse(e):
    return jsonify({"error": str(e)}), 400

def ReplyValue(lines, pattern):
    """Pull a value out of the firmware's acknowledgement line, or None."""
    for line in lines:
        m = re.search(pattern, line)
        if m:
            return m.group(1)
    return None

def CurrentStatusOr503():
    """Firmware STATUS from this session; raises LinkDown if none yet."""
    st = Mega.CurrentStatus()
    if st is None:
        if Mega.IsConnected():
            raise LinkNotReady("firmware state unknown since (re)connect; waiting for STATUS")
        raise LinkDown("link down")
    return st

# -----------------------------------------------------------------------------
# Max RPM (firmware STATUS max_rpm is the source of truth)
# -----------------------------------------------------------------------------
def GetMaxRpm():
    """Max RPM as last reported by the firmware's STATUS this session."""
    st = Mega.CurrentStatus()
    if st is not None and isinstance(st.get("max_rpm"), (int, float)):
        return st["max_rpm"]
    return DEFAULT_MAX_RPM

def CheckRpmLimit(rpm, override):
    """Return a 409 JSON response if rpm exceeds the limit, else None."""
    maxRpm = GetMaxRpm()
    if rpm > maxRpm and not override:
        return jsonify({
            "error": "exceeds_max_rpm",
            "requested_rpm": rpm,
            "max_rpm": maxRpm,
            "message": f"Requested {rpm} RPM exceeds limit of {maxRpm} RPM. "
                       f"Re-send with override=true to proceed.",
        }), 409
    return None

# -----------------------------------------------------------------------------
# Arduino API routes
# -----------------------------------------------------------------------------
@app.route("/ping")
def Ping():
    try:
        return jsonify({"ok": True, "response": Mega.Request("PING")})
    except LinkError as e:
        return LinkErrorResponse(e)

def SnapshotResponse(view):
    """Flatten a cached snapshot: firmware fields plus link freshness."""
    out = dict(view["data"] or {})
    out.update({
        "link": view["link"],
        "age_s": view["age_s"],
        "updated_utc": view["updated_utc"],
        "link_reason": view["reason"],
        "port": view["port"],
    })
    return out

@app.route("/status")
def Status():
    # Served from the poll cache; never touches the port (#8, #29).
    out = SnapshotResponse(Mega.StatusSnapshot())
    out["max_rpm_limit"] = GetMaxRpm()
    out["link_health"] = Mega.Health()
    return jsonify(out)

@app.route("/sensor")
def Sensor():
    # Served from the poll cache; never touches the port (#8, #29).
    return jsonify(SnapshotResponse(Mega.SensorSnapshot()))

@app.route("/start")
def Start():
    try:
        return jsonify({"ok": True, "response": Mega.Request("START")})
    except LinkError as e:
        return LinkErrorResponse(e)

@app.route("/stop")
def Stop():
    # Priority path: on the wire immediately, not queued behind polls (#8).
    try:
        return jsonify({"ok": True, **Mega.Stop("STOP")})
    except LinkError as e:
        return LinkErrorResponse(e)

@app.route("/kill")
def Kill():
    try:
        return jsonify({"ok": True, **Mega.Stop("KILL")})
    except LinkError as e:
        return LinkErrorResponse(e)

@app.route("/setFreq")
def SetFreq():
    try:
        hz = ParseFiniteFloat("hz", lo=FW_HZ_MIN, hi=FW_HZ_MAX)
    except BadInput as e:
        return BadInputResponse(e)
    override = ParseFlag("override", "0")
    autoStart = ParseFlag("start", "1")
    try:
        # RPM for the limit check uses the firmware's own PPR from STATUS.
        ppr = CurrentStatusOr503().get("ppr")
        if not isinstance(ppr, (int, float)) or ppr <= 0:
            raise FirmwareError("STATUS has no valid ppr")
        rpm = hz * 60.0 / ppr
        err = CheckRpmLimit(rpm, override)
        if err is not None:
            return err
        r1 = Mega.Request(f"SETFREQ {FormatNumber(hz)}")
        r2 = Mega.Request("START") if autoStart else []
        return jsonify({"ok": True, "setfreq": r1, "start": r2,
                        "target_hz": ReplyValue(r1, r"^OK SETFREQ (\S+)$")})
    except LinkError as e:
        return LinkErrorResponse(e)

@app.route("/setRpm")
def SetRpm():
    try:
        rpm = ParseFiniteFloat("rpm")
        if rpm <= 0:
            raise BadInput("rpm must be > 0")
    except BadInput as e:
        return BadInputResponse(e)
    override = ParseFlag("override", "0")
    autoStart = ParseFlag("start", "0")
    try:
        ppr = CurrentStatusOr503().get("ppr")
        if not isinstance(ppr, (int, float)) or ppr <= 0:
            raise FirmwareError("STATUS has no valid ppr")
        hz = rpm * ppr / 60.0
        if hz < FW_HZ_MIN or hz > FW_HZ_MAX:
            return BadInputResponse(BadInput(
                f"rpm {rpm} at {ppr} pulses/rev is {hz:.3f} Hz; the firmware "
                f"range is {FW_HZ_MIN} to {FW_HZ_MAX} Hz "
                f"({FW_HZ_MIN * 60.0 / ppr:.3f} to {FW_HZ_MAX * 60.0 / ppr:.1f} RPM)"))
        err = CheckRpmLimit(rpm, override)
        if err is not None:
            return err
        r1 = Mega.Request(f"SETRPM {FormatNumber(rpm)}")
        r2 = Mega.Request("START") if autoStart else []
        return jsonify({"ok": True, "setrpm": r1, "start": r2,
                        "target_hz": ReplyValue(r1, r"Hz=(\S+)$")})
    except LinkError as e:
        return LinkErrorResponse(e)

@app.route("/setDirection")
def SetDirection():
    val = request.args.get("value", "").strip().upper()
    if not val:
        return BadInputResponse(BadInput("value required"))
    token = DIRECTION_TOKENS.get(val)
    if token is None:
        return BadInputResponse(BadInput("value must be one of CW, CCW, 1, 0, +, -"))
    try:
        resp = Mega.Request(f"SETDIR {token}")
        return jsonify({"ok": True, "response": resp,
                        "dir": ReplyValue(resp, r"^OK SETDIR (\d)$")})
    except LinkError as e:
        return LinkErrorResponse(e)

@app.route("/setSteps")
def SetSteps():
    try:
        n = ParseBoundedInt("value", 0, FW_STEPS_MAX)
    except BadInput as e:
        return BadInputResponse(e)
    try:
        return jsonify({"ok": True, "response": Mega.Request(f"SETSTEPS {n}")})
    except LinkError as e:
        return LinkErrorResponse(e)

@app.route("/setMaxRpm")
def SetMaxRpm():
    try:
        n = ParseBoundedInt("value", FW_MAX_RPM_MIN, FW_MAX_RPM_MAX)
    except BadInput as e:
        return BadInputResponse(e)
    try:
        resp = Mega.Request(f"SETMAXRPM {n}")
        applied = ReplyValue(resp, r"^OK SETMAXRPM (\d+)$")
        # Refresh the cache so the limit mirror follows the firmware at once.
        with contextlib.suppress(LinkError):
            Mega.Request("STATUS", RequireStatus=False)
        return jsonify({"ok": True, "response": resp,
                        "max_rpm": int(applied) if applied is not None else GetMaxRpm()})
    except LinkError as e:
        return LinkErrorResponse(e)

@app.route("/setRamp")
def SetRamp():
    try:
        up = ParseFiniteFloat("up", "0", 0.0, HOST_RAMP_MAX_RPM_S)
        down = ParseFiniteFloat("down", "0", 0.0, HOST_RAMP_MAX_RPM_S)
    except BadInput as e:
        return BadInputResponse(e)
    try:
        resp = Mega.Request(f"SETRAMP {FormatNumber(up)} {FormatNumber(down)}")
        # Report what the firmware says it applied, not what was asked for.
        return jsonify({"ok": True, "response": resp,
                        "ramp_up": ReplyValue(resp, r"UP=(\S+)"),
                        "ramp_down": ReplyValue(resp, r"DOWN=(\S+)$")})
    except LinkError as e:
        return LinkErrorResponse(e)

# -----------------------------------------------------------------------------
# DAQ endpoints -- wired to LCRRecorder (BK894 over usbtmc)
# -----------------------------------------------------------------------------
@app.route("/daq/connect")
def DaqConnect():
    """Manually connect to the BK894 (auto-detects /dev/usbtmc*)."""
    device = request.args.get("device", None)
    try:
        idn = lcr.connect(device=device)
        return jsonify({"ok": True, "idn": idn, "device": lcr.device})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/daq/status")
def DaqStatus():
    """Check if the LCR meter is connected."""
    return jsonify({
        "connected": lcr.connected,
        "device": lcr.device,
        "mode": lcr.mode,
        "recording": lcr.thread is not None and lcr.thread.is_alive(),
    })

@app.route("/daq/start")
def DaqStart():
    filename = request.args.get("file", "measurements.csv")
    mode     = request.args.get("mode", "RX")
    try:
        interval = float(request.args.get("interval", "0.5"))
        freq     = float(request.args.get("freq", "1000"))
        voltage  = float(request.args.get("voltage", "1.0"))
    except ValueError:
        return jsonify({"error": "interval, freq, and voltage must be numeric"}), 400
    try:
        lcr.start(filename=filename, interval=interval,
                  mode=mode, freq=freq, voltage=voltage)
        return jsonify({"ok": True, "file": filename, "interval": interval,
                        "mode": mode, "freq": freq, "voltage": voltage})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/daq/stop")
def DaqStop():
    try:
        lcr.stop()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/daq/measure")
def DaqMeasure():
    """Take a single measurement (does not require recording to be active)."""
    try:
        if not lcr.connected:
            lcr.connect()
        primary, secondary, status = lcr.measure_once()
        return jsonify({"primary": primary, "secondary": secondary,
                        "status": status, "mode": lcr.mode})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/daq/data")
def DaqData():
    """Return the most recent measurement from the recording thread."""
    data = lcr.get_last_data()
    if data:
        ts, primary, secondary, status = data
        return jsonify({"timestamp": ts, "primary": primary,
                        "secondary": secondary, "status": status,
                        "mode": lcr.mode})
    return jsonify({"timestamp": None, "primary": None,
                    "secondary": None, "status": None})

# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    # debug=True is gone: the Werkzeug debugger allows arbitrary code
    # execution from any host that can reach port 5000. The routes are still
    # unauthenticated GETs that move the motor; see issue #14 for auth and
    # POST. threaded=True keeps /stop answerable while another request waits.
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
