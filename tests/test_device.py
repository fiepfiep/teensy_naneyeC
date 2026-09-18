"""Device command handling, without hardware.

Written after the first session against a real Teensy: SELFTEST sends two replies, and
ask() stopped as soon as the input buffer was momentarily empty, so the second reply was
read by the next command and that command's own reply went missing.
"""

import numpy as np
import pytest

from naneye import fake, protocol
from naneye.transport import Device


class FakeSerial:
    """Serves queued chunks from read(); an empty chunk stands for one quiet period."""

    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.written = b""
        self.timeout = 1.0

    def read(self, n):
        return self._chunks.pop(0) if self._chunks else b""

    def write(self, data):
        self.written += data
        return len(data)

    def flush(self):
        pass

    def close(self):
        pass


def reply(text):
    return protocol.text_packet(protocol.TYPE_RESPONSE, text)


def test_collects_every_reply_of_a_multi_reply_command():
    ser = FakeSerial([reply("SELFTEST unpack: ... PASS"), reply("SELFTEST exposure: ... PASS")])
    dev = Device(ser=ser)
    assert dev.ask("SELFTEST") == ["SELFTEST unpack: ... PASS", "SELFTEST exposure: ... PASS"]


def test_next_command_gets_its_own_reply_not_the_previous_ones_leftover():
    """The exact failure seen on hardware: STATS was shown SELFTEST's second line."""
    ser = FakeSerial([
        reply("SELFTEST unpack: PASS"), reply("SELFTEST exposure: PASS"), b"",
        reply("STATS frames=0"),
    ])
    dev = Device(ser=ser)
    assert dev.ask("SELFTEST") == ["SELFTEST unpack: PASS", "SELFTEST exposure: PASS"]
    assert dev.ask("STATS") == ["STATS frames=0"]


def test_waits_for_a_slow_first_reply():
    """PROBE runs a whole frame cycle before answering; quiet periods before the first
    reply must not end the wait."""
    ser = FakeSerial([b"", b"", b"", reply("PROBE words=656")])
    dev = Device(ser=ser)
    assert dev.ask("PROBE 2", timeout=5.0) == ["PROBE words=656"]


def test_image_packets_between_replies_are_skipped():
    img = np.zeros((320, 320), dtype=np.uint16)
    ser = FakeSerial([fake.frame_packet(img, 0), reply("ID naneye-teensy"),
                      fake.frame_packet(img, 1)])
    dev = Device(ser=ser)
    assert dev.ask("ID") == ["ID naneye-teensy"]


def test_sends_a_newline_terminated_ascii_line():
    ser = FakeSerial([reply("ok")])
    Device(ser=ser).ask("  EXP 64 ")
    assert ser.written == b"EXP 64\n"


def test_never_reconfigures_the_port():
    """Changing the timeout reconfigures the port; on Windows doing that between sending a
    command and reading its reply threw the reply away. ask() must leave it alone."""
    ser = FakeSerial([reply("ok")])
    ser.timeout = 0.15
    seen = []

    class Watch(FakeSerial):
        def __setattr__(self, name, value):
            if name == "timeout" and "timeout" in self.__dict__:
                seen.append(value)
            super().__setattr__(name, value)

    w = Watch([reply("ok")])
    Device(ser=w).ask("ID")
    assert seen == [], f"ask() reconfigured the port: {seen}"


def test_live_stream_survives_a_quiet_period():
    """The reader reports a read timeout as 'no packet'; a live stream must treat that as a
    pause. Iterating the raw reader stopped at the first lull and ended the viewer."""
    import itertools

    img = np.zeros((320, 320), dtype=np.uint16)
    ser = FakeSerial([fake.frame_packet(img, 0), b"", b"", fake.frame_packet(img, 1)])
    frames = list(itertools.islice(Device(ser=ser).frames(), 2))
    assert [p.header.frame_counter for p in frames] == [0, 1]


def test_ask_returns_promptly_while_streaming():
    """Seen on hardware: while streaming the line is never quiet, so ask() ran to its full
    timeout. An image arriving after the replies means they are complete."""
    img = np.zeros((320, 320), dtype=np.uint16)
    chunks = [reply("START ok  streaming")] + [fake.frame_packet(img, i) for i in range(50)]
    ser = FakeSerial(chunks)
    dev = Device(ser=ser)
    assert dev.ask("START", timeout=30) == ["START ok  streaming"]
    assert len(ser._chunks) > 40, "ask() consumed the stream instead of returning"


def test_returns_empty_when_nothing_answers():
    dev = Device(ser=FakeSerial([]))
    assert dev.ask("ID", timeout=0.3) == []


def test_open_first_refuses_non_teensy_ports(monkeypatch):
    """With no Teensy present it must fail, not open whatever serial port exists."""

    class Port:
        def __init__(self, device, vid):
            self.device, self.vid = device, vid

    monkeypatch.setattr(Device, "find_ports", staticmethod(lambda: [Port("COM3", None)]))
    with pytest.raises(RuntimeError, match="no Teensy"):
        Device.open_first()
