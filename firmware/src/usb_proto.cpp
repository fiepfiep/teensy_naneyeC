#include "usb_proto.h"

#include <Arduino.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>

namespace proto {

// IEEE 802.3 CRC-32 (reflected, poly 0xEDB88320) so it matches Python's zlib.crc32.
static uint32_t crc_table[256];
static bool crc_ready = false;

void crc32_init() {
    if (crc_ready) return;
    for (uint32_t i = 0; i < 256; i++) {
        uint32_t c = i;
        for (int k = 0; k < 8; k++) c = (c & 1u) ? (0xEDB88320u ^ (c >> 1)) : (c >> 1);
        crc_table[i] = c;
    }
    crc_ready = true;
}

uint32_t crc32_update(uint32_t crc, const void* data, size_t len) {
    if (!crc_ready) crc32_init();  // once per call, not per byte
    const uint8_t* p = (const uint8_t*)data;
    while (len--) crc = crc_table[(crc ^ *p++) & 0xFFu] ^ (crc >> 8);
    return crc;
}

void header_init(Header& h, uint8_t type, uint32_t payload_len) {
    memset(&h, 0, sizeof(h));
    h.magic = MAGIC;
    h.version = VERSION;
    h.type = type;
    h.header_len = (uint16_t)sizeof(Header);
    h.payload_len = payload_len;
    h.timestamp_us = micros();
}

void header_finish(Header& h, const void* payload, size_t payload_len) {
    uint32_t crc = crc32_begin();
    crc = crc32_update(crc, &h, HEADER_CRC_BYTES);
    if (payload && payload_len) crc = crc32_update(crc, payload, payload_len);
    h.crc32 = crc32_final(crc);
}

// Write everything or give up after a bounded wait, so a stalled host cannot wedge the
// capture loop. A refused write is reported to the caller, which counts it as a drop.
static bool write_all(const uint8_t* p, size_t len, uint32_t timeout_ms) {
    const uint32_t deadline = millis() + timeout_ms;
    while (len) {
        if (!Serial) return false;
        const int room = Serial.availableForWrite();
        if (room <= 0) {
            if ((int32_t)(millis() - deadline) >= 0) return false;
            yield();
            continue;
        }
        const size_t chunk = (size_t)room < len ? (size_t)room : len;
        const size_t n = Serial.write(p, chunk);
        p += n;
        len -= n;
    }
    return true;
}

bool send(const Header& h, const void* payload, size_t payload_len) {
    if (!write_all((const uint8_t*)&h, sizeof(h), 50)) return false;
    if (payload && payload_len) {
        if (!write_all((const uint8_t*)payload, payload_len, 200)) return false;
    }
    return true;
}

void send_text(uint8_t type, const char* fmt, ...) {
    // One contiguous packet: header then text, so a partial write cannot split them.
    static uint8_t packet[sizeof(Header) + 256];
    char* text = (char*)packet + sizeof(Header);
    const size_t room = sizeof(packet) - sizeof(Header);

    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(text, room, fmt, ap);
    va_end(ap);
    if (n < 0) return;
    // vsnprintf returns the length it *wanted*; clamp to what it actually wrote.
    size_t len = ((size_t)n >= room) ? room - 1 : (size_t)n;

    Header* h = (Header*)packet;
    header_init(*h, type, (uint32_t)len);
    header_finish(*h, text, len);
    write_all(packet, sizeof(Header) + len, 100);
}

}  // namespace proto
