// ArduinoMegaControl.ino
// Arduino Mega 2560: hardware pulse output on D11 (Timer1 OC1A)
// ENV sensor: DHT22 on D53
// DIR: D33
// Serial command set matches the Flask app.

#include <Arduino.h>
#include <DHT.h>
#include <math.h>

// ---------------- Pin Map ----------------
static const uint8_t PIN_PUL = 11;  // D11 = OC1A (Timer1)
static const uint8_t PIN_DIR = 33;  // D33 = direction
static const uint8_t PIN_ENV = 53;  // D53 = DHT22 data

// ---------------- DHT ----------------
#define DHTTYPE DHT22
DHT dht(PIN_ENV, DHTTYPE);

// ---------------- Motion State ----------------
volatile bool     gRunning = false;
volatile uint32_t gToggleCount = 0;        // compare-match toggles on OC1A
volatile uint32_t gToggleTarget = 0;       // goal toggles when SETSTEPS is used
volatile bool     gUseTarget = false;      // whether to stop after N steps

// Frequency / RPM bookkeeping (not volatile; read/written under stop/start)
double   gCurrentHz = 0.0;
uint16_t gCurrentOCR1A = 0;
uint8_t  gCurrentCSBits = 0; // CS10..12
long     gPulsesPerRev = 200; // set with SETPPR; default full-step 200
long     gMaxRPM = 1200;      // stored; not enforced here
int      gDir = 0;            // 0 = CCW, 1 = CW (arbitrary convention)

// Simple ramp storage (not enforced in this minimal build)
int gRampRpmStep = 0;
int gRampMs = 0;

// ENV cache to avoid slow reads on every STATUS
unsigned long gLastEnvMs = 0;
float gLastTempC = NAN, gLastRH = NAN;

// ---------------- Timer1 helpers ----------------
// Compute Timer1 CTC toggle config for desired Hz on OC1A.
// f = F_CPU / (2 * prescaler * (1 + OCR1A))
// Returns true if configured; fills OCR1A and CS bits (CS10..12).
bool ComputeT1ForHz(double hz, uint16_t &ocrOut, uint8_t &csBitsOut) {
  if (hz <= 0.1) hz = 0.1;
  const struct Presc { uint16_t presc; uint8_t csBits; } table[] = {
    {1,   _BV(CS10)},
    {8,   _BV(CS11)},
    {64,  _BV(CS11)|_BV(CS10)},
    {256, _BV(CS12)},
    {1024,_BV(CS12)|_BV(CS10)},
  };
  for (auto &p : table) {
    double ocrf = (F_CPU / (2.0 * (double)p.presc * hz)) - 1.0;
    if (ocrf < 0) continue;
    uint32_t ocr = (uint32_t)lround(ocrf);
    if (ocr <= 65535) {
      if (ocr == 0) ocr = 1; // keep clean 50% toggle
      ocrOut = (uint16_t)ocr;
      csBitsOut = p.csBits;
      return true;
    }
  }
  // If we get here, requested Hz is too low; force max divider
  uint16_t presc = 1024;
  double ocrf = (F_CPU / (2.0 * (double)presc * hz)) - 1.0;
  if (ocrf < 1) ocrf = 1;
  uint32_t ocr = (uint32_t)lround(ocrf);
  if (ocr > 65535) ocr = 65535;
  ocrOut = (uint16_t)ocr;
  csBitsOut = _BV(CS12)|_BV(CS10);
  return true;
}

// Apply Timer1 config but DO NOT start toggling OC1A yet.
void ApplyT1(uint16_t ocr, uint8_t csBits) {
  // Stop output toggling while reconfiguring
  TCCR1A &= ~_BV(COM1A0); // toggle off
  gRunning = false;

  // CTC mode, TOP=OCR1A
  // WGM13:0 = 0100 -> WGM12=1, others 0
  TCCR1A &= ~(_BV(WGM11) | _BV(WGM10));
  TCCR1B &= ~_BV(WGM13);
  TCCR1B |= _BV(WGM12);

  // Load compare
  OCR1A = ocr;

  // Clock select
  TCCR1B &= ~(_BV(CS12) | _BV(CS11) | _BV(CS10));
  TCCR1B |= csBits;

  // Enable compare A interrupt (used for SETSTEPS counting)
  TIMSK1 |= _BV(OCIE1A);
}

// Start toggling OC1A (D11). 50% duty through hardware.
void StartPulseOutput() {
  // Ensure pin is output
  pinMode(PIN_PUL, OUTPUT);
  // Toggle OC1A on compare
  TCCR1A |= _BV(COM1A0);
  gRunning = true;
}

// Stop toggling OC1A.
void StopPulseOutput() {
  TCCR1A &= ~_BV(COM1A0);
  gRunning = false;
  gUseTarget = false;
}

// Safe setter for frequency (does not force start)
void SetPulseHz(double hz) {
  // Limit to a sane band for typical stepper drivers
  if (hz < 0.5) hz = 0.5;
  if (hz > 50000.0) hz = 50000.0;

  uint16_t ocr;
  uint8_t  cs;
  ComputeT1ForHz(hz, ocr, cs);
  ApplyT1(ocr, cs);
  gCurrentHz = hz;
  gCurrentOCR1A = ocr;
  gCurrentCSBits = cs;
}

// Count toggles for SETSTEPS and stop at target.
ISR(TIMER1_COMPA_vect) {
  if (!gUseTarget) return;
  gToggleCount++;
  if (gToggleCount >= gToggleTarget) {
    // Stop on next ISR context switch
    TCCR1A &= ~_BV(COM1A0);
    gRunning = false;
    gUseTarget = false;
  }
}

// ---------------- Utils ----------------
static inline long ClampLong(long v, long lo, long hi) {
  if (v < lo) return lo;
  if (v > hi) return hi;
  return v;
}

void UpdateEnvCache() {
  unsigned long now = millis();
  if (now - gLastEnvMs < 1000) return; // <=1 Hz reads; DHT is slow
  gLastEnvMs = now;
  float t = dht.readTemperature(); // C
  float h = dht.readHumidity();
  if (!isnan(t)) gLastTempC = t;
  if (!isnan(h)) gLastRH = h;
}

// ---------------- Command Handling ----------------
void PrintStatus() {
  UpdateEnvCache();
  // Steps remaining when in targeted move:
  uint32_t stepsRemain = 0;
  if (gUseTarget) {
    uint32_t togglesDone = gToggleCount;
    uint32_t tgt = gToggleTarget;
    if (togglesDone >= tgt) stepsRemain = 0;
    else                   stepsRemain = (tgt - togglesDone) / 2U;
  }

  // Compute RPM from Hz if PPR > 0
  double rpm = (gPulsesPerRev > 0) ? (gCurrentHz * 60.0 / (double)gPulsesPerRev) : 0.0;

  Serial.print("{\"running\":");
  Serial.print(gRunning ? "true" : "false");
  Serial.print(",\"hz\":");     Serial.print(gCurrentHz, 3);
  Serial.print(",\"rpm\":");    Serial.print(rpm, 2);
  Serial.print(",\"dir\":");    Serial.print(gDir);
  Serial.print(",\"ppr\":");    Serial.print(gPulsesPerRev);
  Serial.print(",\"steps_remaining\":"); Serial.print(stepsRemain);
  Serial.print(",\"env\":{");
  Serial.print("\"temp_c\":");  if (isnan(gLastTempC)) Serial.print("null"); else Serial.print(gLastTempC, 1);
  Serial.print(",\"humidity\":"); if (isnan(gLastRH)) Serial.print("null"); else Serial.print(gLastRH, 1);
  Serial.print("}}");
  Serial.println();
}

void HandleLine(char *line) {
  // Trim leading spaces
  while (*line == ' ' || *line == '\t') line++;
  if (*line == 0) return;

  if (!strcmp(line, "PING")) {
    Serial.println("PONG");
    return;
  }
  if (!strcmp(line, "STATUS")) {
    PrintStatus();
    return;
  }
  if (!strcmp(line, "START")) {
    StartPulseOutput();
    Serial.println("OK START");
    return;
  }
  if (!strcmp(line, "STOP") || !strcmp(line, "KILL")) {
    StopPulseOutput();
    Serial.println("OK STOP");
    return;
  }
  if (!strncmp(line, "SETFREQ", 7)) {
    double hz = atof(line + 7);
    SetPulseHz(hz);
    Serial.print("OK SETFREQ "); Serial.println(gCurrentHz, 3);
    return;
  }
  if (!strncmp(line, "SETRPM", 6)) {
    double rpm = atof(line + 6);
    rpm = max(0.0, rpm);
    double hz = rpm * (double)gPulsesPerRev / 60.0;
    SetPulseHz(hz);
    Serial.print("OK SETRPM "); Serial.print(rpm, 2);
    Serial.print(" Hz=");        Serial.println(gCurrentHz, 3);
    return;
  }
  if (!strncmp(line, "SETPPR", 6)) {
    long ppr = atol(line + 6);
    gPulsesPerRev = ClampLong(ppr, 1, 200000);
    Serial.print("OK SETPPR "); Serial.println(gPulsesPerRev);
    return;
  }
  if (!strncmp(line, "SETDIR", 6)) {
    const char *p = line + 6;
    while (*p == ' ' || *p == '\t') p++;
    int val = 0;
    if (!strncasecmp(p, "CW", 2) || *p == '1' || *p == '+') val = 1;
    else val = 0;
    gDir = val;
    digitalWrite(PIN_DIR, val ? HIGH : LOW);
    Serial.print("OK SETDIR "); Serial.println(val);
    return;
  }
  if (!strncmp(line, "SETSTEPS", 8)) {
    long steps = atol(line + 8);
    steps = ClampLong(steps, 0, 2000000000L);
    if (steps <= 0) {
      StopPulseOutput();
      Serial.println("OK SETSTEPS 0");
      return;
    }
    noInterrupts();
    gToggleCount = 0;
    gToggleTarget = (uint32_t)steps * 2UL; // each pulse is 2 toggles
    gUseTarget = true;
    interrupts();
    StartPulseOutput();
    Serial.print("OK SETSTEPS "); Serial.println(steps);
    return;
  }
  if (!strncmp(line, "SETMAXRPM", 9)) {
    long rpm = atol(line + 9);
    gMaxRPM = ClampLong(rpm, 1, 100000);
    Serial.print("OK SETMAXRPM "); Serial.println(gMaxRPM);
    return;
  }
  if (!strncmp(line, "SETRAMP", 7)) {
    int rs = 0, ms = 0;
    sscanf(line + 7, "%d %d", &rs, &ms);
    gRampRpmStep = max(0, rs);
    gRampMs = max(0, ms);
    Serial.print("OK SETRAMP "); Serial.print(gRampRpmStep);
    Serial.print(" "); Serial.println(gRampMs);
    return;
  }
  if (!strcmp(line, "SENSOR?")) {
    UpdateEnvCache();
    Serial.print("ENV ");
    if (isnan(gLastTempC)) Serial.print("nan"); else Serial.print(gLastTempC, 1);
    Serial.print(", ");
    if (isnan(gLastRH)) Serial.print("nan"); else Serial.print(gLastRH, 1);
    Serial.println(" %RH");
    return;
  }

  Serial.println("ERR?");
}

// ---------------- Setup / Loop ----------------
void setup() {
  pinMode(PIN_PUL, OUTPUT);
  digitalWrite(PIN_PUL, LOW);
  pinMode(PIN_DIR, OUTPUT);
  digitalWrite(PIN_DIR, LOW);

  dht.begin();

  // Timer1 init (CTC, stopped)
  TCCR1A = 0;
  TCCR1B = 0;
  TIMSK1 = 0;
  SetPulseHz(1000.0); // default 1 kHz ready

  Serial.begin(115200);
  // brief delay so the Pi opens serial after reset
  delay(50);
  Serial.println("READY");
}

void loop() {
  // Handle serial lines
  static char buf[64];
  static uint8_t idx = 0;

  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\r') continue;
    if (c == '\n') {
      buf[idx] = 0;
      HandleLine(buf);
      idx = 0;
    } else {
      if (idx < sizeof(buf) - 1) buf[idx++] = c;
    }
  }

  // Periodically refresh env cache
  UpdateEnvCache();
}
