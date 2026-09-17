// Pin assignment for the NanEyeC / NanoBerry <-> Teensy 4.1 interface.
// See spec.md section 4.1. Changing these means rewiring; the LPSPI pins in particular are
// fixed by the peripheral (LPSPI3 = Teensy "SPI1").
#pragma once

#include <Arduino.h>

namespace board {

// --- Camera link (LPSPI3 / SPI1) -------------------------------------------------------
// SDAT is bidirectional and is wired to BOTH pins, tied together at the NanoBerry header.
// PIN_SDAT_OUT drives during INTERFACE MODE and is switched to a hi-Z input for readout;
// PIN_SDAT_IN always reads. See spec.md section 4.3.
constexpr uint8_t PIN_SCLK = 27;      // -> J2.23  NanEye_SCK_RP  (through R20 24R)
constexpr uint8_t PIN_SDAT_OUT = 26;  // -> J2.19  Naneye_Data_RP (through R23 24R)
constexpr uint8_t PIN_SDAT_IN = 1;    // -> J2.19  same node

// --- Sensor power ----------------------------------------------------------------------
// Drives the TPS71701 LDO enable (10k pulldown on board, so the sensor is OFF at reset).
constexpr uint8_t PIN_SENSOR_EN = 2;  // -> J2.33  Naneye_EN

// --- Illumination (spec.md section 4.4) ------------------------------------------------
constexpr uint8_t PIN_LED_VCC_ON = 3;  // -> J2.31  LED_VCC_ON_1, enables LT3473 boost
constexpr uint8_t PIN_LED_DAC_CS = 4;  // -> J2.36  LED_DAC_CS_N
constexpr uint8_t PIN_LED_DAC_SDI = 5; // -> J2.38  LED_DAC_SDI
constexpr uint8_t PIN_LED_DAC_SCK = 6; // -> J2.40  LED_DAC_SCK

}  // namespace board
