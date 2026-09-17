// Illumination control: LT3473 boost enable plus the LTC2630 12-bit DAC that sets the
// LT3092 current sink. See spec.md section 4.4.
//
// I_LED ~= V_DAC / 56R, with V_DAC = code/4095 * 2.5 V  ->  0 .. 44.6 mA, 10.9 uA/LSB.
// The DAC resets to zero scale, so it must be programmed for any useful light.
//
// Write-only 24-bit SPI, bit-banged on three GPIOs: no second SPI peripheral and no
// contention with the camera bus.
#pragma once

#include <stdint.h>

namespace led {

constexpr float R_SENSE_OHM = 56.0f;    // R11
constexpr float DAC_FULL_SCALE_V = 2.5f;  // LTC2630-LZ12 internal reference
constexpr uint16_t DAC_MAX_CODE = 4095;
constexpr float MAX_CURRENT_MA = DAC_FULL_SCALE_V / R_SENSE_OHM * 1000.0f;  // 44.6

// Default safety ceiling; raise deliberately with set_max_current_ma().
constexpr float DEFAULT_LIMIT_MA = 20.0f;

void begin();

// Enable/disable the boost rail and power the DAC up/down. Current is re-applied on enable.
void set_enabled(bool on);
bool enabled();

// Request a current in mA, clamped to the configured ceiling. Returns the value actually
// applied (quantised to the DAC step).
float set_current_ma(float ma);
float current_ma();

// Raise or lower the safety ceiling (itself clamped to the hardware maximum).
float set_max_current_ma(float ma);
float max_current_ma();

// Exposed for testing and diagnostics.
uint16_t code_for_current_ma(float ma);
float current_ma_for_code(uint16_t code);

}  // namespace led
