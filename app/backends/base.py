from abc import ABC, abstractmethod

from app.errors import DawError, DawUnavailable

from ..models import MediaItem, Meter, Project, Track, Transport


class DawBackend(ABC):
    """Everything the web app needs from a DAW. Implement this to support another one."""

    name: str



    @abstractmethod
    def tracks(self) -> list[Track]:
        """All tracks of the current project."""

    @abstractmethod
    def transport(self) -> Transport:
        """Current transport status and position."""

    @abstractmethod
    def hw_outputs(self) -> list[str]:
        """Selectable hardware outputs: stereo pairs ('Out 1/2') then mono ('Out 1')."""

    @abstractmethod
    def set_record_targets(self, indices: list[int]) -> None:
        """Arm exactly these tracks for recording and disarm all others."""

    @abstractmethod
    def set_volume_db(self, index: int, db: float) -> None: ...

    @abstractmethod
    def set_mute(self, index: int, on: bool) -> None: ...

    @abstractmethod
    def set_solo(self, index: int, on: bool) -> None: ...

    @abstractmethod
    def set_master_send(self, index: int, on: bool) -> None: ...

    @abstractmethod
    def set_hw_output(self, index: int, label: str, on: bool) -> None:
        """Add / remove a hardware output send ('Out 3/4') on a track."""

    @abstractmethod
    def record(self) -> None: ...

    @abstractmethod
    def play(self) -> None: ...

    @abstractmethod
    def pause(self) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...

    @abstractmethod
    def seek(self, seconds: float) -> None: ...

    @abstractmethod
    def projects(self) -> list[Project]: ...
    
    @abstractmethod
    def open_project(self, path: str) -> None: ...
    
    @abstractmethod
    def select_project(self, index: int) -> None: ...

    def input_options(self) -> list[tuple[int, str, str]]: ...

    def recent_projects(self) -> list[dict]:
        return []

    def project_templates(self) -> list[dict]:
        return []

    def new_from_template(self, path: str) -> None:
        raise DawUnavailable("Demó módban nincs sablon.")
    
    @abstractmethod
    def set_input(self, index: int, value: int) -> None: ...

    def set_take_starred(self, track_indices: list[int], start: float, take_index: int, starred: bool) -> None: ...

    def delete_take(self, track_indices: list[int], start: float, take_index: int) -> None:
        raise DawError("A take törlése ebben a backendben nem támogatott.")

    def meters(self) -> list[Meter]:
        """Current peak level of every track. Optional: no meters = empty list."""
        return []

    def set_take_audible(self, track_indices: list[int], start: float, audible: bool) -> None:
        """Mute / unmute the items that a recording pass created (items starting at `start`
        on these tracks). Optional: the default does nothing."""

    def media_items(self) -> list[MediaItem]:
        """Audio items of the project, for the waveform. Optional: no items = flat waveform."""
        return []