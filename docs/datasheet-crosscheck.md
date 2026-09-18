# Datasheet cross-check

A line-by-line review of the code against DS000503 v3-00, AN000611 v2-00 and the decoded
reference capture, recording **every place the implementation differs from the
documentation** and why.

Read this before "correcting" something in the code to match a datasheet table. Several of
the differences are deliberate, and two of them exist because the datasheet contradicts
itself or contradicts measurement.

Reviewed 2026-09-18, in two passes: constants and formulas first, then the driver
internals and the USB API (§8). Method: re-read each datasheet passage the
code depends on, then check the corresponding constant, bitfield or formula.

## 1. Verified as matching

No discrepancy found in any of these.

| Area | Checked against | Result |
|---|---|---|
| Frame geometry | §6.3.2.2, Table 9 | INTERFACE 648 PP, SYNC 2×328, DELAY (16n+2)×328, READOUT 320×328+8, INITIAL PRE-SYNC 329 PP — all exact |
| Row structure | §6.4.2.5 | 8 training + 320 pixels = 328 PP = 3936 bits |
| Word encodings | Tables 13–16 | pixel `1`+10 bits+`0`, training `0x555`, pre-sync `0xAAA`, EOF `0x000` |
| Register write packet | §6.4.3, Table 17 | `1001` + 3-bit address + 16 data bits MSB-first + `0` = 24 bits |
| CONFIG_0 bitfields | Table 18 | all five fields, positions and value maps |
| CONFIG_1 bitfields | Table 20 | all nine fields, positions and value maps |
| Exposure equations | §6.5.1, Eq. 2–6 | `exposure_pp()` reproduces 105,616 PP for `rows_in_reset=0`, which is also what the reference capture decodes to |
| Clock / frame-rate map | Table 21 | `mclk_mode` 2/1/0 → 12.3/24.7/49.1 MHz → computed 9.6/19.3/38.6 fps vs the table's 9/19/38 |
| SEIM output current | Table 18 | 3.9 / 5.8 / 7.7 / 9.6 mA |
| Upstream setup/hold | Table 7 | 3 ns min required; we give 10–40 ns (half a SCLK period) |
| Input high level | Table 7 | V_IH ≥ VDDA − 0.3 V = 3.0 V; Teensy drives 3.3 V push-pull |
| Phase order after frame 1 | Table 9 note 2, Fig. 10 | INTERFACE → SYNC → DELAY → READOUT, bypassing the initial phases |

Firmware and host constants also agree with each other exactly (`naneye_regs.h` versus
`decode.py`), which `tests/test_unpack.py::test_row_geometry` pins down.

## 2. Deliberate differences from the datasheet's register defaults

We do not use the reset defaults. Every value below is either required, or the datasheet's
own recommendation, or copied from the working reference link.

| Field | Datasheet default | Ours | Why |
|---|---|---|---|
| `output_mode` | `1` = LVDS | **`0` = SEIM** | Required. LVDS needs a comparator front-end and >750 MHz sampling |
| `rows_in_reset` | `0x80` → 258 rows | **`0`** → 2 rows | Copied from the reference link: near-maximum exposure. **Will clip in normal room light** — see below |
| `vref` | `01` = 2.0 V | **`2`** = 2.1 V | Datasheet's own "(recommended)" |
| `cvc_curr` | `10b` | **`01b`** | Datasheet's own "Recommended to set to 01b" |
| `cds_gain` | `1` = 2.0 | **`0`** = 1.3 | Datasheet's own "(recommended)" |
| `offset_ramp` | `01` = 2.0 V | **`3`** = 2.2 V | Recommended, and keeps the required 0.1 V above `vref` |
| `output_curr` | `01` = 5.8 mA | **`3`** = 9.6 mA | Reference link's choice; fastest edges, which flying leads need |
| `vrst_pix` | `10b` = 2.6 V | `2` = 2.6 V | Same as default, which is also the recommended value |
| `ramp_gain` | `01b` | `1` | Same as default (unity) |

!!! note "The default configuration violates the datasheet's own recommendation"
    Table 18 note 2 says `offset_ramp` should always be 0.1 V above `vref`, yet the reset
    defaults put both at 2.0 V. Our values (2.2 V / 2.1 V) satisfy the rule; the silicon's
    defaults do not. Worth knowing if you ever reset to defaults and wonder why the dark
    level looks wrong.

`rows_in_reset = 0` is a bring-up choice, not a good general default: it is maximum
exposure, inherited from a reference capture taken in a dark room. Expect saturation in
normal light and set `EXP` accordingly. Left as-is so that first light on real hardware
looks as close as possible to the capture we validated against.

## 3. We run the clock well below the "typical" SEIM rate

Table 7, SEIM Downstream Interface:

> External clock in single ended mode — 60 — 75 MHz

The 60 lands in the **typ** column and 75 in **max**; **no minimum is specified**. We run
at 12.375 / 24.75 / 49.5 MHz, which is below typical.

This is deliberate and, on the evidence, correct:

- §6.4.2 requires the external clock to match the sensor's internal MCLK, and Table 21's
  MCLK options go down to **12.3 MHz**. A hard 60 MHz floor would make `mclk_mode = ÷2`
  unusable, contradicting the same datasheet.
- AN000611's entire premise is connecting to "standard MCUs", which generally cannot
  produce 60+ MHz.
- The reference link ran at **31.25 MHz** and produced 7 clean frames.
- Our measured round-trip delay (§4) makes 62.6 MHz unworkable on this wiring anyway.

So we treat 60 MHz as a typical/maximum-performance figure rather than a lower bound, and
match the selected internal MCLK instead.

## 4. Measurement overrules the documentation

### Sampling edge

| Source | Says |
|---|---|
| AN000611 §3.3 | Sample on the **falling** edge above 40 MHz (`CPHA` = second edge) |
| Measured, reference capture at 31.25 MHz | Data launches **~8 ns after the rising edge**, giving 24 ns of setup at the *next rising* edge and only ~8 ns at the falling edge |

We sample on the **rising** edge (SPI mode 0). Following the app note here would cut the
timing margin by a factor of three. The decode of 7 frames with all 716,800 pixel words
correctly framed is the evidence.

### Clock-to-data-out delay

Table 7 gives `tdelay` (clock in to data out) as **2.1 ns** typical. Measured at the
header: **~8 ns**. The difference is real but expected — the measurement is a round trip
including our clock's trace delay, the 24 Ω series resistors, ~30 pF of pad and connector
capacitance, and probe skew. Treat 8 ns, not 2.1 ns, as the number that limits clock rate.

### Exposure and gain values

`ramp_gain` is **MCLK-dependent** (Table 19): 0.79/0.99/1.32/1.97 at 12–16 MHz versus
0.83/1.04/1.39/2.10 at 63 MHz. Our decode tool prints the 12–16 MHz column, so labels are
up to ~1.5 % optimistic at 24.75 MHz and ~5 % at 49.5 MHz. Cosmetic — it is a display
string, not used in any calculation.

## 5. Datasheet inconsistencies found

Recorded so nobody loses an afternoon to them.

**The stated minimum exposure is unreachable.** Key specifications give exposure as
0.13–261 ms. The maximum checks out: `rows_delay = 31` at 12.3 MHz gives 268,304 PP =
261.8 ms. The minimum does not. With `rows_in_reset` at its documented maximum (159, i.e.
320 rows in reset), Equation 2 floors at **1,312 PP**:

| SCLK | Minimum `t_exp` from Eq. 2 |
|---|---|
| 12.375 MHz | 1.27 ms |
| 24.75 MHz | 0.64 ms |
| 49.5 MHz | 0.32 ms |
| 62.6 MHz (HS max) | 0.25 ms |

0.13 ms corresponds to 656 PP at 62.6 MHz — exactly the `t_rows_in_readout` term, which
Equation 2 always *subtracts*. So the quoted minimum appears to come from a different
definition than the equation. Our firmware reports the equation's result, which is the one
consistent with the register fields.

**`rows_in_reset` maximum is stated in the wrong units.** §6.5.1 says "rows_in_reset[7:0]
maximum value is equal to the total number of sensor rows". Taken literally that means 320,
which does not fit in 8 bits. It must mean the *derived* row count `2n + 2 ≤ 320`, so
`n ≤ 159`. We enforce 159 (see §7).

**Two encodings for the same clock divider.** Table 20 lists `mclk_mode` `2: Main clock /2`
and `3: Main clock /2`. Harmless; we emit 2.

**Phase order differs between prose and figure.** The §6.3.2.2 body text lists INTERFACE
MODE third, between INITIAL PRE-SYNC and SYNC. Table 9 and Figure 10 agree with each other
and put INTERFACE at the end of each frame. We follow Table 9 and Figure 10, which is also
what the reference capture shows.

**Frame-rate figures disagree.** Key specifications say 4–38 fps (5–49 fps HS); Table 21
says 9/19/38 (12/24/49 HS). The lower bounds come from extending `rows_delay`, not from a
clock mode.

**Last pixel's stop bit.** §6.4.1.1 says the stop bit is missing from pixel (320,320) — in
the LVDS description. The SEIM section does not repeat it, and every stop bit validated in
the reference capture. We count validation failures rather than aborting on the first, so
either behaviour is tolerated.

**Typo,** for what it's worth: Table 7 reads "Clock in to data our delay".

## 6. A limitation of the start/stop validity check

`word_is_pixel()` tests start = 1 and stop = 0. The pre-sync training word `0xAAA` =
`101010101010` **passes that test** and decodes as pixel value 341.

So the cheap per-word check cannot detect a sensor stuck in pre-sync. That is why each row
also verifies its 8 leading training words against the expected pattern
(`count_training()`), and why `PROBE` reports `0x555`, `0xAAA` and `0x000` counts
separately instead of just "valid pixels".

## 7. Bugs this review found, and fixed

| # | Problem | Fix |
|---|---|---|
| 1 | `EXP` accepted `rows_in_reset` up to 255. Above 159 it exceeds the documented maximum and drives Equation 2 negative (`exposure_pp()` clamped to 0, so the reported exposure silently became nonsense while the register still got an out-of-spec value) | Added `ROWS_IN_RESET_MAX = (HEIGHT - 2) / 2` = 159, derived from geometry rather than hard-coded; `EXP` now clamps and says it clamped |
| 2 | `rows_delay` above 31 was silently truncated by `pack()`'s 5-bit mask — a request for 50 quietly became 18 | Clamped explicitly to `ROWS_DELAY_MAX` with a reply |
| 3 | `--source replay` resolved `build/golden` against the working directory, so the viewer only ran from the repository root | Candidate paths now include one resolved against the repo root |
| 4 | After `doc/` became untracked, a fresh clone had no reference capture, so `--source replay` — the documented no-hardware path — could not start at all | Falls back to generated frames, labelled `SYNTHETIC` so it can never be mistaken for sensor data; `allow_synthetic=False` for analysis code that must have real data |
| 5 | README and docs told people to build with `uv run python -m platformio`, but platformio was never a project dependency | Added a `firmware` dependency group |

Items 1 and 2 are datasheet-compliance bugs; 3–5 are why "the viewer won't run".

## 8. Firmware and USB API review

A second pass, over the driver internals and the protocol rather than the constants.

### Bus contention on the alignment clocks — the significant one

`start()` cleared idle mode, released SDAT, and then called `bitbang_clocks(10)` for the
alignment clocks. But that function unconditionally drove SDAT low. By then the sensor has
entered INITIAL PRE-SYNC MODE and is **transmitting**, so we would have been driving the
line against its output driver — precisely what §6.3.2.2's caution makes the host's
responsibility to avoid.

`bitbang_clocks()` now takes an explicit `drive_sdat`: true for the activation clock (SDAT
low, as the reference host does), false for the alignment clocks. Worth noting how this
would have presented on hardware: not as a clean failure, but as a sensor that sometimes
syncs and sometimes does not, depending on drive strengths — the kind of fault that gets
blamed on wiring for a day.

### Interface window: 324 frames to 4

The filler was 322 separate 24-bit LPSPI frames. The sensor counts clocks rather than time,
so the gaps between them do not break alignment — but wall-clock time spent in the interface
window *is* integration time, so 322 gaps would stretch the real exposure past what the
formula predicts. The filler now goes out as two maximum-size frames.

### Other fixes

| Area | Problem | Fix |
|---|---|---|
| `bitbang_clocks` | Restored only the pin mux, not the pad control register, so `pinMode()`'s drive strength and slew survived the hand-back to LPSPI | Cache and restore both |
| `capture_frame`, `probe_sync` | A row timeout returned with the next row's DMA still armed, leaving a channel live for a later stray request | `s_rx.disable()` on the error path |
| `send_text` | `vsnprintf` returns the length it *wanted*, so a truncated message set `payload_len` one byte beyond what was written | Clamp to what was actually written |
| `send_text` | Wrote the header and text as two calls, so a stalled host could leave half a text packet in the stream | Build one contiguous packet and write it in a single call |
| `crc32_update` | Silently produced wrong CRCs if `crc32_init()` had not run | Lazy init, once per call rather than per byte |
| `REG` | Any address above 0 was treated as CONFIG_1, and values wider than 16 bits were truncated silently | Reject both, since only two registers exist |
| `DEPTH` | `DEPTH 99` fell back to gray8 but *reported* 99 | Reject unknown values and leave the format alone |
| `CLK` | Changing the clock mid-stream leaves one frame whose internal MCLK does not match | Still allowed, but the reply now says to restart |

### Deliberate behaviours, not defects

- **Commands are polled between frames, never during one.** `capture_frame()` owns the CPU
  for the whole readout, so a command can wait up to one frame period (~52 ms at 24.75 MHz).
  Handling `START` or `PROBE` mid-readout would re-enter the driver underneath itself.
- **Host to device is ASCII, device to host is framed.** The spec originally claimed framing
  in both directions and the firmware never implemented it; rather than add a parser nothing
  needs, the asymmetry is now documented as the intent. Framing earns its keep in the
  machine-parsed direction and costs nothing in the direction a human types into. The
  `command` packet type stays reserved.
- **A frame is dropped whole rather than truncated**, and USB disconnection mid-payload can
  still leave a partial packet on the wire. The host recovers by resynchronising on the
  magic word and verifying the CRC, which `tests/test_transport.py` covers directly.

## 9. Board population, and who owns SDAT in the last PP

Prompted by learning that **R13 and R33 are not fitted** (the schematic's NoBom was
accurate; an earlier "everything is mounted" was not).

**Floating nets.** Those are the 10k header pull-downs on SDAT and SCLK, and
`SPI1.begin()` configures the pads with no pull, so both nets floated whenever undriven —
SCLK notably while LPSPI is being reconfigured, when a spurious edge could slip word
alignment silently. Fixed by enabling the pads' internal 100k pull-downs; details and the
option of fitting the real parts are in [hardware](hardware.md#r13-and-r33-are-not-fitted).

**The last interface PP.** Working out when SDAT is undriven meant re-reading §6.4.3, which
says the *device* transmits an end-of-interface word (`0x015` in SEIM) in the last PP of
INTERFACE MODE. The firmware drove all 648 PP, following AN000611 — so if the datasheet is
right, it was in contention with the sensor for one PP every frame.

The reference capture does not decide it. On the true PP grid (derived from row starts,
since the run detector absorbs `0x015`'s trailing `0101` into the training run) the last
interface PP reads `0x000` in every frame — but the Pi was driving low through it too, and a
low-impedance GPIO beats a current-limited 9.6 mA output, so the analyser would read `0x000`
either way. It is consistent with both "the sensor is silent" and "the working reference
host fought it every frame".

Given that, the firmware now drives 647 PP and **releases SDAT for the last one**, receiving
it instead of discarding it. Releasing is free if the sensor is silent and removes the
contention if not; receiving means M2 answers the question outright:

| `PROBE` / `STATS` reports | Meaning |
|---|---|
| `end_of_interface=0x015` | the datasheet is right; releasing avoided a fight every frame |
| `end_of_interface=0x000` | the sensor is silent there; AN000611's recipe was harmless |
| anything else | the phase accounting is off by some number of bits — investigate before trusting images |

That third row is the useful by-product: the word lands in a fixed place in every frame, so
it doubles as a per-frame alignment check once its expected value is known.

## 10. Still open

Written before any hardware was attached; updated after first light (2026-09-18).

- *Resolved on hardware:* the three LPSPI risks (receive-only framing via `TXMSK`, SDAT
  release timing, and the clock accounting in `start()`). The first two work as designed.
  The third could not work as designed, because the first frame's length varies, and was
  replaced by a row lock. See the [firmware page](firmware.md#known-risks).
- *Found on hardware:* the datasheet's start sequence (AN000611) was unreliable on this
  board, and the NanoBerry's sensor rail needs about 1 s off for a clean power-on reset.
  Both are in the [design record](design.md) (§4.2, §6.4).
- **Our running `CONFIG_1` does not use the recommended analog settings.** It keeps
  `vref = 01` and `cvc_curr = 11` from the reference host's first write, where the
  datasheet recommends `vref = 10` (2.1 V, 0.1 V below `offset_ramp`) and `cvc_curr = 01`,
  and where the reference host's own running value `0x0065` uses them. This may account
  for part of the high black level (~420 DN) and should be tried first when tuning image
  quality.
- **The LED current formula rests on a part-number inference.** `I_LED ≈ V_DAC / 56 Ω`
  with a 0–2.5 V DAC assumes the `-LZ12` suffix means 2.5 V full scale and power-on reset
  to zero scale. The LTC2630 datasheet is *not* in `doc/`; this came from the part number
  alone. Measure `V_DAC` or the actual LED current at a known code before trusting the mA
  figure — the zero-scale reset behaviour is the part that matters, and it is also the part
  that made DAC control necessary at all.
- Whether `output_curr = 9.6 mA` (maximum drive) is the best choice. It gives the fastest
  edges but is the noisiest option for the analog front end; worth sweeping down once the
  link is reliable (spec.md §6.7).
