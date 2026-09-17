"""Live viewer.

    python -m naneye.viewer --source replay          reference frames, no hardware needed
    python -m naneye.viewer --source auto            the first Teensy found
    python -m naneye.viewer --source COM7 --depth 10

Keys: q quit, s save a PNG, r toggle raw/auto contrast, h toggle the histogram,
      SPACE pause, +/- exposure (rows_in_reset), l toggle the LED, [ / ] LED current.
"""

from __future__ import annotations

import argparse
import time

import numpy as np

from . import protocol
from .sources import autoscale, open_source


def draw_histogram(img: np.ndarray, width: int, height: int = 90) -> np.ndarray:
    """A small log-scaled histogram strip."""
    bins = 128
    hist, _ = np.histogram(img, bins=bins, range=(0, 1024 if img.dtype == np.uint16 else 256))
    hist = np.log1p(hist.astype(np.float32))
    if hist.max() > 0:
        hist = hist / hist.max()
    panel = np.zeros((height, width), dtype=np.uint8)
    for x in range(width):
        v = hist[min(int(x / width * bins), bins - 1)]
        panel[height - int(v * (height - 1)):, x] = 200
    return panel


def main(argv=None):
    import cv2

    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", default="replay",
                    help="'replay', 'auto', a COM port, or a recorded stream file")
    ap.add_argument("--depth", type=int, default=8, choices=(8, 10, 12))
    ap.add_argument("--clock", type=int, default=12375000)
    ap.add_argument("--scale", type=int, default=2)
    ap.add_argument("--fps", type=float, default=19.3, help="replay rate")
    args = ap.parse_args(argv)

    fmt = {8: protocol.FMT_GRAY8, 10: protocol.FMT_GRAY10,
           12: protocol.FMT_RAW12}[args.depth]
    source = open_source(args.source, depth=args.depth, clock_hz=args.clock, fmt=fmt,
                         fps=args.fps)
    print(f"source: {source.name}")
    for line in getattr(source, "log", []):
        print(f"  {line}")

    window = "NanEyeC"
    cv2.namedWindow(window, cv2.WINDOW_NORMAL)
    sized = False  # the window is fitted to the first composite, panels included

    raw_mode = False
    show_hist = True
    paused = False
    rows_in_reset = 0
    led_on = False
    led_ma = 5.0
    saved = 0

    times = []
    last_counter = None
    gaps = 0

    with source:
        for header, img in source.frames():
            if last_counter is not None:
                gaps += max(0, header.frame_counter - last_counter - 1)
            last_counter = header.frame_counter

            now = time.time()
            times.append(now)
            times[:] = [t for t in times if now - t < 2.0]
            fps = (len(times) - 1) / (times[-1] - times[0]) if len(times) > 1 else 0.0

            if not paused:
                disp = img.astype(np.uint8) if (raw_mode and img.dtype == np.uint8) else (
                    (img >> 2).astype(np.uint8) if raw_mode else autoscale(img))
                view = cv2.resize(disp, (320 * args.scale, 320 * args.scale),
                                  interpolation=cv2.INTER_NEAREST)
                view = cv2.cvtColor(view, cv2.COLOR_GRAY2BGR)

                sat = float((img >= (1020 if img.dtype == np.uint16 else 255)).mean() * 100)
                lines = [
                    f"frame {header.frame_counter}  {fps:4.1f} fps  {header.format_name}",
                    f"min {int(img.min())}  max {int(img.max())}  mean {img.mean():6.1f}"
                    f"  sat {sat:.2f}%",
                    f"exp {header.exposure_us() / 1000:6.2f} ms  sclk "
                    f"{header.sclk_hz / 1e6:.3f} MHz  cfg 0x{header.cfg0:04X}/0x{header.cfg1:04X}",
                    f"dropped {header.frames_dropped}  counter gaps {gaps}"
                    f"  rows_failed {header.rows_failed}"
                    + ("  SYNC LOST" if header.sync_lost else ""),
                ]
                bar = np.zeros((22 * len(lines) + 8, view.shape[1], 3), dtype=np.uint8)
                for i, text in enumerate(lines):
                    colour = (0, 0, 255) if ("SYNC LOST" in text) else (220, 220, 220)
                    cv2.putText(bar, text, (8, 18 + i * 22), cv2.FONT_HERSHEY_SIMPLEX,
                                0.45, colour, 1, cv2.LINE_AA)
                panels = [view, bar]
                if show_hist:
                    hist = draw_histogram(img, view.shape[1])
                    panels.append(cv2.cvtColor(hist, cv2.COLOR_GRAY2BGR))
                composite = np.vstack(panels)
                if not sized:
                    cv2.resizeWindow(window, composite.shape[1], composite.shape[0])
                    sized = True
                cv2.imshow(window, composite)

            key = cv2.waitKey(1) & 0xFF
            if key == ord("q") or key == 27:
                break
            elif key == ord("s"):
                name = f"frame_{header.frame_counter:06d}.png"
                cv2.imwrite(name, autoscale(img))
                saved += 1
                print(f"saved {name}")
            elif key == ord("r"):
                raw_mode = not raw_mode
            elif key == ord("h"):
                show_hist = not show_hist
                sized = False
            elif key == ord(" "):
                paused = not paused
            elif key in (ord("+"), ord("=")):
                rows_in_reset = min(159, rows_in_reset + 8)
                source.command(f"EXP {rows_in_reset}")
            elif key == ord("-"):
                rows_in_reset = max(0, rows_in_reset - 8)
                source.command(f"EXP {rows_in_reset}")
            elif key == ord("l"):
                led_on = not led_on
                source.command(f"LEDI {led_ma}")
                source.command(f"LED {1 if led_on else 0}")
            elif key == ord("]"):
                led_ma = min(20.0, led_ma + 1.0)
                source.command(f"LEDI {led_ma}")
            elif key == ord("["):
                led_ma = max(0.0, led_ma - 1.0)
                source.command(f"LEDI {led_ma}")

    cv2.destroyAllWindows()
    if saved:
        print(f"{saved} image(s) saved")


if __name__ == "__main__":
    main()
