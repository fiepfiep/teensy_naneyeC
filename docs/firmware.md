# Firmware architecture

Teensy 4.1 (i.MX RT1062, 600 MHz), PlatformIO + Teensyduino, with direct register access for
LPSPI, DMA and IOMUXC. Build: `uv run --group firmware python -m platformio run -d firmware`.

## Files

| File | Role | Hardware-dependent? |
|---|---|---|
| `naneye_regs.h` | register model, frame geometry, exposure and clock maths | no — pure logic |
| `seim_unpack.h` | 12-bit pixel-period extraction from 32-bit words | no — pure logic |
| `naneye_seim.cpp` | LPSPI3 + DMA driver, phase sequencer | **yes, and unproven** |
| `led_dac.cpp` | LTC2630 bit-bang | yes, simple |
| `usb_proto.cpp` | framing, CRC-32 | no |
| `main.cpp` | command interface, streaming loop, transmit pump | no |
| `golden_vector.h` | GENERATED test vector, one real sensor row | no |
| `board.h` | pin assignment | — |

The split is deliberate: everything that can be reasoned about without a sensor is in the
pure-logic headers, and `SELFTEST` exercises them on the device. The untestable part is
confined to one file.

## The central idea: a phase sequencer

A frame is a **deterministic sequence of clock counts** (see
[frame structure](seim.md#frame-structure)). The sensor's state machine advances on the
clocks we supply, so if we emit exactly the right number in each phase and flip the SDAT
direction at the boundaries, we stay aligned by construction. There is no clock recovery, no
PLL, no search loop in steady state.

```
capture_frame():
  INTERFACE    7776 clocks
                 24 bits   CONFIG_0 write          SDAT driven
                 24 bits   CONFIG_1 write          SDAT driven
                 7716 bits zeros, two frames       SDAT driven (keeps EMI off the line)
               [SDAT -> released, pulled down]
                 12 bits   last PP, received       the sensor's end-of-interface word
  SYNC+DELAY   (656 + rows_delay_pp) PP, clocked and discarded
  READOUT      320 x 3936 bits, DMA'd and unpacked
  EOF          8 PP, discarded
```

Note how cleanly the arithmetic lands: 648 PP = 7776 bits = 324 × 24 exactly, so a register
write is a whole number of pixel periods; and SYNC+DELAY at minimum delay is 4 × 3936 bits,
exactly four row-times.

The **last pixel period is left to the sensor**, which the datasheet says transmits an
end-of-interface word there, and is received rather than discarded so bring-up can see
whether it does. As a side effect SDAT is released a full PP before SYNC begins, so there is
no overlap at the phase boundary at all. See
[SEIM reference](seim.md#registers) for why this departs from AN000611.

The filler is driven as **two maximum-size frames rather than 322 small ones**. The sensor
counts clocks, not time, so gaps inside the interface window do not break alignment — but
wall-clock time spent there is time the pixels keep integrating, so 322 inter-frame gaps
would stretch the real exposure beyond what the formula predicts.

## Why one row per SPI frame

LPSPI supports frame sizes from 8 to **4096 bits** (`TCR[FRAMESZ]`). A row is 328 PP = 3936
bits, which fits. That choice was the single most important one in the driver:

- The peripheral shifts 3936 bits **without interruption**, so "no clock gap within a row"
  holds by construction rather than depending on `TCR[CONT]` behaviour that cannot be
  verified without hardware.
- Any inter-frame gap lands on a row boundary, which AN000611 says is the safe place, and
  which is where the [three-pixel corruption](seim.md#things-the-reference-capture-taught-us)
  does *not* occur.
- 3936 bits = 123 words of 32 bits exactly, and 3 words = 8 PP exactly, so the unpack is a
  fixed repeating pattern with no straddling special cases.
- One DMA arm and one `TCR` write per row — 320 per frame, trivial — instead of per pixel.

The alternative (`FRAMESZ = 12`, one PP per frame) makes the unpack a one-liner but puts the
continuity of the clock at the mercy of FIFO scheduling. It remains the documented fallback
if the row-sized frame misbehaves.

## Unpacking

The sensor sends MSB first and LPSPI (`LSBF=0`) places the first received bit in bit 31, so
a row is simply a big-endian bit stream:

```c
static inline uint16_t pp_at(const uint32_t* w, uint32_t idx) {
    const uint32_t bit = idx * 12, wi = bit >> 5, off = bit & 31;
    const uint64_t acc = ((uint64_t)w[wi] << 32) | (uint64_t)w[wi + 1];
    return (acc >> (52u - off)) & 0xFFFu;
}
```

Row buffers carry **one extra word of padding** so `w[wi + 1]` is always readable — at
`idx = 327` the read reaches word 123. That is what `ROW_WORDS_PADDED` is for.

`host/naneye/decode.py::pp_at` is the same function in Python, and
[tests](host.md#tests) hold both to the golden row that `SELFTEST` uses on the device. The
two implementations cannot drift apart without a test failing.

## Clocking

LPSPI root clock: `CCM_CBCMR[LPSPI_CLK_SEL] = 3` (PLL2_PFD2, 396 MHz) with `LPSPI_PODF = 3`
(÷4) → **99 MHz**. Then `SCLK = 99 MHz / (SCKDIV + 2)`:

| `SCKDIV` | SCLK | Sensor mode | Error vs internal MCLK |
|---|---|---|---|
| 6 | 12.375 MHz | `mclk_mode=2` | +0.6 % |
| 2 | 24.750 MHz | `mclk_mode=1` | +0.2 % |
| 0 | 49.500 MHz | `mclk_mode=0` | +0.8 % |

99 MHz was chosen precisely because these three land within 1 %. The obvious 132 MHz root
(PLL2 ÷ 4) would give 6–7 % errors, which costs dynamic range —
[the external clock must match the internal MCLK](seim.md#clock-rates).

The gate must be off while `CBCMR` is written, and the change affects **all** LPSPI
instances. Only LPSPI3 is used here; the LED DAC is bit-banged specifically so no second
SPI peripheral is involved.

`CCR[DBT] = 0` keeps the inter-frame gap as short as the peripheral allows, because that gap
is the row boundary.

## SDAT direction

```c
static inline void sdat_drive() { *portConfigRegister(PIN_SDAT_OUT) = s_mux_sdat; }
static inline void sdat_hiz()   { pinMode(PIN_SDAT_OUT, INPUT); }
```

`SPI1.begin()` does the pin muxing once at startup; the mux **and pad control** values for
SDO and SCK are cached, so the direction flip and the hand-back after bit-banging are single
register writes. The pad registers matter because `pinMode()` overwrites drive strength and
slew, which at 49.5 MHz is not something to leave to chance.

Bit-banging exists because LPSPI cannot produce frames shorter than 8 bits, and the start-up
sequence needs exactly **1** activation clock and then **10** alignment clocks. The
reference host bit-banged these too.

!!! warning "The alignment clocks must not drive SDAT"
    `bitbang_clocks()` takes an explicit `drive_sdat` flag. The activation clock is sent
    with SDAT driven low, as the reference host does. The 10 alignment clocks are **not**:
    they come after idle mode is cleared, by which point the sensor has entered INITIAL
    PRE-SYNC and is driving SDAT itself. Driving it then is bus contention, and the
    datasheet makes releasing the upstream driver before the sensor transmits the host's
    responsibility. This was a real bug, found in review rather than on hardware.

## Buffering and the rule that must not be broken

> The capture loop owns the sensor link and must never block on USB.

A stalled host would cost sensor synchronisation, which is far more expensive than a lost
frame. So:

- Two 128,000-byte frame buffers in `DMAMEM` (OCRAM), enough for packed 10-bit.
- Capture fills one while the other is transmitted.
- Transmission happens in `tx_pump()`, which writes **only what the USB endpoint has room
  for right now** and is called from the row gaps via the `IdleFn` callback — ~159 µs of
  idle CPU per row at 24.75 MHz, against a few hundred cycles of unpacking.
- If a frame is still in flight when the next completes, the new frame is **dropped whole
  and counted**. A frame is never truncated.

`raw12` (209,920 bytes) borrows the whole region and so runs single-buffered; the loop waits
for the transmit to finish before capturing over it. It is a diagnostic format, not a
streaming one.

Memory, measured at link time: RAM2 269,408 bytes used of 524,288; RAM1 has 447 KB free for
locals. **PSRAM is not required** — row-chunked capture rather than whole-frame DMA is what
makes that true.

## Error handling

Every row is validated: the 8 training words must match, and all 320 pixel words must have
start = 1 and stop = 0. This is cheap, and since it passed 100 % across the reference
capture, any failure is signal rather than noise. Failures are counted per frame and
reported in the header; a failed capture triggers a re-run of `start()`. Cycling
`NanEye_EN` is the recovery of last resort.

## Register writes land one frame late

[Measured.](seim.md#exposure) The frame header therefore reports the configuration that was
*in force for that frame*, not the most recently written values — otherwise a recording's
metadata would be silently wrong by one frame, which for measurement work is worse than
useless.

## Commands

Plain ASCII lines in, always-framed packets out (spec.md §7). Full list in
`main.cpp::handle_command` and the [host page](host.md#commands).

Commands are polled between frames, not during one, because `capture_frame()` owns the CPU
for the whole readout. So expect up to one frame period of latency — ~52 ms at 24.75 MHz,
~104 ms at 12.375 MHz. That is deliberate: handling `START`, `STOP` or `PROBE` halfway
through a readout would re-enter the driver underneath itself.

## Diagnostics

Kept in the shipping firmware on purpose: these are what found the faults that hid first
light (see [Hardware: first light](hardware.md#first-light-what-it-took-2026-09-18)). None of
them is needed for normal streaming. All except `ID` and `SELFTEST` need streaming stopped.

| Command | What it does | Use it when |
|---|---|---|
| `ID` | Firmware version, actual SCLK (derived from the clock registers, not assumed), registers, format, and **last reset cause** (`normal` / `WATCHDOG`) | Always first. A `WATCHDOG` right after flashing is normal; see below |
| `SELFTEST` | Unpack and exposure maths against the embedded golden row | Separating decode bugs from link bugs |
| `PROBE [rows]` | A phase-correct frame cycle reporting word statistics instead of an image | Checking an already-running link without disturbing its phase |
| `LISTEN [rows]` | Clocks up to 2000 rows with SDAT released and classifies each one: `.` zeros, `A` 0xAAA, `S` 0x555, `P` pixels, `?` mixed. Prints a run-length map, e.g. `Ax3 Px320 ? . Sx4 Px320` | Finding out what the sensor is doing, with no assumptions about phase. Never drives SDAT, so it is always safe |
| `START REF [VERBATIM] [FAST] [EARLY] [FIRST] [rows]` | The reference host's start sequence, then (with `rows`) a gapless `LISTEN`. `VERBATIM` uses the reference's exact register values, `FAST` sends the first write pair at SCLK rate instead of bit-banged, `EARLY` releases SDAT straight after the idle-off write, `FIRST` stops after the idle-on pair | Bisecting a start-up that does not start |
| `START AN` | AN000611's single-write sequence. Known not to work reliably on this board (and 2 clocks off when it does); kept for comparison | Re-testing that finding |
| `ALIGN n` | Alignment clocks used by `START AN` (datasheet: 10) | Only with `START AN` |
| `CLKMEAS` | Measures SCLK on the pin, sensor off | After touching the clock tree |
| `WDTEST` | Hangs on purpose; the watchdog must reset the board within 2 s | Proving the watchdog still works |

Host-side companions, all driving the Saleae through its MCP server:

- `tools/show_bringup.py`: triggers on NanEye_EN, runs a start command, and plots an
  overview plus zooms of the idle-off write, the sensor's first output, and its launch delay.
- `tools/check_alignment.py`: where each row transfer lands relative to the sensor's own row
  starts. Every burst at the same offset, and that offset −96, means phase-locked.
- `tools/capture_link.py` / `tools/analyze_link.py`: general capture and link checks
  (clock rate, exact phase counts, register writes).

## Watchdog

RTWDOG (WDOG3), 2 s timeout on the 32 kHz LPO clock (`watchdog.cpp`). Fed from `loop()`,
per row in `LISTEN`, and while `power(true)` waits out the sensor's power-off time. The
longest legitimate blocking operation is about 0.7 s. After a reset the USB port
re-enumerates in about 0.3 s and `ID` reports it. Flashing usually leaves `last reset:
WATCHDOG` too: the old image parks in the bootloader hand-off with the watchdog running.
That is harmless.

## Known risks

Ordered by how likely they are to bite, all carried in the [design record](design.md):

1. Whether a bare `TCR` write with `TXMSK=1` really initiates a receive-only frame with no
   TX data. If not, a TX DMA feeding dummy words is needed.
2. Whether `pinMode(INPUT)` plus `TXMSK` releases SDAT quickly enough at the
   INTERFACE→SYNC boundary.
3. The clock accounting in `start()` — our counts and the sensor's state machine must agree
   to the bit, and the initial sequence has the least margin for error.
4. Signal integrity above 24.75 MHz on flying leads; `SAMPLE` (`CFGR1[SAMPLE]`) is the knob.
