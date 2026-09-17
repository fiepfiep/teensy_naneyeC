"""Frame sources, especially the two ways `--source replay` used to fail.

It resolved build/golden against the working directory, so it only ran from the repository
root; and after doc/ became untracked a fresh clone had no reference capture at all, so the
documented "works with no hardware" path was broken.
"""

import numpy as np
import pytest

from naneye import decode, fake, protocol
from naneye.sources import ReplaySource


def test_golden_dirs_includes_a_repo_root_candidate():
    """A relative path must also be tried against the repo root, not just the cwd."""
    dirs = [str(d) for d in fake.golden_dirs()]
    assert any(d == "build\\golden" or d == "build/golden" for d in dirs)
    root = str(fake.repo_root())
    assert any(d.startswith(root) for d in dirs), dirs


def test_repo_root_looks_like_the_checkout():
    root = fake.repo_root()
    assert (root / "pyproject.toml").is_file()
    assert (root / "host" / "naneye").is_dir()


def test_absolute_golden_dir_is_used_as_given(tmp_path):
    dirs = fake.golden_dirs(tmp_path)
    assert dirs == [tmp_path]


def test_missing_capture_raises_with_actionable_message(tmp_path):
    with pytest.raises(FileNotFoundError) as e:
        fake.load_golden_frames(tmp_path / "nope")
    msg = str(e.value)
    assert "doc/digital.csv" in msg
    assert "decode_golden.py" in msg
    assert "looked in" in msg


def test_replay_falls_back_to_synthetic_frames(tmp_path):
    """A fresh clone has no reference capture; replay must still start."""
    src = ReplaySource(fps=0, golden_dir=tmp_path / "absent")
    assert "SYNTHETIC" in src.name
    frames = src.frames()
    header, img = next(frames)
    assert img.shape == (decode.HEIGHT, decode.WIDTH)


def test_replay_can_refuse_to_fabricate(tmp_path):
    """Analysis code must be able to demand real data."""
    with pytest.raises(FileNotFoundError):
        ReplaySource(golden_dir=tmp_path / "absent", allow_synthetic=False)


def test_synthetic_frames_are_valid_10_bit_and_not_identical():
    frames = fake.synthetic_frames(count=4)
    assert len(frames) == 4
    for f in frames:
        assert f.dtype == np.uint16
        assert f.min() >= 0 and f.max() <= 1023
        assert f.shape == (decode.HEIGHT, decode.WIDTH)
    # A moving marker plus per-frame noise: consecutive frames must differ.
    assert not np.array_equal(frames[0], frames[1])
    # The pattern must exercise both ends of the range.
    assert frames[0].max() >= 1020 and frames[0].min() == 0


def test_replay_round_trips_through_the_wire_format():
    """What replay yields must equal what the encoder was given, via a real packet."""
    frames = fake.synthetic_frames(count=2)
    src = ReplaySource(frames=frames, fps=0, fmt=protocol.FMT_GRAY10)
    header, img = next(src.frames())
    assert header.format == protocol.FMT_GRAY10
    assert np.array_equal(img, frames[0]), "10-bit replay must be lossless"
