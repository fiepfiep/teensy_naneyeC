# Host usage and API

Python, managed with [uv](https://docs.astral.sh/uv/). The package lives in `host/naneye`.

```bash
uv sync                  # from uv.lock
uv sync --extra saleae   # adds logic2-automation
uv sync --group docs     # adds mkdocs-material
```

## The sources abstraction

Every tool takes a `--source`, and all of them accept the same three kinds. This is the
reason the host side could be built and tested before any hardware existed: `replay`
re-encodes reference frames **through the real wire protocol**, so what a tool receives is
byte-for-byte what the Teensy would send.

| `--source` | What it is |
|---|---|
| `replay` | reference frames from `build/golden`, or generated ones if that is absent |
| `auto` | the first Teensy serial port found (PJRC VID `0x16C0` preferred) |
| `COM7` | that serial port |
| a `.csv` path | a Saleae capture of the link, decoded and played back as frames |
| a file path | a recorded packet stream |

```bash
uv run python -m naneye.viewer --source doc/digital.csv
```

Pointing it at a capture decodes the raw logic-analyser export: bits sampled on SCLK rising
edges, rows found by the training-pattern alternation break, and each frame re-encoded
through the wire format so the viewer sees what it would see from the device. The clock rate
comes from the capture's own edge timing, so exposure and SCLK in the status bar are
measured rather than assumed, and per-frame start/stop-bit failures land in `rows_failed`.

Sampling a 434 MB export takes ~50 s, so the bit stream is cached under `build/golden` with
a `source.json` recording which capture produced it. An unlabelled cache is spot-checked
against the first 20,000 edges rather than trusted blindly.

`replay` no longer requires the reference capture: without it you get generated frames,
labelled `SYNTHETIC` in the source name so they cannot be mistaken for sensor data.

```python
from naneye.sources import open_source

with open_source("replay", fps=19.3) as src:
    for header, img in src.frames():
        print(header.describe(), img.shape, img.dtype)
```

## Viewer

```bash
uv run python -m naneye.viewer --source replay
uv run python -m naneye.viewer --source auto --depth 10 --clock 24750000
uv run python -m naneye.viewer --source replay --snapshot shot.png   # one frame, no window
```

![Viewer on replayed reference frames](images/viewer-replay.png)

The status bar is the point of it: frame counter and rate, min/max/mean, saturated
percentage, exposure in ms, SCLK, both config registers, and the three numbers that tell you
whether to trust the data — `dropped`, `counter gaps` and `rows_failed`. `SYNC LOST` turns
red.

| Key | Action |
|---|---|
| ++q++ / ++esc++ | quit |
| ++s++ | save the current frame as PNG |
| ++r++ | raw vs autoscaled contrast |
| ++h++ | histogram on/off |
| ++space++ | pause |
| ++plus++ / ++minus++ | exposure (`rows_in_reset`, ±8) |
| ++l++ | LED on/off |
| ++bracket-left++ / ++bracket-right++ | LED current ∓1 mA |

`--snapshot` composes exactly what the window shows and writes it to a file, which is how the
image above is generated — handy for bug reports and for documenting a setup.

## Recorder

```bash
uv run python -m naneye.record --source auto --depth 10 --frames 1200 --out capture/run1
uv run python -m naneye.record --source auto --frames 100 --exposure 64 --led 8 --out capture/run2
```

Per run:

| File | Contents |
|---|---|
| `frames.npy` | `(N, 320, 320)`, `uint16` for 10-bit, `uint8` for 8-bit |
| `meta.csv` | one row per frame: counter, timestamp, exposure, config, drop counters, min/max/mean |
| `run.json` | settings and a summary, including measured fps and counter gaps |
| `stream.bin` | with `--raw`, the verbatim packet stream |

A recording is **self-describing**: every frame carried its own exposure, gain, clock rate
and drop counters in its header, so nothing has to be remembered separately. For measurement
work that matters more than it sounds — it is what lets you come back to data months later
and still know what it is.

```python
import json, numpy as np
frames = np.load("capture/run1/frames.npy")
meta = json.load(open("capture/run1/run.json"))
assert meta["counter_gaps"] == 0 and meta["rows_failed_total"] == 0
dark = frames.mean(axis=0)
print("temporal noise", frames.astype(float).std(axis=0).mean(), "DN")
```

## Decoding

`naneye.decode` is the canonical decoder, used for live frames, Saleae captures and the
golden-capture tool alike.

```python
from naneye import decode

# Device payloads
img = decode.decode_payload(header, payload)      # -> (320, 320)
img = decode.raw12_to_image(raw)                  # drop per-row training words

# Pixel periods from 32-bit words (mirrors the firmware exactly)
pp = decode.words_to_pp(words, decode.ROW_PP)
ok = decode.validate_pp(pp)                       # start=1, stop=0
px = decode.pp_to_pixels(pp)

# Logic-analyser captures
bits, times = decode.sample_saleae_csv("capture.csv")
for i, img, stats in decode.decode_frames_from_bits(bits):
    assert stats["bad_start_bits"] == 0
idx, lens = decode.find_clock_gaps(times, threshold_s=1e-6)
writes = decode.decode_register_writes(bits[:200])
```

`decode_frames_from_bits` finds rows by the
[alternation break](seim.md#finding-row-boundaries) and yields only frames whose row pitch is
exactly 3936 bits, so a malformed capture is skipped rather than silently mis-decoded.

## Protocol

`naneye.protocol` mirrors `firmware/src/usb_proto.h`. A 52-byte little-endian header, CRC-32
over header bytes 0–47 plus the payload, then the payload.

```python
from naneye import protocol
h = protocol.Header.unpack(buf)
print(h.describe(), h.exposure_us(), h.sync_lost)
```

`transport.PacketReader` resynchronises on the magic word and verifies every CRC, so a
corrupted or partial packet costs one frame rather than the stream. It works over any
`read(n)` callable — serial port, socket, file, `BytesIO` — which is what makes it testable.

```python
from naneye.transport import Device
with Device.open_first() as dev:
    print(dev.ask("ID"))
    print(dev.ask("SELFTEST"))
    dev.command("START")
    for pkt in dev.frames():
        ...
```

## Commands

Sent as plain text lines; replies come back framed.

```
ID                      firmware version and current settings
POWER 0|1               sensor LDO enable (off at reset)
CLK 12375000            12375000 | 24750000 | 49500000
SAMPLE 0|1              delay the input sampling point one LPSPI clock
START / STOP
DEPTH 8|10|12           gray8 | packed 10-bit | raw pixel periods
EXP <rows_in_reset> [rows_delay]
GAIN <ramp_gain> <cds_gain>
REG <0|1> <0xHHHH>      raw register write
LED 0|1                 illumination (needs LEDI first — see hardware)
LEDI <mA>               LED current, clamped
LEDMAX <mA>             raise the clamp, up to 44.6 mA
PROBE [rows]            report what the sensor is transmitting
SELFTEST                verify unpack and exposure maths on-device
STATS
```

## Tests

```bash
uv run pytest          # 34 tests, none needing hardware
```

| File | Covers |
|---|---|
| `test_protocol.py` | every header field offset against the firmware's `static_assert`s, CRC behaviour |
| `test_unpack.py` | pixel extraction against the golden row, lossless 10-bit packing |
| `test_transport.py` | CRC rejection, resync past junk, truncation, drop accounting |
| `test_golden.py` | the reference capture: row pitch, start/stop bits, noise, mono, registers, the gap defect |

Tests needing the 434 MB capture skip cleanly when it is absent. `test_unpack.py` parses the
generated `golden_vector.h` so the Python decoder is held to the exact data the device's
`SELFTEST` checks itself against — the C and Python implementations cannot diverge silently.

## Tools

```bash
uv run python tools/decode_golden.py [capture.csv] [--out build/golden]
uv run python tools/make_golden_vector.py --row 200
```

`decode_golden.py` is the reference decode and the regression baseline: it reports clock
rate, register writes with fields expanded, per-frame validation, temporal noise and the
mono check. It caches `bits.npy`, so only the first parse costs ~50 s.

`make_golden_vector.py` regenerates the firmware's embedded test row. Pick a clean row —
rows 129 and 262 of the reference frame 1 contain the host's
[clock-gap corruption](seim.md#things-the-reference-capture-taught-us).
