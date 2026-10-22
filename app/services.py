"""Application logic on top of a DawBackend (the web layer only talks to this)."""
import logging
import os
import threading
from collections.abc import Callable
from dataclasses import replace
import re
from .backends.base import DawBackend
from .errors import BadInput, InvalidState
from .models import Meter, Piece, Project, Status, Take, Track, Transport
from .units import MAX_DB, MIN_DB
from .waveform import compute_peaks, signature

log = logging.getLogger(__name__)

_MAX_TAKES = 100
_DEFAULT_JUMP_AFTER = 0.0
_MAX_JUMP_AFTER = 24 * 3600.0
_TAKE_GROUP_WINDOW = 0.5
_NUMBERED = re.compile(r"^(\d+)_")

def _on_off(on: bool) -> str:
    return "be" if on else "ki"


class RecorderService:
    def __init__(self, backend: DawBackend):
        self._daw = backend
        self._lock = threading.RLock()
        self._take_start: float | None = None
        self._take_tracks: tuple[str, ...] = ()
        self._take_indices: tuple[int, ...] = ()
        self._takes: list[Take] = []
        self._jump_after = _DEFAULT_JUMP_AFTER
        self._peaks_lock = threading.Lock()
        self._peaks_key = None
        self._peaks_val: list[float] = []
        log.info("Szolgáltatás kész, backend: %s", backend.name)

    def launch_reaper(self, path: str | None = None) -> None:
        if path:
            path = path.strip()
            if not path.lower().endswith(".rpp") or not os.path.isfile(path):
                raise BadInput(f"Nincs ilyen projektfájl: {path}")
        with self._lock:
            self._daw.launch(path)
        log.info("REAPER indítás kérve%s", f" ({path})" if path else "")

    def _sync_takes(self) -> None:
        rows = sorted(self._daw.media_items(include_muted=True, all_takes=True), key=lambda r: (r.position, r.take_index))
        rows = [r for r in rows if r.length >= 0.01]
        if not rows:
            self._takes = []
            return

        by_item: dict[tuple[int, float], list] = {}
        for row in rows:
            by_item.setdefault((row.track_index, round(row.position, 2)), []).append(row)
        for group in by_item.values():
            group.sort(key=lambda r: r.take_index)

        keys = sorted(by_item, key=lambda k: k[1])
        clusters: list[list[tuple[int, float]]] = []
        for key in keys:
            if clusters and key[1] - clusters[-1][-1][1] <= _TAKE_GROUP_WINDOW:
                clusters[-1].append(key)
            else:
                clusters.append([key])

        by_track = {t.index: t for t in self._daw.tracks()}
        raw_takes = []
        for cluster in clusters:
            depth = max(len(by_item[key]) for key in cluster)
            for depth_from_top in range(depth):
                members = []
                for key in cluster:
                    group = by_item[key]
                    local_idx = len(group) - 1 - depth_from_top
                    if local_idx >= 0:
                        members.append((key[0], group[local_idx]))
                if not members:
                    continue
                by_pos: dict[float, list] = {}
                for track_idx, row in members:
                    by_pos.setdefault(round(row.position, 2), []).append((track_idx, row))
                for members_at_pos in by_pos.values():
                    start = min(row.position for _, row in members_at_pos)
                    end = max(row.position + row.length for _, row in members_at_pos)
                    track_indices = tuple(sorted({tidx for tidx, _ in members_at_pos}))
                    raw_name = next((row.take_name for _, row in members_at_pos if row.take_name), "")
                    audible = all(row.active and not row.muted for _, row in members_at_pos)
                    starred = all(row.starred for _, row in members_at_pos)
                    # ugyanazon a pozíción lévő darabok egyazon felvételi pillanatból jönnek,
                    # tehát a valódi (item-en belüli) take_indexüknek egyeznie kell
                    take_index = members_at_pos[0][1].take_index
                    raw_takes.append((start, take_index, track_indices, end, raw_name, audible, starred))

        raw_takes.sort(key=lambda r: (r[0], r[1]))

        groups: dict[str, list] = {}
        for raw in raw_takes:
            start, stack_i, _, _, raw_name, _, _ = raw
            groups.setdefault(raw_name or f"@{start:.3f}/{stack_i}", []).append(raw)

        used = [int(m.group(1)) for key in groups if (m := _NUMBERED.match(key))]
        next_number = max(used, default=0) + 1

        takes: list[Take] = []
        for parts in groups.values():
            raw_name = parts[0][4]
            if _NUMBERED.match(raw_name):
                name = raw_name
            else:
                name = f"{next_number}_{raw_name or f'Take @ {parts[0][0]:.1f}s'}"
                next_number += 1
                for start, stack_i, track_indices, *_ in parts:
                    if track_indices:
                        self._daw.rename_take(list(track_indices), start, stack_i, name)
                log.info("Take elnevezve: %s", name)
            track_indices = tuple(sorted({i for part in parts for i in part[2]}))
            takes.append(
                Take(
                    name=name,
                    start=min(part[0] for part in parts),
                    end=max(part[3] for part in parts),
                    take_index=parts[0][1],
                    tracks=tuple(f"{i + 1} · {by_track[i].name}" for i in track_indices if i in by_track),
                    track_indices=track_indices,
                    audible=all(part[5] for part in parts),
                    starred=all(part[6] for part in parts),
                    pieces=tuple(Piece(part[0], part[1], part[2]) for part in parts),
                )
            )
        self._takes = takes

    def takes(self) -> list[Take]:
        with self._lock:
            self._sync_takes()
            return list(self._takes)

    def rename_take(self, name: str, new_name: str) -> tuple[str, list[Take]]:
        new_name = new_name.strip()
        if not new_name:
            raise BadInput("A take neve nem lehet üres.")
        with self._lock:
            self._sync_takes()
            take = next((t for t in self._takes if t.name == name), None)
            if take is None:
                raise BadInput(f"Ismeretlen take: {name}")
            if not _NUMBERED.match(new_name):
                new_name = f"{_NUMBERED.match(take.name).group(1)}_{new_name}"
            if new_name != take.name and any(t.name == new_name for t in self._takes):
                raise BadInput(f"Már van ilyen nevű take: {new_name}")
            for piece in take.pieces:
                if piece.track_indices:
                    self._daw.rename_take(list(piece.track_indices), piece.start, piece.take_index, new_name)
            self._sync_takes()
            log.info("Take átnevezve: %s -> %s", name, new_name)
            return new_name, list(self._takes)

    def delete_take(self, name: str) -> list[Take]:
        with self._lock:
            if self._daw.transport().status is Status.RECORDING:
                raise InvalidState("Felvétel közben nem lehet take-et törölni.")
            self._sync_takes()
            take = next((t for t in self._takes if t.name == name), None)
            if take is None:
                raise BadInput(f"Ismeretlen take: {name}")
            log.info("Törlés: %s, darabok: %s", name, take.pieces)
            for piece in take.pieces:
                if piece.track_indices:
                    self._daw.delete_take(list(piece.track_indices), piece.start, piece.take_index)
            self._sync_takes()
            log.info("Take törölve: %s", name)
            return list(self._takes)

    def save_project_as(self, directory: str, name: str, create_subdirectory: bool, move_media: bool) -> str:
        name = name.strip()
        if not name:
            raise BadInput("Adj nevet a projektnek.")
        if not directory:
            raise BadInput("Válassz mappát.")
        with self._lock:
            if self._daw.transport().status is Status.RECORDING:
                raise InvalidState("Felvétel közben nem lehet menteni.")
            path = self._daw.save_project_as(directory, name, create_subdirectory, move_media)
        log.info("Projekt mentve: %s", path)
        return path

    def projects(self) -> list[Project]:
        with self._lock:
            return self._daw.projects()

    def _not_recording(self) -> None:
        if self._daw.transport().status is Status.RECORDING:
            raise InvalidState("Felvétel közben nem lehet projektet váltani.")

    def open_project(self, path: str) -> list[Project]:
        path = path.strip()
        if not path.lower().endswith(".rpp") or not os.path.isfile(path):
            raise BadInput(f"Nincs ilyen projektfájl: {path}")
        with self._lock:
            self._not_recording()
            self._daw.open_project(path)
            self._take_start = None
            return self._daw.projects()

    def recent_projects(self) -> list[dict]:
        return self._daw.recent_projects()

    def project_templates(self) -> list[dict]:
        return self._daw.project_templates()

    def new_from_template(self, path: str) -> list[Project]:
        path = path.strip()
        if path not in {t["path"] for t in self._daw.project_templates()}:
            raise BadInput("Ismeretlen sablon.")
        with self._lock:
            self._not_recording()
            self._daw.new_from_template(path)
            self._take_start = None
            return self._daw.projects()

    def select_project(self, index: int) -> list[Project]:
        with self._lock:
            self._not_recording()
            if not 0 <= index < len(self._daw.projects()):
                raise BadInput(f"Nincs ilyen projekt: {index + 1}")
            self._daw.select_project(index)
            self._take_start = None
            return self._daw.projects()

    @staticmethod
    def _shares_item(piece: Piece, takes: list[Take]) -> bool:
        """Rétegzett take-eknél több take ugyanazon az itemen osztozik; azt nem szabad némítani."""
        return any(
            abs(other.start - piece.start) <= _TAKE_GROUP_WINDOW
            and set(other.track_indices) & set(piece.track_indices)
            for take in takes
            for other in take.pieces
        )

    def select_takes(self, names: list[str]) -> list[Take]:
        with self._lock:
            if self._daw.transport().status is Status.RECORDING:
                raise InvalidState("Felvétel közben nem lehet takeket választani.")
            self._sync_takes()
            wanted = set(names)
            unknown = wanted - {take.name for take in self._takes}
            if unknown:
                raise BadInput(f"Ismeretlen take: {', '.join(sorted(unknown))}")
            chosen = [take for take in self._takes if take.name in wanted]
            for take in chosen:
                for piece in take.pieces:
                    tracks = list(piece.track_indices)
                    self._daw.set_active_take(tracks, piece.start, piece.take_index)
                    self._daw.set_take_audible(tracks, piece.start, True)
            for take in self._takes:
                if take.name in wanted:
                    continue
                for piece in take.pieces:
                    if not self._shares_item(piece, chosen):
                        self._daw.set_take_audible(list(piece.track_indices), piece.start, False)
            self._sync_takes()
            log.info("Hallható takek: %s", ", ".join(sorted(wanted)) or "egyik sem")
            return list(self._takes)

    def set_take_starred(self, name: str, starred: bool) -> list[Take]:
        with self._lock:
            self._sync_takes()
            take = next((t for t in self._takes if t.name == name), None)
            if take is None:
                raise BadInput(f"Ismeretlen take: {name}")
            for piece in take.pieces:
                self._daw.set_take_starred(list(piece.track_indices), piece.start, piece.take_index, starred)
            self._sync_takes()
            log.info("Take %s: csillag %s", name, _on_off(starred))
            return list(self._takes)

    @property
    def backend_name(self) -> str:
        return self._daw.name

    # ---------- settings ----------

    @property
    def jump_after(self) -> float:
        with self._lock:
            return self._jump_after

    def set_jump_after(self, seconds: float) -> float:
        seconds = min(_MAX_JUMP_AFTER, max(0.0, seconds))
        with self._lock:
            self._jump_after = seconds
        log.info("Ugrás felvétel után: %.1f s", seconds)
        return seconds

    # ---------- reading ----------

    def tracks(self) -> list[Track]:
        with self._lock:
            return self._daw.tracks()

    def transport(self) -> Transport:
        with self._lock:
            return self._daw.transport()

    def hw_outputs(self) -> list[str]:
        with self._lock:
            return self._daw.hw_outputs()
        
    def meters(self) -> list[Meter]:
        with self._lock:
            return self._daw.meters()

    def peaks(self, buckets: int) -> dict:
        with self._lock:
            items = self._daw.media_items()
            length = self._daw.transport().length
        key = (buckets, round(length, 3), signature(items))
        with self._peaks_lock:
            if key != self._peaks_key:
                self._peaks_val = compute_peaks(items, length, buckets)
                self._peaks_key = key
            return {"length": round(length, 3), "peaks": self._peaks_val}

    # ---------- transport ----------

    def start_recording(self, indices: list[int]) -> Transport:
        with self._lock:
            before = self._daw.transport()
            if before.status is Status.RECORDING:
                log.warning("Felvétel indítása elutasítva: már folyik a felvétel")
                raise InvalidState("Már folyik a felvétel.")
            targets = sorted(set(indices))
            if not targets:
                raise BadInput("Válassz legalább egy sávot a felvételhez.")
            self._daw.set_record_targets(targets)  # also validates the indexes
            by_index = {t.index: t for t in self._daw.tracks()}
            self._take_indices = tuple(i for i in targets if i in by_index)
            self._take_tracks = tuple(f"{i + 1} · {by_index[i].name}" for i in self._take_indices)
            self._take_start = before.position
            log.info(
                "Felvétel indul: sávok=[%s], pozíció=%.2f s, állapot előtte=%s",
                ", ".join(self._take_tracks), before.position, before.status.value,
            )
            self._daw.record()
            after = self._daw.transport()
            log.debug("Felvétel indítás után: %s", after)
            return after

    def stop(self) -> Transport:
        with self._lock:
            before = self._daw.transport()
            was_recording = before.status is Status.RECORDING
            self._daw.stop()
            if was_recording and self._take_start is not None:
                end = max(before.position, self._take_start)
                log.info(
                    "Felvétel kész: %.2f–%.2f s (%.2f s), sávok=[%s]",
                    self._take_start, end, end - self._take_start, ", ".join(self._take_tracks),
                )
                target = end + self._jump_after
                self._daw.seek(target)
                log.info("Ugrás a felvétel után: %.2f s", target)
                self._take_start = None
            else:
                log.info("Stop (előtte: %s, pozíció %.2f s)", before.status.value, before.position)
            return self._daw.transport()

    def play(self) -> Transport:
        return self._unless_recording("lejátszás", self._daw.play, "Felvétel közben nem lehet lejátszani.")

    def pause(self) -> Transport:
        return self._unless_recording("szünet", self._daw.pause, "Felvétel közben nincs szünet, állítsd le.")

    def seek(self, seconds: float) -> Transport:
        seconds = max(0.0, seconds)
        return self._unless_recording(
            f"ugrás {seconds:.2f} s-ra", lambda: self._daw.seek(seconds), "Felvétel közben nem lehet tekerni."
        )

    # ---------- tracks ----------

    def set_arm(self, index: int, on: bool) -> bool:
        """Arm / disarm one track, leaving the other tracks as they are."""
        with self._lock:
            if self._daw.transport().status is Status.RECORDING:
                raise InvalidState("Felvétel közben nem lehet élesíteni.")
            armed = {t.index for t in self._daw.tracks() if t.armed}
            if on:
                armed.add(index)
            else:
                armed.discard(index)
            self._daw.set_record_targets(sorted(armed))
        log.info("Sáv %d: felvételre élesítve %s", index + 1, _on_off(on))
        return on

    def set_volume_db(self, index: int, db: float) -> float:
        db = min(MAX_DB, max(MIN_DB, db))
        with self._lock:
            self._daw.set_volume_db(index, db)
        log.debug("Sáv %d hangerő: %.1f dB", index + 1, db)
        return db

    def set_mute(self, index: int, on: bool) -> bool:
        with self._lock:
            self._daw.set_mute(index, on)
        log.info("Sáv %d: mute %s", index + 1, _on_off(on))
        return on

    def set_solo(self, index: int, on: bool) -> bool:
        with self._lock:
            self._daw.set_solo(index, on)
        log.info("Sáv %d: solo %s", index + 1, _on_off(on))
        return on

    def set_master_send(self, index: int, on: bool) -> bool:
        with self._lock:
            self._daw.set_master_send(index, on)
        log.info("Sáv %d: send master %s", index + 1, _on_off(on))
        return on

    def set_hw_output(self, index: int, label: str, on: bool) -> bool:
        with self._lock:
            self._daw.set_hw_output(index, label, on)
        log.info("Sáv %d: hardver kimenet %s %s", index + 1, label, _on_off(on))
        return on

    def input_options(self) -> list[tuple[int, str, str]]:
        with self._lock:
            return self._daw.input_options()

    def set_input(self, index: int, value: int) -> int:
        with self._lock:
            if self._daw.transport().status is Status.RECORDING:
                raise InvalidState("Felvétel közben nem lehet bemenetet váltani.")
            if value not in {v for v, *_ in self._daw.input_options()}:
                raise BadInput("Ismeretlen bemenet.")
            self._daw.set_input(index, value)
        log.info("Sáv %d: bemenet %d", index + 1, value)
        return value

    # ---------- internals ----------

    def _unless_recording(self, what: str, command: Callable[[], None], message: str) -> Transport:
        with self._lock:
            if self._daw.transport().status is Status.RECORDING:
                log.warning("Parancs elutasítva felvétel közben: %s", what)
                raise InvalidState(message)
            log.info("Parancs: %s", what)
            command()
            return self._daw.transport()