# app.py


from flask import Flask, render_template, request, Response
import subprocess
import requests
from lcr_controller import LCRRecorder


app = Flask(__name__)
ARDUINO_IP = '192.168.0.227'


def generate_mjpeg():
    cmd = [
        "libcamera-vid",
        "-t", "0",
        "--inline",
        "--codec", "mjpeg",
        "--width", "640",
        "--height", "480",
        "-o", "-"
    ]

    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, bufsize=0)

    buffer = b""
    try:
        while True:
            byte = process.stdout.read(1)
            if not byte:
                break
            buffer += byte

            # Check for end of JPEG frame
            if buffer[-2:] == b'\xFF\xD9':  # JPEG EOI marker
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' +
                       buffer + b'\r\n')
                buffer = b""
    except GeneratorExit:
        process.terminate()
        process.wait()
    # Start libcamera-vid in MJPEG stream mode
    cmd = [
        "libcamera-vid",
        "-t", "0",              # unlimited time
        "--inline",             # required for MJPEG
        "--codec", "mjpeg",
        "--width", "640",
        "--height", "480",
        "-o", "-"               # output to stdout
    ]


    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, bufsize=0)

    def find_jpeg_frames(buffer):
        frames = []
        start = 0
        while True:
            soi = buffer.find(b'\xff\xd8', start)
            if soi == -1:
                break
            eoi = buffer.find(b'\xff\xd9', soi)
            if eoi == -1:
                break
            frames.append(buffer[soi:eoi+2])
            start = eoi + 2
        return frames, buffer[start:]

    buffer = b''
    try:
        while True:
            chunk = process.stdout.read(4096)
            if not chunk:
                break
            buffer += chunk
            frames, buffer = find_jpeg_frames(buffer)
            for frame in frames:
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' +
                       frame + b'\r\n')
    except GeneratorExit:
        process.terminate()
        process.wait()

@app.route('/video_feed')
def video_feed():
    return Response(generate_mjpeg(),
                    mimetype='multipart/x-mixed-replace; boundary=frame')


@app.route('/')
def index():
    # Render the main page; the form should now have an input named "freq"
    return render_template('index.html')

@app.route('/send', methods=['POST'])
def send():
    # Grab the desired frequency (Hz) from the form
    freq = request.form.get('freq', '').strip()
    if not freq:
        status = "ERROR: No frequency provided."
        return render_template('index.html', status=status)

    try:
        # Send to the new /setFreq endpoint on the Arduino
        r = requests.get(f"http://{ARDUINO_IP}/setFreq?value={freq}", timeout=2)
        status = r.text
    except Exception as e:
        status = f"ERROR: {e}"
    return render_template('index.html', status=status)

@app.route('/stop', methods=['POST'])
def stop():
    try:
        r = requests.get(f"http://{ARDUINO_IP}/stop", timeout=2)
        status = r.text
    except Exception as e:
        status = f"ERROR: {e}"
    return render_template('index.html', status=status)

@app.route('/direction', methods=['POST'])
def direction():
    dir_value = request.form.get('direction', '').strip().upper()
    if dir_value not in ['CW', 'CCW']:
        status = "ERROR: Invalid direction."
    else:
        try:
            r = requests.get(f"http://{ARDUINO_IP}/setDirection?value={dir_value}", timeout=2)
            status = r.text
        except Exception as e:
            status = f"ERROR: {e}"
    return render_template('index.html', status=status)

@app.route('/status')
def status():
    try:
        r = requests.get(f"http://{ARDUINO_IP}/status", timeout=1)
        return r.text
    except Exception:
        return "STATUS:OFFLINE"

# SCPI Communication for Flask Webserver

from flask import Flask, request, render_template, jsonify


lcr = LCRRecorder()

@app.route("/daq")
def daq_dashboard():
    return render_template("daq.html")

import traceback

@app.route("/daq/start", methods=["POST"])
def daq_start():
    print("== DAQ start form data ==", request.form.to_dict())
    try:
        lcr.connect()
        lcr.configure(
            request.form["mode"],
            float(request.form["freq"]),
            float(request.form["voltage"]),
            request.form["speed"]
        )
        lcr.start(
            request.form["filename"],
            float(request.form["interval"]),
            request.form["mode"]
        )
        return "OK"
    except Exception as e:
        traceback.print_exc()                # full trace to console
        return f"ERROR: {e}", 400

@app.route("/daq/stop", methods=["POST"])
def daq_stop():
    lcr.stop()
    return "Stopped"

@app.route("/daq/data")
def daq_data():
    data = lcr.get_last_data()
    return jsonify({"timestamp": data[0], "value": data[1]} if data else {})




if __name__ == '__main__':
    # Listen on all interfaces, port 5000
    app.run(host='0.0.0.0', port=5000, debug=True)

    