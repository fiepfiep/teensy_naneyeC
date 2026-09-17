#include "led_dac.h"

#include <Arduino.h>

#include "board.h"

namespace led {

// LTC2630 commands (upper nibble of the first byte).
constexpr uint8_t CMD_WRITE_UPDATE = 0x30;  // write input register and update, power up
constexpr uint8_t CMD_POWER_DOWN = 0x40;

static bool s_enabled = false;
static float s_limit_ma = DEFAULT_LIMIT_MA;
static uint16_t s_code = 0;

uint16_t code_for_current_ma(float ma) {
    if (ma <= 0.0f) return 0;
    const float volts = ma * 0.001f * R_SENSE_OHM;
    float code = volts / DAC_FULL_SCALE_V * (float)DAC_MAX_CODE;
    if (code < 0.0f) code = 0.0f;
    if (code > (float)DAC_MAX_CODE) code = (float)DAC_MAX_CODE;
    return (uint16_t)(code + 0.5f);
}

float current_ma_for_code(uint16_t code) {
    if (code > DAC_MAX_CODE) code = DAC_MAX_CODE;
    return (float)code / (float)DAC_MAX_CODE * DAC_FULL_SCALE_V / R_SENSE_OHM * 1000.0f;
}

// 24 bits, MSB first: command byte then the 12-bit code left-justified in 16 bits.
// SDI is set while SCK is low and captured on the rising edge; CS high loads the value.
static void dac_write(uint8_t cmd, uint16_t code) {
    const uint8_t bytes[3] = {cmd, (uint8_t)(code >> 4), (uint8_t)((code & 0x0F) << 4)};
    digitalWriteFast(board::PIN_LED_DAC_SCK, LOW);
    digitalWriteFast(board::PIN_LED_DAC_CS, LOW);
    delayNanoseconds(100);
    for (int i = 0; i < 3; i++) {
        for (int b = 7; b >= 0; b--) {
            digitalWriteFast(board::PIN_LED_DAC_SDI, (bytes[i] >> b) & 1);
            delayNanoseconds(100);
            digitalWriteFast(board::PIN_LED_DAC_SCK, HIGH);
            delayNanoseconds(100);
            digitalWriteFast(board::PIN_LED_DAC_SCK, LOW);
        }
    }
    delayNanoseconds(100);
    digitalWriteFast(board::PIN_LED_DAC_CS, HIGH);
    digitalWriteFast(board::PIN_LED_DAC_SDI, LOW);
}

void begin() {
    pinMode(board::PIN_LED_VCC_ON, OUTPUT);
    pinMode(board::PIN_LED_DAC_CS, OUTPUT);
    pinMode(board::PIN_LED_DAC_SDI, OUTPUT);
    pinMode(board::PIN_LED_DAC_SCK, OUTPUT);
    digitalWriteFast(board::PIN_LED_VCC_ON, LOW);
    digitalWriteFast(board::PIN_LED_DAC_CS, HIGH);
    digitalWriteFast(board::PIN_LED_DAC_SDI, LOW);
    digitalWriteFast(board::PIN_LED_DAC_SCK, LOW);
    s_enabled = false;
    s_code = 0;
    // Leave the DAC powered down and at zero scale, which is also its reset state.
    dac_write(CMD_POWER_DOWN, 0);
}

void set_enabled(bool on) {
    if (on) {
        digitalWriteFast(board::PIN_LED_VCC_ON, HIGH);
        dac_write(CMD_WRITE_UPDATE, s_code);  // powers the DAC up and re-applies the code
    } else {
        dac_write(CMD_POWER_DOWN, 0);
        digitalWriteFast(board::PIN_LED_VCC_ON, LOW);
    }
    s_enabled = on;
}

bool enabled() { return s_enabled; }

float set_current_ma(float ma) {
    if (ma < 0.0f) ma = 0.0f;
    if (ma > s_limit_ma) ma = s_limit_ma;
    s_code = code_for_current_ma(ma);
    if (s_enabled) dac_write(CMD_WRITE_UPDATE, s_code);
    return current_ma_for_code(s_code);
}

float current_ma() { return current_ma_for_code(s_code); }

float set_max_current_ma(float ma) {
    if (ma < 0.0f) ma = 0.0f;
    if (ma > MAX_CURRENT_MA) ma = MAX_CURRENT_MA;
    s_limit_ma = ma;
    if (current_ma() > s_limit_ma) set_current_ma(s_limit_ma);
    return s_limit_ma;
}

float max_current_ma() { return s_limit_ma; }

}  // namespace led
