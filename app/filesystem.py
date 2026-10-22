"""Server oldali mappabongeszes a Save Project mappavalasztojahoz.

Csak konyvtarak listazasara szolgal - nem altalanos fajlrendszer API, kizarolag
a projekt-mentes mappavalasztojahoz. A bongeszes a backendet futtato gepen
tortenik, direktben os/pathlib-bel, nem a reapy-n keresztul.
"""
import os
import string
import logging
from pathlib import Path

log = logging.getLogger(__name__)


def _drives() -> list[dict]:
    """Windows meghajtok (C:\\, E:\\, ...) - path nelkuli hivasnal adjuk vissza."""
    drives = []
    if os.name == "nt":
        import ctypes
        bitmask = ctypes.windll.kernel32.GetLogicalDrives()
        for i, letter in enumerate(string.ascii_uppercase):
            if bitmask & (1 << i):
                drives.append({"name": f"{letter}:\\", "path": f"{letter}:\\"})
    else:
        drives.append({"name": "/", "path": "/"})
    return drives


def list_dirs(path: str | None, ext: str | None = None) -> dict:
    if not path:
        return {"path": "", "parent": None, "dirs": _drives(), "files": []}

    p = Path(path)
    if not p.exists() or not p.is_dir():
        raise FileNotFoundError(f"Nincs ilyen mappa: {path}")

    dirs, files = [], []
    try:
        for entry in sorted(p.iterdir(), key=lambda e: e.name.lower()):
            try:
                if entry.name.startswith("."):
                    continue
                if entry.is_dir():
                    dirs.append({"name": entry.name, "path": str(entry)})
                elif ext and entry.suffix.lower() == ext.lower():
                    files.append({"name": entry.name, "path": str(entry)})
            except OSError:
                continue
    except PermissionError:
        log.warning("Nincs jogosultsag a mappahoz: %s", path)

    parent = str(p.parent) if p.parent != p else None
    return {"path": str(p), "parent": parent, "dirs": dirs, "files": files}