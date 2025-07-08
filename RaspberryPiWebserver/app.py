# app.py

from flask import Flask, render_template, request
import requests

app = Flask(__name__)
ARDUINO_IP = '192.168.0.227'

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

if __name__ == '__main__':
    # Listen on all interfaces, port 5000
    app.run(host='0.0.0.0', port=5000, debug=False)

