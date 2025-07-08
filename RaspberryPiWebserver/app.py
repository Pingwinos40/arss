from flask import Flask, render_template, request
import requests

app = Flask(__name__)
ARDUINO_IP = '192.168.0.227'

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/send', methods=['POST'])
def send():
    rpm = request.form['rpm']
    try:
        r = requests.get(f"http://{ARDUINO_IP}/setRPM?value={rpm}")
        status = r.text
    except Exception as e:
        status = f"ERROR: {str(e)}"
    return render_template('index.html', status=status)

@app.route('/stop', methods=['POST'])
def stop():
    try:
        r = requests.get(f"http://{ARDUINO_IP}/stop")
        status = r.text
    except Exception as e:
        status = f"ERROR: {str(e)}"
    return render_template('index.html', status=status)

@app.route('/direction', methods=['POST'])
def direction():
    dir_value = request.form['direction'].strip().upper()
    if dir_value not in ['CW', 'CCW']:
        status = "Invalid direction."
    else:
        try:
            r = requests.get(f"http://{ARDUINO_IP}/setDirection?value={dir_value}")
            status = r.text
        except Exception as e:
            status = f"Error setting direction: {str(e)}"
    return render_template('index.html', status=status)

@app.route('/status')
def status():
    try:
        r = requests.get(f"http://{ARDUINO_IP}/status", timeout=1)
        return r.text
    except Exception:
        return "STATUS:OFFLINE"

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
