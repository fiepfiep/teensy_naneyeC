#include "watchdog.h"

#include <Arduino.h>

namespace watchdog {

namespace {

// RTWDOG register fields (i.MX RT1060 reference manual, chapter "RTWDOG").
constexpr uint32_t CS_UPDATE = 1u << 5;   // allow later reconfiguration
constexpr uint32_t CS_EN = 1u << 7;
constexpr uint32_t CS_CLK_LPO = 1u << 8;  // CLK = 01: 32 kHz low-power oscillator
constexpr uint32_t CS_RCS = 1u << 10;     // reconfiguration success
constexpr uint32_t CS_ULK = 1u << 11;     // unlocked
constexpr uint32_t CS_CMD32EN = 1u << 13;  // 32-bit unlock / refresh words
constexpr uint32_t UNLOCK_KEY = 0xD928C520u;
constexpr uint32_t REFRESH_KEY = 0xB480A602u;
constexpr uint32_t LPO_HZ = 32000;

uint32_t s_srsr = 0;

}  // namespace

void begin() {
    s_srsr = SRC_SRSR;
    SRC_SRSR = s_srsr;  // write-1-to-clear, so the next boot reports its own cause

    CCM_CCGR5 |= CCM_CCGR5_WDOG3(CCM_CCGR_ON);
    // Unlock, then configure within the 255-bus-clock window the unlock opens.
    __disable_irq();
    WDOG3_CNT = UNLOCK_KEY;
    while (!(WDOG3_CS & CS_ULK)) {
    }
    WDOG3_TOVAL = (uint32_t)((uint64_t)LPO_HZ * WATCHDOG_TIMEOUT_MS / 1000u);
    WDOG3_WIN = 0;
    WDOG3_CS = CS_EN | CS_CLK_LPO | CS_UPDATE | CS_CMD32EN;
    __enable_irq();
    while (!(WDOG3_CS & CS_RCS)) {
    }
}

void feed() {
    __disable_irq();
    WDOG3_CNT = REFRESH_KEY;
    __enable_irq();
}

bool last_reset_was_watchdog() { return (s_srsr & SRC_SRSR_WDOG3_RST_B) != 0; }

uint32_t reset_status() { return s_srsr; }

}  // namespace watchdog
