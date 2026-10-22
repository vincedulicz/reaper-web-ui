"""Plain data objects shared by the backends, the service and the web layer."""
from dataclasses import asdict, dataclass
from enum import Enum


class Status(str, Enum):
    STOPPED = "stopped"
    PLAYING = "playing"
    PAUSED = "paused"
    RECORDING = "recording"


@dataclass(frozen=True)
class Track:
    index: int  # 0-based
    name: str
    armed: bool
    muted: bool
    solo: bool
    volume_db: float
    input: str  # e.g. "In 1", "In 1/2", "MIDI"
    master_send: bool  # sends to master / parent
    hw_outputs: tuple[str, ...]  # hardware output sends, e.g. ("Out 3/4",)
    input_value: int = -1  # nyers I_RECINPUT érték

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Transport:
    status: Status
    position: float  # seconds
    length: float  # seconds, project length without the take being recorded

    def to_dict(self) -> dict:
        return {
            "status": self.status.value,
            "position": round(self.position, 3),
            "length": round(self.length, 3),
        }


@dataclass(frozen=True)
class Piece:
    """Egy item-darab. Ha REAPER szétvágja a take-et, egy take több darabból áll."""

    start: float
    take_index: int
    track_indices: tuple[int, ...]


@dataclass(frozen=True)
class Take:
    name: str
    start: float
    end: float
    take_index: int = 0  # melyik rétegzett take az itemben — ez kell a set_active_take-hez
    tracks: tuple[str, ...] = ()
    track_indices: tuple[int, ...] = ()
    audible: bool = True
    starred: bool = False
    pieces: tuple[Piece, ...] = ()

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "tracks": list(self.tracks),
            "audible": self.audible,
            "starred": self.starred,
        }


@dataclass(frozen=True)
class Meter:
    """Current peak level of one track, in dB per channel (left, right)."""

    index: int
    levels_db: tuple[float, float]

    def to_dict(self) -> dict:
        return {"index": self.index, "levels": list(self.levels_db)}


@dataclass(frozen=True)
class MediaItem:
    path: str
    position: float
    length: float
    offset: float
    rate: float
    track_index: int = -1
    muted: bool = False
    take_name: str = ""
    take_index: int = 0  # 0-based pozíció az item take-stackjében (REAPER saját számozása)
    active: bool = True  # ez-e az item jelenleg aktív (= hallható) take-je
    starred: bool = False  # zöld színű take = csillagozott

@dataclass(frozen=True)
class Project:
    index: int
    name: str
    path: str  # teljes .rpp út, "" = még nem mentett
    current: bool

    def to_dict(self) -> dict:
        return asdict(self)