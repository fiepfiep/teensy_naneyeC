"""tools/link_quality.py's analysis, on synthetic bit streams with a known answer."""

import importlib.util
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("link_quality",
                                               ROOT / "tools" / "link_quality.py")
lq = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lq)


def rows_of_words(n_rows=20, seed=1):
    """(n_rows, 328) words: 8 x 0x555 training, then 320 valid pixel words."""
    rng = np.random.default_rng(seed)
    pixels = rng.integers(0, 1024, size=(n_rows, 320))
    words = (1 << 11) | (pixels << 1)          # start 1, 10 data bits, stop 0
    training = np.full((n_rows, 8), 0x555)
    return np.hstack([training, words]).astype(np.uint16)


def test_words_to_bits_is_msb_first_and_continuous():
    bits = lq.words_to_bits(np.array([[0x800, 0x001]], dtype=np.uint16))
    assert bits.tolist() == [1] + [0] * 11 + [0] * 11 + [1]


def test_perfect_stream_has_no_bad_words():
    st = lq.classify(lq.words_to_bits(rows_of_words()))
    assert st["bad"] == 0
    assert st["pixel"] == 20 * 320 and st["training"] == 20 * 8


def test_alignment_is_found_whatever_the_offset():
    bits = lq.words_to_bits(rows_of_words())
    for shift in (1, 5, 11):
        st = lq.classify(bits[shift:])
        assert st["bad"] <= 2          # at most the partial words at the ends
        assert st["offset"] == (12 - shift) % 12


def test_broken_framing_is_counted():
    words = rows_of_words()
    words[3, 100] &= 0x7FF             # knock out a start bit
    words[7, 200] |= 0x001             # set a stop bit
    st = lq.classify(lq.words_to_bits(words))
    assert st["bad"] == 2


def test_a_data_bit_error_is_invisible_to_framing():
    # The documented limitation: SEIM framing cannot see errors in the ten data bits.
    words = rows_of_words()
    words[5, 50] ^= 0x010
    assert lq.classify(lq.words_to_bits(words))["bad"] == 0
