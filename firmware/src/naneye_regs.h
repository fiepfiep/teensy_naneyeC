// NanEyeC register model, frame geometry and timing math.
//
// Pure logic, no hardware dependencies, so it can be reasoned about and unit tested on its
// own. Mirrors host/naneye/regs.py field for field; tests/test_regs.py checks the two agree.
//
// References: DS000503 section 6.3.2.2 (SEIM sequence), 6.4.2 (encoding), 6.4.3 (register
// access), 6.5.1 (exposure), section 7 (registers). Values verified against the reference
// capture in spec.md section 3.
#pragma once

#include <stdint.h>

namespace naneye {

// --- Frame geometry (DS000503 section 6.3.2.2) -----------------------------------------
constexpr uint32_t WIDTH = 320;
constexpr uint32_t HEIGHT = 320;
constexpr uint32_t PP_BITS = 12;            // one pixel period: start + 10 data + stop
constexpr uint32_t TRAINING_PP = 8;         // start-of-row identification
constexpr uint32_t ROW_PP = TRAINING_PP + WIDTH;   // 328
constexpr uint32_t EOF_PP = 8;
constexpr uint32_t READOUT_PP = HEIGHT * ROW_PP + EOF_PP;   // 104,968
constexpr uint32_t INTERFACE_PP = 648;
constexpr uint32_t SYNC_PP = 2 * ROW_PP;    // 656
constexpr uint32_t PRESYNC_PP = 329;        // INITIAL PRE-SYNC MODE, first frame only

// --- Word encodings (DS000503 section 6.4.2) -------------------------------------------
constexpr uint16_t WORD_TRAINING = 0x555;   // SYNC, DELAY, start-of-row
constexpr uint16_t WORD_PRESYNC = 0xAAA;    // INITIAL PRE-SYNC, and first row after POR
constexpr uint16_t WORD_EOF = 0x000;

// A pixel word must have start=1 and stop=0; anything else means lost alignment.
inline bool word_is_pixel(uint16_t w) { return (w >> 11) == 1u && (w & 1u) == 0u; }
inline uint16_t word_pixel(uint16_t w) { return (uint16_t)((w >> 1) & 0x3FFu); }

// --- Field limits (DS000503 section 6.5.1, 7) ------------------------------------------
// "rows_in_reset[7:0] maximum value is equal to the total number of sensor rows", and
// rows in reset = 2n + 2, so n <= (HEIGHT - 2) / 2 = 159. The field is 8 bits wide, so
// larger values fit but are out of specification and make the exposure formula negative.
constexpr uint8_t ROWS_IN_RESET_MAX = (uint8_t)((HEIGHT - 2) / 2);  // 159
constexpr uint8_t ROWS_DELAY_MAX = 31;                              // 5-bit field

// --- Register field packing (DS000503 section 7) ---------------------------------------
struct Config0 {
    uint8_t rows_in_reset = 0;   // [15:8] rows in reset = 2*n + 2
    uint8_t vrst_pix = 2;        // [7:6]  0:2.2V 1:2.4V 2:2.6V(rec) 3:2.8V
    uint8_t ramp_gain = 1;       // [5:4]  0:0.79x 1:0.99x 2:1.32x 3:1.97x
    uint8_t offset_ramp = 3;     // [3:2]  0:1.9V 1:2.0V 2:2.1V 3:2.2V(rec)
    uint8_t output_curr = 3;     // [1:0]  SEIM drive: 0:3.9 1:5.8 2:7.7 3:9.6 mA

    uint16_t pack() const {
        return (uint16_t)((uint16_t)rows_in_reset << 8 | (uint16_t)(vrst_pix & 3) << 6 |
                          (uint16_t)(ramp_gain & 3) << 4 | (uint16_t)(offset_ramp & 3) << 2 |
                          (uint16_t)(output_curr & 3));
    }
    static Config0 unpack(uint16_t v) {
        Config0 c;
        c.rows_in_reset = (uint8_t)(v >> 8);
        c.vrst_pix = (uint8_t)((v >> 6) & 3);
        c.ramp_gain = (uint8_t)((v >> 4) & 3);
        c.offset_ramp = (uint8_t)((v >> 2) & 3);
        c.output_curr = (uint8_t)(v & 3);
        return c;
    }
};

struct Config1 {
    uint8_t rows_delay = 0;          // [15:11] delay rows = 16*n + 2
    uint8_t bias_curr_increase = 0;  // [10]
    uint8_t cds_gain = 0;            // [9]  0:1.3(rec) 1:2.0
    uint8_t output_mode = 0;         // [8]  0:SEIM 1:LVDS
    uint8_t mclk_mode = 1;           // [7:6] 0:2x 1:default 2,3:/2
    uint8_t vref = 2;                // [5:4] 0:1.9V 1:2.0V 2:2.1V(rec) 3:2.2V
    uint8_t cvc_curr = 1;            // [3:2] recommended 1
    uint8_t idle_mode = 1;           // [1]  1 = idle (no streaming)
    uint8_t high_speed = 0;          // [0]

    uint16_t pack() const {
        return (uint16_t)((uint16_t)(rows_delay & 0x1F) << 11 |
                          (uint16_t)(bias_curr_increase & 1) << 10 |
                          (uint16_t)(cds_gain & 1) << 9 | (uint16_t)(output_mode & 1) << 8 |
                          (uint16_t)(mclk_mode & 3) << 6 | (uint16_t)(vref & 3) << 4 |
                          (uint16_t)(cvc_curr & 3) << 2 | (uint16_t)(idle_mode & 1) << 1 |
                          (uint16_t)(high_speed & 1));
    }
    static Config1 unpack(uint16_t v) {
        Config1 c;
        c.rows_delay = (uint8_t)(v >> 11);
        c.bias_curr_increase = (uint8_t)((v >> 10) & 1);
        c.cds_gain = (uint8_t)((v >> 9) & 1);
        c.output_mode = (uint8_t)((v >> 8) & 1);
        c.mclk_mode = (uint8_t)((v >> 6) & 3);
        c.vref = (uint8_t)((v >> 4) & 3);
        c.cvc_curr = (uint8_t)((v >> 2) & 3);
        c.idle_mode = (uint8_t)((v >> 1) & 1);
        c.high_speed = (uint8_t)(v & 1);
        return c;
    }
};

// Reference-capture values (spec.md section 3.2), used verbatim for bring-up.
constexpr uint16_t REF_CONFIG0 = 0x009F;
constexpr uint16_t REF_CONFIG1_IDLE = 0x009F;    // SEIM selected, idle still on
constexpr uint16_t REF_CONFIG1_STREAM = 0x0065;  // idle off -> streaming

// --- Register write packet (DS000503 section 6.4.3) ------------------------------------
// 24 bits: 1001 | 3-bit address | 16-bit data (MSB first) | 0
inline uint32_t reg_write_packet(uint8_t addr, uint16_t data) {
    return ((uint32_t)0x9u << 20) | ((uint32_t)(addr & 0x7u) << 17) | ((uint32_t)data << 1);
}

// --- Timing / exposure (DS000503 section 6.5.1) ----------------------------------------
inline uint32_t rows_delay_pp(uint8_t rows_delay) {
    return (16u * (rows_delay & 0x1Fu) + 2u) * ROW_PP;
}
inline uint32_t rows_in_reset_pp(uint8_t rows_in_reset) {
    return (2u * (uint32_t)rows_in_reset + 2u) * ROW_PP;
}
// Total pixel periods per frame, including the inter-frame phases.
inline uint32_t frame_total_pp(uint8_t rows_delay) {
    return INTERFACE_PP + SYNC_PP + rows_delay_pp(rows_delay) + READOUT_PP;
}
// Effective exposure in pixel periods. Returns 0 if the settings would make it negative.
inline uint32_t exposure_pp(uint8_t rows_in_reset, uint8_t rows_delay) {
    const uint32_t t_btw = INTERFACE_PP + SYNC_PP + rows_delay_pp(rows_delay);
    const uint32_t t_matrix = HEIGHT * ROW_PP + EOF_PP;
    const uint32_t t_readout = 2u * ROW_PP;
    const uint32_t subtract = rows_in_reset_pp(rows_in_reset) + t_readout;
    const uint32_t total = t_btw + t_matrix;
    return (subtract >= total) ? 0u : (total - subtract);
}

// --- Clock plan (spec.md section 5.3) --------------------------------------------------
// LPSPI root clock is PLL2_PFD2 (396 MHz) / 4 = 99 MHz; SCLK = 99 MHz / (SCKDIV + 2).
constexpr uint32_t LPSPI_ROOT_HZ = 99000000u;

struct ClockSetting {
    uint32_t sclk_hz;    // actual generated SCLK
    uint8_t sckdiv;      // LPSPI CCR SCKDIV
    uint8_t mclk_mode;   // matching sensor mclk_mode
    uint8_t high_speed;  // matching sensor high_speed
};

// Supported rates, all within 1 % of the sensor's internal MCLK.
constexpr ClockSetting CLOCKS[] = {
    {12375000u, 6, 2, 0},  // vs 12.3 MHz nominal, +0.6 % -> ~9.6 fps
    {24750000u, 2, 1, 0},  // vs 24.7 MHz nominal, +0.2 % -> ~19.3 fps
    {49500000u, 0, 0, 0},  // vs 49.1 MHz nominal, +0.8 % -> ~38.6 fps
};
constexpr uint32_t CLOCK_COUNT = sizeof(CLOCKS) / sizeof(CLOCKS[0]);

// Nearest supported setting to the requested rate.
inline const ClockSetting& nearest_clock(uint32_t want_hz) {
    uint32_t best = 0;
    uint32_t best_err = 0xFFFFFFFFu;
    for (uint32_t i = 0; i < CLOCK_COUNT; i++) {
        const uint32_t err = (CLOCKS[i].sclk_hz > want_hz) ? CLOCKS[i].sclk_hz - want_hz
                                                           : want_hz - CLOCKS[i].sclk_hz;
        if (err < best_err) {
            best_err = err;
            best = i;
        }
    }
    return CLOCKS[best];
}

}  // namespace naneye
