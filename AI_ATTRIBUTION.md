# AI Attribution

Parts of this repository were developed with Anthropic's Claude models as coding and engineering assistants, used through Claude Code. This file records which models were used and for what, so that anyone reviewing or reusing the code knows what to verify independently.

## Project lead

**Anatol M. Gogoj** (University of Connecticut, Duduta Lab): design intent, requirements, hardware, all bench and hardware-in-the-loop validation, and final review of every change. The human author is the responsible engineer for the rig and its data.

## Models used

| Model | When | Used for |
|---|---|---|
| Claude Opus 4.6 | up to 2026-03 | Instrument driver library (`instruments.py`, the earlier raw-usbtmc version), the Flask dashboard and DAQ integration, the standalone tkinter instrument GUI (since removed), Arduino firmware refinements, and the README |
| Claude Sonnet 4.6 | up to 2026-03 | Same scope as above, alongside Opus 4.6 |
| Claude Opus 5.5 | 2026-10-14 onward | Code review against the Digital_Multitool and dea-characterization-board repositories; the BK894 driver port and LCR recorder (`instruments.py`, `lcr_format.py`, `lcr_controller.py`, `html/daq.html`); the serial link and dashboard rework (`mega_link.py`, `app.py`, `html/index.html`); vendoring uPlot; the git history scrub script; the README refresh, the service file fix, and this file. Commits carry a `Co-Authored-By: Claude Opus 5.5` trailer |
| Claude Sonnet (Claude Code `sonnet` model alias, the 5.5 generation at the time) | 2026-10-14 | Drafting and filing GitHub issues #5 to #42 from review findings, each checked against the code before filing |

The first code review on 2026-10-14 (adversarial review with fake-serial probes) ran in an earlier session; its model was not recorded.

## What was and was not verified

- All 2026-10 changes are covered by hardware-free unit tests (fake Mega firmware, fake BK894). None of them had been run on the Raspberry Pi, the BK894, or the Arduino Mega when they were merged.
- Instrument behavior that the code relies on (for example the BK894 `:VOLT` command and its 2.0 V / 500 kHz limits) comes from bench verification in the Digital_Multitool repository on its meter, not on the rig's meter.
- Claude did not operate any hardware, deploy to the Pi, or flash firmware.

## Verification expectations

Before trusting results from the rig:

1. Run `bench/bk894_smoke.py` on the Pi and confirm the `:VOLT?` read-back.
2. Check STOP latency and unplug recovery on the real USB link.
3. Treat the safety limits listed in the README's Safety Notes as open until the firmware issues there are closed.

## Last updated

- 2026-10-15: created; attribution moved here from the README.
