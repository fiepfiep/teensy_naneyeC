// Hardware watchdog: RTWDOG (WDOG3) of the i.MX RT1062, clocked from the 32 kHz LPO so it
// keeps running whatever happens to the PLLs.
//
// If the firmware stops feeding it for WATCHDOG_TIMEOUT_MS the chip resets, the USB serial
// port re-enumerates, and the host sees the device come back. The reset cause is latched at
// boot so ID can say "last reset: watchdog" -- a hang is then visible, not silent.
//
// Feed it from the main loop and from inside anything that can legitimately run long: one
// row of the sensor link is 0.3 ms, the longest blocking operation (START's one-frame run-in,
// or LISTEN 2000) about 0.7 s at 12.375 MHz, so 2 s leaves margin without letting a real hang
// sit for long.
#pragma once

#include <stdint.h>

namespace watchdog {

constexpr uint32_t WATCHDOG_TIMEOUT_MS = 2000;

// Latch and clear the reset cause, then start the watchdog. Call once, early in setup().
void begin();
void feed();
// True if the previous reset was the watchdog firing.
bool last_reset_was_watchdog();
// SRC_SRSR as it was at boot, for diagnostics.
uint32_t reset_status();

}  // namespace watchdog
