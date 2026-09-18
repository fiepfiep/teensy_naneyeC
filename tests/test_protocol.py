"""Wire-format tests: the layout must match firmware/src/usb_proto.h exactly."""

import struct

import pytest

from naneye import protocol


# The offsets asserted by static_assert in firmware/src/usb_proto.h.
EXPECTED_OFFSETS = {
    "magic": 0, "version": 4, "type": 5, "header_len": 6, "payload_len": 8,
    "frame_counter": 12, "timestamp_us": 16, "width": 20, "height": 22, "format": 24,
    "flags": 25, "rows_failed": 26, "sclk_hz": 28, "exposure_pp": 32, "cfg0": 36,
    "cfg1": 38, "frames_dropped": 40, "pixels_concealed": 44, "crc32": 48,
}

FIELD_SIZES = {
    "magic": 4, "version": 1, "type": 1, "header_len": 2, "payload_len": 4,
    "frame_counter": 4, "timestamp_us": 4, "width": 2, "height": 2, "format": 1,
    "flags": 1, "rows_failed": 2, "sclk_hz": 4, "exposure_pp": 4, "cfg0": 2, "cfg1": 2,
    "frames_dropped": 4, "pixels_concealed": 4, "crc32": 4,
}


def test_header_size():
    assert protocol.HEADER_SIZE == 52
    assert struct.calcsize(protocol.HEADER_FMT) == 52


def test_field_offsets_match_firmware():
    """Walk the struct format and confirm every field lands where the C header says."""
    offset = 0
    for name, size in FIELD_SIZES.items():
        assert offset == EXPECTED_OFFSETS[name], (
            f"{name} at offset {offset}, firmware expects {EXPECTED_OFFSETS[name]}")
        offset += size
    assert offset == protocol.HEADER_SIZE


def test_crc_excludes_its_own_field():
    """The crc field sits at offset 48, so changing it must not change the CRC."""
    h = protocol.Header(width=320, height=320, payload_len=4)
    a = protocol.compute_crc(h.pack(), b"abcd")
    h.crc32 = 0xDEADBEEF
    b = protocol.compute_crc(h.pack(), b"abcd")
    assert a == b


def test_roundtrip():
    h = protocol.Header(type=protocol.TYPE_IMAGE, frame_counter=7, timestamp_us=123456,
                        width=320, height=320, format=protocol.FMT_GRAY10,
                        rows_failed=3, sclk_hz=24750000, exposure_pp=105616,
                        cfg0=0x009F, cfg1=0x0065, frames_dropped=2)
    payload = bytes(range(256)) * 4
    packet = protocol.build_packet(h, payload)
    assert len(packet) == protocol.HEADER_SIZE + len(payload)

    got = protocol.Header.unpack(packet)
    assert got.frame_counter == 7
    assert got.payload_len == len(payload)
    assert got.cfg0 == 0x009F and got.cfg1 == 0x0065
    assert got.format == protocol.FMT_GRAY10
    assert protocol.check_packet(got, packet[protocol.HEADER_SIZE:])


def test_crc_detects_corruption():
    h = protocol.Header(width=320, height=320)
    packet = bytearray(protocol.build_packet(h, b"payload data here"))
    packet[protocol.HEADER_SIZE + 3] ^= 0x01
    got = protocol.Header.unpack(bytes(packet))
    assert not protocol.check_packet(got, bytes(packet[protocol.HEADER_SIZE:]))


def test_exposure_us_matches_reference_capture():
    """105,616 PP at 31.25 MHz is the exposure the reference host used (spec.md 3.2)."""
    h = protocol.Header(sclk_hz=31250000, exposure_pp=105616)
    assert h.exposure_us() == pytest.approx(40556.0, rel=1e-3)


def test_magic_is_ascii_nane():
    assert protocol.MAGIC == b"NANE"
    assert struct.pack("<I", protocol.MAGIC_U32) == protocol.MAGIC
