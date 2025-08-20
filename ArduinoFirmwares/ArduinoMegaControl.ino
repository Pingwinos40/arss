/*
  ArduinoMegaControl.ino
  Hardware PWM step pulses + direction + optional DHT22 on Arduino Mega 2560.

  Pins:
    PUL+  -> D11 (OC1A, Timer1 hardware PWM)
    DIR+  -> D33
    DHT22 -> D53  (use ~10k pull-up to 5V on data)

  Commands (send ASCII lines ending with '\n'):
    HELP
    PING
    SETRPM <rpm>
    SETFREQ <hz>
    SETDIR <CW|CCW|0|1>
    START
    STOP
    KILL
    STATUS
    SENSOR?
    SETSTEPS <steps_per_rev>         (default 6400)
    SETMAXRPM <rpm>                  (default 600)
    SETRAMP <rpm_step> <ms_interval> (default 5 50)
*/

#include <Arduino.h>
#include <DHT.h>
#include <math.h>
#include <stdint.h>
#include <string.h>
#include <ctype.h>
#include <stdlib.h>

// ---------- Forward declarations so Arduino's auto-prototypes compile ----------
enum Prescale : uint16_t;
struct T1Config;
// (function prototypes are optional but harmless)
struct T1Config ComputeT1ForHz(uint32_t hz);
void ApplyT1Config(const T1Config& cfg);

// ================== User Pins ==================
static const uint8_t PulPin = 11;  // OC1A hardware PWM
static const uint8_t DirPin = 33;  // Direction
static const uint8_t EnvPin = 53;  // DHT22

// ================== DHT ========================
#define DHTTYPE DHT22
DHT Dht(EnvPin, DHTTYPE);
float LastTempC = NAN, LastHumidity = NAN;

// ================== Motion Params ==============
volatile uint32_t StepsPerRevolution = 6400; // microsteps per rev
volatile uint16_t MaxRpm = 600;
volatile uint16_t RpmStep = 5;       // ramp delta per tick
volatile uint16_t RampIntervalMs = 50;

volatile float TargetRpm = 0.0f;
volatile float CurrentRpm = 0.0f;
volatile uint32_t PulseHz = 0;

volatile bool MotorRunning = false;     // actively producing pulses
volatile bool OutputConnected = false;  // OC1A routed to D11

// ================== Serial Parsing =============
static const size_t CmdBufferSize = 96;
char CmdBuffer[CmdBufferSize];
size_t CmdLen = 0;

// ================== Small Helpers ==============
static inline float ClampF(float v, float lo, float hi) {
  if (v < lo) return lo;
  if (v > hi) return hi;
  return v;
}
static inline bool NearlyEqualF(float a, float b, float eps) {
  return fabsf(a - b) <= eps;
}

// ================== Timer1 Helpers =============
// Timer1 Fast PWM, TOP = ICR1, f = F_CPU / (N * (1 + ICR1))
enum Prescale : uint16_t { PS1=1, PS8=8, PS64=64, PS256=256, PS1024=1024 };

struct T1Config {
  uint16_t Icr1;
  Prescale Ps;
  bool Valid;
};

T1Config ComputeT1ForHz(uint32_t hz) {
  T1Config cfg; cfg.Icr1 = 0; cfg.Ps = PS1; cfg.Valid = false;
  if (hz == 0) return cfg;

  const uint32_t fcpu = F_CPU;
  const Prescale choices[5] = { PS1, PS8, PS64, PS256, PS1024 };

  for (uint8_t i = 0; i < 5; i++) {
    uint32_t denom = (uint32_t)choices[i] * hz;
    if (!denom) continue;
    uint32_t icr = (fcpu / denom);
    if (icr > 0) icr -= 1;
    if (icr >= 2 && icr <= 65535UL) {
      cfg.Icr1 = (uint16_t)icr;
      cfg.Ps = choices[i];
      cfg.Valid = true;
      return cfg;
    }
  }

  // Fallback clamp with largest prescaler
  uint32_t denom = (uint32_t)PS1024 * hz;
  if (denom) {
    uint32_t icr = (fcpu / denom);
    if (icr > 0) icr -= 1;
    if (icr < 1) icr = 1;
    if (icr > 65535UL) icr = 65535UL;
    cfg.Icr1 = (uint16_t)icr;
    cfg.Ps = PS1024;
    cfg.Valid = true;
  }
  return cfg;
}

void DisconnectPwm() {
  // Disconnect OC1A from pin and force LOW.
  TCCR1A &= ~((1 << COM1A1) | (1 << COM1A0));
  pinMode(PulPin, OUTPUT);
  digitalWrite(PulPin, LOW);
  OutputConnected = false;
}

void ConnectPwmNonInverting() {
  // Non-inverting OC1A output
  TCCR1A = (TCCR1A & ~((1 << COM1A1) | (1 << COM1A0))) | (1 << COM1A1);
  OutputConnected = true;
}

void ApplyT1Config(const T1Config& cfg) {
  if (!cfg.Valid) {
    TCCR1A = 0;
    TCCR1B = 0;
    TCNT1  = 0;
    DisconnectPwm();
    return;
  }

  // Stop counter while reconfiguring
  TCCR1A = 0;
  TCCR1B = 0;
  TCNT1  = 0;

  // Mode 14: Fast PWM, TOP = ICR1 (WGM13:0 = 1110)
  TCCR1A |= (1 << WGM11);
  TCCR1B |= (1 << WGM13) | (1 << WGM12);

  ICR1 = cfg.Icr1;

  // 50% duty
  uint32_t topPlus1 = (uint32_t)cfg.Icr1 + 1;
  OCR1A = (uint16_t)(topPlus1 / 2);

  // Prescaler
  switch (cfg.Ps) {
    case PS1:    TCCR1B |= (1 << CS10); break;
    case PS8:    TCCR1B |= (1 << CS11); break;
    case PS64:   TCCR1B |= (1 << CS11) | (1 << CS10); break;
    case PS256:  TCCR1B |= (1 << CS12); break;
    case PS1024: TCCR1B |= (1 << CS12) | (1 << CS10); break;
  }
}

void UpdateFromCurrentRpm() {
  float rpm = CurrentRpm;
  uint32_t hz = (rpm <= 0.0f) ? 0u : (uint32_t)((StepsPerRevolution * rpm) / 60.0f + 0.5f);
  PulseHz = hz;

  T1Config cfg = ComputeT1ForHz(hz);
  ApplyT1Config(cfg);

  if (hz == 0) {
    DisconnectPwm();
  } else if (!OutputConnected && MotorRunning) {
    ConnectPwmNonInverting();
  }
}

// ================== Direction & Ramping =====================
void SetDirection(bool cw) { digitalWrite(DirPin, cw ? HIGH : LOW); }

void SetTargetRpm(float rpm) {
  rpm = ClampF(rpm, 0.0f, (float)MaxRpm);
  TargetRpm = rpm;
}

void SetCurrentRpm(float rpm) {
  rpm = ClampF(rpm, 0.0f, (float)MaxRpm);
  CurrentRpm = rpm;
  UpdateFromCurrentRpm();
}

unsigned long LastRampTick = 0;
void ServiceRamp() {
  unsigned long now = millis();
  if (now - LastRampTick < RampIntervalMs) return;
  LastRampTick = now;

  float cur = CurrentRpm, tgt = TargetRpm;
  if (NearlyEqualF(cur, tgt, 1e-3f)) {
    if (tgt <= 0.0f) {
      MotorRunning = false;
      DisconnectPwm();
    }
    return;
  }
  float step = (tgt > cur) ? (float)RpmStep : -(float)RpmStep;
  float next = cur + step;
  if ((step > 0 && next > tgt) || (step < 0 && next < tgt)) next = tgt;
  if (next > 0.0f) MotorRunning = true;
  SetCurrentRpm(next);
}

// ================== Env Sensor ==============================
bool ReadEnvOnce(float& tC, float& h) {
  float hR = Dht.readHumidity();
  float t  = Dht.readTemperature();
  if (isnan(hR) || isnan(t)) return false;
  tC = t; h = hR; return true;
}

// ================== Serial Helpers ==========================
void PrintHelp() {
  Serial.println(F("OK CMDS: HELP, PING, SETRPM <rpm>, SETFREQ <hz>, SETDIR <CW|CCW|0|1>, START, STOP, KILL, STATUS, SENSOR?, SETSTEPS <n>, SETMAXRPM <n>, SETRAMP <rpm_step> <ms>"));
}

void PrintStatus() {
  Serial.print(F("{\"status\":\""));
  Serial.print(MotorRunning ? F("RUNNING") : F("STOPPED"));
  Serial.print(F("\",\"rpm_cur\":")); Serial.print(CurrentRpm, 3);
  Serial.print(F(",\"rpm_tgt\":"));  Serial.print(TargetRpm, 3);
  Serial.print(F(",\"pulse_hz\":")); Serial.print(PulseHz);
  Serial.print(F(",\"dir\":\""));    Serial.print((digitalRead(DirPin)==HIGH)?F("CW"):F("CCW"));
  Serial.print(F("\",\"temp_c\":")); if (isnan(LastTempC)) Serial.print(F("null")); else Serial.print(LastTempC, 2);
  Serial.print(F(",\"humidity\":")); if (isnan(LastHumidity)) Serial.print(F("null")); else Serial.print(LastHumidity, 2);
  Serial.println(F("}"));
}

static void TrimLeading(char*& p) { while (*p==' ' || *p=='\t') ++p; }

void HandleCommand(char* line) {
  // Uppercase the command keyword only (first token) for easy matching
  char* p = line; TrimLeading(p);
  char* end = p;
  while (*end && *end!=' ' && *end!='\t' && *end!='\r' && *end!='\n') { *end = toupper(*end); ++end; }
  char saved = *end; *end = '\0';
  const char* cmd = p;
  char* args = end; *end = saved;

  if (!strcmp(cmd, "HELP"))   { PrintHelp(); return; }
  if (!strcmp(cmd, "PING"))   { Serial.println(F("PONG")); return; }

  if (!strcmp(cmd, "START"))  {
    if (TargetRpm > 0.0f) { MotorRunning = true; UpdateFromCurrentRpm(); ConnectPwmNonInverting(); }
    Serial.println(F("OK")); return;
  }
  if (!strcmp(cmd, "STOP"))   { SetTargetRpm(0.0f); Serial.println(F("OK")); return; }
  if (!strcmp(cmd, "KILL"))   {
    MotorRunning = false; TargetRpm = 0.0f; SetCurrentRpm(0.0f); DisconnectPwm(); Serial.println(F("OK")); return;
  }
  if (!strcmp(cmd, "STATUS")) { PrintStatus(); return; }

  if (!strcmp(cmd, "SETRPM")) {
    char* q = args; TrimLeading(q);
    long v = strtol(q, nullptr, 10);
    SetTargetRpm((float)v);
    Serial.println(F("OK")); return;
  }

  if (!strcmp(cmd, "SETFREQ")) {
    char* q = args; TrimLeading(q);
    double hz = strtod(q, nullptr);
    if (hz < 0.0) { Serial.println(F("ERR")); return; }
    double rpm = (hz * 60.0) / (double)StepsPerRevolution;
    SetTargetRpm((float)rpm);
    Serial.println(F("OK")); return;
  }

  if (!strcmp(cmd, "SETDIR")) {
    char* q = args; TrimLeading(q);
    char tok[8] = {0};
    sscanf(q, " %7s", tok);
    for (char* s = tok; *s; ++s) *s = toupper(*s);
    if (!strcmp(tok, "CW") || !strcmp(tok, "1")) { SetDirection(true);  Serial.println(F("OK")); return; }
    if (!strcmp(tok, "CCW")|| !strcmp(tok, "0")) { SetDirection(false); Serial.println(F("OK")); return; }
    Serial.println(F("ERR")); return;
  }

  if (!strcmp(cmd, "SETSTEPS")) {
    char* q = args; TrimLeading(q);
    unsigned long n = strtoul(q, nullptr, 10);
    if (n == 0 || n > 100000UL) { Serial.println(F("ERR")); return; }
    StepsPerRevolution = n; UpdateFromCurrentRpm(); Serial.println(F("OK")); return;
  }

  if (!strcmp(cmd, "SETMAXRPM")) {
    char* q = args; TrimLeading(q);
    unsigned long n = strtoul(q, nullptr, 10);
    if (n == 0 || n > 100000UL) { Serial.println(F("ERR")); return; }
    MaxRpm = (uint16_t)n;
    if (TargetRpm > MaxRpm) TargetRpm = MaxRpm;
    if (CurrentRpm > MaxRpm) SetCurrentRpm((float)MaxRpm);
    Serial.println(F("OK")); return;
  }

  if (!strcmp(cmd, "SETRAMP")) {
    char* q = args; TrimLeading(q);
    char* endptr = nullptr;
    long step = strtol(q, &endptr, 10);
    long ms   = strtol(endptr, nullptr, 10);
    if (step <= 0 || step > 10000 || ms < 5 || ms > 10000) { Serial.println(F("ERR")); return; }
    RpmStep = (uint16_t)step; RampIntervalMs = (uint16_t)ms; Serial.println(F("OK")); return;
  }

  if (!strcmp(cmd, "SENSOR?")) {
    if (MotorRunning) Serial.println(F("WARN:RUNNING"));
    float t, h;
    if (ReadEnvOnce(t, h)) {
      LastTempC = t; LastHumidity = h;
      Serial.print(F("ENV ")); Serial.print(t, 2); Serial.print(F(" C, ")); Serial.print(h, 2); Serial.println(F(" %RH"));
    } else {
      Serial.println(F("ENV ERR"));
    }
    return;
  }

  Serial.println(F("ERR"));
}

// ================== Setup/Loop ==============================
void SetupPins() {
  pinMode(PulPin, OUTPUT); digitalWrite(PulPin, LOW); // OC1A pin
  pinMode(DirPin, OUTPUT); digitalWrite(DirPin, LOW); // CCW default
}

void setup() {
  Serial.begin(115200);
  SetupPins();
  Dht.begin();
  DisconnectPwm(); // idle quiet
  Serial.println(F("READY (PUL=D11 hardware PWM, DIR=D33, DHT=D53)"));
  PrintHelp();
}

void loop() {
  // Serial line buffering
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\r') continue;
    if (c == '\n') {
      if (CmdLen >= CmdBufferSize) CmdLen = 0;
      CmdBuffer[CmdLen] = '\0';
      HandleCommand(CmdBuffer);
      CmdLen = 0;
    } else {
      if (CmdLen < (CmdBufferSize - 1)) CmdBuffer[CmdLen++] = c;
      else { CmdLen = 0; Serial.println(F("ERR")); }
    }
  }

  // Ramp
  ServiceRamp();

  // Opportunistic env refresh while stopped
  static unsigned long lastEnv = 0;
  if (!MotorRunning) {
    unsigned long now = millis();
    if (now - lastEnv > 2000UL) {
      float t, h; if (ReadEnvOnce(t, h)) { LastTempC = t; LastHumidity = h; }
      lastEnv = now;
    }
  }
}
