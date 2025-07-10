const int PwmPin = 3; // OC2B — Pin D3 on Nano (hardware PWM output)

void setup() {
  Serial.begin(9600);         // UART from UNO (connected to D0)
  pinMode(PwmPin, OUTPUT);
  SetupHardwarePwm(0);        // Start with PWM disabled
}

void loop() {
  if (Serial.available()) {
    String input = Serial.readStringUntil('\n');
    input.trim();
    long freq = input.toInt();

    if (freq > 0) {
      SetupHardwarePwm(freq);
      Serial.print("PWM set to ");
      Serial.print(freq);
      Serial.println(" Hz");
    } else {
      StopHardwarePwm();
      Serial.println("PWM stopped");
    }
  }
}

void SetupHardwarePwm(uint32_t frequencyHz) {
  // Disable Timer2
  TCCR2A = 0;
  TCCR2B = 0;
  TCNT2 = 0;

  const uint32_t f_cpu = 16000000UL;
  const struct Prescaler {
    uint16_t value;
    uint8_t bits;
  } prescalers[] = {
    {1,    0b001},
    {8,    0b010},
    {32,   0b011},
    {64,   0b100},
    {128,  0b101},
    {256,  0b110},
    {1024, 0b111}
  };

  bool success = false;
  for (auto p : prescalers) {
    uint32_t top = (f_cpu / (2UL * p.value * frequencyHz)) - 1;
    if (top <= 255) {
      OCR2A = top;
      TCCR2A = (1 << COM2B0) | (1 << WGM21); // Toggle OC2B on compare match
      TCCR2B = (1 << WGM22) | p.bits;
      success = true;
      break;
    }
  }

  if (!success) {
    Serial.println("Frequency too low for Timer2.");
  }
}

void StopHardwarePwm() {
  TCCR2A = 0;
  TCCR2B = 0;
  digitalWrite(PwmPin, LOW); // Ensure output is low
}
