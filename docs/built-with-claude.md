# How this was built with Claude

This project was developed with **Claude** (Anthropic's AI model), working through
**Claude Code** with access to the repository, a shell, the Teensy over USB and the logic
analyser. Claude wrote the specification, firmware, host software, tests, tools and this
documentation, and did the hardware bring-up itself, taking measurements on the bench
rather than asking for them. A person owned the hardware and the decisions: they chose the
goals, wired and re-wired the bench, checked the board's population, answered questions,
and corrected Claude when it was wrong.

This page records how that worked, including what went wrong, because the method is as
reusable as the code.

## The division of work

| The person | Claude |
|---|---|
| Set the goal: NanEyeC → Teensy 4.1 → USB camera, developed autonomously | Asked the questions that turned the goal into [spec.md](design.md), and kept it current |
| Supplied the datasheet, app note, schematic and a reference capture of a working link | Decoded the capture completely before writing code, and cross-checked everything against the datasheet |
| Answered design questions: USB serial and a Python viewer, mono, jumper wires, bit depth, LEDs | Wrote the firmware, host package, viewer, recorder, tests and docs |
| Wired the bench, fitted parts, reported which resistors were fitted | Flashed the firmware, drove the Teensy over USB, captured and analysed the link on the logic analyser |
| Corrected the probe map when Claude had it wrong, and fixed a wiring mistake on SCLK | Proposed and ran each experiment, and reported results with the evidence |
| Decided what to commit, push and publish | Committed and pushed only when asked |

## Before any hardware

The first stage ran with no sensor attached:

1. **Specification by conversation.** Claude asked about interface, colour, wiring, clock
   rate, illumination and the purpose of the camera, then wrote `spec.md` as a living
   design record.
2. **Ground truth from a working link.** A 434 MB Saleae export of the NanoBerry talking to
   a Raspberry Pi was decoded bit by bit: clock rate, sampling edge, the exact register
   sequence, frame timing and noise, and a defect in the Pi's implementation (clock gaps
   inside rows corrupting three pixels each). These numbers, not the datasheet's nominal
   values, drove the design.
3. **Code written blind, checked twice.** Firmware and host software were written against
   the datasheet and the capture, then reviewed line by line against both
   ([datasheet cross-check](datasheet-crosscheck.md)). That review found real bugs,
   including bus contention during start-up, before any hardware existed.
4. **Testable without hardware.** One real sensor row became a test vector compiled into
   the firmware (`SELFTEST`), and the host replays reference frames through the real wire
   protocol, so the decode path was proven before the link existed.

## Bring-up on the bench

Claude talked to two instruments directly:

- **The Teensy**, over its USB serial port, with the firmware's text commands (`ID`,
  `START`, `LISTEN`, `EXP` …), flashing new builds with PlatformIO as needed.
- **A Saleae Logic Pro 16**, through the **MCP server** built into the Logic 2 software.
  Claude configured channels and sample rates, armed captures triggered on the sensor's
  power enable, exported the raw data as binary, and analysed it in Python. The client is
  `host/naneye/saleae.py`; the tools built on it are in [Host software](host.md#tools).

The loop was the same each time: form a hypothesis, change one thing, capture both what the
Teensy reported and what was on the wire, and compare. Plots made from the captures
(`tools/show_bringup.py`) were shared along the way so the person could follow progress.

Bring-up went in stages ([Hardware: bring-up](hardware.md#bring-up)):

1. **No sensor attached**: clock rate, phase counts and register writes checked on the
   analyser. This caught the SPI clock running 2.4× too fast, because a library call
   overwrote the clock configuration.
2. **Sensor attached**: power-rail ramp, register writes arriving bit-exact at the sensor,
   signal levels against the datasheet's thresholds.
3. **First light.** The sensor answered, but the firmware saw zeros or an endless training
   pattern. Three faults, each looking like a sensor problem, were found by comparing the
   firmware's view with the wire's ([details](hardware.md#first-light-what-it-took-2026-09-18)):
   an uninvalidated data cache over the DMA buffers, a datasheet start sequence that was
   unreliable on this board (replaced by a bit-level row lock), and a power-off time too
   short for a clean reset.
4. **After first light**: 24.75 MHz verified, a hardware watchdog added and tested by
   deliberately hanging the firmware, and live register control added to the viewer,
   checked field by field on the sensor.
5. **49.5 MHz and error handling.** Asked to make the higher clock work and to find a way
   to do error correction, Claude first measured instead of theorising: a tool that counts
   broken words at each of the four sampling points the Teensy can use. One point was
   perfect. That became automatic calibration at every start, followed by detection and
   concealment of whatever errors still get through, tested by injecting 500 errors per
   frame. Result: 35 fps, 60 s without a single bad word.

## What went wrong along the way

Recorded because it is part of how the result was reached:

- **Probe map mistakes.** Claude assumed the wrong logic-analyser channels twice. Once it
  concluded from a supposedly dead supply rail that power had failed, when it was actually
  looking at the data line. The rail was fine; the conclusion was not. The person corrected
  the channel map, and Claude withdrew it.
- **Hypotheses that were wrong.** Before the cache bug was found, several experiments chased
  the start-up sequence: clock rates, register values, timing of the release. Each was
  measured and ruled out. The deciding step was comparing the firmware's received bits
  with the analyser's view of the same wire, which showed the sensor had been streaming
  correctly all along.
- **A plausible physical explanation that was wrong.** 49.5 MHz first failed, and Claude
  attributed it to slow edges: the sensor's weak output driver against the capacitance of
  jumper wires and probes. The numbers seemed to fit, and the docs said so for a while. It
  was the sampling point. The logic analyser, at 4 ns per sample, could not resolve 20 ns
  bits well enough to tell the difference; measuring the error rate directly could.
- **Documentation drift.** Claims written before the hardware existed ("never run on
  hardware", a register sequence, a datasheet section number) went stale or were wrong,
  and were corrected when checked against measurements.

## Lessons that carry over

- **Decode a working reference first.** A capture of a known-good link is worth more than
  any amount of datasheet reading.
- **When the firmware and the wire disagree, believe the wire.** An AI assistant with direct
  access to a logic analyser can check its own assumptions instead of reasoning about them.
- **Make it testable before the hardware arrives.** A real-data test vector and a replay
  path meant only the link itself was unknown on the first day.
- **Keep a human on the hardware and the decisions.** Wiring, board facts and corrections
  came from the person; several of Claude's mistakes were caught that way.
