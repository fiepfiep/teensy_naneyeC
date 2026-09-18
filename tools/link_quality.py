"""Measure the SEIM link's word error rate at a clock rate, for each receive setting.

    uv run python tools/link_quality.py --clock 49500000
    uv run python tools/link_quality.py --clock 24750000 --settings "0 0 0"

For each receive setting (SAMPLE delay, PHASE edge, HYS input hysteresis) this starts the
sensor with START FORCE -- which streams even when the row lock fails -- in DEPTH 12, where
every row transfer arrives as its raw bits. On the host the bits are reassembled into the
continuous stream the sensor sent, the best of the 12 word alignments is found, and every
12-bit word is classified:

  pixel     start bit 1, stop bit 0: a valid pixel word
  training  exactly 0x555
  bad       anything else: at least one bit is wrong

A perfect link gives 320 pixel and 8 training words in every 328, so bad = 0. Framing bits
are only 2 of 12, so a bad-word rate underestimates the bit error rate: most flipped data
bits leave the framing intact. It is still the right yardstick for comparing settings.
"""

import argparse
import os
import sys
import time

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "host"))

from naneye import decode, protocol  # noqa: E402
from naneye.transport import Device  # noqa: E402

ROW_PP = 328


def raw_frames(dev, n, skip=1, limit=10.0):
    out, seen, t0 = [], 0, time.time()
    while len(out) < n and time.time() - t0 < limit:
        p = dev.reader.next_packet()
        if p is None or not p.is_image or p.header.format != protocol.FMT_RAW12:
            continue
        seen += 1
        if seen > skip:
            out.append(decode.decode_payload(p.header, p.payload))
    return out


def words_to_bits(pp: np.ndarray) -> np.ndarray:
    """(rows, 328) 12-bit words -> one continuous bit stream, MSB first."""
    w = pp.astype(np.uint16).reshape(-1)
    shifts = np.arange(11, -1, -1, dtype=np.uint16)
    return ((w[:, None] >> shifts) & 1).astype(np.uint8).reshape(-1)


def classify(bits: np.ndarray) -> dict:
    best = None
    for off in range(12):
        n = (len(bits) - off) // 12
        words = bits[off:off + n * 12].reshape(n, 12)
        pixel = (words[:, 0] == 1) & (words[:, 11] == 0)
        value = words @ (1 << np.arange(11, -1, -1))
        training = value == 0x555
        stats = {"offset": off, "words": n, "pixel": int(pixel.sum()),
                 "training": int(training.sum()),
                 "bad": int(n - pixel.sum() - training.sum())}
        if best is None or stats["pixel"] > best["pixel"]:
            best = stats
    best["bad_rate"] = best["bad"] / best["words"]
    best["training_share"] = best["training"] / best["words"]
    return best


def measure(dev, clock, sample, phase, hys, frames):
    dev.ask("STOP", timeout=3)
    dev.ask(f"CLK {clock}")
    dev.ask(f"SAMPLE {sample}")
    dev.ask(f"PHASE {phase}")
    dev.ask(f"HYS {hys}")
    dev.ask("DEPTH 12")
    start = dev.ask("START FORCE", timeout=15)
    got = raw_frames(dev, frames)
    dev.ask("STOP", timeout=3)
    if not got:
        return None, start
    return classify(words_to_bits(np.concatenate(got))), start


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--clock", type=int, default=49500000)
    ap.add_argument("--frames", type=int, default=2)
    ap.add_argument("--settings", nargs="*",
                    default=[f"{s} {p} {h}" for h in (0, 1) for p in (0, 1) for s in (0, 1)],
                    help='"SAMPLE PHASE HYS" triples to try')
    args = ap.parse_args()

    with Device.open_first() as dev:
        time.sleep(0.3)
        dev.serial.reset_input_buffer()
        print(f"clock {args.clock / 1e6:.3f} MHz, {args.frames} frames per setting")
        print(f"{'SAMPLE PHASE HYS':<17} {'words':>9} {'pixel':>9} {'training':>9} "
              f"{'bad':>9}  bad rate  training share (ideal 2.44 %)")
        for triple in args.settings:
            s, p, h = (int(x) for x in triple.split())
            st, start = measure(dev, args.clock, s, p, h, args.frames)
            if st is None:
                print(f"{s:>6} {p:>5} {h:>3}      no frames ({start[-1] if start else '-'})")
                continue
            print(f"{s:>6} {p:>5} {h:>3}   {st['words']:>9,} {st['pixel']:>9,} "
                  f"{st['training']:>9,} {st['bad']:>9,}  {st['bad_rate']:8.2%}  "
                  f"{st['training_share']:6.2%}")
        dev.ask("SAMPLE 0")
        dev.ask("PHASE 0")
        dev.ask("HYS 0")
        dev.ask("DEPTH 10")
        dev.ask("POWER 0")


if __name__ == "__main__":
    main()
