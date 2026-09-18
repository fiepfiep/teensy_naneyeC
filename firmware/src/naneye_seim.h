// NanEyeC SEIM capture driver for Teensy 4.1 (LPSPI3 + eDMA).
//
// The whole frame is a deterministic sequence of clock counts (spec.md section 5.2), so the
// driver is a phase sequencer: emit exactly the right number of clocks per phase and switch
// the SDAT direction at the phase boundaries.
//
//   INTERFACE   648 PP = 7776 bits = 324 frames of 24 bits, MCU driving SDAT
//                 frame 0   CONFIG_0 write
//                 frame 1   CONFIG_1 write
//                 frames 2+ zeros (the datasheet asks for the bus to stay driven)
//   SYNC+DELAY  (656 + delay) PP, clocked and discarded, SDAT hi-Z
//   READOUT     320 rows x 3936 bits, captured by DMA
//   EOF         8 PP, discarded
//
// !! Not yet run on hardware. Everything here is derived from the datasheet, AN000611 and
// the decoded reference capture; the register-level setup is the part most likely to need
// adjustment during M1/M2 bring-up.
#pragma once

#include <stdint.h>

#include "naneye_regs.h"
#include "seim_unpack.h"

namespace seim {

// Invoked while a row transfer is in flight, so the caller can use the ~159 us of idle CPU
// (at 24.75 MHz) to push the previous frame to USB. Must not block.
typedef void (*IdleFn)();

struct FrameInfo {
    uint32_t rows_failed;     // rows with a bad training pattern or bad start/stop bits
    uint32_t pixels_failed;   // individual pixel words that failed validation
    uint32_t timestamp_us;    // start of readout
    uint32_t duration_us;     // readout duration
};

void begin();

// Sensor power via the on-board LDO enable. power(false) is also the recovery of last
// resort: it forces a full power-on reset of the sensor.
void power(bool on);
bool powered();

// Select one of the supported SCLK rates (spec.md section 5.3); the matching sensor
// mclk_mode/high_speed bits are applied to the config on the next frame.
const naneye::ClockSetting& set_clock(uint32_t want_hz);
uint32_t sclk_hz();

// Delay the input sampling point by one LPSPI functional-clock cycle (CFGR1[SAMPLE]).
// Intended as a timing knob for the higher clock rates; see spec.md R2.
void set_delayed_sample(bool on);
bool delayed_sample();

void set_config(uint16_t cfg0, uint16_t cfg1);
uint16_t config0();
uint16_t config1();

// Run the power-on sequence up to the point where the sensor is streaming, per AN000611
// section 3.3: activation clock, register writes, alignment clocks, then the initial
// pre-sync / sync / delay phases and the first (discarded) frame.
// Returns false if no valid training pattern is ever seen.
bool start();
void stop();
bool streaming();

// One complete frame cycle. Pixels are written to dst in the requested format; returns
// false only if the sensor link lost sync badly enough to abandon the frame.
bool capture_frame(uint8_t* dst, uint8_t format, FrameInfo& info, IdleFn idle);

// Diagnostics for bring-up (M2): clock the bus and report what the sensor is sending.
struct SyncReport {
    uint32_t words;            // pixel periods examined
    uint32_t training_555;     // words equal to 0x555
    uint32_t training_AAA;     // words equal to 0xAAA
    uint32_t zeros;            // words equal to 0x000
    uint32_t pixel_like;       // words with start=1 and stop=0
    uint16_t first_words[16];  // the first few words, for eyeballing
};
void probe_sync(SyncReport& report, uint32_t rows);

// The word received in the final pixel period of the most recent INTERFACE MODE, which the
// driver leaves undriven for the sensor. The datasheet says the sensor sends 0x015 there;
// AN000611 implies it does not. 0x000 means silent (SDAT is pulled down), 0xFFFF means no
// frame has run yet. Bring-up (M2) should settle which.
uint16_t last_interface_pp();

}  // namespace seim
