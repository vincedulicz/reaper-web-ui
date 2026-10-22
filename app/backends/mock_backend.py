"""In-memory fake DAW so the UI can be tried (and tested) without REAPER."""
import logging
import time
from collections.abc import Callable
from dataclasses import replace

from ..errors import BadInput, TrackNotFound
from ..models import Status, Track, Transport
from ..units import hw_output_dest, hw_output_labels
from .base import DawBackend

log = logging.getLogger(__name__)

_RUNNING = (Status.PLAYING, Status.RECORDING)
_CHANNELS = 8


class MockBackend(DawBackend):
    name = "mock"

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._status = Status.STOPPED
        self._anchor_pos = 0.0  # position at the moment of the last status change / seek
        self._anchor_time = clock()
        self._length = 0.0
        self._tracks = [
            Track(0, "Ének", False, False, False, 0.0, "In 1", True, ()),
            Track(1, "Gitár", False, False, False, -6.0, "In 2", True, ("Out 3/4",)),
            Track(2, "Basszus", False, False, False, -3.0, "In 3", True, ()),
            Track(3, "Dob", False, False, False, -9.5, "In 4/5", False, ("Out 1/2",)),
        ]
        log.info("Mock backend kész, %d sáv", len(self._tracks))

    def _position(self) -> float:
        if self._status in _RUNNING:
            return self._anchor_pos + (self._clock() - self._anchor_time)
        return self._anchor_pos

    def _move_to(self, status: Status, position: float | None = None) -> None:
        pos = self._position() if position is None else position
        log.debug("mock: %s -> %s, pozíció %.3f", self._status.value, status.value, pos)
        self._anchor_pos, self._anchor_time, self._status = pos, self._clock(), status

    def _tick(self) -> None:
        """Playback reaching the end of the material stops, like 'stop at end of project'."""
        if self._status is Status.PLAYING and self._position() >= self._length:
            self._move_to(Status.STOPPED, self._length)

    def _check(self, index: int) -> None:
        if not 0 <= index < len(self._tracks):
            raise TrackNotFound(f"Nincs ilyen sáv: {index + 1}")

    def _update(self, index: int, **changes) -> None:
        self._check(index)
        self._tracks[index] = replace(self._tracks[index], **changes)

    def tracks(self) -> list[Track]:
        return list(self._tracks)

    def transport(self) -> Transport:
        self._tick()
        return Transport(self._status, self._position(), self._length)

    def hw_outputs(self) -> list[str]:
        return hw_output_labels(_CHANNELS)

    def set_record_targets(self, indices: list[int]) -> None:
        for index in indices:
            self._check(index)
        wanted = set(indices)
        self._tracks = [replace(t, armed=t.index in wanted) for t in self._tracks]
        log.debug("mock: armed tracks %s", sorted(wanted))

    def set_volume_db(self, index: int, db: float) -> None:
        self._update(index, volume_db=db)

    def set_mute(self, index: int, on: bool) -> None:
        self._update(index, muted=on)

    def set_solo(self, index: int, on: bool) -> None:
        self._update(index, solo=on)

    def set_master_send(self, index: int, on: bool) -> None:
        self._update(index, master_send=on)

    def set_hw_output(self, index: int, label: str, on: bool) -> None:
        self._check(index)
        hw_output_dest(label)  # validates the format
        if label not in self.hw_outputs():
            raise BadInput(f"Nincs ilyen kimenet: {label}")
        current = self._tracks[index].hw_outputs
        updated = tuple(o for o in current if o != label) + ((label,) if on else ())
        self._update(index, hw_outputs=updated)

    def record(self) -> None:
        self._tick()
        if self._status is not Status.RECORDING:
            self._move_to(Status.RECORDING)

    def play(self) -> None:
        self._tick()
        if self._status in (Status.STOPPED, Status.PAUSED):
            self._move_to(Status.PLAYING)

    def pause(self) -> None:
        self._tick()
        if self._status is Status.PLAYING:
            self._move_to(Status.PAUSED)

    def stop(self) -> None:
        self._tick()
        if self._status is Status.RECORDING:
            self._length = max(self._length, self._position())
        self._move_to(Status.STOPPED)

    def seek(self, seconds: float) -> None:
        self._move_to(self._status, max(0.0, seconds))
