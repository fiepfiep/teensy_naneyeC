"""Frame sources: a live Teensy, a recorded stream, or replayed reference frames.

Every tool takes a source, so the viewer, recorder and analyser all work identically on
live hardware and on replayed data. That is what makes the host side developable and
demonstrable with no camera attached.
"""

from __future__ import annotations

import io
import time
from typing import Iterator, Optional

import numpy as np

from . import decode, protocol
from .transport import Packet, PacketReader


class Source:
    """Yields (header, image) pairs."""

    name = "source"

    def frames(self) -> Iterator[tuple]:
        raise NotImplementedError

    def command(self, text: str) -> None:
        """Send a command, where the source supports it."""
        pass

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _decode(packet: Packet):
    img = decode.decode_payload(packet.header, packet.payload)
    if packet.header.format == protocol.FMT_RAW12:
        img = decode.raw12_to_image(img)
    return packet.header, img


class DeviceSource(Source):
    """A live Teensy on a serial port."""

    def __init__(self, port: Optional[str] = None, start: bool = True,
                 clock_hz: int = 12375000, depth: int = 8):
        from .transport import Device

        self.device = Device(port) if port else Device.open_first()
        self.name = f"device {self.device.serial.port}"
        self._log = []
        if start:
            for cmd in (f"CLK {clock_hz}", f"DEPTH {depth}", "POWER 1", "START"):
                self._log += self.device.ask(cmd)

    @property
    def log(self):
        return self._log

    def command(self, text: str) -> None:
        self._log += self.device.ask(text)

    def frames(self):
        for packet in self.device.reader:
            if packet.is_image:
                yield _decode(packet)
            elif packet.header.type in (protocol.TYPE_LOG, protocol.TYPE_RESPONSE):
                self._log.append(packet.text)

    def close(self):
        try:
            self.device.ask("STOP")
        except Exception:
            pass
        self.device.close()


class FileSource(Source):
    """A recorded packet stream (see record.py --raw)."""

    def __init__(self, path):
        self.name = f"file {path}"
        self._f = open(path, "rb")
        self._reader = PacketReader(self._f.read)

    def frames(self):
        for packet in self._reader:
            if packet.is_image:
                yield _decode(packet)

    def close(self):
        self._f.close()


class ReplaySource(Source):
    """Reference frames re-encoded in the wire format and played back at a set rate.

    Exercises the real packet path, so what the viewer receives here is byte-for-byte what
    it would receive from the Teensy.
    """

    def __init__(self, frames=None, fps: float = 19.3, fmt: int = protocol.FMT_GRAY8,
                 loop: bool = True, golden_dir: str = "build/golden"):
        from . import fake

        if frames is None:
            frames = fake.load_golden_frames(golden_dir)
        self._frames = list(frames)
        self._fake = fake
        self._fmt = fmt
        self._fps = fps
        self._loop = loop
        self.name = f"replay of {len(self._frames)} reference frames at {fps:.1f} fps"

    def frames(self):
        i = 0
        period = 1.0 / self._fps if self._fps > 0 else 0.0
        while True:
            if not self._loop and i >= len(self._frames):
                return
            img = self._frames[i % len(self._frames)]
            data = self._fake.frame_packet(img, i, fmt=self._fmt)
            reader = PacketReader(io.BytesIO(data).read)
            packet = reader.next_packet()
            t0 = time.time()
            yield _decode(packet)
            i += 1
            if period:
                dt = period - (time.time() - t0)
                if dt > 0:
                    time.sleep(dt)


def open_source(spec: str, **kwargs) -> Source:
    """Build a source from a command-line string.

    'replay'        reference frames from build/golden
    'auto'          the first Teensy serial port found
    'COM7'          that serial port
    a file path     a recorded packet stream
    """
    import os

    if spec == "replay":
        return ReplaySource(**{k: v for k, v in kwargs.items()
                               if k in ("fps", "fmt", "loop", "golden_dir")})
    if spec == "auto":
        return DeviceSource(None, **{k: v for k, v in kwargs.items()
                                     if k in ("start", "clock_hz", "depth")})
    if os.path.exists(spec):
        return FileSource(spec)
    return DeviceSource(spec, **{k: v for k, v in kwargs.items()
                                 if k in ("start", "clock_hz", "depth")})


def autoscale(img: np.ndarray, low: float = 0.5, high: float = 99.5) -> np.ndarray:
    """Percentile contrast stretch to 8-bit, for display."""
    f = img.astype(np.float32)
    lo, hi = np.percentile(f, [low, high])
    if hi <= lo:
        hi = lo + 1
    return (((f - lo) / (hi - lo)) * 255).clip(0, 255).astype(np.uint8)
