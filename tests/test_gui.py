"""The Qt GUI, headless: frame accounting, and a smoke test on replayed frames."""

import os
import time

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PyQt6.QtWidgets")

from naneye import gui, protocol  # noqa: E402
from naneye.sources import open_source  # noqa: E402


def header(counter, dropped=0):
    return protocol.Header(frame_counter=counter, frames_dropped=dropped)


def feed(reader, frames):
    img = np.zeros((4, 4), np.uint16)
    for counter, dropped in frames:
        reader._frame(header(counter, dropped), img)


class _NoSource:
    name = "none"


def test_gaps_the_device_counted_are_not_blamed_on_the_pc():
    r = gui.FrameReader(_NoSource())
    # frames 1..3, then the device drops 4 and 5 (and says so), then 6
    feed(r, [(1, 0), (2, 0), (3, 0), (6, 2)])
    assert r.dropped_on_device == 2 and r.lost_on_pc == 0


def test_gaps_the_device_did_not_count_are_lost_on_the_pc():
    r = gui.FrameReader(_NoSource())
    feed(r, [(1, 0), (2, 0), (4, 0), (5, 0)])      # frame 3 sent, never arrived
    assert r.lost_on_pc == 1 and r.dropped_on_device == 0


def test_reset_forgets_history():
    r = gui.FrameReader(_NoSource())
    feed(r, [(1, 0), (5, 0)])
    r.reset_counts()
    feed(r, [(100, 7), (101, 7)])                    # e.g. after a restart
    assert r.lost_on_pc == 0 and r.dropped_on_device == 0


def test_take_latest_hands_each_frame_over_once():
    r = gui.FrameReader(_NoSource())
    feed(r, [(1, 0), (2, 0)])
    got = r.take_latest()
    assert got[0].frame_counter == 2
    assert r.take_latest() is None


def test_rate():
    now = time.monotonic()
    assert gui.rate([now - 1.0 + i * 0.1 for i in range(11)]) == pytest.approx(10.0)
    assert gui.rate([]) == 0.0


def test_window_receives_and_displays_replayed_frames():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    source = open_source("replay", depth=10, fmt=protocol.FMT_GRAY10, fps=50,
                         allow_synthetic=True)
    win = gui.MainWindow(source, 49500000)
    win.show()
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and len(win.painted) < 10:
        app.processEvents()
        time.sleep(0.01)
    win._update_stats()
    assert win.reader.received >= 10
    assert len(win.painted) >= 10
    assert win.table.item(0, 0).text() == "0.rows_in_reset"
    assert not win.btn_start.isEnabled()             # no device: acquisition disabled
    win.close()
