# SEIM protocol reference

Everything needed to talk to a NanEyeC over its single-ended interface, in one place, so
nobody has to re-derive it from two PDFs and a 434 MB capture. Sources: DS000503 v3-00
(datasheet), AN000611 v2-00 (MCU interface app note), and the decoded reference capture.

**Where measurement and datasheet disagree, the measurement wins** and is marked as such.

## The link

Two wires. The host drives `SCLK`; `SDAT` is bidirectional and shared by time, not by a
direction pin. The sensor has no chip select and cannot share a bus.

| | |
|---|---|
| SCLK | host → sensor, continuous while streaming |
| SDAT | sensor → host during readout; host → sensor during INTERFACE MODE |
| Bit rate | one bit per SCLK **rising** edge |
| Levels | 3.3 V CMOS. V_IL ≤ 0.4 V, V_IH ≥ VDDA − 0.3 V |
| Supply | VDDA 3.2–3.4 V, ~9.7 mW in SEIM |

LVDS mode exists on the same two pins but needs a comparator front-end and >750 MHz
sampling to recover the Manchester-encoded clock. Not feasible on an MCU; out of scope.

## Words

Everything is a 12-bit **pixel period** (PP), MSB first.

| Word | Encoding | Value | Where |
|---|---|---|---|
| Pixel | `1` + 10 data bits + `0` | — | READOUT |
| Training | `010101010101` | `0x555` | SYNC, DELAY, start-of-row |
| Pre-sync training | `101010101010` | `0xAAA` | INITIAL PRE-SYNC, and the first row after power-on only |
| End of frame | `000000000000` | `0x000` | 8× after the last row |

A pixel word always has **start = 1 and stop = 0**. That is the cheapest possible integrity
check and it is worth applying to every word: across 7 reference frames, all 716,800 pixel
words validated, so any failure is real information rather than noise.

```
pixel value = (word >> 1) & 0x3FF
valid       = (word >> 11) == 1 and (word & 1) == 0
```

!!! note "The last pixel of a frame"
    DS000503 §6.4.1.1 notes the stop bit is missing from the very last pixel word (320,320)
    in LVDS mode. This has not been observed to matter in SEIM in the reference capture —
    all stop bits validated — but it is why the firmware counts failures rather than
    aborting on the first one.

## Frame structure

| Phase | Duration | Direction | Content |
|---|---|---|---|
| INTERFACE | 648 PP | host drives SDAT | register writes, then filler |
| SYNC | 2 × 328 = 656 PP | sensor | `0x555` |
| DELAY | (16·`rows_delay` + 2) × 328 PP, min 656 | sensor | `0x555` |
| READOUT | 320 × (8 × `0x555` + 320 pixels) = 104,960 PP | sensor | image |
| EOF | 8 PP | sensor | `0x000` |

With the minimum delay that is **106,928 PP per frame**. A row is 328 PP = 3936 bits
exactly, which is the number the whole firmware design leans on: it is a whole number of
32-bit words (123) and fits inside one LPSPI frame (4096-bit limit), and 3 words = 8 PP
exactly.

After power-on reset the first frame is preceded by a shorter sequence instead of a full
INTERFACE MODE:

```
INITIAL INTERFACE     1 activation clock + at least 2 PP   (host drives)
INITIAL PRE-SYNC      10 clocks + 329 PP of 0xAAA
```

### Finding row boundaries

Eight training words are 96 alternating bits ending in `1`, and the next word's start bit is
also `1`. The alternation therefore breaks exactly at the first pixel bit — which is how the
reference capture was decoded, yielding a row pitch of exactly 3936 bits with zero
exceptions over 0.38 s. See `host/naneye/decode.py::find_row_starts`.

## Registers

Two write-only 16-bit registers, writable during INTERFACE MODE. A write is 24 bits:

```
1001 | 3-bit address | 16 data bits (MSB first) | 0
      addr 000 = CONFIG_0,  addr 001 = CONFIG_1
```

Rules that bite:

- Never send data on the **first** clock after power-up — at least one activation clock must
  come first.
- Never write in the **last** PP of INTERFACE MODE — and do not drive it at all. §6.4.3
  says the sensor transmits an end-of-interface word there: `0x015` in SEIM
  (`000000010101`, whose trailing `0101` runs straight into the `0x555` of SYNC).
- Drive the bus for the rest of the window even when there is nothing to write, to keep EMI
  off a floating line (DS000503 §6.3.2.1 note 1).
- The shift register only commits after a correct `1001` code and 24 clocks, so a
  mid-stream false match is harmless.

!!! warning "AN000611 and the datasheet disagree about that last PP"
    The app note's recipe — and so the reference host — drives all 648 PP. The datasheet
    says the sensor drives the 648th. The reference capture reads `0x000` there, but that
    cannot decide it: a Raspberry Pi GPIO driving low would simply win against the sensor's
    current-limited output, so the analyser would see `0x000` whether or not the sensor was
    trying to send `0x015`. We release that PP and record what arrives, so first light on
    real hardware answers the question (`PROBE`, `STATS`).

### CONFIG_0 (address 0)

| Bits | Field | Meaning |
|---|---|---|
| 15:8 | `rows_in_reset` | rows in reset = 2n + 2. **Primary exposure control** |
| 7:6 | `vrst_pix` | pixel reset voltage: 2.2 / 2.4 / **2.6** / 2.8 V |
| 5:4 | `ramp_gain` | ADC ramp gain: 0.79 / **0.99** / 1.32 / 1.97× |
| 3:2 | `offset_ramp` | dark level: 1.9 / 2.0 / 2.1 / **2.2** V |
| 1:0 | `output_curr` | SEIM drive strength: 3.9 / 5.8 / 7.7 / 9.6 mA |

### CONFIG_1 (address 1)

| Bits | Field | Meaning |
|---|---|---|
| 15:11 | `rows_delay` | delay rows = 16n + 2. Coarse exposure, costs frame rate |
| 10 | `bias_curr_increase` | ~2× bias, shorter settling for high speed |
| 9 | `cds_gain` | **1.3** or 2.0 |
| 8 | `output_mode` | **0 = SEIM**, 1 = LVDS |
| 7:6 | `mclk_mode` | 0 = 2×, 1 = default, 2/3 = ÷2 |
| 5:4 | `vref` | CDS reference: 1.9 / 2.0 / **2.1** / 2.2 V |
| 3:2 | `cvc_curr` | CVC current, recommended **01** |
| 1 | `idle_mode` | 1 = idle (no streaming). Clearing this starts the stream |
| 0 | `high_speed` | high-speed clock tree |

Bold values are the datasheet's recommendations. Keep `offset_ramp` 0.1 V **above** `vref`
or the sensor clips in the dark.

## Clock rates

`mclk_mode` and `high_speed` set the sensor's internal MCLK. In SEIM the sensor is a slave:
we supply SCLK, but the ADC counter still runs from the internal oscillator, so **the
external clock must match the internal MCLK** or dynamic range is lost — too fast clips the
ADC low (reduced full scale), too slow raises the black level.

| `high_speed` | `mclk_mode` | Internal MCLK | Frame rate | Our SCLK (99 MHz ÷ (SCKDIV+2)) | Error |
|---|---|---|---|---|---|
| 0 | 2 (÷2) | 12.3 MHz | 9 fps | 12.375 MHz (SCKDIV 6) | +0.6 % |
| 0 | 1 (default) | 24.7 MHz | 19 fps | 24.750 MHz (SCKDIV 2) | +0.2 % |
| 0 | 0 (2×) | 49.1 MHz | 38 fps | 49.500 MHz (SCKDIV 0) | +0.8 % |
| 1 | 2 | 15.7 MHz | 12 fps | — | no clean divisor |
| 1 | 1 | 31.1 MHz | 24 fps | — | (reference host used 31.25 MHz, +0.5 %) |
| 1 | 0 | 62.6 MHz | 49 fps | — | exceeds the timing budget below |

The three non-high-speed rates all land within 1 % using an LPSPI root clock of
PLL2_PFD2 ÷ 4 = 99 MHz. See [firmware architecture](firmware.md#clocking).

## Measured timing

From the reference capture at 31.25 MHz (32.0 ns period), probed at the header:

| | |
|---|---|
| Sensor data launch | **~8 ns after each SCLK rising edge** |
| Setup at the next rising edge | 24 ns median, 20 ns minimum |
| Correct sample edge | **rising** — SPI mode 0 (CPOL 0, CPHA 0) |

That 8 ns is a **round trip**: our clock edge out, sensor response back. It is the hard
limit on clock rate, because it does not shrink as the period does:

| SCLK | Period | Setup window |
|---|---|---|
| 24.75 MHz | 40.4 ns | ~32 ns — comfortable |
| 49.5 MHz | 20.2 ns | ~12 ns — workable |
| 62.6 MHz | 16.0 ns | ~8 ns — don't |

Those figures come from the reference board, a Raspberry Pi plugged straight onto the
NanoBerry. On the Teensy bench the round trip is longer (pad delays, jumper wires), and at
49.5 MHz rising-edge sampling lands on the transition while falling-edge sampling is clean.
The firmware measures the best sampling point at every start instead of assuming one: see
[Firmware: choosing the sampling point](firmware.md#choosing-the-sampling-point).

!!! warning "AN000611 disagrees, and it is wrong for this board"
    The app note advises sampling on the **falling** edge above 40 MHz. Measured here,
    falling-edge sampling would give ~8 ns of setup where the rising edge gives 24 ns. Trust
    the measurement. `CFGR1[SAMPLE]` (the `SAMPLE` command) delays the sampling point by one
    LPSPI functional-clock cycle and is the right knob if the higher rates misbehave.

## Exposure

Rolling shutter. A programmable number of rows sit in reset while one row is read out.

```
t_exp = t_rows_btw_frame + t_rows_matrix - t_rows_in_reset - t_rows_in_readout   [PP]

t_rows_btw_frame = 648 + 656 + (16·rows_delay + 2)·328
t_rows_matrix    = 320·328 + 8 = 104,968
t_rows_in_reset  = (2·rows_in_reset + 2)·328
t_rows_in_readout= 2·328 = 656
```

Convert with `t_exp_seconds = t_exp × 12 / SCLK`. Implemented identically in
`naneye::exposure_pp()` and `protocol.Header.exposure_us()`.

| | `rows_in_reset` | `t_exp` | At 24.75 MHz |
|---|---|---|---|
| Longest | 0 | 105,616 PP | 51.2 ms |
| Datasheet default | 128 | 21,648 PP | 10.5 ms |
| Shortest | 159 | 1,312 PP | 0.6 ms |

`rows_delay` extends exposure further (up to 261 ms at 12.3 MHz) at the cost of frame rate.

!!! important "Writes land one frame late"
    Measured: `rows_in_reset` 0 → 127 written in the window before frame 6 only took effect
    on frame **7**, because integration for a frame overlaps the previous readout. Signal
    above black scaled 7.4×, consistent with the 4.74× exposure change plus a ~200 DN black
    level. So a frame header must report the settings that were *in force for that frame*,
    not the most recently written ones.

## Things the reference capture taught us

Each of these cost nothing to learn from `doc/digital.csv` and would have cost hours on the
bench.

**The working sequence.** Activation clock, then `CONFIG_0=0x009F` / `CONFIG_1=0x009F` (SEIM
selected, idle still on), then `CONFIG_1=0x0065` to clear idle and start streaming. The host
then **rewrites both registers every frame** — that is how it does auto-exposure. Apart from
`rows_in_reset` it uses the datasheet's recommended values at unity gain and maximum output
drive.

**Discard the first frame.** The frame after idle-off is fully saturated (mean 1020 of 1023).
Frames after it are normal (mean ≈ 274, temporal σ = 2.74 DN). The datasheet warns about
this; it is confirmed and the firmware discards it.

**Never gap the clock mid-row.** The reference host restarts its DMA every 65,532 bytes,
leaving 50–200 µs clock gaps inside rows. Each corrupts exactly three consecutive pixels,
two pixels after the gap (the ADC pipeline drains first):

```
row 129:  266 274 270 278 271 275 | 1020  84   0 | 272 274 277
row 128:  275 271 276 271 274 274 |  272 273 272 | 275 277 272   (same columns, clean)
```

The sensor's ADC counter runs on its own oscillator, so a host clock gap desynchronises
readout from conversion. Avoiding this drove the one-row-per-SPI-frame decision in the
[firmware](firmware.md#why-one-row-per-spi-frame), and "no SCK gap > 1 PP within a row" is
an acceptance criterion, not an aspiration.
