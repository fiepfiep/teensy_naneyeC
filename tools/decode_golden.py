"""Decode the golden Saleae capture of a working NanoBerry <-> Raspberry Pi SEIM link.

Reproduces every measured value in spec.md section 3, using the same decoder the live host
uses (host/naneye/decode.py). Run this before touching hardware: it is the reference
decode and the regression baseline for the firmware.

    python tools/decode_golden.py                    # doc/digital.csv -> build/golden
    python tools/decode_golden.py path/to/other.csv

Outputs bits.npy (the sampled bit stream, cached for later runs), frame<N>.npy and
frame1.png. The first parse of a 433 MB CSV takes about 50 s.
"""

import argparse
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "host"))

from naneye import decode  # noqa: E402

VRST_PIX = ("2.2 V", "2.4 V", "2.6 V", "2.8 V")
RAMP_GAIN = ("0.79x", "0.99x", "1.32x", "1.97x")
RAMP_OFFSET = ("1.9 V", "2.0 V", "2.1 V", "2.2 V")
VREF = ("1.9 V", "2.0 V", "2.1 V", "2.2 V")
OUTPUT_CURR = ("3.9 mA", "5.8 mA", "7.7 mA", "9.6 mA")
MCLK_MODE = ("2x", "default", "/2", "/2")


def describe_cfg0(v):
    rir = v >> 8
    return (f"rows_in_reset={rir} (->{2 * rir + 2} rows in reset), "
            f"vrst_pix={VRST_PIX[(v >> 6) & 3]}, ramp_gain={RAMP_GAIN[(v >> 4) & 3]}, "
            f"offset_ramp={RAMP_OFFSET[(v >> 2) & 3]}, output_curr={OUTPUT_CURR[v & 3]}")


def describe_cfg1(v):
    rd = v >> 11
    return (f"rows_delay={rd} (->{16 * rd + 2} rows), bias_curr={(v >> 10) & 1}, "
            f"cds_gain={'2.0' if (v >> 9) & 1 else '1.3'}, "
            f"output_mode={'LVDS' if (v >> 8) & 1 else 'SEIM'}, "
            f"mclk_mode={MCLK_MODE[(v >> 6) & 3]}, vref={VREF[(v >> 4) & 3]}, "
            f"cvc_curr={(v >> 2) & 3}, idle={(v >> 1) & 1}, high_speed={v & 1}")


def exposure_pp(cfg0, cfg1):
    """Effective exposure in pixel periods, per DS000503 section 6.5.1."""
    rows_delay, rows_in_reset = cfg1 >> 11, cfg0 >> 8
    t_btw = 648 + 656 + (16 * rows_delay + 2) * decode.ROW_PP
    t_matrix = decode.WIDTH * decode.ROW_PP + 8
    t_reset = (2 * rows_in_reset + 2) * decode.ROW_PP
    return t_btw + t_matrix - t_reset - 656


def print_writes(writes, indent="  "):
    for pos, addr, data in writes:
        name = "CONFIG_0" if addr == 0 else "CONFIG_1"
        desc = describe_cfg0(data) if addr == 0 else describe_cfg1(data)
        print(f"{indent}bit {pos:<9} {name} = 0x{data:04X}  {desc}")


def load_bits(csv_path, cache_dir):
    bits_npy = os.path.join(cache_dir, "bits.npy")
    times_npy = os.path.join(cache_dir, "bit_times.npy")
    if os.path.exists(bits_npy) and os.path.exists(times_npy):
        return np.load(bits_npy), np.load(times_npy)

    print(f"sampling {csv_path} (this takes ~50 s for a 433 MB export)...")
    t0 = time.time()
    bits, times = decode.sample_saleae_csv(csv_path)
    os.makedirs(cache_dir, exist_ok=True)
    np.save(bits_npy, bits)
    np.save(times_npy, times)
    print(f"  {len(bits):,} bits in {time.time() - t0:.1f} s (cached in {cache_dir})")
    return bits, times


def write_png(img, path):
    try:
        from PIL import Image
    except ImportError:
        print("pillow not installed, skipping PNG", file=sys.stderr)
        return
    f = img.astype(np.float32)
    lo, hi = np.percentile(f, [0.5, 99.5])
    a = ((f - lo) / max(hi - lo, 1) * 255).clip(0, 255).astype(np.uint8)
    Image.fromarray(a).resize((decode.WIDTH * 2, decode.HEIGHT * 2), Image.NEAREST).save(path)
    print(f"wrote {path}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", nargs="?", default="doc/digital.csv")
    ap.add_argument("--out", default="build/golden")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    bits, times = load_bits(args.csv, args.out)
    period = np.median(np.diff(times))
    print(f"bits sampled          : {len(bits):,}")
    print(f"SCLK                  : {1 / period / 1e6:.3f} MHz "
          f"(period {period * 1e9:.1f} ns)")
    gap_idx, gap_len = decode.find_clock_gaps(times)
    print(f"clock gaps > 200 ns   : {len(gap_idx)}")

    print("\npower-up configuration (INITIAL INTERFACE MODE):")
    print_writes(decode.decode_register_writes(bits[:200]))

    row_starts, run_lens, run_starts = decode.find_row_starts(bits)
    frame_first = np.where(run_lens > 1000)[0]
    print(f"\nrow starts found      : {len(row_starts)}")
    print(f"frame boundaries      : {len(frame_first)}")

    frames = []
    for fi, i0 in enumerate(frame_first):
        # INTERFACE MODE is the 648 PP immediately before this frame's SYNC run.
        sync = int(run_starts[i0])
        lo = max(0, sync - decode.INTERFACE_PP * decode.PP_BITS)
        writes = decode.decode_register_writes(bits[lo:sync], lo)
        cfg = {a: v for _, a, v in writes}
        exp = (f"  t_exp={exposure_pp(cfg[0], cfg[1]):,} PP"
               if 0 in cfg and 1 in cfg else "")
        print(f"\nframe {fi}  interface window t={times[lo]:.4f}  "
              + " ".join(f"CFG{a}=0x{v:04X}" for _, a, v in writes) + exp)
        print_writes(writes, indent="    ")

        if i0 + decode.HEIGHT > len(row_starts):
            print(f"  frame {fi}: truncated capture, not decoded")
            break
        rows = row_starts[i0:i0 + decode.HEIGHT]
        if np.any(np.diff(rows) != decode.ROW_BITS):
            print(f"  frame {fi}: irregular row pitch, skipped")
            continue

        img = np.zeros((decode.HEIGHT, decode.WIDTH), dtype=np.uint16)
        bad_start = bad_stop = 0
        for r, s in enumerate(rows):
            w = decode.decode_row_bits(bits, s)
            bad_start += int(((w >> 11) != 1).sum())
            bad_stop += int(((w & 1) != 0).sum())
            img[r] = decode.pp_to_pixels(w)
        t0 = times[rows[0]]
        t1 = times[rows[-1] + decode.WIDTH * decode.PP_BITS - 1]
        print(f"  readout t={t0:.4f}..{t1:.4f} ({(t1 - t0) * 1e3:.2f} ms)  "
              f"start-bits {'OK' if bad_start == 0 else f'{bad_start} BAD'}  "
              f"stop-bits {'OK' if bad_stop == 0 else f'{bad_stop} BAD'}  "
              f"min={img.min()} max={img.max()} mean={img.mean():.1f}")
        np.save(os.path.join(args.out, f"frame{fi}.npy"), img)
        frames.append(img)

    if len(frames) > 1:
        write_png(frames[1], os.path.join(args.out, "frame1.png"))
        st = np.stack([f.astype(np.float32) for f in frames[1:]])
        print(f"\ntemporal noise        : {st.std(axis=0).mean():.2f} DN "
              f"(over {len(frames) - 1} frames, frame 0 discarded)")
        sub = frames[1][100:300, 100:300].astype(np.float32)
        means = [sub[0::2, 0::2].mean(), sub[0::2, 1::2].mean(),
                 sub[1::2, 0::2].mean(), sub[1::2, 1::2].mean()]
        spread = (max(means) - min(means)) / np.mean(means) * 100
        print(f"Bayer sub-lattice     : {['%.2f' % m for m in means]} "
              f"spread {spread:.2f}% -> {'mono' if spread < 2 else 'COLOUR'}")


if __name__ == "__main__":
    main()
