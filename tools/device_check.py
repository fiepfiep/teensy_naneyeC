"""Talk to the Teensy with no sensor attached: the M0 check in docs/hardware.md.

    uv run python tools/device_check.py            # first Teensy found
    uv run python tools/device_check.py COM7
    uv run python tools/device_check.py --probe    # also exercise the LPSPI/DMA path

ID and SELFTEST touch no hardware beyond USB. --probe runs a full, phase-correct frame
cycle on the real LPSPI + DMA path. With nothing on the header, SDAT sits on its pull-down,
so every word should read 0x000 -- and the rows completing at all proves that receive-only
frames are being clocked and DMA'd, which is the firmware's biggest untested assumption.
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "host"))

from naneye.transport import Device  # noqa: E402


def find_teensy():
    from serial.tools import list_ports

    for p in list_ports.comports():
        if p.vid == 0x16C0:
            return p.device
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("port", nargs="?")
    ap.add_argument("--probe", action="store_true",
                    help="run PROBE (full frame cycle on LPSPI + DMA, no sensor needed)")
    ap.add_argument("--wait", type=float, default=15.0,
                    help="seconds to wait for the port to appear after flashing")
    args = ap.parse_args()

    port = args.port
    deadline = time.time() + args.wait
    while port is None and time.time() < deadline:
        port = find_teensy()
        if port is None:
            time.sleep(0.5)
    if port is None:
        print("no Teensy serial port found (VID 0x16C0). Is the USB Serial firmware flashed?")
        return 1

    print(f"port {port}")
    with Device(port) as dev:
        time.sleep(0.3)
        dev.serial.reset_input_buffer()

        failures = 0
        for cmd in ("ID", "SELFTEST", "STATS"):
            replies = dev.ask(cmd, timeout=3.0)
            if not replies:
                print(f"{cmd:9} -> (no reply)")
                failures += 1
            for r in replies:
                print(f"{cmd:9} -> {r}")
                if "FAIL" in r:
                    failures += 1

        if args.probe:
            # A frame cycle at 12.375 MHz is ~104 ms; allow far longer in case the
            # peripheral stalls and the firmware's spin limits have to expire.
            print("\nPROBE 2 (no sensor attached: expect every word 0x000) ...")
            t0 = time.time()
            replies = dev.ask("PROBE 2", timeout=20.0)
            dt = time.time() - t0
            for r in replies:
                print(f"PROBE     -> {r}")
            print(f"          ({dt:.2f} s)")
            if not replies:
                print("PROBE produced no reply: the LPSPI/DMA path stalled")
                failures += 1

        print("\n" + ("all checks passed" if failures == 0 else f"{failures} check(s) failed"))
        return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
