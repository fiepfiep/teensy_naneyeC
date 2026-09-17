# NanEyeC → Teensy 4.1 → Windows USB camera

Streams 320×320 mono images from an ams-OSRAM NanEyeC (on a NanoBerry board) to a Windows PC
through a Teensy 4.1, over the sensor's half-duplex single-ended interface (SEIM).

Read [spec.md](spec.md) first — it is the living design record: decisions, the measured
ground truth from a working reference link, milestones and risks.

Full documentation is an MkDocs site under [docs/](docs/) — overview, hardware and bring-up,
firmware architecture, host usage and API, and a distilled SEIM protocol reference:

```bash
uv sync --group docs
uv run mkdocs serve      # http://127.0.0.1:8000
```

It is published to <https://fiepfiep.github.io/teensy_naneyeC/> by
`.github/workflows/docs.yml` on every push that touches the docs.

## Status

| | |
|---|---|
| Reference capture decoded | done — 7 frames, every start/stop bit valid |
| Host decode + transport + recorder | done, 34 tests passing |
| Firmware | compiles clean; **not yet run on hardware** |
| Hardware bring-up (M1 onward) | blocked on wiring |

Nothing in the firmware has touched a sensor yet. The register-level LPSPI setup is the part
most likely to need adjustment during bring-up; the decode path it feeds is already
validated against real sensor data.

## Layout

```
spec.md                 living design record: decisions, measurements, milestones
docs/                   MkDocs documentation site
doc/                    UNTRACKED: datasheets, schematic, reference capture
firmware/               PlatformIO project for the Teensy 4.1
  src/naneye_regs.h     register model, frame geometry, exposure and clock maths
  src/seim_unpack.h     12-bit pixel-period extraction (pure logic)
  src/naneye_seim.cpp   LPSPI3 + DMA capture driver and phase sequencer
  src/led_dac.cpp       LTC2630 illumination control
  src/usb_proto.cpp     framing and CRC
  src/golden_vector.h   GENERATED: one real row + expected pixels, for SELFTEST
host/naneye/            decoder, transport, sources, viewer, recorder
tools/                  golden-capture decoder, test-vector generator
tests/                  34 tests, no hardware required
```

## The reference capture

`doc/` is **not tracked** — it holds vendor datasheets, the NanoBerry schematic and a
434 MB logic capture: third-party or raw input rather than project source, in a public
repository. See `.gitignore` for the file list and where each comes from.

The important one is `doc/digital.csv`, a Saleae export (2 channels, 500 MS/s, 0.383 s) of a
**working** NanoBerry ↔ Raspberry Pi link. It is the source of every measured figure in
spec.md section 3; `tools/decode_golden.py` turns it into `build/golden/`.

What the repo does carry is the part that matters for testing: `firmware/src/golden_vector.h`
holds one real row from that capture plus its expected pixel values, so both the on-device
`SELFTEST` and `tests/test_unpack.py` check against genuine sensor data. Tests that need the
full capture skip cleanly when it is absent.

## Getting started

The host side is managed with [uv](https://docs.astral.sh/uv/):

```bash
uv sync                                    # create the environment from uv.lock
uv run python tools/decode_golden.py       # decode the reference capture (~50 s first run)
uv run pytest                              # 34 tests
uv run --group firmware python -m platformio run -d firmware  # build the firmware
```

The host stack runs with no camera attached, replaying the reference frames through the real
wire protocol:

```bash
uv run python -m naneye.viewer --source replay
uv run python -m naneye.record --source replay --frames 20 --depth 10 --out build/demo
```

With hardware connected, swap `--source replay` for `--source auto`. Saleae capture
automation is an optional extra: `uv sync --extra saleae`.

## Wiring

Teensy 4.1 to the NanoBerry 40-pin Raspberry Pi header (J2). See spec.md section 4.1;
keep SCLK and SDAT short, and leave J1/P1 unconnected.

| Teensy | J2 | Signal |
|---|---|---|
| 27 | 23 | SCLK |
| 26 + 1 (tied at the header) | 19 | SDAT, bidirectional |
| 2 | 33 | NanEye_EN — sensor is off until this is driven high |
| 3 | 31 | LED_VCC_ON |
| 4 / 5 / 6 | 36 / 38 / 40 | LED DAC CS / SDI / SCK |
| VUSB | 2 or 4 | 5 V |
| GND | 6, 14, 20, 25 | ground (and 9 = GNDL if using the LEDs) |

## Device commands

The USB port accepts plain text lines, so it is usable straight from a terminal; replies and
images come back framed (spec.md section 7).

```
ID                      firmware version and current settings
POWER 0|1               sensor LDO enable
CLK 12375000            SCLK: 12375000, 24750000 or 49500000
START / STOP            begin or end streaming
DEPTH 8|10|12           8-bit, packed 10-bit, or raw 12-bit pixel periods
EXP <rows_in_reset> [rows_delay]
GAIN <ramp_gain> <cds_gain>
REG <0|1> <0xHHHH>      raw register write
LED 0|1                 illumination on/off
LEDI <mA>               LED current, clamped (default ceiling 20 mA of 44.6 mA)
PROBE [rows]            report what the sensor is transmitting (bring-up)
SELFTEST                verify the unpack against the embedded reference row
STATS
```

## Bring-up order

Follow the milestones in spec.md section 9, written up as a procedure with pass/fail checks
in [docs/hardware.md](docs/hardware.md#bring-up). In short: `SELFTEST` and `PROBE` before
believing any image, then a Saleae capture decoded independently with
`host/naneye/decode.py` and compared against what the Teensy reported for the same frame.
