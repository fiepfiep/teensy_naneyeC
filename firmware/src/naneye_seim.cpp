#include "naneye_seim.h"

#include <Arduino.h>
#include <DMAChannel.h>
#include <SPI.h>
#include <string.h>

#include "board.h"

namespace seim {

using namespace naneye;

// --- LPSPI register bits (i.MX RT1060 RM, chapter 47) ----------------------------------
// Defined locally rather than relying on header macro names, which vary between cores.
namespace reg {
constexpr uint32_t CR_MEN = 1u << 0;
constexpr uint32_t CR_RST = 1u << 1;
constexpr uint32_t CR_RTF = 1u << 8;  // reset transmit FIFO
constexpr uint32_t CR_RRF = 1u << 9;  // reset receive FIFO

constexpr uint32_t SR_TDF = 1u << 0;
constexpr uint32_t SR_FCF = 1u << 9;   // frame complete
constexpr uint32_t SR_REF = 1u << 12;  // receive error (FIFO overflow)

constexpr uint32_t CFGR1_MASTER = 1u << 0;
constexpr uint32_t CFGR1_SAMPLE = 1u << 1;

constexpr uint32_t DER_RDDE = 1u << 1;

constexpr uint32_t TCR_TXMSK = 1u << 18;
constexpr uint32_t TCR_RXMSK = 1u << 19;

constexpr uint32_t SR_STICKY = 0x3F00u;

inline uint32_t framesz(uint32_t bits) { return (bits - 1u) & 0xFFFu; }
}  // namespace reg

// Largest frame the peripheral supports, rounded down to a whole number of pixel periods.
constexpr uint32_t MAX_FRAME_BITS = 4092;                    // 341 PP
constexpr uint32_t ROW_BITS = ROW_PP * PP_BITS;              // 3936
constexpr uint32_t INTERFACE_BITS = INTERFACE_PP * PP_BITS;  // 7776
constexpr uint32_t REG_WRITE_BITS = 24;                      // one register write = 2 PP
constexpr uint32_t INTERFACE_FRAMES = INTERFACE_BITS / REG_WRITE_BITS;  // 324, exact
constexpr uint32_t EOF_BITS = EOF_PP * PP_BITS;              // 96

// Spin limit for the hardware waits: generous enough never to trip in normal operation,
// small enough that a mis-configured peripheral reports a fault instead of hanging.
constexpr uint32_t SPIN_LIMIT = 40000000u;

#define LPSPI IMXRT_LPSPI3_S

// Two row buffers, each with one extra word of padding so pp_at() may read w[wi + 1].
static DMAMEM uint32_t s_row[2][ROW_WORDS_PADDED] __attribute__((aligned(32)));
static DMAChannel s_rx;

static bool s_powered = false;
static bool s_streaming = false;
static bool s_delayed_sample = false;
static bool s_first_frame_after_por = true;
static uint16_t s_cfg0 = REF_CONFIG0;
static uint16_t s_cfg1 = REF_CONFIG1_IDLE;
static const ClockSetting* s_clock = &CLOCKS[0];
static uint32_t s_mux_sdat = 0;  // IOMUX value connecting pin 26 to LPSPI3_SDO
static uint32_t s_mux_sclk = 0;  // IOMUX value connecting pin 27 to LPSPI3_SCK
static uint32_t s_pad_sdat = 0;  // pad control (drive, slew) as SPI1.begin() left it
static uint32_t s_pad_sclk = 0;

// --- SDAT direction (spec.md section 4.3) ----------------------------------------------
// Pin 26 drives during INTERFACE MODE and is a hi-Z input otherwise; pin 1 always reads.
// TCR[TXMSK] tristates the output too, so this is belt and braces.
static inline void sdat_drive() { *portConfigRegister(board::PIN_SDAT_OUT) = s_mux_sdat; }
static inline void sdat_hiz() { pinMode(board::PIN_SDAT_OUT, INPUT); }

// --- Low-level frame helpers -----------------------------------------------------------
// CPOL=0, CPHA=0: the sensor launches data ~8 ns after the rising edge and we sample on the
// following rising edge, which measured 24 ns of setup at 31.25 MHz (spec.md section 3.1).
static inline uint32_t tcr_base() { return 0; }

static inline bool wait_frame() {
    uint32_t guard = 0;
    while (!(LPSPI.SR & reg::SR_FCF)) {
        if (++guard > SPIN_LIMIT) return false;
    }
    LPSPI.SR = reg::SR_FCF;
    return true;
}

// Clock `pp` pixel periods with the output masked, discarding everything received.
static bool clock_pp_discard(uint32_t pp) {
    uint32_t bits = pp * PP_BITS;
    while (bits) {
        uint32_t chunk = bits > MAX_FRAME_BITS ? MAX_FRAME_BITS : bits;
        LPSPI.TCR = tcr_base() | reg::framesz(chunk) | reg::TCR_TXMSK | reg::TCR_RXMSK;
        if (!wait_frame()) return false;
        bits -= chunk;
    }
    return true;
}

// Drive `bits` clocks with SDAT held at zero, in as few LPSPI frames as possible.
// The datasheet asks for the bus to stay driven for the whole interface window; doing that
// as 322 separate 24-bit frames would add 322 inter-frame gaps, and wall-clock time spent
// in the interface window is time the pixels keep integrating.
static bool drive_zeros(uint32_t bits) {
    while (bits) {
        uint32_t chunk = bits > MAX_FRAME_BITS ? MAX_FRAME_BITS : bits;
        LPSPI.TCR = tcr_base() | reg::framesz(chunk) | reg::TCR_RXMSK;
        for (uint32_t sent = 0; sent < chunk; sent += 32) {
            uint32_t guard = 0;
            while (!(LPSPI.SR & reg::SR_TDF)) {
                if (++guard > SPIN_LIMIT) return false;
            }
            LPSPI.TDR = 0;
        }
        if (!wait_frame()) return false;
        bits -= chunk;
    }
    return true;
}

// Emit one 24-bit word (two pixel periods) with SDAT driven: a register write, or filler.
static inline bool send24(uint32_t word) {
    LPSPI.TCR = tcr_base() | reg::framesz(24) | reg::TCR_RXMSK;
    uint32_t guard = 0;
    while (!(LPSPI.SR & reg::SR_TDF)) {
        if (++guard > SPIN_LIMIT) return false;
    }
    LPSPI.TDR = word & 0xFFFFFFu;
    return wait_frame();
}

// Start a row transfer: 3936 clocks, output masked, received words DMA'd into buf.
static inline void start_row(uint32_t* buf) {
    s_rx.destinationBuffer(buf, ROW_WORDS * 4);
    s_rx.enable();
    LPSPI.TCR = tcr_base() | reg::framesz(ROW_BITS) | reg::TCR_TXMSK;
}

static inline bool wait_row() {
    uint32_t guard = 0;
    while (!s_rx.complete()) {
        if (++guard > SPIN_LIMIT) return false;
    }
    s_rx.clearComplete();
    LPSPI.SR = reg::SR_FCF;
    return true;
}

static void configure_lpspi() {
    LPSPI.CR = 0;
    LPSPI.CR = reg::CR_RST;
    LPSPI.CR = 0;
    LPSPI.CFGR1 = reg::CFGR1_MASTER | (s_delayed_sample ? reg::CFGR1_SAMPLE : 0u);
    // SCK = root / (SCKDIV + 2). DBT = 0 keeps the gap between frames as short as possible,
    // so row boundaries cost the sensor as little as possible (spec.md section 6.5).
    LPSPI.CCR = (uint32_t)s_clock->sckdiv;
    LPSPI.FCR = 0;  // RX watermark 0: request DMA as soon as one word has arrived
    LPSPI.DER = reg::DER_RDDE;
    LPSPI.SR = reg::SR_STICKY;
    LPSPI.CR = reg::CR_RTF | reg::CR_RRF;
    LPSPI.CR = reg::CR_MEN;
}

// Bit-bang `n` clocks. LPSPI cannot make frames shorter than 8 bits, and the start-up
// sequence needs exactly 1 and then 10 clocks (AN000611 section 3.3). The reference host
// bit-banged these too (spec.md section 3.2).
//
// drive_sdat MUST be false once idle mode has been cleared: by then the sensor has entered
// INITIAL PRE-SYNC MODE and is driving SDAT itself, and the datasheet makes tristating the
// upstream driver before that point the host's responsibility.
static void bitbang_clocks(uint32_t n, bool drive_sdat) {
    pinMode(board::PIN_SCLK, OUTPUT);
    digitalWriteFast(board::PIN_SCLK, LOW);
    if (drive_sdat) {
        pinMode(board::PIN_SDAT_OUT, OUTPUT);
        digitalWriteFast(board::PIN_SDAT_OUT, LOW);
    } else {
        sdat_hiz();
    }
    for (uint32_t i = 0; i < n; i++) {
        delayNanoseconds(200);
        digitalWriteFast(board::PIN_SCLK, HIGH);
        delayNanoseconds(200);
        digitalWriteFast(board::PIN_SCLK, LOW);
    }
    // Hand SCLK back to LPSPI, restoring the pad settings pinMode() overwrote as well as
    // the mux: at 49.5 MHz the drive strength and slew configured by SPI1.begin() matter.
    *portControlRegister(board::PIN_SCLK) = s_pad_sclk;
    *portConfigRegister(board::PIN_SCLK) = s_mux_sclk;
    if (drive_sdat) {
        *portControlRegister(board::PIN_SDAT_OUT) = s_pad_sdat;
        *portConfigRegister(board::PIN_SDAT_OUT) = s_mux_sdat;
    }
}

// --- Setup -----------------------------------------------------------------------------
void begin() {
    pinMode(board::PIN_SENSOR_EN, OUTPUT);
    digitalWriteFast(board::PIN_SENSOR_EN, LOW);  // sensor stays off until asked
    pinMode(board::PIN_SDAT_IN, INPUT);

    // LPSPI root clock = PLL2_PFD2 (396 MHz) / 4 = 99 MHz (spec.md section 5.3).
    // The clock gate must be off while CBCMR is changed.
    CCM_CCGR1 &= ~CCM_CCGR1_LPSPI3(CCM_CCGR_ON);
    uint32_t cbcmr = CCM_CBCMR;
    cbcmr &= ~(CCM_CBCMR_LPSPI_PODF_MASK | CCM_CBCMR_LPSPI_CLK_SEL_MASK);
    cbcmr |= CCM_CBCMR_LPSPI_PODF(3) | CCM_CBCMR_LPSPI_CLK_SEL(3);  // /4, PLL2_PFD2
    CCM_CBCMR = cbcmr;
    CCM_CCGR1 |= CCM_CCGR1_LPSPI3(CCM_CCGR_ON);

    // Let the core library mux the pins, then remember the values so the SDAT direction can
    // be flipped, and the pins reclaimed after bit-banging, with single register writes.
    SPI1.begin();
    s_mux_sdat = *portConfigRegister(board::PIN_SDAT_OUT);
    s_mux_sclk = *portConfigRegister(board::PIN_SCLK);
    s_pad_sdat = *portControlRegister(board::PIN_SDAT_OUT);
    s_pad_sclk = *portControlRegister(board::PIN_SCLK);

    s_rx.begin();
    s_rx.source((volatile uint32_t&)LPSPI.RDR);
    s_rx.triggerAtHardwareEvent(DMAMUX_SOURCE_LPSPI3_RX);
    s_rx.disable();

    configure_lpspi();
    sdat_hiz();
}

void power(bool on) {
    digitalWriteFast(board::PIN_SENSOR_EN, on ? HIGH : LOW);
    s_powered = on;
    s_streaming = false;
    if (on) {
        delay(5);  // LDO ramp plus the sensor's internal power-on reset
        s_first_frame_after_por = true;
    }
}

bool powered() { return s_powered; }

const ClockSetting& set_clock(uint32_t want_hz) {
    s_clock = &nearest_clock(want_hz);
    Config1 c = Config1::unpack(s_cfg1);
    c.mclk_mode = s_clock->mclk_mode;
    c.high_speed = s_clock->high_speed;
    s_cfg1 = c.pack();
    configure_lpspi();
    return *s_clock;
}

uint32_t sclk_hz() { return s_clock->sclk_hz; }

void set_delayed_sample(bool on) {
    s_delayed_sample = on;
    configure_lpspi();
}

bool delayed_sample() { return s_delayed_sample; }

void set_config(uint16_t cfg0, uint16_t cfg1) {
    s_cfg0 = cfg0;
    s_cfg1 = cfg1;
}

uint16_t config0() { return s_cfg0; }
uint16_t config1() { return s_cfg1; }

// --- Phases ----------------------------------------------------------------------------
// INTERFACE MODE: exactly 324 frames of 24 bits with SDAT driven. The two register writes
// go first; the datasheet forbids writing in the last pixel period and asks for the bus to
// stay driven for the whole window to keep EMI off the floating line.
static void interface_window(uint16_t cfg0, uint16_t cfg1) {
    sdat_drive();
    send24(reg_write_packet(0, cfg0));
    send24(reg_write_packet(1, cfg1));
    drive_zeros(INTERFACE_BITS - 2 * REG_WRITE_BITS);
    sdat_hiz();
}

static inline uint32_t sync_delay_pp() {
    return SYNC_PP + rows_delay_pp(Config1::unpack(s_cfg1).rows_delay);
}

bool start() {
    if (!s_powered) power(true);

    Config1 c1 = Config1::unpack(s_cfg1);
    c1.output_mode = 0;  // SEIM
    c1.mclk_mode = s_clock->mclk_mode;
    c1.high_speed = s_clock->high_speed;

    // INITIAL INTERFACE MODE: one activation clock, then select SEIM with idle still on, as
    // the reference host does (spec.md section 3.2).
    bitbang_clocks(1, true);  // activation clock, SDAT low as in the reference capture
    c1.idle_mode = 1;
    sdat_drive();
    send24(reg_write_packet(0, s_cfg0));
    send24(reg_write_packet(1, c1.pack()));
    delayMicroseconds(20);

    // Release idle: the sensor starts streaming after this write.
    c1.idle_mode = 0;
    s_cfg1 = c1.pack();
    send24(reg_write_packet(0, s_cfg0));
    send24(reg_write_packet(1, s_cfg1));
    sdat_hiz();

    // 10 alignment clocks fix the 12-bit word phase, then INITIAL PRE-SYNC MODE. SDAT is
    // left released: the sensor is already transmitting by this point.
    bitbang_clocks(10, false);
    if (!clock_pp_discard(PRESYNC_PP)) return false;

    // SYNC + DELAY, then the first frame, which is discarded: its exposure is invalid
    // (confirmed saturated in the reference capture, spec.md section 3.5).
    if (!clock_pp_discard(sync_delay_pp())) return false;
    if (!clock_pp_discard(READOUT_PP)) return false;

    s_first_frame_after_por = false;
    s_streaming = true;
    return true;
}

void stop() {
    Config1 c = Config1::unpack(s_cfg1);
    c.idle_mode = 1;
    s_cfg1 = c.pack();
    interface_window(s_cfg0, s_cfg1);
    s_streaming = false;
}

bool streaming() { return s_streaming; }

bool capture_frame(uint8_t* dst, uint8_t format, FrameInfo& info, IdleFn idle) {
    memset(&info, 0, sizeof(info));

    // 1. INTERFACE MODE, rewriting both registers as the reference host does.
    interface_window(s_cfg0, s_cfg1);

    // 2. SYNC + DELAY, discarded.
    if (!clock_pp_discard(sync_delay_pp())) return false;

    // 3. READOUT. Row n's DMA runs while row n-1 is unpacked and the host is serviced.
    info.timestamp_us = micros();
    const uint32_t row_bytes = row_payload_bytes(format);
    const uint16_t expect = s_first_frame_after_por ? WORD_PRESYNC : WORD_TRAINING;

    start_row(s_row[0]);
    for (uint32_t r = 0; r < HEIGHT; r++) {
        uint32_t* cur = s_row[r & 1];
        if (!wait_row()) {
            s_rx.disable();  // do not leave a transfer armed for a later stray request
            info.rows_failed += HEIGHT - r;
            return false;
        }
        // Arm the next row first, then do the slow work while it is in flight.
        if (r + 1 < HEIGHT) start_row(s_row[(r + 1) & 1]);

        bool row_bad = count_training(cur, expect) < TRAINING_PP;
        uint8_t* out = dst + (size_t)r * row_bytes;
        uint32_t bad;
        switch (format) {
            case 1: bad = unpack_row_gray10(cur, out); break;
            case 2: bad = unpack_row_raw12(cur, out); break;
            default: bad = unpack_row_gray8(cur, out); break;
        }
        info.pixels_failed += bad;
        if (bad || row_bad) info.rows_failed++;
        if (idle) idle();
    }
    info.duration_us = micros() - info.timestamp_us;

    // 4. End of frame.
    if (!clock_pp_discard(EOF_PP)) return false;
    return true;
}

// Bring-up diagnostic (M2). Runs a complete, phase-correct frame cycle but tallies word
// statistics over the first `rows` rows instead of producing an image, then clocks out the
// rest of the frame so the sensor's state machine stays aligned.
void probe_sync(SyncReport& report, uint32_t rows) {
    memset(&report, 0, sizeof(report));
    if (rows > HEIGHT) rows = HEIGHT;

    interface_window(s_cfg0, s_cfg1);
    if (!clock_pp_discard(sync_delay_pp())) return;

    for (uint32_t r = 0; r < rows; r++) {
        start_row(s_row[0]);
        if (!wait_row()) {
            s_rx.disable();
            return;
        }
        for (uint32_t i = 0; i < ROW_PP; i++) {
            const uint16_t w = pp_at(s_row[0], i);
            if (report.words < 16) report.first_words[report.words] = w;
            report.words++;
            if (w == WORD_TRAINING) report.training_555++;
            else if (w == WORD_PRESYNC) report.training_AAA++;
            else if (w == WORD_EOF) report.zeros++;
            if (word_is_pixel(w)) report.pixel_like++;
        }
    }
    clock_pp_discard((HEIGHT - rows) * ROW_PP + EOF_PP);
}

}  // namespace seim
