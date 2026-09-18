# Glossary

Terms used across these pages, in plain words. Datasheet references are to DS000503 v3-00
(NanEyeC datasheet) and AN000611 v2-00 (its MCU interface app note).

**Activation clock**
: The single clock pulse the host sends after power-up, before the first register write,
  to wake the sensor's configuration interface.

**Concealment**
: Replacing a pixel whose word arrived with broken framing (so is known to be wrong) by the
  mean of its intact neighbours. Counted per frame as `pixels_concealed`. It hides an
  error; it does not recover the true value, which SEIM gives no way to do.

**CONFIG_0, CONFIG_1**
: The sensor's only two registers, 16 bits each. CONFIG_0 holds exposure (`rows_in_reset`)
  and analog settings. CONFIG_1 holds the output mode, clock mode, frame delay and the
  **idle** bit that starts and stops streaming. Field by field: [SEIM reference](seim.md#registers).

**D-cache invalidation**
: The Teensy's processor caches memory. When DMA writes received data into RAM, the
  processor may still hold an old copy, so the firmware must discard ("invalidate") the
  cached copy before reading the buffer. Forgetting this was the bug that hid the first images.

**DMA**
: Direct memory access: hardware that moves received data into memory without the
  processor, so the processor can pack the previous row meanwhile.

**DN**
: Digital number, the raw pixel value: 0–1023 for 10-bit data.

**EOF**
: End of frame: 8 pixel periods of zeros the sensor sends after the last row.

**Exposure**
: How long each pixel collects light. Set by `rows_in_reset` in CONFIG_0 (the `EXP`
  command): 0 is the longest, about 102 ms at 12.375 MHz; 159 the shortest, about 1.3 ms.
  It scales with the clock rate.

**First frame**
: The first frame after power-up or leaving idle is overexposed, so the firmware always
  discards it (datasheet §6.3.2.2, confirmed by measurement). Nobody looks their best
  first thing in the morning.

**Frame phases**
: Each frame is a fixed sequence: **interface window** (648 PP), **SYNC** (656 PP of
  training pattern), **DELAY** (at least 656 PP, more to slow the frame rate), **READOUT**
  (320 rows) and **EOF** (8 PP). The sensor counts clock pulses to move through them.

**Golden vector**
: One real row of sensor data from the reference capture, with its expected pixel values,
  compiled into the firmware (`golden_vector.h`). `SELFTEST` and the host tests decode it
  and compare.

**Idle mode**
: A CONFIG_1 bit. While it is set the sensor sends nothing; clearing it starts streaming.
  The sensor powers up in idle.

**Interface window** (INTERFACE MODE)
: The 648 pixel periods between frames when the data line changes direction: the host
  drives SDAT and can write the registers. The sensor owns the window's last pixel period.

**LPSPI**
: The Teensy's SPI peripheral. LPSPI3 (Teensyduino's `SPI1`, pins 27/26/1) generates SCLK
  and shifts SDAT in and out.

**LVDS**
: The sensor's other output mode: differential, with the clock embedded in the data. It
  needs far faster sampling than a microcontroller has, so this project uses SEIM only.

**MCLK**
: The sensor's internal master clock. In SEIM mode the host's SCLK drives the sensor, but
  its ADC still runs on its own oscillator, so SCLK should match the MCLK setting
  (`mclk_mode`, `high_speed`) closely. The firmware's clock table does this.

**NanoBerry**
: ams's evaluation board carrying the NanEyeC, a 3.3 V regulator for it, LED illumination
  and a Raspberry Pi header.

**Pixel period (PP)**
: 12 clock pulses carrying one 12-bit word: a start bit (1), 10 data bits and a stop bit
  (0). All the sensor's timing is counted in PP. A row is 328 PP = 3936 clocks.

**Pre-sync** (INITIAL PRE-SYNC MODE)
: A stretch of training pattern that only the first frame after power-up has, before SYNC.
  Its length in clocks turned out to vary slightly between starts, which is why the
  firmware *finds* the row timing rather than counting it.

**Reference capture**
: A logic-analyser recording of a working NanoBerry ↔ Raspberry Pi link, decoded before any
  code was written. Kept locally in `doc/digital.csv`; not in the repository.

**Row lock**
: What `START` does after starting the sensor: it scans the incoming bits for the end of a
  row's training pattern and adjusts the clock count so every later transfer starts exactly
  on a row. From then on everything is counted.

**rows_failed**
: Per-frame count of rows that failed validation (wrong training words, or pixel words with
  bad start/stop bits). Should be 0. Reported in every frame header.

**rows_in_reset, rows_delay**
: The exposure field (CONFIG_0) and the frame-delay field (CONFIG_1). See
  [Exposure](seim.md#exposure).

**Sampling point**
: Where within each bit the Teensy reads SDAT: on the rising or falling SCLK edge, with or
  without one extra clock of delay. At 49.5 MHz only one of the four works on the bench
  wiring, so `START` measures all four on the training pattern and picks the best.

**SCLK**
: The clock the Teensy sends to the sensor. One data bit moves per rising edge. Supported
  rates: 49.5 MHz (default, ~35 fps), 24.75 and 12.375 MHz.

**SDAT**
: The single data line, used in both directions at different times: sensor → Teensy during
  readout, Teensy → sensor during the interface window.

**SEIM**
: Single-Ended Interface Mode: the sensor's two-wire serial mode (SCLK + SDAT, ordinary
  3.3 V logic levels), which a microcontroller can handle.

**SELFTEST**
: A firmware command that checks decoding and exposure arithmetic against the golden
  vector. It needs no camera.

**Training pattern**
: An alternating 0/1 pattern the sensor sends so the receiver can find the bit and word
  boundaries: `0x555` normally, `0xAAA` in the first frame after power-up. Every row starts
  with 8 training words.

**Watchdog**
: A hardware timer that resets the Teensy if the firmware stops responding for 2 s. `ID`
  reports whether the last reset came from it. It does not accept excuses.
