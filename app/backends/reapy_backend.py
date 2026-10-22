"""REAPER backend on top of python-reapy (the ReaScript API over a local socket).

See the README for the one-time REAPER setup. python-reapy is imported lazily, so the
web UI starts (and recovers) even when REAPER is not running yet.
"""
import functools
import re
import logging
import os
import shutil
import subprocess
import sys
import time
import warnings
from contextlib import contextmanager

from ..errors import DawError, DawUnavailable, TrackNotFound
from ..models import MediaItem, Meter, Project, Status, Track, Transport
from ..units import db_to_gain, gain_to_db, hw_output_dest, hw_output_label, hw_output_labels
from .base import DawBackend

log = logging.getLogger(__name__)

_ACTION_NEW_TAB = 40859  # "File: New project tab"
# Main_OnCommand ids from REAPER's "Transport" action list
_ACTION_PLAY, _ACTION_PAUSE, _ACTION_RECORD, _ACTION_STOP = 1007, 1008, 1013, 1016
# GetPlayState bit flags
_FLAG_PLAYING, _FLAG_PAUSED, _FLAG_RECORDING = 1, 2, 4
_HW_OUTPUTS = 1  # send category "hardware outputs"
_DEST_MASK = 1023 | 1024  # channel bits + mono flag of I_DSTCHAN
_SOLO_IN_PLACE = 2  # I_SOLO: 0 = off, 1 = solo, 2 = solo in place (REAPER's default solo button)
_PROJECT = 0  # 0 = current project
_TAKE_MATCH = 0.5  # an item belongs to a take when it starts within this many seconds of the take start
STAR_COLOR = (46, 204, 113)  # zöld
_ACTION_DELETE_ACTIVE_TAKE = 40129  # "Take: Delete active take from selected items"
_MIN_ITEM_LENGTH = 0.01  # ennél rövidebb item = üres
_DELETE_MATCH = 0.02  # törlésnél csak a (gyakorlatilag) ugyanott kezdődő itemek


def _status_from_flags(flags: int) -> Status:
    if flags & _FLAG_RECORDING:
        return Status.PAUSED if flags & _FLAG_PAUSED else Status.RECORDING
    if flags & _FLAG_PAUSED:
        return Status.PAUSED
    if flags & _FLAG_PLAYING:
        return Status.PLAYING
    return Status.STOPPED


def _input_label(rec_input: int) -> str:
    """Decode REAPER's I_RECINPUT bit field."""
    if rec_input < 0:
        return "—"
    if rec_input & 4096:
        dev, ch = (rec_input >> 5) & 63, rec_input & 31
        return f"MIDI {'mind' if dev == 63 else 'VKB' if dev == 62 else dev} · {f'{ch}. cs.' if ch else 'mind'}"
    if rec_input & 2048:
        return "Multi"
    first = rec_input & 1023
    if first >= 512:
        return f"ReaRoute {first - 511}"
    return f"In {first + 1}/{first + 2}" if rec_input & 1024 else f"In {first + 1}"

def _str_result(res) -> str:
    """A reapy az output puffert tuple-ben adja vissza; a kitöltött str kell."""
    res = (res,) if isinstance(res, str) else res
    return next((x for x in res if isinstance(x, str) and x and not x.startswith("(")), "")

def _list_projects(rpr) -> list[tuple[str, str]]:
    """[(ReaProject pointer, .rpp út)] az összes megnyitott tabról."""
    out = []
    for i in range(64):
        res = rpr.EnumProjects(i, "", 1024)
        res = (res,) if isinstance(res, str) else res
        if _is_null(res[0]):
            break
        path = next((x for x in res[1:] if isinstance(x, str) and x and not x.startswith("(")), "")
        out.append((res[0], path))
    return out


def _track_id(rpr, index: int):
    if not 0 <= index < rpr.CountTracks(_PROJECT):
        raise TrackNotFound(f"Nincs ilyen sáv: {index + 1}")
    return rpr.GetTrack(_PROJECT, index)


def _hw_send_dests(rpr, tid) -> list[int]:
    return [
        int(rpr.GetTrackSendInfo_Value(tid, _HW_OUTPUTS, send, "I_DSTCHAN"))
        for send in range(rpr.GetTrackNumSends(tid, _HW_OUTPUTS))
    ]


def _is_null(ptr) -> bool:
    return ptr is None or bool(re.search(r"0x0+$", str(ptr)))

def _source_path(rpr, source) -> str:
    """File name of a media source ('' for MIDI / generated sources)."""
    result = rpr.GetMediaSourceFileName(source, "", 1024)
    if isinstance(result, str):
        result = (result,)
    # reapy returns the call's arguments; the filled-in buffer is the str that isn't a pointer
    return next((x for x in result if isinstance(x, str) and x and not x.startswith("(")), "")

def _is_empty_take(rpr, take) -> bool:
    """Üres take: nincs forrása, vagy a forrás hossza 0."""
    if _is_null(take):
        return True
    source = rpr.GetMediaItemTake_Source(take)
    if _is_null(source):
        return True
    result = rpr.GetMediaSourceLength(source, False)
    length = result[0] if isinstance(result, (tuple, list)) else result
    return length <= 0


def _delete_take_from_item(rpr, item, take) -> None:
    """Egyetlen take törlése egy többtake-es itemből (REAPER saját műveletével)."""
    rpr.SelectAllMediaItems(_PROJECT, False)
    rpr.SetMediaItemSelected(item, True)
    rpr.SetActiveTake(take)
    rpr.Main_OnCommand(_ACTION_DELETE_ACTIVE_TAKE, 0)

def _purge_empty_takes(rpr, tid, start: float, whole_items_only: bool = False, tolerance: float = _TAKE_MATCH) -> int:
    """Kitörli az üres take-eket és az üres itemeket a `start`-nál kezdődő itemekből.

    whole_items_only: csak a teljesen üres itemeket törli, egyes take-eket nem
    (így a take-indexek nem csúsznak el).
    tolerance: pozíció-egyezés tűrése; törlésnél szorosabbra kell venni (_DELETE_MATCH),
    hogy ne söpörjön be más, csak közelben lévő take-eket.
    """
    items = [rpr.GetTrackMediaItem(tid, i) for i in range(rpr.CountTrackMediaItems(tid))]
    items = [it for it in items if abs(rpr.GetMediaItemInfo_Value(it, "D_POSITION") - start) <= tolerance]
    removed = 0
    for item in items:
        count = rpr.CountTakes(item)
        empty = [k for k in range(count) if _is_empty_take(rpr, rpr.GetTake(item, k))]
        too_short = rpr.GetMediaItemInfo_Value(item, "D_LENGTH") < _MIN_ITEM_LENGTH
        if count == 0 or too_short or len(empty) == count:
            rpr.DeleteTrackMediaItem(tid, item)
            removed += 1
            continue
        if whole_items_only:
            continue
        for k in reversed(empty):
            _delete_take_from_item(rpr, item, rpr.GetTake(item, k))
            removed += 1
    return removed

def _read_track(rpr, index: int) -> Track:
    tid = rpr.GetTrack(_PROJECT, index)
    rec_input = int(rpr.GetMediaTrackInfo_Value(tid, "I_RECINPUT"))
    name = rpr.GetSetMediaTrackInfo_String(tid, "P_NAME", "", False)[3]
    return Track(
        index=index,
        name=name or f"Track {index + 1}",
        armed=bool(rpr.GetMediaTrackInfo_Value(tid, "I_RECARM")),
        muted=bool(rpr.GetMediaTrackInfo_Value(tid, "B_MUTE")),
        solo=rpr.GetMediaTrackInfo_Value(tid, "I_SOLO") > 0,
        volume_db=round(gain_to_db(rpr.GetMediaTrackInfo_Value(tid, "D_VOL")), 1),
        input=_input_label(rec_input),
        input_value=rec_input,
        master_send=bool(rpr.GetMediaTrackInfo_Value(tid, "B_MAINSEND")),  # master / parent folder
        hw_outputs=tuple(hw_output_label(dest) for dest in _hw_send_dests(rpr, tid))
        # rec_input = int(rpr.GetMediaTrackInfo_Value(tid, "I_RECINPUT"))
    )


def _guarded(method):
    """Any failure talking to REAPER becomes DawUnavailable (-> HTTP 503 with the reason).

    The UI polls several times a second, so an outage is logged with a traceback only once,
    then at DEBUG until the connection is back.
    """

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        try:
            result = method(self, *args, **kwargs)
        except DawError:
            raise
        except Exception as exc:
            self._stale = True  # következő híváskor újratöltjük a reapy-t
            if self._outage:
                log.debug("REAPER továbbra sem elérhető (%s): %s", method.__name__, exc)
            else:
                self._outage = True
                if isinstance(exc, OSError):  # pl. ConnectionResetError: a REAPER bezárult
                    log.warning("REAPER kapcsolat megszakadt (%s): %s", method.__name__, exc)
                else:
                    log.exception("REAPER hívás sikertelen (%s)", method.__name__)
            raise DawUnavailable(f"REAPER nem elérhető: {exc}") from exc
        if self._outage:
            self._outage = False
            log.info("REAPER kapcsolat helyreállt")
        return result

    return wrapper


def find_reaper_exe():
    env = os.environ.get("REAPER_EXE")
    if env and os.path.isfile(env):
        return env

    candidates = []

    if sys.platform == "win32":
        import winreg

        for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                try:
                    with winreg.OpenKey(root, r"SOFTWARE\REAPER", 0,
                                        winreg.KEY_READ | view) as k:
                        folder, _ = winreg.QueryValueEx(k, "")
                        candidates.append(os.path.join(folder, "reaper.exe"))
                except OSError:
                    pass
                try:
                    with winreg.OpenKey(
                        root,
                        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\REAPER",
                        0, winreg.KEY_READ | view,
                    ) as k:
                        uninstall, _ = winreg.QueryValueEx(k, "UninstallString")
                        folder = os.path.dirname(uninstall.strip('"'))
                        candidates.append(os.path.join(folder, "reaper.exe"))
                except OSError:
                    pass

        for var in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
            base = os.environ.get(var)
            if base:
                for name in ("REAPER (x64)", "REAPER"):
                    candidates.append(os.path.join(base, name, "reaper.exe"))

    which = shutil.which("reaper")
    if which:
        candidates.append(which)

    for path in candidates:
        if os.path.isfile(path):
            return path
    return None
    

class ReapyBackend(DawBackend):
    name = "reapy"
    _REAPER_EXE = find_reaper_exe() # OR r"C:\YourDirNameThing\Program Files\REAPER (x64)\reaper.exe"

    def __init__(self):
        self._reapy = None
        self._rpr = None
        self._outage = False
        self._waiting = False
        self._last_try = 0.0
        self._stale = True  # a kapcsolat valószínűleg elromlott: következő híváskor újracsatlakozunk

    def _load_reapy(self) -> None:
        """A reapy modulokat kidobjuk és újratöltjük: így a halott socket is eltűnik."""
        for name in [m for m in sys.modules if m == "reapy" or m.startswith("reapy.")]:
            del sys.modules[name]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # DisabledDistAPIWarning, ha a REAPER nem fut
            import reapy
            from reapy import reascript_api
        self._reapy, self._rpr = reapy, reascript_api

    def _try_connect(self) -> bool:
        """Újracsatlakozás (max. 1/mp). Csak akkor True, ha a REAPER tényleg válaszol."""
        if time.monotonic() - self._last_try < 1.0:
            return False
        self._last_try = time.monotonic()
        try:
            self._load_reapy()
            with self._reapy.inside_reaper():
                self._rpr.CountTracks(_PROJECT)  # próbahívás, pont úgy, ahogy a többi
            return True
        except Exception as exc:
            log.debug("Csatlakozás sikertelen: %r", exc)
            return False

    @contextmanager
    def _session(self):
        """Yield the ReaScript API; inside_reaper batches the calls into one fast round trip."""
        if self._rpr is None or self._stale:
            if not self._try_connect():
                if not self._waiting:
                    self._waiting = True
                    log.warning("REAPER nem elérhető, várakozás...")
                raise DawUnavailable("REAPER nem elérhető. Indítsd el, utána magától csatlakozik.")
            self._stale = False
            log.info("REAPER kapcsolat él")
        if self._waiting:
            self._waiting = False
        with self._reapy.inside_reaper():
            yield self._rpr

    @staticmethod
    def _is_starred(rpr, take) -> bool:
        color = int(rpr.GetMediaItemTakeInfo_Value(take, "I_CUSTOMCOLOR"))
        return color == (rpr.ColorToNative(*STAR_COLOR) | 0x1000000)

    @_guarded
    def projects(self) -> list[Project]:
        with self._session() as rpr:
            cur = rpr.EnumProjects(-1, "", 0)
            cur = cur if isinstance(cur, str) else cur[0]
            return [
                Project(i, os.path.splitext(os.path.basename(p))[0] if p else "Névtelen", p, ptr == cur)
                for i, (ptr, p) in enumerate(_list_projects(rpr))
            ]

    @_guarded
    def select_project(self, index: int) -> None:
        with self._session() as rpr:
            rpr.SelectProjectInstance(_list_projects(rpr)[index][0])

    def launch(self, path: str | None = None) -> None:
        """Elindítja a REAPER-t, opcionálisan egy .rpp-vel megnyitva. Nem vár rá."""
        if not os.path.isfile(self._REAPER_EXE):
            raise DawUnavailable(f"REAPER nem található itt: {self._REAPER_EXE}")
        args = [self._REAPER_EXE]
        if path:
            args.append(os.path.normpath(path))
        try:
            subprocess.Popen(args, close_fds=True)
        except OSError as exc:
            raise DawUnavailable(f"REAPER indítása sikertelen: {exc}") from exc
        log.info("REAPER indítása%s: %s", f" ({path})" if path else "", self._REAPER_EXE)

    # ---------- legutóbbi projektek + sablonok (fájlból, a REAPER-nek nem kell futnia) ----------

    def _reaper_ini(self) -> str:
        portable = os.path.join(os.path.dirname(self._REAPER_EXE), "reaper.ini")
        if os.path.isfile(portable):  # hordozható telepítés
            return portable
        return os.path.join(os.environ.get("APPDATA", ""), "REAPER", "reaper.ini")

    def recent_projects(self, limit: int = 12) -> list[dict]:
        try:
            with open(self._reaper_ini(), encoding="utf-8", errors="replace") as f:
                lines = f.read().splitlines()
        except OSError:
            return []
        found, in_recent = [], False
        for line in lines:
            line = line.strip()
            if line.startswith("["):
                in_recent = line.lower() == "[recent]"
            elif in_recent and (m := re.match(r"recent(\d+)=(.+)$", line, re.I)):
                found.append((int(m.group(1)), m.group(2)))
        out = []
        for _, path in sorted(found):  # recent01 = legfrissebb
            if path.lower().endswith(".rpp") and os.path.isfile(path):
                out.append({
                    "name": os.path.splitext(os.path.basename(path))[0],
                    "path": path,
                    "dir": os.path.dirname(path),
                })
        return out[:limit]

    def project_templates(self) -> list[dict]:
        root = os.path.join(os.path.dirname(self._reaper_ini()), "ProjectTemplates")
        out = []
        for folder, dirs, files in os.walk(root):
            dirs.sort(key=str.lower)
            for name in sorted(files, key=str.lower):
                if name.lower().endswith(".rpp"):
                    full = os.path.join(folder, name)
                    label = os.path.splitext(os.path.relpath(full, root))[0]
                    out.append({"name": label.replace(os.sep, " / "), "path": full})
        return out

    @_guarded
    def new_from_template(self, path: str) -> None:
        path = os.path.normpath(path)
        with self._session() as rpr:
            rpr.Main_OnCommand(_ACTION_NEW_TAB, 0)  # új tab, a mostanit nem bántjuk
            rpr.Main_openProject("template:" + path)  # sablonként nyílik: nem írható felül

    @_guarded
    def open_project(self, path: str) -> None:
        path = os.path.normpath(path)
        with self._session() as rpr:
            for ptr, p in _list_projects(rpr):  # már nyitva van: csak átváltunk
                if p and os.path.normcase(os.path.normpath(p)) == os.path.normcase(path):
                    rpr.SelectProjectInstance(ptr)
                    return
            rpr.Main_OnCommand(_ACTION_NEW_TAB, 0)  # új tab, nem írjuk felül a mostanit
            rpr.Main_openProject("noprompt:" + path)

    @_guarded
    def save_project_as(self, directory: str, name: str, create_subdirectory: bool, move_media: bool) -> str:
        directory = os.path.normpath(directory)
        if create_subdirectory:
            directory = os.path.join(directory, name)
        os.makedirs(directory, exist_ok=True)
        project_path = os.path.join(directory, f"{name}.rpp")

        with self._session() as rpr:
            rpr.Main_SaveProjectEx(_PROJECT, project_path, 0)  # 0 = teljes projekt

        if move_media:
            self._move_media(directory)
            with self._session() as rpr:
                rpr.Main_SaveProjectEx(_PROJECT, project_path, 0)
        log.info("Projekt elmentve: %s (media mozgatva=%s)", project_path, move_media)
        return project_path

    def _move_media(self, directory: str) -> None:
        """Médiafájlok a projekt mappába másolva, a take-ek forrása átállítva."""
        media_dir = os.path.join(directory, "Media")
        os.makedirs(media_dir, exist_ok=True)
        with self._session() as rpr:
            takes = []
            for t in range(rpr.CountTracks(_PROJECT)):
                tid = rpr.GetTrack(_PROJECT, t)
                for i in range(rpr.CountTrackMediaItems(tid)):
                    item = rpr.GetTrackMediaItem(tid, i)
                    for k in range(rpr.CountTakes(item)):
                        take = rpr.GetTake(item, k)
                        if _is_null(take):
                            continue
                        path = _source_path(rpr, rpr.GetMediaItemTake_Source(take))
                        if path:
                            takes.append((take, path))
        copied: dict[str, str] = {}
        for _, path in takes:
            if path in copied or not os.path.isfile(path):
                continue
            if os.path.normcase(os.path.dirname(os.path.abspath(path))) == os.path.normcase(os.path.abspath(media_dir)):
                copied[path] = path
                continue
            dest, n = os.path.join(media_dir, os.path.basename(path)), 1
            while os.path.exists(dest):  # névütközés
                stem, ext = os.path.splitext(os.path.basename(path))
                dest, n = os.path.join(media_dir, f"{stem}_{n}{ext}"), n + 1
            shutil.copy2(path, dest)
            copied[path] = dest
        with self._session() as rpr:
            for take, path in takes:
                if path in copied and copied[path] != path:
                    rpr.SetMediaItemTake_Source(take, rpr.PCM_Source_CreateFromFile(copied[path]))
            rpr.UpdateArrange()

    @_guarded
    def set_take_starred(self, track_indices: list[int], start: float, take_index: int, starred: bool) -> None:
        with self._session() as rpr:
            native = (rpr.ColorToNative(*STAR_COLOR) | 0x1000000) if starred else 0
            for index in track_indices:
                tid = _track_id(rpr, index)
                for i in range(rpr.CountTrackMediaItems(tid)):
                    item = rpr.GetTrackMediaItem(tid, i)
                    if abs(rpr.GetMediaItemInfo_Value(item, "D_POSITION") - start) <= _TAKE_MATCH:
                        take = rpr.GetTake(item, take_index)
                        if _is_null(take):
                            continue
                        rpr.SetMediaItemTakeInfo_Value(take, "I_CUSTOMCOLOR", native)
                        if rpr.CountTakes(item) == 1:  # egy takes itemnél az item színe felülírná a take színét
                            rpr.SetMediaItemInfo_Value(item, "I_CUSTOMCOLOR", native)
            rpr.UpdateArrange()
        log.debug("REAPER: take @%.2f s csillag=%s", start, starred)

    @_guarded
    def tracks(self) -> list[Track]:
        with self._session() as rpr:
            return [_read_track(rpr, i) for i in range(rpr.CountTracks(_PROJECT))]

    @_guarded
    def transport(self) -> Transport:
        with self._session() as rpr:
            flags = rpr.GetPlayState()
            position = rpr.GetPlayPosition() if flags else rpr.GetCursorPosition()
            return Transport(_status_from_flags(flags), position, rpr.GetProjectLength(_PROJECT))

    @_guarded
    def hw_outputs(self) -> list[str]:
        with self._session() as rpr:
            channels = int(rpr.GetNumAudioOutputs())
        log.debug("REAPER hardver kimeneti csatornák: %d", channels)
        return hw_output_labels(channels)

    @_guarded
    def set_record_targets(self, indices: list[int]) -> None:
        with self._session() as rpr:
            for index in indices:
                _track_id(rpr, index)  # validate all before touching anything
            wanted = set(indices)
            for i in range(rpr.CountTracks(_PROJECT)):
                rpr.SetMediaTrackInfo_Value(rpr.GetTrack(_PROJECT, i), "I_RECARM", 1 if i in wanted else 0)
        log.debug("REAPER: felvételre élesített sávok: %s", sorted(wanted))

    @_guarded
    def set_volume_db(self, index: int, db: float) -> None:
        with self._session() as rpr:
            rpr.SetMediaTrackInfo_Value(_track_id(rpr, index), "D_VOL", db_to_gain(db))

    @_guarded
    def set_mute(self, index: int, on: bool) -> None:
        with self._session() as rpr:
            rpr.SetMediaTrackInfo_Value(_track_id(rpr, index), "B_MUTE", 1 if on else 0)
        log.debug("REAPER: sáv %d mute=%s", index + 1, on)

    @_guarded
    def set_solo(self, index: int, on: bool) -> None:
        with self._session() as rpr:
            rpr.SetMediaTrackInfo_Value(_track_id(rpr, index), "I_SOLO", _SOLO_IN_PLACE if on else 0)
        log.debug("REAPER: sáv %d solo=%s", index + 1, on)

    @_guarded
    def set_master_send(self, index: int, on: bool) -> None:
        with self._session() as rpr:
            rpr.SetMediaTrackInfo_Value(_track_id(rpr, index), "B_MAINSEND", 1 if on else 0)
        log.debug("REAPER: sáv %d master send=%s", index + 1, on)

    @_guarded
    def input_options(self) -> list[tuple[int, str, str]]:
        """(érték, felirat, csoport). A csoport "" = a lista elején, csoport nélkül."""
        with self._session() as rpr:
            n = int(rpr.GetNumAudioInputs())
            names = [_str_result(rpr.GetInputChannelName(i)) for i in range(n)]
            midi = [(d, _str_result(rpr.GetMIDIInputName(d, "", 256))) for d in range(int(rpr.GetNumMIDIInputs()))]

        mono = lambda i: f"{i + 1}: {names[i]}" if names[i] else f"In {i + 1}"
        opts = [(-1, "— (nincs)", "")]
        opts += [(i, mono(i), "Mono") for i in range(n)]
        opts += [(1024 | i, f"{mono(i)}  +  {mono(i + 1)}", "Sztereó") for i in range(0, n - 1, 2)]

        devices = [(63, "Minden MIDI eszköz"), (62, "Virtuális MIDI billentyűzet")]
        devices += [(d, name) for d, name in midi if name]
        for dev, name in devices:
            group = f"MIDI · {name}"
            base = 4096 | (dev << 5)
            opts.append((base, "Minden csatorna", group))
            opts += [(base | ch, f"{ch}. csatorna", group) for ch in range(1, 17)]
        return opts

    @_guarded
    def set_input(self, index: int, value: int) -> None:
        with self._session() as rpr:
            rpr.SetMediaTrackInfo_Value(_track_id(rpr, index), "I_RECINPUT", value)
        log.debug("REAPER: sáv %d bemenet=%d", index + 1, value)

    @_guarded
    def set_hw_output(self, index: int, label: str, on: bool) -> None:
        dest = hw_output_dest(label)
        with self._session() as rpr:
            tid = _track_id(rpr, index)
            matches = [i for i, d in enumerate(_hw_send_dests(rpr, tid)) if (d & _DEST_MASK) == dest]
            if on and not matches:
                # destination 0 (NULL) = hardware output send instead of a track send
                send = rpr.CreateTrackSend(tid, 0)
                rpr.SetTrackSendInfo_Value(tid, _HW_OUTPUTS, send, "I_DSTCHAN", dest)
                log.debug("REAPER: sáv %d hardver kimenet hozzáadva: %s (send #%s)", index + 1, label, send)
            elif not on:
                for send in reversed(matches):  # remove from the end so indexes stay valid
                    rpr.RemoveTrackSend(tid, _HW_OUTPUTS, send)
                log.debug("REAPER: sáv %d hardver kimenet eltávolítva: %s (%d db)", index + 1, label, len(matches))

    @_guarded
    def record(self) -> None:
        self._run_action(_ACTION_RECORD)

    @_guarded
    def play(self) -> None:
        self._run_action(_ACTION_PLAY)

    @_guarded
    def pause(self) -> None:
        self._run_action(_ACTION_PAUSE)

    @_guarded
    def stop(self) -> None:
        self._run_action(_ACTION_STOP)

    @_guarded
    def seek(self, seconds: float) -> None:
        with self._session() as rpr:
            rpr.SetEditCurPos(seconds, True, True)  # moveview, seekplay

    @_guarded
    def meters(self) -> list[Meter]:
        with self._session() as rpr:
            return [
                Meter(
                    index=i,
                    levels_db=tuple(
                        round(gain_to_db(rpr.Track_GetPeakInfo(tid, channel)), 1) for channel in (0, 1)
                    ),
                )
                for i in range(rpr.CountTracks(_PROJECT))
                for tid in (rpr.GetTrack(_PROJECT, i),)
            ]

    @_guarded
    def delete_take(self, track_indices: list[int], start: float, take_index: int) -> None:
        removed_items = 0
        removed_takes = 0
        with self._session() as rpr:
            targets = []
            for index in track_indices:
                tid = _track_id(rpr, index)
                for i in range(rpr.CountTrackMediaItems(tid)):
                    item = rpr.GetTrackMediaItem(tid, i)
                    if abs(rpr.GetMediaItemInfo_Value(item, "D_POSITION") - start) <= _DELETE_MATCH:
                        take = rpr.GetTake(item, take_index)
                        if not _is_null(take):
                            targets.append((tid, item, take))

            for tid, item, take in targets:
                log.info(
                    "Törlés célpont: pos=%.3f hossz=%.3f take-ek=%d aktív=%d név=%s",
                    rpr.GetMediaItemInfo_Value(item, "D_POSITION"),
                    rpr.GetMediaItemInfo_Value(item, "D_LENGTH"),
                    rpr.CountTakes(item),
                    int(rpr.GetMediaItemInfo_Value(item, "I_CURTAKE")),
                    rpr.GetSetMediaItemTakeInfo_String(take, "P_NAME", "", False)[3],
                )
            if not targets:
                return

            rpr.Undo_BeginBlock()
            rpr.SelectAllMediaItems(_PROJECT, False)
            for tid, item, take in targets:
                # Ha ez az egyetlen take az itemen, az egész itemet töröljük —
                # így sosem marad üres take/item, és nem csúszik az indexelés.
                if rpr.CountTakes(item) <= 1:
                    rpr.DeleteTrackMediaItem(tid, item)
                    removed_items += 1
                    continue
                # Egyesével, külön-külön jelöljük ki és töröljük az aktív take-et,
                # hogy a batch-akció véletlenül se nyúljon más itemhez.
                rpr.SetMediaItemSelected(item, True)
                rpr.SetActiveTake(take)
                rpr.Main_OnCommand(_ACTION_DELETE_ACTIVE_TAKE, 0)
                rpr.SetMediaItemSelected(item, False)
                removed_takes += 1
            rpr.SelectAllMediaItems(_PROJECT, False)
            rpr.Undo_EndBlock("Take törlése (Remote)", -1)
            rpr.UpdateArrange()
        log.debug(
            "REAPER: take törölve @%.2f s (#%d, %d egész item törölve, %d take törölve itemből)",
            start, take_index, removed_items, removed_takes,
        )

    
    @_guarded
    def rename_take(self, track_indices: list[int], start: float, take_index: int, name: str) -> None:
        with self._session() as rpr:
            for index in track_indices:
                tid = _track_id(rpr, index)
                for i in range(rpr.CountTrackMediaItems(tid)):
                    item = rpr.GetTrackMediaItem(tid, i)
                    if abs(rpr.GetMediaItemInfo_Value(item, "D_POSITION") - start) <= _TAKE_MATCH:
                        take = rpr.GetTake(item, take_index)
                        if not _is_null(take):
                            rpr.GetSetMediaItemTakeInfo_String(take, "P_NAME", name, True)
            rpr.UpdateArrange()

    @_guarded
    def set_take_audible(self, track_indices: list[int], start: float, audible: bool) -> None:
        changed = 0
        with self._session() as rpr:
            for index in track_indices:
                tid = _track_id(rpr, index)
                for i in range(rpr.CountTrackMediaItems(tid)):
                    item = rpr.GetTrackMediaItem(tid, i)
                    if abs(rpr.GetMediaItemInfo_Value(item, "D_POSITION") - start) <= _TAKE_MATCH:
                        rpr.SetMediaItemInfo_Value(item, "B_MUTE", 0 if audible else 1)
                        changed += 1
            rpr.UpdateArrange()
        log.debug("REAPER: take @%.2f s %s (%d item)", start, "hallható" if audible else "némítva", changed)

    @_guarded
    def set_active_take(self, track_indices: list[int], start: float, take_index: int) -> None:
        changed = 0
        with self._session() as rpr:
            for index in track_indices:
                tid = _track_id(rpr, index)
                for i in range(rpr.CountTrackMediaItems(tid)):
                    item = rpr.GetTrackMediaItem(tid, i)
                    if abs(rpr.GetMediaItemInfo_Value(item, "D_POSITION") - start) <= _TAKE_MATCH:
                        take = rpr.GetTake(item, take_index)
                        if not _is_null(take):
                            rpr.SetActiveTake(take)
                            changed += 1
            rpr.UpdateArrange()
        log.debug("REAPER: #%d take aktív @%.2f s (%d item)", take_index, start, changed)

    @_guarded
    def media_items(self, include_muted: bool = False, all_takes: bool = False) -> list[MediaItem]:
        items = []
        with self._session() as rpr:
            for t in range(rpr.CountTracks(_PROJECT)):
                tid = rpr.GetTrack(_PROJECT, t)
                for i in range(rpr.CountTrackMediaItems(tid)):
                    item = rpr.GetTrackMediaItem(tid, i)
                    muted = bool(rpr.GetMediaItemInfo_Value(item, "B_MUTE"))
                    if muted and not include_muted:
                        continue
                    position = rpr.GetMediaItemInfo_Value(item, "D_POSITION")
                    length = rpr.GetMediaItemInfo_Value(item, "D_LENGTH")
                    active_idx = int(rpr.GetMediaItemInfo_Value(item, "I_CURTAKE"))
                    for k in (range(rpr.CountTakes(item)) if all_takes else [active_idx]):
                        take = rpr.GetTake(item, k)
                        if _is_null(take):
                            continue
                        path = _source_path(rpr, rpr.GetMediaItemTake_Source(take))
                        if not path:
                            continue
                        take_name = rpr.GetSetMediaItemTakeInfo_String(take, "P_NAME", "", False)[3]
                        items.append(
                            MediaItem(
                                path=path,
                                position=position,
                                length=length,
                                offset=rpr.GetMediaItemTakeInfo_Value(take, "D_STARTOFFS"),
                                rate=rpr.GetMediaItemTakeInfo_Value(take, "D_PLAYRATE") or 1.0,
                                track_index=t,
                                muted=muted,
                                take_name=take_name or "",
                                take_index=k,
                                active=(k == active_idx),
                                starred=self._is_starred(rpr, take),
                            )
                        )
        return items

    def _run_action(self, command_id: int) -> None:
        log.debug("REAPER: Main_OnCommand(%d)", command_id)
        with self._session() as rpr:
            rpr.Main_OnCommand(command_id, 0)