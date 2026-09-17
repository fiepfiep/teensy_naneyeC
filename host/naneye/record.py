"""Lossless frame recorder.

    python -m naneye.record --source auto --depth 10 --frames 100 --out capture/run1
    python -m naneye.record --source replay --frames 5 --out capture/demo

Writes, per run:
    frames.npy    (N, 320, 320) uint16 (or uint8 for 8-bit captures)
    meta.csv      one row per frame: counter, timestamp, exposure, config, drop counters
    run.json      capture settings and a summary
    stream.bin    optional verbatim packet stream (--raw), replayable as a FileSource

Every frame's own header travels with it, so a recording is self-describing: nothing about
exposure, gain, clock rate or dropped frames has to be remembered separately.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import time

import numpy as np

from . import protocol
from .sources import open_source

META_FIELDS = ["index", "frame_counter", "timestamp_us", "exposure_pp", "exposure_us",
               "sclk_hz", "cfg0", "cfg1", "format", "flags", "rows_failed",
               "frames_dropped", "min", "max", "mean"]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", default="replay")
    ap.add_argument("--out", required=True, help="output directory")
    ap.add_argument("--frames", type=int, default=100)
    ap.add_argument("--depth", type=int, default=10, choices=(8, 10, 12))
    ap.add_argument("--clock", type=int, default=12375000)
    ap.add_argument("--exposure", type=int, default=None,
                    help="rows_in_reset (0 = longest exposure)")
    ap.add_argument("--led", type=float, default=None, help="LED current in mA")
    ap.add_argument("--raw", action="store_true", help="also save the verbatim packet stream")
    args = ap.parse_args(argv)

    os.makedirs(args.out, exist_ok=True)
    fmt = {8: protocol.FMT_GRAY8, 10: protocol.FMT_GRAY10,
           12: protocol.FMT_RAW12}[args.depth]
    source = open_source(args.source, depth=args.depth, clock_hz=args.clock, fmt=fmt)
    print(f"source: {source.name}")

    if args.exposure is not None:
        source.command(f"EXP {args.exposure}")
    if args.led is not None:
        source.command(f"LEDI {args.led}")
        source.command("LED 1")

    images = []
    meta = []
    raw_file = open(os.path.join(args.out, "stream.bin"), "wb") if args.raw else None
    t_start = time.time()
    first_counter = None
    last_counter = None
    counter_gaps = 0

    try:
        with source:
            for header, img in source.frames():
                if first_counter is None:
                    first_counter = header.frame_counter
                if last_counter is not None:
                    counter_gaps += max(0, header.frame_counter - last_counter - 1)
                last_counter = header.frame_counter

                images.append(img)
                meta.append({
                    "index": len(images) - 1,
                    "frame_counter": header.frame_counter,
                    "timestamp_us": header.timestamp_us,
                    "exposure_pp": header.exposure_pp,
                    "exposure_us": round(header.exposure_us(), 3),
                    "sclk_hz": header.sclk_hz,
                    "cfg0": f"0x{header.cfg0:04X}",
                    "cfg1": f"0x{header.cfg1:04X}",
                    "format": header.format_name,
                    "flags": header.flags,
                    "rows_failed": header.rows_failed,
                    "frames_dropped": header.frames_dropped,
                    "min": int(img.min()),
                    "max": int(img.max()),
                    "mean": round(float(img.mean()), 3),
                })
                if raw_file:
                    raw_file.write(protocol.build_packet(header, b""))  # header only
                if len(images) % 10 == 0:
                    print(f"\r{len(images)}/{args.frames} frames", end="", flush=True)
                if len(images) >= args.frames:
                    break
    except KeyboardInterrupt:
        print("\ninterrupted")
    finally:
        if raw_file:
            raw_file.close()

    if not images:
        print("no frames captured")
        return 1

    elapsed = time.time() - t_start
    stack = np.stack(images)
    np.save(os.path.join(args.out, "frames.npy"), stack)

    with open(os.path.join(args.out, "meta.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=META_FIELDS)
        w.writeheader()
        w.writerows(meta)

    summary = {
        "source": source.name,
        "frames": len(images),
        "shape": list(stack.shape),
        "dtype": str(stack.dtype),
        "depth": args.depth,
        "elapsed_s": round(elapsed, 3),
        "measured_fps": round(len(images) / elapsed, 2) if elapsed > 0 else None,
        "counter_gaps": counter_gaps,
        "frames_dropped_reported": meta[-1]["frames_dropped"],
        "rows_failed_total": sum(m["rows_failed"] for m in meta),
        "sclk_hz": meta[-1]["sclk_hz"],
        "exposure_us": meta[-1]["exposure_us"],
        "cfg0": meta[-1]["cfg0"],
        "cfg1": meta[-1]["cfg1"],
    }
    with open(os.path.join(args.out, "run.json"), "w") as f:
        json.dump(summary, f, indent=2)

    print(f"\n{len(images)} frames -> {args.out}")
    print(f"  {stack.shape} {stack.dtype}, {elapsed:.1f}s, "
          f"{summary['measured_fps']} fps measured")
    print(f"  counter gaps {counter_gaps}, device reported "
          f"{summary['frames_dropped_reported']} dropped, "
          f"{summary['rows_failed_total']} failed rows")
    if counter_gaps or summary["rows_failed_total"]:
        print("  NOTE: frames were lost or rows failed validation; see meta.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
