"""Packet reassembly, CRC rejection and resynchronisation.

Driven by naneye.fake, which re-encodes real frames in the wire format, so the host stack
is exercised end to end without a Teensy attached.
"""

import numpy as np
import pytest

from naneye import decode, fake, protocol


@pytest.fixture
def frames():
    """Three distinguishable 10-bit frames."""
    out = []
    for k in range(3):
        img = np.full((decode.HEIGHT, decode.WIDTH), 100 * (k + 1), dtype=np.uint16)
        img[0, 0] = 1023
        img[k, 1] = 512 + k
        out.append(img)
    return out


def read_all(data):
    return list(fake.reader_over_bytes(data))


def test_reads_every_frame(frames):
    data = fake.stream_bytes(frames, count=6)
    packets = read_all(data)
    assert len(packets) == 6
    assert all(p.is_image for p in packets)
    assert [p.header.frame_counter for p in packets] == list(range(6))


def test_payload_decodes_back_to_the_original(frames):
    data = fake.stream_bytes(frames, count=3, fmt=protocol.FMT_GRAY10)
    for packet, original in zip(read_all(data), frames):
        img = decode.decode_payload(packet.header, packet.payload)
        assert np.array_equal(img, original)


def test_corrupt_payload_is_rejected_not_returned(frames):
    """A bad CRC must cost exactly one frame, not desynchronise the stream."""
    data = fake.stream_bytes(frames, count=6, corrupt_every=2)
    reader = fake.reader_over_bytes(data)
    packets = list(reader)
    corrupted = [i for i in range(6) if i and i % 2 == 0]
    assert reader.bad_crc == len(corrupted)
    assert len(packets) == 6 - len(corrupted)
    # Frames after a corrupted one still arrive intact.
    assert [p.header.frame_counter for p in packets] == [
        i for i in range(6) if i not in corrupted]


def test_resyncs_past_leading_junk(frames):
    data = fake.stream_bytes(frames, count=3, noise="prefix")
    reader = fake.reader_over_bytes(data)
    packets = list(reader)
    assert len(packets) == 3
    assert reader.resyncs >= 1


def test_dropped_frames_are_visible_in_the_counters(frames):
    """Gaps in frame_counter and a rising frames_dropped are how drops are reported."""
    data = fake.stream_bytes(frames, count=9, drop_every=3)
    packets = read_all(data)
    counters = [p.header.frame_counter for p in packets]
    assert 3 not in counters and 6 not in counters
    assert packets[-1].header.frames_dropped == 2
    # The counter gap and the dropped total must agree.
    assert (counters[-1] + 1) - len(counters) == packets[-1].header.frames_dropped


def test_truncated_stream_ends_cleanly(frames):
    """A cable yanked mid-frame must yield the complete frames and then stop."""
    data = fake.stream_bytes(frames, count=3)
    packets = read_all(data[:-500])
    assert len(packets) == 2


def test_truncated_packet_does_not_swallow_the_next_one(frames):
    """Seen on hardware: a reply written while an image was still going out. Whatever the
    cause, a packet cut short on the wire claims bytes belonging to the packets after it;
    skipping its full claimed length on the CRC failure lost the reply that followed."""
    image = fake.frame_packet(frames[0], 0)
    cut = image[:protocol.HEADER_SIZE + 1000]            # header promises far more than this
    data = cut + protocol.text_packet(protocol.TYPE_RESPONSE, "STOP")
    data += fake.frame_packet(frames[1], 1) + b"\x00" * 200_000
    reader = fake.reader_over_bytes(data)
    packets = list(reader)
    texts = [p.text for p in packets if not p.is_image]
    assert texts == ["STOP"], "the reply after a truncated image must survive"
    assert [p.header.frame_counter for p in packets if p.is_image] == [1]
    assert reader.bad_crc >= 1


def test_text_packets_are_not_images():
    data = protocol.text_packet(protocol.TYPE_RESPONSE, "CLK 24750000 Hz")
    data += protocol.text_packet(protocol.TYPE_LOG, "re-syncing")
    packets = read_all(data)
    assert [p.header.type for p in packets] == [protocol.TYPE_RESPONSE, protocol.TYPE_LOG]
    assert packets[0].text == "CLK 24750000 Hz"
    assert not any(p.is_image for p in packets)


def test_interleaved_text_and_images_both_survive(frames):
    """Log output arriving between frames must not disturb image parsing."""
    data = b""
    for i, img in enumerate(frames):
        data += protocol.text_packet(protocol.TYPE_LOG, f"before frame {i}")
        data += fake.frame_packet(img, i)
    packets = read_all(data)
    assert len(packets) == 6
    images = [p for p in packets if p.is_image]
    assert len(images) == 3
    for packet, original in zip(images, frames):
        got = decode.decode_payload(packet.header, packet.payload)
        assert np.array_equal(got, (original >> 2).astype(np.uint8))


def test_absurd_payload_length_does_not_allocate(frames):
    """A header whose payload_len is nonsense must be skipped, not trusted."""
    bogus = protocol.Header(type=protocol.TYPE_IMAGE)
    bogus.payload_len = 0x7FFFFFFF
    bogus.header_len = protocol.HEADER_SIZE
    data = bogus.pack() + fake.frame_packet(frames[0], 0)
    reader = fake.reader_over_bytes(data)
    packets = list(reader)
    assert len(packets) == 1
    assert packets[0].header.frame_counter == 0
