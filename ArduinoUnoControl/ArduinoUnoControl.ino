#include <WiFiS3.h>
#include "FspTimer.h"
#include "DHT.h"

const char* SSID = "REDACTED";
const char* Password = "REDACTED";
WiFiServer MyServer(80);

const int PulsePin = 13;
const int DirPin = 12;
const int StepsPerRevolution = 6400;
const int MaxRpm = 600;

const int RpmStep = 5;
const int RampIntervalMs = 50;

float frequency = 0;

volatile int TargetRpm = 0;
volatile int CurrentRpm = 0;
volatile unsigned long PulseDelayUs = 0;
volatile bool MotorRunning = false;
volatile bool PulseState = false;
String Direction = "CW";
String Status = "STOPPED";

unsigned long LastRampTime = 0;

FspTimer StepperTimer;

// ===== DHT Sensor Setup =====
#define DHTPIN 2
#define DHTTYPE DHT22
DHT dht(DHTPIN, DHTTYPE);
float tempC = 0.0;
float humidity = 0.0;
unsigned long lastSensorRead = 0;

// ===== Timer Overflow Callback =====
void TimerCallback(timer_callback_args_t *p_args) {
  if (MotorRunning && PulseDelayUs > 0) {
    PulseState = !PulseState;
    digitalWrite(PulsePin, PulseState);
  }
}

// ===== Begin Hardware Timer for Stepper Control =====
bool BeginStepperTimer(float frequency) {
  uint8_t timerType = GPT_TIMER;
  int8_t tIndex = FspTimer::get_available_timer(timerType);
  if (tIndex < 0) tIndex = FspTimer::get_available_timer(timerType, true);
  if (tIndex < 0) return false;
  FspTimer::force_use_of_pwm_reserved_timer();
  if (!StepperTimer.begin(TIMER_MODE_PERIODIC, timerType, tIndex, frequency, 0.0f, TimerCallback)) return false;
  if (!StepperTimer.setup_overflow_irq()) return false;
  if (!StepperTimer.open()) return false;
  if (!StepperTimer.start()) return false;
  return true;
}

// ===== Update PWM Frequency Based on RPM =====
void UpdateTimerFrequencyFromRpm(int rpm) {
  float rps = rpm / 60.0;
  float pulsesPerSecond = rps * StepsPerRevolution;
  PulseDelayUs = 1000000UL / (2 * pulsesPerSecond); // Each pulse is HIGH/LOW
  float frequency = 1000000.0 / PulseDelayUs;

  StepperTimer.end();
  BeginStepperTimer(frequency);

  // Send frequency to Nano via UART
  Serial1.println((int)frequency);
}

void setup() {
  Serial.begin(115200);    // USB debugging
  Serial1.begin(9600);     // UART to Nano
  delay(2000);             // Let Nano finish booting

  pinMode(PulsePin, OUTPUT);
  pinMode(DirPin, OUTPUT);
  digitalWrite(DirPin, HIGH);

  dht.begin();

  WiFi.begin(SSID, Password);
  while (WiFi.status() != WL_CONNECTED) {
    delay(1000);
    Serial.println("Connecting to WiFi...");
  }

  MyServer.begin();
  Serial.println("Server started.");
}

void loop() {
  // ===== Periodic DHT Reading =====
  if (millis() - lastSensorRead > 2000) {
    lastSensorRead = millis();
    float t = dht.readTemperature();
    float h = dht.readHumidity();
    if (!isnan(t)) tempC = t;
    if (!isnan(h)) humidity = h;
  }

  // ===== Handle Web Requests =====
  WiFiClient Client = MyServer.available();
  if (Client) {
    String Request = Client.readStringUntil('\r');
    Serial.print("⟪HTTP REQ⟫ ");
    Serial.println(Request);

    Client.read();                 // consume the '\n'
    while (Client.available())     // discard rest of headers
      Client.read();

    String Response = "INVALID_COMMAND";

  if (Request.indexOf("GET /setFreq?value=") >= 0) {
    int idx         = Request.indexOf('=') + 1;
    float freqInput = Request.substring(idx).toFloat();        // Hz
    int   rpm       = int(freqInput * 42.5 + 0.5);            // rpm = Hz × 42.5

    if (rpm > 0 && rpm <= MaxRpm) {
      TargetRpm   = rpm;
      frequency   = freqInput;                                // store for status
      MotorRunning = true;
      Status      = "ACCELERATING";
      Response    = "TARGET_FREQ_SET_TO:" + String(freqInput, 2) + "Hz";
    } else {
      TargetRpm   = 0;
      Status      = "STOPPING";
      Response    = "INVALID_FREQUENCY";
    }
  }

    if (Request.indexOf("GET /stop") >= 0) {
      TargetRpm = 0;
      Status = "DECELERATING";
      Response = "RAMPING_DOWN";
    }

    if (Request.indexOf("GET /setDirection?value=") >= 0) {
      int idx = Request.indexOf("=") + 1;
      String dir = Request.substring(idx);
      dir.trim(); dir.toUpperCase();
      if (dir == "CW") {
        Direction = "CW";
        digitalWrite(DirPin, HIGH);
        Response = "DIRECTION_SET_TO:CW";
      } else if (dir == "CCW") {
        Direction = "CCW";
        digitalWrite(DirPin, LOW);
        Response = "DIRECTION_SET_TO:CCW";
      } else {
        Response = "INVALID_DIRECTION";
      }
    }

    if (Request.indexOf("GET /status") >= 0) {
      Response = "RPM:" + String(CurrentRpm) +
                 ",RUNNING:" + String(MotorRunning ? "YES" : "NO") +
                 ",DIR:" + Direction +
                 ",STATUS:" + Status +
                 ",TEMP(C):" + String(tempC, 1) +
                 ",HUM(rel.):" + String(humidity, 1) +
                 ",FREQ(Hz):" + String(frequency, 1);
    }

    Client.println("HTTP/1.1 200 OK");
    Client.println("Content-Type: text/plain");
    Client.println("Connection: close");
    Client.println();
    Client.println(Response);
    Client.stop();
  }

  // ===== Handle Stepper Ramping =====
  if (MotorRunning && millis() - LastRampTime >= RampIntervalMs) {
    LastRampTime = millis();

    if (CurrentRpm < TargetRpm) {
      CurrentRpm += RpmStep;
      if (CurrentRpm > TargetRpm) CurrentRpm = TargetRpm;
      UpdateTimerFrequencyFromRpm(CurrentRpm);
      Status = "ACCELERATING";
    } else if (CurrentRpm > TargetRpm) {
      CurrentRpm -= RpmStep;
      if (CurrentRpm < TargetRpm) CurrentRpm = TargetRpm;
      UpdateTimerFrequencyFromRpm(CurrentRpm);
      Status = "DECELERATING";
    } else {
      if (CurrentRpm == 0) {
        MotorRunning = false;
        StepperTimer.end();
        digitalWrite(PulsePin, LOW);
        Status = "STOPPED";

        // ✅ Tell Nano to stop PWM
        Serial1.println(0);
      } else {
        Status = "RUNNING";
      }
    }
  }
}