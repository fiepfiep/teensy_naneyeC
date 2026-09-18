"""Capture the Teensy's SEIM link on the Saleae, triggered by the sensor power enable.

    uv run python tools/capture_link.py                      # START FORCE, 12.375 MHz
    uv run python tools/capture_link.py --start START --clock 24750000

Arms a capture on the rising edge of NanEye_EN, then sends the start command, so the whole
power-up sequence is recorded -- activation clock, register writes, alignment clocks,
pre-sync -- followed by a few streaming frames. Exports per-channel binaries plus a .sal
that opens in Logic 2. Analyse with tools/analyze_link.py.

Default channel map (current bench wiring, docs/hardware.md "Bench setup"):
    D0 = Teensy pin 1  (SDAT in,  LPSPI3_SDI)
    D1 = Teensy pin 2  (NanEye_EN)
    D2 = Teensy pin 26 (SDAT out, LPSPI3_SDO)
    D3 = Teensy pin 27 (SCLK)
    D4 = VCC_SENSOR on the NanoBerry (switched 3.3 V sensor rail)
    D5 = SDAT on the NanoBerry, sensor side of R23
    D6 = SCLK on the NanoBerry, sensor side of R20
Pass a negative number to leave a channel out.
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "host"))

from serial.tools import list_ports  # noqa: E402

from naneye.saleae import Logic, SaleaeError  # noqa: E402
from naneye.transport import Device  # noqa: E402


def find_teensy(wait_s=15.0):
    deadline = time.time() + wait_s
    while time.time() < deadline:
        port = next((p.device for p in list_ports.comports() if p.vid == 0x16C0), None)
        if port:
            return port
        time.sleep(0.5)
    raise SystemExit("no Teensy serial port found")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", default="START FORCE",
                    help="command that powers up and starts the link (default: START FORCE)")
    ap.add_argument("--clock", type=int, default=12375000)
    ap.add_argument("--rate", type=float, default=250e6, help="Saleae digital sample rate")
    ap.add_argument("--after", type=float, default=0.55, help="seconds to record after EN rises")
    ap.add_argument("--out", default="build/saleae/link")
    ap.add_argument("--sdi", type=int, default=0)
    ap.add_argument("--en", type=int, default=1)
    ap.add_argument("--sdo", type=int, default=2)
    ap.add_argument("--sclk", type=int, default=3)
    ap.add_argument("--vcc", type=int, default=4)
    ap.add_argument("--sdat-board", type=int, default=5)
    ap.add_argument("--sclk-board", type=int, default=6)
    args = ap.parse_args()

    chmap = {k: v for k, v in {"sdi": args.sdi, "en": args.en, "sdo": args.sdo,
                               "sclk": args.sclk, "vcc": args.vcc,
                               "sdat_board": args.sdat_board,
                               "sclk_board": args.sclk_board}.items() if v >= 0}
    channels = sorted(chmap.values())
    logic = Logic()
    devs = logic.devices()
    print("saleae:", ", ".join(f"{d['deviceType']} {d['deviceId']}" for d in devs))

    with Device(find_teensy()) as dev:
        time.sleep(0.3)
        dev.serial.reset_input_buffer()
        # A clean low on EN, so the trigger sees a real rising edge.
        for cmd in ("STOP", "POWER 0", f"CLK {args.clock}"):
            dev.ask(cmd, timeout=3)
        time.sleep(0.2)

        rate = args.rate
        while True:
            try:
                cap = logic.start_triggered(channels, rate=rate, trigger=chmap["en"],
                                            after_s=args.after)
                break
            except SaleaeError as e:
                if rate <= 50e6:
                    raise
                print(f"  {rate / 1e6:.0f} MS/s refused ({e}); trying lower")
                rate /= 2
        print(f"armed: capture {cap}, {rate / 1e6:.0f} MS/s on D{channels}, "
              f"trigger D{chmap['en']} rising, {args.after} s after")
        time.sleep(1.0)  # let the capture arm before the edge happens

        t0 = time.time()
        replies = dev.ask(args.start, timeout=10)
        print(f"{args.start} -> {replies}")
        logic.wait(cap)
        print(f"capture complete ({time.time() - t0:.1f} s)")
        for cmd in ("STOP", "POWER 0"):
            dev.ask(cmd, timeout=3)

    os.makedirs(args.out, exist_ok=True)
    traces = logic.export_digital(cap, args.out, channels)
    logic.save(cap, args.out + ".sal")
    logic.close(cap)
    with open(os.path.join(args.out, "meta.json"), "w") as f:
        json.dump({"channels": chmap, "rate": rate, "clock": args.clock,
                   "start": args.start, "replies": replies}, f, indent=2)
    for ch, tr in traces.items():
        name = next(k for k, v in chmap.items() if v == ch)
        print(f"  D{ch} {name:10}: {len(tr.times):>10,} transitions, initial {tr.initial}")
    print(f"exported to {args.out}/ and {args.out}.sal")


if __name__ == "__main__":
    main()
