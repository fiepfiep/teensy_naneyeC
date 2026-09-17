"""SEIM decoding: device payloads and raw logic-analyser bit streams.

This is the canonical decoder. It is used for live frames from the Teensy, for Saleae
captures, and by tools/decode_golden.py, so one implementation is exercised by every path.
The pixel-period extraction mirrors firmware/src/seim_unpack.h exactly; tests/test_unpack.py
checks the two agree on the golden row embedded in the firmware.
"""

from __future__ import annotations

import numpy as np

from . import protocol

# --- Geometry (DS000503 section 6.3.2.2) -----------------------------------------------
WIDTH = 320
HEIGHT = 320
PP_BITS = 12
TRAINING_PP = 8
ROW_PP = TRAINING_PP + WIDTH          # 328
ROW_BITS = ROW_PP * PP_BITS           # 3936
ROW_WORDS = ROW_BITS // 32            # 123
EOF_PP = 8
INTERFACE_PP = 648
SYNC_PP = 2 * ROW_PP

WORD_TRAINING = 0x555
WORD_PRESYNC = 0xAAA
WORD_EOF = 0x000


# --- 12-bit extraction from 32-bit words (mirrors seim_unpack.h) -----------------------
def pp_at(words, idx: int) -> int:
    """The idx-th 12-bit pixel period of a big-endian bit stream held in 32-bit words."""
    bit = idx * PP_BITS
    wi, off = bit >> 5, bit & 31
    acc = (int(words[wi]) << 32) | int(words[wi + 1])
    return (acc >> (52 - off)) & 0xFFF


def words_to_pp(words, count: int) -> np.ndarray:
    """Vectorised equivalent of calling pp_at() for idx in range(count)."""
    w = np.asarray(words, dtype=np.uint32)
    if len(w) * 32 < count * PP_BITS + 32:
        w = np.concatenate([w, np.zeros(1, dtype=np.uint32)])
    idx = np.arange(count, dtype=np.int64)
    bit = idx * PP_BITS
    wi, off = bit >> 5, bit & 31
    acc = (w[wi].astype(np.uint64) << np.uint64(32)) | w[wi + 1].astype(np.uint64)
    return ((acc >> (52 - off).astype(np.uint64)) & np.uint64(0xFFF)).astype(np.uint16)


def validate_pp(pp: np.ndarray) -> np.ndarray:
    """True where a word looks like a pixel: start bit 1, stop bit 0."""
    return ((pp >> 11) == 1) & ((pp & 1) == 0)


def pp_to_pixels(pp: np.ndarray) -> np.ndarray:
    return ((pp >> 1) & 0x3FF).astype(np.uint16)


# --- Device payloads -------------------------------------------------------------------
def decode_payload(header: protocol.Header, payload: bytes) -> np.ndarray:
    """Decode one image payload into a 2-D array.

    gray8 -> uint8, gray10 -> uint16 (0..1023), raw12 -> uint16 pixel periods per row
    including the 8 training words, which is the diagnostic format.
    """
    h, w = header.height or HEIGHT, header.width or WIDTH
    buf = np.frombuffer(payload, dtype=np.uint8)

    if header.format == protocol.FMT_GRAY8:
        if buf.size != h * w:
            raise ValueError(f"gray8 payload {buf.size} != {h * w}")
        return buf.reshape(h, w).copy()

    if header.format == protocol.FMT_GRAY10:
        expect = h * (w // 4 * 5)
        if buf.size != expect:
            raise ValueError(f"gray10 payload {buf.size} != {expect}")
        g = buf.reshape(-1, 5).astype(np.uint16)
        px = np.empty((g.shape[0], 4), dtype=np.uint16)
        for k in range(4):
            px[:, k] = g[:, k] | (((g[:, 4] >> (2 * k)) & 3) << 8)
        return px.reshape(h, w).copy()

    if header.format == protocol.FMT_RAW12:
        expect = h * ROW_PP * 2
        if buf.size != expect:
            raise ValueError(f"raw12 payload {buf.size} != {expect}")
        return buf.view(np.uint16).reshape(h, ROW_PP).copy()

    raise ValueError(f"unknown format {header.format}")


def raw12_to_image(raw: np.ndarray) -> np.ndarray:
    """Pixels from a raw12 frame, dropping the per-row training words."""
    return pp_to_pixels(raw[:, TRAINING_PP:])


# --- Saleae capture decoding -----------------------------------------------------------
def sample_saleae_csv(path, progress=None, max_bits: int | None = None):
    """Sample the data channel on each SCLK rising edge of a Saleae CSV export.

    The export is transition-based: each row gives the new state of every channel, so the
    row carrying a clock rising edge already holds the data value valid at that edge.

    Returns (bits uint8 array, times float64 array).
    """
    bits = bytearray()
    times = []
    with open(path, "rb") as f:
        f.readline()  # header
        prev_clk = 0
        for n, line in enumerate(f):
            p = line.rstrip().split(b",")
            if len(p) != 3:
                continue
            d, c = p[1], p[2]
            if d == b"X" or c == b"X":  # Saleae marks invalid samples with X
                continue
            d = d[0] - 48
            c = c[0] - 48
            if c == 1 and prev_clk == 0:
                bits.append(d)
                times.append(p[0])
                if max_bits is not None and len(bits) >= max_bits:
                    break
            prev_clk = c
            if progress and (n & 0xFFFFF) == 0:
                progress(n)
    return (np.frombuffer(bytes(bits), dtype=np.uint8),
            np.array([float(t) for t in times], dtype=np.float64))


SPOT_CHECK_BITS = 20000


def load_or_sample_csv(path, cache_dir=None, verbose: bool = True):
    """Sample a Saleae CSV, reusing a cached bit stream when one is available.

    Parsing a 434 MB export takes about 50 s, so the sampled bits and timestamps are
    cached. A `source.json` beside them records which file produced the cache, and a
    mismatch forces a re-sample. A cache with no such record is spot-checked against the
    first SPOT_CHECK_BITS edges of the capture and then labelled, rather than being
    trusted blindly or thrown away.

    Returns (bits, times).
    """
    import json
    import os
    import pathlib
    import time as _time

    from .fake import repo_root

    src = pathlib.Path(path)
    if not src.is_file():
        raise FileNotFoundError(f"capture not found: {src}")
    cache = pathlib.Path(cache_dir) if cache_dir else repo_root() / "build" / "golden"
    bits_npy, times_npy = cache / "bits.npy", cache / "bit_times.npy"
    stamp = cache / "source.json"

    want = {"path": str(src.resolve()), "size": src.stat().st_size,
            "mtime": int(src.stat().st_mtime)}

    if bits_npy.is_file() and times_npy.is_file():
        have = None
        if stamp.is_file():
            try:
                have = json.load(open(stamp))
            except Exception:
                have = None
        if have == want:
            if verbose:
                print(f"using cached bit stream in {cache}")
            return np.load(bits_npy), np.load(times_npy)
        if have is None:
            # A cache from before provenance was recorded. Rather than nag forever or
            # throw away 50 s of work, spot-check it: re-sample just the first few
            # thousand edges and compare. Cheap, and enough to catch the wrong file.
            cached_bits = np.load(bits_npy)
            probe = SPOT_CHECK_BITS
            head, _ = sample_saleae_csv(src, max_bits=probe)
            n = min(len(head), probe, len(cached_bits))
            if n and np.array_equal(head[:n], cached_bits[:n]):
                with open(stamp, "w") as f:
                    json.dump(want, f, indent=2)
                if verbose:
                    print(f"verified the existing cache in {cache} against {src.name} "
                          f"({n} bits) and recorded its provenance")
                return cached_bits, np.load(times_npy)
            if verbose:
                print(f"the cache in {cache} does not match {src.name}, re-sampling")
        if verbose:
            print(f"cache in {cache} came from a different capture, re-sampling")

    if verbose:
        mb = want["size"] / 1e6
        print(f"sampling {src} ({mb:.0f} MB); this takes ~{max(mb / 9, 1):.0f} s")
    t0 = _time.time()
    bits, times = sample_saleae_csv(src)
    os.makedirs(cache, exist_ok=True)
    np.save(bits_npy, bits)
    np.save(times_npy, times)
    with open(stamp, "w") as f:
        json.dump(want, f, indent=2)
    if verbose:
        print(f"  {len(bits):,} bits in {_time.time() - t0:.1f} s, cached in {cache}")
    return bits, times


def find_row_starts(bits: np.ndarray):
    """Locate row starts in a sampled bit stream.

    Eight 0x555 training words are 96 alternating bits ending in 1, and the first pixel
    word's start bit is also 1, so the alternation breaks exactly at the first pixel bit.

    Returns (row_start_bit_indices, preceding_alternating_run_lengths, run_start_indices).
    """
    alt = (bits[:-1] != bits[1:])
    d = np.diff(np.concatenate(([0], alt.view(np.int8), [0])))
    starts = np.where(d == 1)[0]
    ends = np.where(d == -1)[0]
    lens = ends - starts
    sel = lens >= 90
    return ends[sel] + 1, lens[sel], starts[sel]


def decode_row_bits(bits: np.ndarray, start: int) -> np.ndarray:
    """The 320 pixel words of a row whose pixel data begins at bit `start`."""
    seg = bits[start:start + WIDTH * PP_BITS].reshape(WIDTH, PP_BITS)
    w = np.zeros(WIDTH, dtype=np.uint16)
    for k in range(PP_BITS):
        w = (w << 1) | seg[:, k]
    return w


def decode_frames_from_bits(bits: np.ndarray, max_frames: int | None = None):
    """Decode every complete frame in a sampled bit stream.

    Yields (frame_index, image uint16, stats dict).
    """
    row_starts, run_lens, _ = find_row_starts(bits)
    frame_first = np.where(run_lens > 1000)[0]
    for fi, i0 in enumerate(frame_first):
        if max_frames is not None and fi >= max_frames:
            return
        if i0 + HEIGHT > len(row_starts):
            return
        rows = row_starts[i0:i0 + HEIGHT]
        if np.any(np.diff(rows) != ROW_BITS):
            continue
        img = np.zeros((HEIGHT, WIDTH), dtype=np.uint16)
        bad_start = bad_stop = 0
        for r, s in enumerate(rows):
            w = decode_row_bits(bits, s)
            bad_start += int(((w >> 11) != 1).sum())
            bad_stop += int(((w & 1) != 0).sum())
            img[r] = pp_to_pixels(w)
        yield fi, img, {"bad_start_bits": bad_start, "bad_stop_bits": bad_stop,
                        "first_row_bit": int(rows[0])}


def find_clock_gaps(times: np.ndarray, threshold_s: float = 200e-9):
    """Indices of bit positions followed by a clock gap longer than `threshold_s`."""
    per = np.diff(times)
    idx = np.where(per > threshold_s)[0]
    return idx, per[idx]


# --- Register decoding -----------------------------------------------------------------
def decode_register_writes(bits: np.ndarray, base: int = 0):
    """Find 24-bit register writes: 1001 + 3-bit address + 16-bit data + 0.

    Only addresses 0 and 1 exist, which rejects most false positives, but callers should
    still restrict the search to INTERFACE MODE windows since pixel data can contain the
    update code by chance.
    """
    out = []
    i = 0
    n = len(bits)
    while i + 24 <= n:
        if (bits[i] == 1 and bits[i + 1] == 0 and bits[i + 2] == 0 and bits[i + 3] == 1
                and bits[i + 23] == 0):
            addr = (int(bits[i + 4]) << 2) | (int(bits[i + 5]) << 1) | int(bits[i + 6])
            if addr in (0, 1):
                data = 0
                for k in range(16):
                    data = (data << 1) | int(bits[i + 7 + k])
                out.append((base + i, addr, data))
                i += 24
                continue
        i += 1
    return out
