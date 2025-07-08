# mjpeg_stream.py

from flask import Flask, Response
import subprocess

app = Flask(__name__)

def generate_mjpeg():
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

    try:
        while True:
            # Read the JPEG frame by scanning for JPEG markers
            data = process.stdout.read(4096)
            if data:
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' +
                       data + b'\r\n')
    except GeneratorExit:
        process.terminate()
        process.wait()

@app.route('/video_feed')
def video_feed():
    return Response(generate_mjpeg(),
                    mimetype='multipart/x-mixed-replace; boundary=frame')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, threaded=True)
