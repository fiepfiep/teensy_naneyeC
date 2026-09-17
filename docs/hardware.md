# Hardware and bring-up

## What is on the bench

- **NanoBerry board** with a NanEyeC mounted as `S1` (mono, confirmed by measurement),
  exposing a 40-pin Raspberry Pi header `J2`, plus `J1` (FPC) and `P1` (6-pin) on the same
  sensor nets.
- **Teensy 4.1**, 600 MHz i.MX RT1062. 3.3 V I/O, not 5 V tolerant.
- **Saleae Logic Pro 16** — 500 MS/s on up to 2 channels, which is what the 12–50 MHz link
  needs.

## Wiring

```
Teensy 27 ──────────────── J2.23 ──[R20 24R]── S1.B1  (SCLK)
Teensy 26 ┐                                    C11 15pF
Teensy  1 ┴─ tie at J2 ──── J2.19 ──[R23 24R]── S1.B2  (SDAT, bidirectional)
                                               C13 15pF
Teensy  2 ──────────────── J2.33 ─ R19 10k pd ─ TPS71701 EN → VCC_SENSOR 3.3 V
Teensy  3 ──────────────── J2.31   LED_VCC_ON_1
Teensy  4 ──────────────── J2.36   LED_DAC_CS_N
Teensy  5 ──────────────── J2.38   LED_DAC_SDI
Teensy  6 ──────────────── J2.40   LED_DAC_SCK
Teensy VUSB ────────────── J2.2 / J2.4   (5vs)
Teensy GND ─────────────── J2.6, 14, 20, 25   (and J2.9 = GNDL if using the LEDs)
```

Pin choices are in `firmware/src/board.h`. LPSPI3 ("SPI1") fixes 27/26/1; LPSPI3 was picked
over the default LPSPI4 to keep a 12–50 MHz clock off pin 13 and its onboard LED
capacitance.

!!! danger "Three things that will waste your afternoon"
    - **The sensor is powered off at reset.** `NanEye_EN` has a 10 k pulldown, so nothing
      responds until pin 2 is driven high. `POWER 1` does this; `START` does it implicitly.
    - **Leave `J1` and `P1` unconnected.** They sit on the same `D+`/`D-` nets as the
      onboard sensor. Anything plugged in there contends on the bus.
    - **Keep SCLK and SDAT short, each with its own adjacent ground return.** At 24.75 MHz
      on flying leads this is the difference between working and not. The measured setup
      window is ~32 ns at that rate — don't spend it on wire.

### Why SDAT goes to two pins

SDAT is half-duplex, shared by time. Tying pin 26 (`LPSPI3_SDO`) and pin 1 (`LPSPI3_SDI`)
together at the header means the direction flip is a single IOMUXC write, with no
peripheral-behaviour unknowns:

| Phase | Pin 26 | Pin 1 |
|---|---|---|
| INTERFACE MODE | `LPSPI3_SDO`, driving | reads (harmless) |
| SYNC / DELAY / READOUT | GPIO input, hi-Z | reads sensor data |

`TCR[TXMSK]` also tristates the output during readout, so this is belt and braces. A
single-pin variant using `CFGR1[PINCFG]=10b` would save a wire and ~5 pF; the Raspberry Pi
reference does exactly that (SPI0 MOSI on J2.19, MISO unconnected), so it is known to work
electrically. Deferred to M7.

## Power

`5vs` from Teensy VUSB → TPS71701 LDO → `VCC_SENSOR` 3.3 V, gated by `NanEye_EN`.

The sensor is negligible (9.7 mW). LED current is bounded by the DAC ceiling, 20 mA by
default against a 44.6 mA hardware maximum, so the USB budget is safe. Cycling
`NanEye_EN` is also the recovery of last resort: it forces a full sensor power-on reset.

## Illumination

VIS (`D3`/`D4`, DURIS S2) and NIR (`D1`/`D2`, SFH 4053) strings in parallel between
`+VCC_LED` and `LED_CATHODE`, fed by an LT3473 boost, sunk by an LT3092 whose set point
comes from an LTC2630 12-bit DAC.

```
LTC2630 VOUT ──[R9 1k]──┬── LT3092 SET      LED_CATHODE ──[R7 12R]── LT3092 IN
                        └── C10 1nF                                  LT3092 OUT
                                                                         │
                                                                    [R11 56R] → GNDL
```

R9 carries only the LT3092's internal 10 µA bias, so `V_SET ≈ V_DAC` and:

```
I_LED ≈ V_DAC / 56 Ω        0 → 44.6 mA, 10.9 µA per LSB
```

!!! note "On/off alone gives you no light"
    The `-LZ12` part resets to **zero scale**, so `LED_VCC_ON` by itself yields ~0.18 mA.
    The DAC has to be programmed: `LEDI 5` then `LED 1`.

**Unresolved:** `R16` and `R17` are 0 Ω jumpers selecting the NIR and VIS strings. If both
are fitted, the two strings share one current sink and the split follows their forward
voltages rather than anything we control — so a commanded 10 mA is a total, unevenly shared.
Normally only one would be fitted. Check visually and write down which; it changes nothing
in the firmware but it changes what the numbers mean.

## Bring-up

Staged, each stage with something that can actually fail. Do not skip ahead: a wrong answer
at M2 looks exactly like a wrong answer at M3 if you never checked M2.

### M0 — before the camera is connected

```bash
uv run pytest                                     # 42 tests
uv run python -m platformio run -d firmware -t upload
```

Then, with only USB attached:

```
ID
SELFTEST
```

`SELFTEST` unpacks a real sensor row embedded in the firmware
(`firmware/src/golden_vector.h`, one row of the reference capture) and compares against its
known pixel values, then checks the exposure arithmetic against two measured cases. Both
must say `PASS`. This isolates *decode* bugs from *link* bugs — if it fails, nothing
downstream is worth debugging.

### M1 — clock and configure

Wire SCLK, SDAT, EN, 5 V and ground. Probe SCLK and SDAT at the header with short ground
leads. Saleae: 2 channels, 500 MS/s, ~200 ms.

```
CLK 12375000
POWER 1
START
```

Capture, export as CSV with columns `Time, data, clk`, then:

```bash
uv run python tools/decode_golden.py path/to/capture.csv --out build/m1
```

**Pass:** the activation clock, then `CONFIG_0=0x009F`, `CONFIG_1=0x009F`, then
`CONFIG_1=0x0065`, bit-identical to the reference sequence, at 12.375 MHz.

**If SCLK is absent:** the LPSPI root clock or pin mux is wrong, not the sensor.
**If SCLK runs but SDAT never leaves idle:** the sensor is not powered (check `NanEye_EN`
and 3.3 V at the module) or SDAT is not reaching `S1.B2`.

### M2 — first light

```
PROBE 2
```

This runs a complete, phase-correct frame cycle but reports word statistics instead of an
image, then clocks out the rest of the frame so the sensor's state machine stays aligned.

**Pass:** a healthy count of `0x555` (or `0xAAA` on the very first frame) and `first:`
showing the training pattern. `words=656` with `0x555=656` for the first two rows of SYNC is
the shape you want.

| What you see | What it means |
|---|---|
| all `0x000` | sensor not driving — power, or SDAT not connected |
| all `0xFFF` | SDAT stuck high, or we never released the bus (direction switch failed) |
| plausible but non-training words | word alignment is off — suspect the activation/alignment clock counts in `seim::start()` |
| `0xAAA` when expecting `0x555` | this is the first frame after power-on; normal |

### M3 — first frame

```
DEPTH 10
```

then on the host:

```bash
uv run python -m naneye.record --source auto --frames 1 --depth 10 --out build/m3
```

**Pass:** all 102,400 pixels validate (`rows_failed 0`), and the image looks like the room.
Then the real check — capture the *same* frame on the Saleae, decode it independently, and
compare pixel for pixel:

```bash
uv run python tools/decode_golden.py build/m3_saleae.csv --out build/m3_logic
```

Two independent observers agreeing is what makes the image trustworthy. Anything less and
you are trusting one decoder to check itself.

### M4 — continuous streaming

```bash
uv run python -m naneye.record --source auto --frames 1200 --depth 10 --out build/m4
```

**Pass:** 60 s at 12.375 MHz and then 24.75 MHz with zero counter gaps, zero failed rows,
and a Saleae capture showing **no SCK gap longer than 1 PP within a row**. That last one is
the defect the reference host has; see
[SEIM reference](seim.md#things-the-reference-capture-taught-us). Check it with:

```python
from naneye import decode
bits, times = decode.sample_saleae_csv("capture.csv")
idx, lens = decode.find_clock_gaps(times, threshold_s=1e-6)
print(len(idx), "gaps over 1 us")
```

### M5 — control, and M5b — illumination

Sweep `EXP` and confirm measured brightness tracks the
[exposure formula](seim.md#exposure) within 1 %. Sweep `LEDI` and confirm brightness tracks
commanded current; measure the actual LED current against `V_DAC / 56 Ω` while you are
there.

### M6 — measurement readiness

Dark frame, temporal noise, fixed-pattern noise. Temporal noise should land near the
reference's **2.74 DN**; materially worse means a signal-integrity or clock-matching
problem, not a sensor problem. Then characterise black level and full scale against
SCLK-versus-MCLK mismatch, which is the one parameter we deliberately left at ≤1 %.

## Probing notes

- 500 MS/s on ≤2 channels is a Logic Pro 16 limit — don't add channels during timing work.
- 0.38 s at 500 MS/s is 434 MB as CSV. Prefer the binary export and keep captures short;
  one frame is 52 ms at 24.75 MHz.
- `tools/decode_golden.py` caches its sampled bit stream as `bits.npy`, so re-analysis is
  instant after the first ~50 s parse.
