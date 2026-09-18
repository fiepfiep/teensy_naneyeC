"""Analyse a Saleae capture of the Teensy's SEIM link against what the firmware should emit.

    uv run python tools/analyze_link.py [build/saleae/link]

Works with or without a sensor attached. Checks:
  - SCLK frequency, measured from the edges themselves
  - the clock bursts (one per LPSPI frame or bit-bang), against the expected sequence
  - register writes decoded from SDO, sampled on SCLK rising edges as the sensor does
  - exact clock counts between phases: the start-up sequence and every steady-state frame
  - clock continuity inside each 3936-clock row (M4: no gap longer than 1 PP in a row)
  - SDO setup time before the sampling edge, and EN-to-first-clock delay
"""

import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "host"))

from naneye import decode  # noqa: E402
from naneye.saleae import read_digital_bin  # noqa: E402

PP = 12
ROW = 328 * PP                                     # 3936
FRAME = (648 + 656 + 656 + 104968) * PP            # 1,283,136 at minimum rows_delay
# From the end of the idle-off CONFIG_1 write to the first steady-state CONFIG_0 write:
# 10 alignment clocks + INITIAL PRE-SYNC 329 PP + SYNC/DELAY 1312 PP + first frame (discarded).
STARTUP_GAP = 10 + 329 * PP + 1312 * PP + 104968 * PP   # 1,279,318


def rle(seq):
    """Run-length encode a sequence: [(value, count), ...]."""
    out = []
    for v in seq:
        if out and out[-1][0] == v:
            out[-1][1] += 1
        else:
            out.append([v, 1])
    return out


def fmt_rle(pairs, limit=40):
    parts = [f"{v}" if n == 1 else f"{v}x{n}" for v, n in pairs[:limit]]
    more = f" ... (+{len(pairs) - limit} runs)" if len(pairs) > limit else ""
    return " ".join(parts) + more


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else "build/saleae/link"
    meta = json.load(open(os.path.join(root, "meta.json")))
    ch = meta["channels"]
    tr = {name: read_digital_bin(os.path.join(root, f"digital_{idx}.bin"))
          for name, idx in ch.items()}
    ok = True

    def check(cond, msg):
        nonlocal ok
        ok &= bool(cond)
        print(f"  [{'PASS' if cond else 'FAIL'}] {msg}")

    sclk = tr["sclk"].edges(rising=True)
    # The median edge interval is useless at this resolution: at 250 MS/s intervals are
    # quantised to 4 ns, so an 80.8 ns period reads as 80 ns most of the time. Average over
    # long unbroken bursts instead, where the quantisation error divides by thousands.
    coarse = float(np.median(np.diff(sclk)))
    g0 = np.diff(sclk)
    b0 = np.where(g0 > 1.5 * coarse)[0]
    s0 = np.concatenate(([0], b0 + 1))
    e0 = np.concatenate((b0 + 1, [len(sclk)]))
    long_runs = [(s, e) for s, e in zip(s0, e0) if e - s >= 1000]
    period = float(np.mean([(sclk[e - 1] - sclk[s]) / (e - s - 1) for s, e in long_runs])) \
        if long_runs else coarse
    f = 1.0 / period
    print(f"capture {root}: {meta['start']!r} at nominal {meta['clock'] / 1e6:.3f} MHz, "
          f"sampled {meta['rate'] / 1e6:.0f} MS/s")
    print(f"  {len(sclk):,} SCLK rising edges")

    print("\nclock")
    err = (f - meta["clock"]) / meta["clock"] * 1e6
    print(f"  measured {f / 1e6:.4f} MHz (period {period * 1e9:.2f} ns), {err:+.0f} ppm vs nominal")
    check(abs(err) < 2000, "SCLK within 0.2 % of nominal")

    # --- EN -----------------------------------------------------------------------------
    en_rise = tr["en"].edges(rising=True)
    if len(en_rise):
        dt = (sclk[0] - en_rise[0]) * 1e3
        print(f"\npower\n  EN rises at t={en_rise[0] * 1e3:.3f} ms; first SCLK edge {dt:.3f} ms later")
        check(dt >= 4.9, "sensor supply given >= 5 ms to settle before the first clock")

    # --- bursts: split the clock at every gap longer than 1.5 periods --------------------
    gaps = np.diff(sclk)
    brk = np.where(gaps > 1.5 * period)[0]
    starts = np.concatenate(([0], brk + 1))
    ends = np.concatenate((brk + 1, [len(sclk)]))
    lengths = ends - starts
    print(f"\nbursts (clocks per contiguous run; one per LPSPI frame or bit-bang)")
    print(f"  {len(lengths):,} bursts; first 40 runs:")
    print("   ", fmt_rle(rle(lengths.tolist()), 40))

    # --- SDO sampled on rising SCLK: what the sensor would clock in ---------------------
    sdo_bits = tr["sdo"].level_at(sclk)
    writes = decode.decode_register_writes(sdo_bits)
    print(f"\nregister writes on SDO ({len(writes)} found)")
    for pos, addr, data in writes[:8]:
        c1 = (f"idle={(data >> 1) & 1} mclk_mode={(data >> 6) & 3} hs={data & 1} "
              f"vref={(data >> 4) & 3} cvc={(data >> 2) & 3}") if addr == 1 else \
             f"rows_in_reset={data >> 8} vrst={(data >> 6) & 3} gain={(data >> 4) & 3} " \
             f"offset={(data >> 2) & 3} drive={data & 3}"
        print(f"  clock {pos:>9,}  CONFIG_{addr} = 0x{data:04X}   {c1}")
    if len(writes) > 8:
        print(f"  ... {len(writes) - 8} more")

    cfg0_pos = [p for p, a, _ in writes if a == 0]
    cfg1 = [(p, d) for p, a, d in writes if a == 1]

    print("\nstart-up sequence")
    check(len(writes) >= 4, "two write pairs at start-up (idle on, then idle off)")
    if len(cfg1) >= 2:
        check((cfg1[0][1] >> 1) & 1 == 1, f"first CONFIG_1 keeps idle on (0x{cfg1[0][1]:04X})")
        check((cfg1[1][1] >> 1) & 1 == 0, f"second CONFIG_1 clears idle (0x{cfg1[1][1]:04X})")
        check((cfg1[1][1] >> 8) & 1 == 0, "output_mode = SEIM")
    # The activation clock: exactly one clock before the first write.
    if writes:
        check(writes[0][0] == 1, f"exactly 1 activation clock before the first write "
                                 f"(first write starts at clock {writes[0][0]})")
    if len(writes) >= 4 and len(cfg0_pos) >= 3:
        idle_off_end = writes[3][0] + 24
        gap = cfg0_pos[2] - idle_off_end
        check(gap == STARTUP_GAP,
              f"idle-off write -> first frame's CONFIG_0: {gap:,} clocks (expect {STARTUP_GAP:,})")

    # --- steady state: every frame exactly FRAME clocks -------------------------------
    steady = cfg0_pos[2:]
    if len(steady) >= 2:
        d = np.diff(steady)
        print(f"\nsteady-state frames ({len(steady)} CONFIG_0 writes)")
        print(f"  clocks between consecutive frames: {sorted(set(d.tolist()))}")
        check(np.all(d == FRAME), f"every frame is exactly {FRAME:,} clocks")
        t_frames = sclk[np.array(steady)]
        fp = np.diff(t_frames)
        print(f"  frame period {np.mean(fp) * 1e3:.3f} ms -> {1 / np.mean(fp):.3f} fps "
              f"(ideal {f / FRAME:.3f} fps; {(np.mean(fp) - FRAME * period) * 1e3:.3f} ms "
              f"per frame is inter-burst overhead, "
              f"{(np.mean(fp) - FRAME * period) / np.mean(fp) * 100:.1f} %)")

    # --- rows: continuity inside, and the cost of the gaps between ----------------------
    row_idx = np.where(lengths == ROW)[0]
    if len(row_idx):
        worst_inside = 0.0
        for i in row_idx:
            g = np.diff(sclk[starts[i]:ends[i]])
            worst_inside = max(worst_inside, float(g.max()))
        between = [(sclk[starts[i + 1]] - sclk[ends[i] - 1]) for i in row_idx
                   if i + 1 < len(starts) and lengths[i + 1] == ROW]
        print(f"\nrows ({len(row_idx)} bursts of exactly {ROW} clocks)")
        print(f"  longest clock period inside any row: {worst_inside * 1e9:.1f} ns "
              f"({worst_inside / period:.2f} periods)")
        check(worst_inside < 1.5 * period, "no gap inside any row (M4: none longer than 1 PP)")
        if between:
            b = np.array(between) * 1e6
            print(f"  gap between rows: median {np.median(b):.2f} us, max {b.max():.2f} us "
                  f"({np.median(b) * 1e-6 / period:.0f} clock periods)")

    # --- SDO: setup inside writes, and silence outside them ---------------------------
    # Only bits inside a register write are latched by the sensor, so only they are held to
    # the 3 ns setup requirement. Outside the writes SDO is released or driving zeros, and
    # any transition there means it is not where the design assumes.
    sdo_t = tr["sdo"].times
    if len(sdo_t):
        idx = np.searchsorted(sclk, sdo_t)
        keep = idx < len(sclk)
        sdo_t, idx = sdo_t[keep], idx[keep]
        wstart = np.array([p for p, _, _ in writes])
        inside = np.array([bool(len(wstart[wstart <= i])) and (i - wstart[wstart <= i][-1]) < 24
                           for i in idx])
        setup = (sclk[idx] - sdo_t) * 1e9
        print(f"\nSDO ({len(sdo_t)} transitions: {inside.sum()} inside register writes, "
              f"{(~inside).sum()} outside)")
        if inside.any():
            print(f"  setup inside writes: min {setup[inside].min():.1f} ns, "
                  f"median {np.median(setup[inside]):.1f} ns (sensor needs >= 3 ns)")
            check(setup[inside].min() >= 3.0, "every written bit has >= 3 ns setup")
        check((~inside).sum() == 0,
              "SDO quiet outside register writes (no drive-high at frame end, no "
              "crosstalk crossing the threshold)")

    # --- SDI: nothing should drive it with no sensor attached ---------------------------
    print(f"\nSDI (pin 1): {len(tr['sdi'].times)} transitions, level {tr['sdi'].initial}")

    print("\n" + ("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
