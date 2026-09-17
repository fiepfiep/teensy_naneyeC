"""Pixel-period extraction, checked against the row embedded in the firmware.

firmware/src/golden_vector.h holds one real row from the working reference link plus its
expected pixel values; the on-device SELFTEST compares the C unpack against it. These tests
hold the Python decoder to the same data, so the two implementations cannot drift.
"""

import numpy as np
import pytest

from naneye import decode, fake, protocol


def test_pp_at_matches_expected_pixels(golden_vector):
    words = golden_vector["words"]
    expected = golden_vector["pixels"]
    assert len(words) == decode.ROW_WORDS + 1, "expected 123 words plus one of padding"
    assert len(expected) == decode.WIDTH

    for i, want in enumerate(expected):
        word = decode.pp_at(words, decode.TRAINING_PP + i)
        assert (word >> 11) == 1, f"pixel {i}: start bit not set ({word:#05x})"
        assert (word & 1) == 0, f"pixel {i}: stop bit not clear ({word:#05x})"
        assert ((word >> 1) & 0x3FF) == want, f"pixel {i}"


def test_training_words_are_555(golden_vector):
    words = golden_vector["words"]
    for i in range(decode.TRAINING_PP):
        assert decode.pp_at(words, i) == decode.WORD_TRAINING


def test_vectorised_matches_scalar(golden_vector):
    """words_to_pp() is the fast path; it must agree with pp_at() everywhere."""
    words = golden_vector["words"]
    fast = decode.words_to_pp(words, decode.ROW_PP)
    slow = np.array([decode.pp_at(words, i) for i in range(decode.ROW_PP)], dtype=np.uint16)
    assert np.array_equal(fast, slow)


def test_vectorised_pixels_match_expected(golden_vector):
    pp = decode.words_to_pp(golden_vector["words"], decode.ROW_PP)
    assert np.all(decode.validate_pp(pp[decode.TRAINING_PP:]))
    pixels = decode.pp_to_pixels(pp[decode.TRAINING_PP:])
    assert np.array_equal(pixels, np.array(golden_vector["pixels"], dtype=np.uint16))


def test_row_geometry():
    """Row arithmetic the whole design leans on (spec.md section 5.2)."""
    assert decode.ROW_PP == 328
    assert decode.ROW_BITS == 3936
    assert decode.ROW_WORDS == 123
    assert decode.ROW_BITS % 32 == 0, "a row must be a whole number of 32-bit words"
    assert decode.ROW_BITS <= 4096, "a row must fit one LPSPI frame"
    assert 96 % decode.PP_BITS == 0, "3 words = 8 pixel periods exactly"


# --- payload codecs, round-tripped against the firmware's packing ----------------------
@pytest.fixture
def ramp():
    """An image covering the full 10-bit range, including values needing all 10 bits."""
    img = (np.arange(decode.HEIGHT * decode.WIDTH, dtype=np.uint16) % 1024)
    return img.reshape(decode.HEIGHT, decode.WIDTH)


def test_gray10_roundtrip_is_lossless(ramp):
    payload = fake.encode_gray10(ramp)
    assert len(payload) == decode.HEIGHT * (decode.WIDTH // 4 * 5)
    h = protocol.Header(width=decode.WIDTH, height=decode.HEIGHT,
                        format=protocol.FMT_GRAY10, payload_len=len(payload))
    out = decode.decode_payload(h, payload)
    assert out.dtype == np.uint16
    assert np.array_equal(out, ramp), "10-bit packing must preserve every bit"


def test_gray8_truncates_two_bits(ramp):
    payload = fake.encode_gray8(ramp)
    h = protocol.Header(width=decode.WIDTH, height=decode.HEIGHT,
                        format=protocol.FMT_GRAY8, payload_len=len(payload))
    out = decode.decode_payload(h, payload)
    assert np.array_equal(out, (ramp >> 2).astype(np.uint8))


def test_raw12_roundtrip(ramp):
    payload = fake.encode_raw12(ramp)
    h = protocol.Header(width=decode.WIDTH, height=decode.HEIGHT,
                        format=protocol.FMT_RAW12, payload_len=len(payload))
    raw = decode.decode_payload(h, payload)
    assert raw.shape == (decode.HEIGHT, decode.ROW_PP)
    assert np.all(raw[:, :decode.TRAINING_PP] == decode.WORD_TRAINING)
    assert np.array_equal(decode.raw12_to_image(raw), ramp)


def test_decode_payload_rejects_wrong_size():
    h = protocol.Header(width=decode.WIDTH, height=decode.HEIGHT,
                        format=protocol.FMT_GRAY8)
    with pytest.raises(ValueError):
        decode.decode_payload(h, b"\x00" * 100)
