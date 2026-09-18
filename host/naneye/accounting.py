"""Frame-loss accounting shared by the GUI and the recorder.

Every frame header carries a running frame counter and the device's running count of frames
it dropped itself (because the host had not yet taken the previous one). A jump in the
counter is therefore one of two things, with different causes and remedies:

- dropped by device: the host did not keep up; the header's frames_dropped went up by the
  same amount;
- lost on PC: the frame left the device but never arrived intact (a packet rejected by its
  CRC, or bytes lost on the way).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FrameAccounting:
    frames: int = 0
    dropped_on_device: int = 0
    lost_on_pc: int = 0
    rows_failed: int = 0
    pixels_concealed: int = 0
    _last_counter: int | None = field(default=None, repr=False)
    _last_dropped: int | None = field(default=None, repr=False)

    def add(self, header) -> None:
        if self._last_counter is not None:
            gap = max(0, header.frame_counter - self._last_counter - 1)
            by_device = max(0, header.frames_dropped - self._last_dropped)
            self.dropped_on_device += by_device
            self.lost_on_pc += max(0, gap - by_device)
        self._last_counter = header.frame_counter
        self._last_dropped = header.frames_dropped
        self.frames += 1
        self.rows_failed += header.rows_failed
        self.pixels_concealed += header.pixels_concealed

    def reset(self) -> None:
        """Forget everything, e.g. after the device was restarted."""
        self.__init__()

    @property
    def missing(self) -> int:
        return self.dropped_on_device + self.lost_on_pc
