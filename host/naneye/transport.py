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

            if not protocol.check_packet(header, payload):
                # Skip only the magic word, not the whole claimed length. A packet that was
                # cut short on the wire claims bytes that really belong to the packets after
                # it; discarding payload_len of them would swallow those too.
                self.bad_crc += 1
                del self._buf[:len(protocol.MAGIC)]
                continue
            del self._buf[:total]
            return Packet(header, payload)

    def __iter__(self) -> Iterator[Packet]:
        while True:
            p = self.next_packet()
            if p is None:
                return
            yield p


class Device:
    """A Teensy on a USB serial port.

    The port is opened with a short read timeout, set once and never changed. Two lessons
    from the first session against hardware are behind that:

    - Changing the timeout reconfigures the port, and on Windows doing that between sending
      a command and reading its reply discarded replies the device had already sent.
    - PacketReader reports a read timeout as "no packet", so with a long timeout a live
      stream ended on any lull longer than it. Here a quiet line is a pause: packets() keeps
      waiting, and ask() treats one quiet read as "the device has finished answering".
    """

    PJRC_VID = 0x16C0
    READ_TIMEOUT = 0.15  # one quiet read; long enough to span USB and driver latency

    def __init__(self, port: str | None = None, timeout: float = READ_TIMEOUT, ser=None):
        """Open `port`, or wrap an already-open serial-like object passed as `ser`.

        Anything with read(n), write(b) and flush() will do, which is what lets the command
        logic be tested without hardware.
        """
        if ser is None:
            import serial  # imported lazily so the decoder works without pyserial

            ser = serial.Serial(port, 115200, timeout=timeout)
        self.serial = ser
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
    def open_first(cls, timeout: float = READ_TIMEOUT) -> "Device":
        """The first Teensy found. Never falls back to some other serial port: sending
        START to a modem or a management console is worse than failing."""
        teensy = [p for p in cls.find_ports() if p.vid == cls.PJRC_VID]
        if not teensy:
            raise RuntimeError(
                "no Teensy serial port found (USB VID 0x16C0). Is it plugged in and running "
                "the USB Serial firmware? A Teensy running a Raw HID sketch has no COM port.")
        return cls(teensy[0].device, timeout=timeout)

    def command(self, text: str) -> None:
        """Send an ASCII command line. Host to device is plain text; replies are framed."""
        self.serial.write((text.strip() + "\n").encode())
        self.serial.flush()

    def ask(self, text: str, timeout: float = 2.0) -> list:
        """Send a command and collect every response/log packet it produces.

        Waits up to `timeout` for the first reply -- some commands, like PROBE, run a whole
        frame cycle before answering -- then stops at the first quiet read, which means the
        device has finished. Two-reply commands such as SELFTEST send back to back, so both
        arrive before the line goes quiet.

        While streaming the line is never quiet, so an image arriving after the replies ends
        the collection too: the firmware finishes any image in flight, sends every reply,
        and only then starts the next image.
        """
        self.command(text)
        out = []
        deadline = time.time() + timeout
        while time.time() < deadline:
            p = self.reader.next_packet()
            if p is None:
                if out:
                    break  # replies have arrived and the line has gone quiet
                continue   # nothing yet: keep waiting for a slow command
            if p.header.type in (protocol.TYPE_RESPONSE, protocol.TYPE_LOG):
                out.append(p.text)
            elif p.is_image and out:
                break  # streaming: the replies are complete once an image follows them
        return out

    def packets(self) -> Iterator[Packet]:
        """Every packet, indefinitely. A quiet line is a pause, not the end of the stream:
        a device that drops frames for a while must not end the caller's loop."""
        while True:
            p = self.reader.next_packet()
            if p is not None:
                yield p

    def frames(self) -> Iterator[Packet]:
        for p in self.packets():
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
