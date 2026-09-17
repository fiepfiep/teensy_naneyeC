"""Synthetic device streams, for developing and testing the host without hardware.

Frames decoded from the reference capture are re-encoded in the wire format exactly as the
firmware would send them, so the viewer, recorder and packet reader can be exercised (and
regression tested) end to end with no Teensy attached.
"""

from __future__ import annotations

import io
import numpy as np

from . import decode, protocol


def encode_gray8(img: np.ndarray) -> bytes:
    """10-bit image -> gray8 payload, matching unpack_row_gray8() in the firmware."""
    return (np.asarray(img, dtype=np.uint16) >> 2).astype(np.uint8).tobytes()


def encode_gray10(img: np.ndarray) -> bytes:
    """10-bit image -> packed payload, matching unpack_row_gray10() in the firmware."""
    px = np.asarray(img, dtype=np.uint16).reshape(-1, 4)
    out = np.empty((px.shape[0], 5), dtype=np.uint8)
    for k in range(4):
        out[:, k] = (px[:, k] & 0xFF).astype(np.uint8)
    out[:, 4] = (((px[:, 0] >> 8) & 3) | (((px[:, 1] >> 8) & 3) << 2)
                 | (((px[:, 2] >> 8) & 3) << 4) | (((px[:, 3] >> 8) & 3) << 6)).astype(np.uint8)
    return out.tobytes()


def encode_raw12(img: np.ndarray) -> bytes:
    """10-bit image -> raw12 payload: training words then pixel periods, per row."""
    h, w = img.shape
    rows = np.empty((h, decode.ROW_PP), dtype=np.uint16)
    rows[:, :decode.TRAINING_PP] = decode.WORD_TRAINING
    # Rebuild the pixel words: start bit 1, 10-bit value, stop bit 0.
    rows[:, decode.TRAINING_PP:] = (1 << 11) | ((np.asarray(img, np.uint16) & 0x3FF) << 1)
    return rows.tobytes()


ENCODERS = {
    protocol.FMT_GRAY8: encode_gray8,
    protocol.FMT_GRAY10: encode_gray10,
    protocol.FMT_RAW12: encode_raw12,
}


def frame_packet(img: np.ndarray, counter: int, fmt: int = protocol.FMT_GRAY8,
                 sclk_hz: int = 24750000, cfg0: int = 0x009F, cfg1: int = 0x0065,
                 timestamp_us: int | None = None, dropped: int = 0,
                 rows_failed: int = 0) -> bytes:
    """One image packet, as the firmware would emit it."""
    payload = ENCODERS[fmt](img)
    frame_pp = decode.INTERFACE_PP + decode.SYNC_PP + 2 * decode.ROW_PP + (
        decode.HEIGHT * decode.ROW_PP + decode.EOF_PP)
    period_us = frame_pp * decode.PP_BITS * 1_000_000 // sclk_hz
    h = protocol.Header(
        type=protocol.TYPE_IMAGE,
        frame_counter=counter,
        timestamp_us=counter * period_us if timestamp_us is None else timestamp_us,
        width=img.shape[1], height=img.shape[0], format=fmt,
        flags=protocol.FLAG_SYNC_LOST if rows_failed else 0,
        rows_failed=rows_failed, sclk_hz=sclk_hz,
        exposure_pp=105616, cfg0=cfg0, cfg1=cfg1, frames_dropped=dropped)
    return protocol.build_packet(h, payload)


def stream_bytes(frames, count: int | None = None, fmt: int = protocol.FMT_GRAY8,
                 corrupt_every: int | None = None, drop_every: int | None = None,
                 noise: str | None = None) -> bytes:
    """A byte stream of image packets, cycling through `frames`.

    corrupt_every  flip a payload byte every Nth frame, so CRC rejection can be tested
    drop_every     skip a frame counter every Nth frame, simulating a dropped frame
    noise          'prefix' to prepend junk before the first packet, exercising resync
    """
    frames = list(frames)
    if not frames:
        raise ValueError("no frames given")
    n = count if count is not None else len(frames)
    out = io.BytesIO()
    if noise == "prefix":
        out.write(b"garbage before the stream starts\x00\xff")

    dropped = 0
    for i in range(n):
        if drop_every and i and i % drop_every == 0:
            dropped += 1
            continue
        pkt = bytearray(frame_packet(frames[i % len(frames)], i, fmt=fmt, dropped=dropped))
        if corrupt_every and i and i % corrupt_every == 0:
            pkt[protocol.HEADER_SIZE + 10] ^= 0xFF
        out.write(pkt)
    return out.getvalue()


def reader_over_bytes(data: bytes):
    """A PacketReader fed from an in-memory byte string."""
    from .transport import PacketReader

    buf = io.BytesIO(data)
    return PacketReader(buf.read)


def repo_root():
    """The repository root, derived from this file's location (host/naneye/fake.py)."""
    import pathlib

    return pathlib.Path(__file__).resolve().parents[2]


def golden_dirs(path=None):
    """Candidate locations for decoded reference frames, in search order.

    Resolved against the repository root as well as the working directory, so tools work
    from anywhere rather than only from the root.
    """
    import pathlib

    if path is not None:
        p = pathlib.Path(path)
        return [p] if p.is_absolute() else [p, repo_root() / p]
    return [pathlib.Path("build/golden"), repo_root() / "build" / "golden"]


def load_golden_frames(path=None, limit: int | None = None):
    """Frames produced by tools/decode_golden.py, skipping the saturated first frame."""
    import re

    tried = []
    for d in golden_dirs(path):
        tried.append(str(d))
        files = sorted(d.glob("frame*.npy"),
                       key=lambda p: int(re.search(r"frame(\d+)", p.name).group(1)))
        files = [f for f in files if f.name != "frame0.npy"]
        if files:
            if limit:
                files = files[:limit]
            return [np.load(f) for f in files]

    raise FileNotFoundError(
        "no decoded reference frames found; looked in: " + ", ".join(tried) +
        "\nThey come from the reference capture, which is not in the repository:"
        "\n  1. put the Saleae export at doc/digital.csv"
        "\n  2. run: uv run python tools/decode_golden.py")


def synthetic_frames(count: int = 6, seed: int = 7):
    """Generated 10-bit frames, for when no reference capture is available.

    Not a substitute for real sensor data, but enough to exercise the whole host path:
    a horizontal gradient, a checkerboard, a saturated patch and a dark patch, plus a
    marker that moves between frames and per-frame noise.
    """
    rng = np.random.default_rng(seed)
    ys, xs = np.mgrid[0:decode.HEIGHT, 0:decode.WIDTH]
    base = (xs / (decode.WIDTH - 1) * 700 + 160).astype(np.float32)
    checker = (((xs // 20) + (ys // 20)) % 2) * 60.0
    base = base + checker
    base[40:80, 40:80] = 1023.0   # saturated patch
    base[40:80, 240:280] = 0.0    # dark patch

    out = []
    for k in range(count):
        f = base + rng.normal(0.0, 2.7, base.shape).astype(np.float32)
        cx = 40 + int((decode.WIDTH - 80) * k / max(count - 1, 1))
        f[decode.HEIGHT - 70:decode.HEIGHT - 30, cx:cx + 40] = 900.0
        out.append(np.clip(f, 0, 1023).astype(np.uint16))
    return out
