"""Live viewer.

    python -m naneye.viewer --source replay          reference frames, no hardware needed
    python -m naneye.viewer --source auto            the first Teensy found
    python -m naneye.viewer --source COM7 --depth 10
    python -m naneye.viewer --source replay --snapshot shot.png   one frame, then exit

Keys: q quit, s save a PNG, r toggle raw/auto contrast, h toggle the histogram,
      SPACE pause, +/- exposure (rows_in_reset), l toggle the LED, [ / ] LED current.
"""

from __future__ import annotations

import argparse
import time

import numpy as np

from . import protocol
from .sources import autoscale, open_source

LINE_H = 22


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


def status_lines(header: protocol.Header, img: np.ndarray, fps: float, gaps: int) -> list:
    saturated = float((img >= (1020 if img.dtype == np.uint16 else 255)).mean() * 100)
    return [
        f"frame {header.frame_counter}  {fps:4.1f} fps  {header.format_name}",
        f"min {int(img.min())}  max {int(img.max())}  mean {img.mean():6.1f}"
        f"  sat {saturated:.2f}%",
        f"exp {header.exposure_us() / 1000:6.2f} ms  sclk {header.sclk_hz / 1e6:.3f} MHz"
        f"  cfg 0x{header.cfg0:04X}/0x{header.cfg1:04X}",
        f"dropped {header.frames_dropped}  counter gaps {gaps}"
        f"  rows_failed {header.rows_failed}"
        + ("  SYNC LOST" if header.sync_lost else ""),
    ]


def compose(header: protocol.Header, img: np.ndarray, scale: int = 2,
            raw_mode: bool = False, show_hist: bool = True, fps: float = 0.0,
            gaps: int = 0) -> np.ndarray:
    """Build the full display image: frame, status bar and optional histogram.

    Shared by the live loop and --snapshot so the two cannot disagree.
    """
    import cv2

    if raw_mode:
        disp = img.astype(np.uint8) if img.dtype == np.uint8 else (img >> 2).astype(np.uint8)
    else:
        disp = autoscale(img)
    view = cv2.resize(disp, (img.shape[1] * scale, img.shape[0] * scale),
                      interpolation=cv2.INTER_NEAREST)
    view = cv2.cvtColor(view, cv2.COLOR_GRAY2BGR)

    lines = status_lines(header, img, fps, gaps)
    bar = np.zeros((LINE_H * len(lines) + 8, view.shape[1], 3), dtype=np.uint8)
    for i, text in enumerate(lines):
        colour = (0, 0, 255) if "SYNC LOST" in text else (220, 220, 220)
        cv2.putText(bar, text, (8, 18 + i * LINE_H), cv2.FONT_HERSHEY_SIMPLEX, 0.45,
                    colour, 1, cv2.LINE_AA)

    panels = [view, bar]
    if show_hist:
        panels.append(cv2.cvtColor(draw_histogram(img, view.shape[1]), cv2.COLOR_GRAY2BGR))
    return np.vstack(panels)


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
    ap.add_argument("--snapshot", metavar="PATH",
                    help="write one composed frame to PATH and exit (no window)")
    args = ap.parse_args(argv)

    fmt = {8: protocol.FMT_GRAY8, 10: protocol.FMT_GRAY10,
           12: protocol.FMT_RAW12}[args.depth]
    source = open_source(args.source, depth=args.depth, clock_hz=args.clock, fmt=fmt,
                         fps=args.fps)
    print(f"source: {source.name}")
    for line in getattr(source, "log", []):
        print(f"  {line}")

    if args.snapshot:
        with source:
            # Skip a couple of frames so the reported rate is representative.
            for n, (header, img) in enumerate(source.frames()):
                if n < 2:
                    continue
                shot = compose(header, img, scale=args.scale, fps=args.fps)
                cv2.imwrite(args.snapshot, shot)
                print(f"wrote {args.snapshot} ({shot.shape[1]}x{shot.shape[0]})")
                print(f"  {header.describe()}")
                return 0
        print("no frames received")
        return 1

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
                composite = compose(header, img, scale=args.scale, raw_mode=raw_mode,
                                    show_hist=show_hist, fps=fps, gaps=gaps)
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
