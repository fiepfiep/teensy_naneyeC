"""Capture a bring-up sequence on the Saleae and plot it, so progress can be followed.

    uv run --with matplotlib python tools/show_bringup.py                    # START REF, LISTEN 800
    uv run --with matplotlib python tools/show_bringup.py --start "START" --listen 400

Triggers on NanEye_EN, sends the start command then LISTEN, and leaves the capture open in
Logic 2 so it can be explored there too. Writes a PNG with an overview and three zooms:
the idle-off register write, the moment the sensor starts driving SDAT, and a few pixel
periods of what it sends -- with the sensor's clock-to-data launch delay measured.
"""

import argparse
import os
import sys
import time

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "host"))

from serial.tools import list_ports  # noqa: E402

from naneye import decode  # noqa: E402
from naneye.saleae import Logic  # noqa: E402
from naneye.transport import Device  # noqa: E402

CH = {"sdi": 0, "en": 1, "sdo": 2, "sclk": 3, "vcc": 4, "sdat_board": 5, "sclk_board": 6}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", default="START REF")
    ap.add_argument("--listen", type=int, default=800)
    ap.add_argument("--clock", type=int, default=12375000)
    ap.add_argument("--after", type=float, default=0.5)
    ap.add_argument("--rate", type=float, default=250e6,
                    help="digital sample rate; 250 MS/s is the Logic Pro 16 maximum with 7 channels")
    ap.add_argument("--out", default=os.path.join(ROOT, "build", "saleae", "bringup"))
    args = ap.parse_args()

    logic = Logic()
    port = None
    t0 = time.time()
    while not port and time.time() - t0 < 15:
        port = next((p.device for p in list_ports.comports() if p.vid == 0x16C0), None)
        time.sleep(0.3)
    chans = sorted(CH.values())
    with Device(port) as dev:
        time.sleep(0.3)
        dev.serial.reset_input_buffer()
        dev.ask("STOP", timeout=3)
        dev.ask("POWER 0")
        dev.ask(f"CLK {args.clock}")
        time.sleep(0.3)
        cap = logic.start_triggered(chans, rate=args.rate, trigger=CH["en"], after_s=args.after)
        time.sleep(1.0)
        replies = dev.ask(args.start, timeout=10)
        replies += dev.ask(f"LISTEN {args.listen}", timeout=30)
        logic.wait(cap)
        dev.ask("POWER 0")
    for r in replies:
        print(r)

    d = logic.export_digital(cap, args.out, chans)
    logic.save(cap, args.out + ".sal")
    print(f"capture {cap} left open in Logic 2; saved {args.out}.sal")

    en = d[CH["en"]].edges(True)[0]
    sclk = d[CH["sclk"]].edges(True)
    sclk_f = d[CH["sclk"]].edges(False)
    sdo = d[CH["sdo"]]
    sdb = d[CH["sdat_board"]]
    bits_teensy = sdo.level_at(sclk)
    writes = decode.decode_register_writes(bits_teensy[:2_000_000])
    idle_off = next((p for p, a, v in writes if a == 1 and not (v >> 1) & 1), None)

    # When does the sensor first drive SDAT on its own? Our own writes are the only other
    # source of 1s, so look after the idle-off write's interface window ends.
    first_sensor_edge = None
    if idle_off is not None:
        win_end = idle_off + 24 + (648 * 12 - 48)
        t_after = sclk[min(idle_off + 24, len(sclk) - 1)]
        tt = sdb.times[sdb.times > t_after]
        if len(tt):
            first_sensor_edge = tt[0]
    # Sensor launch delay: SDAT (board) transitions relative to the preceding rising SCLK.
    launch = np.array([])
    if first_sensor_edge is not None:
        tt = sdb.times[sdb.times > first_sensor_edge][:20000]
        idx = np.searchsorted(sclk, tt) - 1
        ok = idx >= 0
        launch = (tt[ok] - sclk[idx[ok]]) * 1e9

    print(f"register writes found: {[(hex(v), a) for _, a, v in writes[:6]]}")
    if idle_off is not None:
        print(f"idle-off write at clock {idle_off:,}")
    if first_sensor_edge is not None:
        k = int(np.searchsorted(sclk, first_sensor_edge))
        print(f"sensor first drives SDAT at clock {k:,} "
              f"({k - idle_off - 24:,} clocks after the idle-off write ends), "
              f"t = {(first_sensor_edge - en) * 1e3:.3f} ms after EN")
    if len(launch):
        print(f"sensor clock-to-data launch delay: median {np.median(launch):.1f} ns "
              f"(reference board: ~8 ns)")

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig = plt.figure(figsize=(13, 10), constrained_layout=True)
    gs = fig.add_gridspec(3, 2, height_ratios=[1.1, 1, 1])
    fig.suptitle(f"NanEyeC bring-up: '{args.start}' then 'LISTEN {args.listen}' at "
                 f"{args.clock / 1e6:.3f} MHz", fontsize=12, fontweight="bold")

    # Overview: activity per channel in 100 us bins
    ax = fig.add_subplot(gs[0, :])
    tend = max(tr.end for tr in d.values())
    bins = np.arange(en - 0.002, min(tend, en + args.after), 100e-6)
    names = [("EN (pin 2)", CH["en"]), ("VCC_SENSOR", CH["vcc"]), ("SCLK (pin 27)", CH["sclk"]),
             ("SDAT Teensy (pin 26)", CH["sdo"]), ("SDAT at sensor", CH["sdat_board"])]
    for row, (name, c) in enumerate(names):
        tr = d[c]
        lvl = tr.level_at(bins).astype(float)
        cnt, _ = np.histogram(tr.times, bins=np.append(bins, bins[-1] + 100e-6))
        act = np.clip(cnt / 1000.0, 0, 1)
        y = len(names) - 1 - row
        ax.fill_between((bins - en) * 1e3, y, y + 0.35 * lvl + 0.55 * act, step="post",
                        alpha=0.8)
        ax.text(-2.1, y + 0.2, name, ha="right", va="center", fontsize=8)
    if idle_off is not None:
        ax.axvline((sclk[idle_off] - en) * 1e3, color="k", ls="--", lw=0.8)
        ax.text((sclk[idle_off] - en) * 1e3, len(names) - 0.1, " idle-off write",
                fontsize=8, va="top")
    ax.set_yticks([])
    ax.set_xlabel("ms after EN rises")
    ax.set_title("overview — band height shows level; the thick part is switching activity",
                 loc="left", fontsize=9)

    def trace(axz, t0z, t1z, title):
        tt = np.linspace(t0z, t1z, 4000)
        rows = [("SCLK", CH["sclk"]), ("SDAT Teensy", CH["sdo"]), ("SDAT sensor", CH["sdat_board"])]
        for i, (nm, c) in enumerate(rows):
            y = 2 - i
            axz.step((tt - t0z) * 1e6, d[c].level_at(tt) * 0.8 + y * 1.2, where="post", lw=0.8)
            axz.text(-0.01 * (t1z - t0z) * 1e6, y * 1.2 + 0.4, nm, ha="right", va="center",
                     fontsize=7)
        axz.set_yticks([])
        axz.set_xlabel("µs")
        axz.set_title(title, loc="left", fontsize=9)

    if idle_off is not None:
        t_w = sclk[idle_off]
        trace(fig.add_subplot(gs[1, 0]), t_w - 1e-6, t_w + 2.3e-6,
              "idle-off write: 1001 001 + CONFIG_1, driven by the Teensy")
    if first_sensor_edge is not None:
        trace(fig.add_subplot(gs[1, 1]), first_sensor_edge - 2e-6, first_sensor_edge + 3e-6,
              "the sensor starts driving SDAT")
        trace(fig.add_subplot(gs[2, 0]), first_sensor_edge + 50e-6,
              first_sensor_edge + 50e-6 + 12 * 3 / args.clock,
              "3 pixel periods of what the sensor sends")
    if len(launch):
        axh = fig.add_subplot(gs[2, 1])
        axh.hist(launch, bins=np.arange(0, 45, 2), color="tab:purple")
        axh.axvline(8, color="k", ls="--", lw=0.8)
        axh.text(8.5, axh.get_ylim()[1] * 0.9, "reference board ~8 ns", fontsize=8)
        axh.set_xlabel("ns from SCLK rising edge to SDAT change at the sensor")
        axh.set_title(f"sensor launch delay, {len(launch):,} edges: median "
                      f"{np.median(launch):.1f} ns", loc="left", fontsize=9)

    png = args.out + ".png"
    fig.savefig(png, dpi=110)
    print("wrote", png)


if __name__ == "__main__":
    main()
