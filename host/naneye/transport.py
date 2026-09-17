"""Framed packet transport over a byte stream (USB serial, socket, pipe or file).

The reader resynchronises on the magic word and verifies every CRC, so a corrupted or
partial packet costs at most one frame rather than desynchronising the stream. Keeping it
stream-agnostic means tools/fake_device.py can exercise the whole host stack with no
hardware attached.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Iterator, Optional

from . import protocol


@dataclass
class Packet:
    header: protocol.Header
    payload: bytes

    @property
    def is_image(self) -> bool:
        return self.header.type == protocol.TYPE_IMAGE

    @property
    def text(self) -> str:
        return self.payload.decode("utf-8", "replace")


class PacketReader:
    """Reassembles packets from a stream exposing read(n) -> bytes."""

    def __init__(self, read: Callable[[int], bytes], max_payload: int = 1 << 20):
        self._read = read
        self._buf = bytearray()
        self._max_payload = max_payload
        self.bad_crc = 0
        self.resyncs = 0
        self.bytes_read = 0

    def _fill(self, n: int) -> bool:
        """Ensure the buffer holds at least n bytes. False if the stream ended."""
        while len(self._buf) < n:
            chunk = self._read(max(n - len(self._buf), 4096))
            if not chunk:
                return False
            self._buf += chunk
            self.bytes_read += len(chunk)
        return True

    def _find_magic(self) -> bool:
        """Drop bytes until the buffer starts with the magic word."""
        while True:
            idx = self._buf.find(protocol.MAGIC)
            if idx == 0:
                return True
            if idx > 0:
                del self._buf[:idx]
                self.resyncs += 1
                return True
            # Keep the last 3 bytes: the magic may straddle the boundary.
            keep = min(len(self._buf), 3)
            if len(self._buf) > keep:
                del self._buf[:len(self._buf) - keep]
                self.resyncs += 1
            if not self._fill(len(self._buf) + 1):
                return False

    def next_packet(self) -> Optional[Packet]:
        """The next valid packet, or None when the stream ends."""
        while True:
            if not self._find_magic():
                return None
            if not self._fill(protocol.HEADER_SIZE):
                return None
            try:
                header = protocol.Header.unpack(bytes(self._buf[:protocol.HEADER_SIZE]))
            except ValueError:
                del self._buf[:1]
                continue

            if (header.version != protocol.VERSION
                    or header.header_len != protocol.HEADER_SIZE
                    or header.payload_len > self._max_payload):
                del self._buf[:1]  # not a real header, keep scanning
                self.resyncs += 1
                continue

            total = protocol.HEADER_SIZE + header.payload_len
            if not self._fill(total):
                return None
            payload = bytes(self._buf[protocol.HEADER_SIZE:total])
            del self._buf[:total]

            if not protocol.check_packet(header, payload):
                self.bad_crc += 1
                continue
            return Packet(header, payload)

    def __iter__(self) -> Iterator[Packet]:
        while True:
            p = self.next_packet()
            if p is None:
                return
            yield p


class Device:
    """A Teensy on a USB serial port."""

    def __init__(self, port: str, timeout: float = 1.0):
        import serial  # imported lazily so the decoder works without pyserial

        self.serial = serial.Serial(port, 115200, timeout=timeout)
        self.reader = PacketReader(self.serial.read)

    @staticmethod
    def find_ports() -> list:
        """Candidate Teensy serial ports, most likely first."""
        from serial.tools import list_ports

        ports = list(list_ports.comports())
        # Teensy uses PJRC's vendor id 0x16C0.
        teensy = [p for p in ports if (p.vid == 0x16C0)]
        return teensy + [p for p in ports if p not in teensy]

    @classmethod
    def open_first(cls, timeout: float = 1.0) -> "Device":
        ports = cls.find_ports()
        if not ports:
            raise RuntimeError("no serial ports found; is the Teensy plugged in?")
        return cls(ports[0].device, timeout=timeout)

    def command(self, text: str) -> None:
        """Send an ASCII command line (the device also accepts framed commands)."""
        self.serial.write((text.strip() + "\n").encode())
        self.serial.flush()

    def ask(self, text: str, timeout: float = 2.0) -> list:
        """Send a command and collect the response/log packets it produces."""
        self.command(text)
        out = []
        deadline = time.time() + timeout
        while time.time() < deadline:
            p = self.reader.next_packet()
            if p is None:
                break
            if p.header.type in (protocol.TYPE_RESPONSE, protocol.TYPE_LOG):
                out.append(p.text)
                # Responses arrive promptly; stop once the stream goes quiet.
                if not self.serial.in_waiting:
                    break
            elif p.is_image:
                continue
        return out

    def frames(self) -> Iterator[Packet]:
        for p in self.reader:
            if p.is_image:
                yield p

    def close(self) -> None:
        try:
            self.serial.close()
        except Exception:
            pass

    def __enter__(self) -> "Device":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
