"""Regression tests against the reference capture (doc/digital.csv).

These pin down every measured claim in spec.md section 3. If a change to the decoder breaks
one of these, the decoder is wrong: this data came off a working link.

Needs the cached bit stream, so run this once first:
    python tools/decode_golden.py
"""

import numpy as np
import pytest

from naneye import decode


def test_clock_period_is_31_25_mhz(root):
    import os

    times_path = os.path.join(root, "build", "golden", "bit_times.npy")
    if not os.path.exists(times_path):
        pytest.skip("run tools/decode_golden.py first")
    times = np.load(times_path)
    period = np.median(np.diff(times))
    assert period == pytest.approx(32e-9, rel=1e-6), "reference link ran at 31.25 MHz"


def test_row_pitch_is_exactly_one_row(golden_bits):
    """Every row boundary is 3936 bits apart, with no exceptions anywhere in 0.38 s."""
    row_starts, run_lens, _ = decode.find_row_starts(golden_bits)
    frame_first = np.where(run_lens > 1000)[0]
    assert len(frame_first) == 8, "expected 8 frame boundaries in the capture"

    for i0 in frame_first[:-1]:
        rows = row_starts[i0:i0 + decode.HEIGHT]
        assert np.all(np.diff(rows) == decode.ROW_BITS)


def test_frames_decode_with_every_start_and_stop_bit_valid(golden_bits):
    """716,800 pixel words, all correctly framed. This is what proves the decode."""
    frames = list(decode.decode_frames_from_bits(golden_bits))
    assert len(frames) == 7

    total_pixels = 0
    for _, img, stats in frames:
        assert stats["bad_start_bits"] == 0
        assert stats["bad_stop_bits"] == 0
        assert img.shape == (decode.HEIGHT, decode.WIDTH)
        assert img.max() <= 1023
        total_pixels += img.size
    assert total_pixels == 7 * 320 * 320


def test_first_frame_is_saturated_and_must_be_discarded(golden_bits):
    """Confirms the datasheet warning; the firmware discards this frame (spec.md 3.5)."""
    frames = list(decode.decode_frames_from_bits(golden_bits, max_frames=3))
    first = frames[0][1]
    rest = frames[1][1]
    assert first.mean() > 1000, "first frame after idle-off should be saturated"
    assert 200 < rest.mean() < 400, "later frames should be normally exposed"


def test_temporal_noise_matches_the_measured_figure(golden_bits):
    frames = [img for _, img, _ in decode.decode_frames_from_bits(golden_bits)][1:]
    stack = np.stack([f.astype(np.float32) for f in frames])
    noise = stack.std(axis=0).mean()
    assert noise == pytest.approx(2.74, abs=0.15), "expected ~2.74 DN of temporal noise"


def test_sensor_is_mono(golden_bits):
    """A colour part would show four distinct Bayer sub-lattice means (spec.md 3.4)."""
    frames = list(decode.decode_frames_from_bits(golden_bits, max_frames=2))
    img = frames[1][1][100:300, 100:300].astype(np.float32)
    means = [img[0::2, 0::2].mean(), img[0::2, 1::2].mean(),
             img[1::2, 0::2].mean(), img[1::2, 1::2].mean()]
    spread = (max(means) - min(means)) / np.mean(means)
    assert spread < 0.02, f"sub-lattice spread {spread:.3%} suggests a colour filter array"


def test_reference_register_values(golden_bits):
    """The configuration the working host wrote, per spec.md section 3.2."""
    writes = decode.decode_register_writes(golden_bits[:200])
    assert len(writes) == 2
    (_, addr0, data0), (_, addr1, data1) = writes
    assert (addr0, data0) == (0, 0x009F), "CONFIG_0 at power-up"
    assert (addr1, data1) == (1, 0x009F), "CONFIG_1 at power-up, idle still enabled"

    # Field decode: SEIM selected, idle on, recommended analog settings.
    assert (data1 >> 8) & 1 == 0, "output_mode must be SEIM"
    assert (data1 >> 1) & 1 == 1, "idle_mode on for the first write"
    assert (data0 >> 6) & 3 == 2, "vrst_pix 2.6 V (recommended)"
    assert (data0 >> 4) & 3 == 1, "ramp_gain unity"


def test_streaming_config_releases_idle(golden_bits):
    """Each frame's INTERFACE MODE window carries the idle-off configuration."""
    row_starts, run_lens, run_starts = decode.find_row_starts(golden_bits)
    frame_first = np.where(run_lens > 1000)[0]
    sync = int(run_starts[frame_first[1]])
    lo = max(0, sync - decode.INTERFACE_PP * decode.PP_BITS)
    writes = decode.decode_register_writes(golden_bits[lo:sync], lo)
    cfg = {addr: data for _, addr, data in writes}
    assert cfg[0] == 0x009F
    assert cfg[1] == 0x0065
    assert (cfg[1] >> 1) & 1 == 0, "idle_mode cleared while streaming"
    assert (cfg[1] >> 6) & 3 == 1 and cfg[1] & 1 == 1, "mclk default + high_speed -> 31.1 MHz"


def test_host_clock_gaps_corrupt_three_pixels(golden_bits, root):
    """The defect our design must avoid (spec.md section 3.3).

    The reference host's DMA restarts leave 50-200 us clock gaps mid-row; each corrupts
    exactly three consecutive pixels while its neighbours stay normal.
    """
    import os

    times_path = os.path.join(root, "build", "golden", "bit_times.npy")
    if not os.path.exists(times_path):
        pytest.skip("run tools/decode_golden.py first")
    times = np.load(times_path)
    gap_idx, gap_len = decode.find_clock_gaps(times, threshold_s=20e-6)

    row_starts, run_lens, _ = decode.find_row_starts(golden_bits)
    frame_first = np.where(run_lens > 1000)[0]
    base = int(row_starts[frame_first[1]])
    frames = list(decode.decode_frames_from_bits(golden_bits, max_frames=2))
    img = frames[1][1]

    # Gaps that land inside frame 1's readout.
    inside = [int(i) for i in gap_idx
              if base <= i < base + decode.HEIGHT * decode.ROW_BITS]
    assert inside, "expected at least one mid-frame clock gap in the reference"

    checked = 0
    for bit in inside:
        off = bit - base
        row, px = off // decode.ROW_BITS, (off % decode.ROW_BITS) // decode.PP_BITS
        if not (8 <= px < decode.WIDTH - 8):
            continue

        # The pixels already in the ADC pipeline survive, so the damage starts a couple of
        # pixels after the gap. Measured signature: one saturated pixel, then two far below
        # the local level.
        window = img[row, px:px + 5].astype(float)
        hit = int(np.argmax(window))
        assert window[hit] >= 1000, f"row {row}: no saturated pixel after the gap"

        local = float(np.median(img[row, px - 8:px - 1]))
        after = img[row, px + hit + 1:px + hit + 3].astype(float)
        assert np.all(after < local * 0.5), (
            f"row {row}: expected two suppressed pixels after the saturated one, got {after}")

        before = img[row, px - 8:px - 1].astype(float)
        assert abs(before.mean() - local) < 30, "pixels before the gap should be undisturbed"
        checked += 1

    assert checked >= 2, "expected at least two mid-row gaps to verify"
