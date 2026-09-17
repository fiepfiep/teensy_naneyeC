import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "host"))

GOLDEN_BITS = os.path.join(ROOT, "build", "golden", "bits.npy")
GOLDEN_VECTOR = os.path.join(ROOT, "firmware", "src", "golden_vector.h")


@pytest.fixture(scope="session")
def root():
    return ROOT


@pytest.fixture(scope="session")
def golden_bits():
    """Bit stream sampled from doc/digital.csv, cached by tools/decode_golden.py."""
    import numpy as np

    if not os.path.exists(GOLDEN_BITS):
        pytest.skip("run tools/decode_golden.py first to cache the reference bit stream")
    return np.load(GOLDEN_BITS)


@pytest.fixture(scope="session")
def golden_vector():
    """The row and expected pixels embedded in the firmware, parsed back out.

    Reading the generated header keeps the host decoder honest against the exact data the
    on-device SELFTEST checks itself against.
    """
    import re

    if not os.path.exists(GOLDEN_VECTOR):
        pytest.skip("run tools/make_golden_vector.py first")
    text = open(GOLDEN_VECTOR).read()

    def array(name, pattern):
        m = re.search(name + r"\[\]\s*=\s*\{(.*?)\};", text, re.S)
        assert m, f"{name} not found in {GOLDEN_VECTOR}"
        return [int(v, 0) for v in re.findall(pattern, m.group(1))]

    return {
        "words": array("ROW_WORDS_DATA", r"0x[0-9A-Fa-f]{8}"),
        "pixels": array("EXPECTED_PIXELS", r"\d+"),
    }
