"""A REAPER saját (Windows) párbeszédablakai: kiolvasás és gombnyomás. Csak Windowson működik."""
import os

from .errors import BadInput

if os.name == "nt":
    import ctypes
    from ctypes import wintypes as wt

    _u32, _k32 = ctypes.windll.user32, ctypes.windll.kernel32
    _ENUM = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    _BM_CLICK, _GWL_STYLE = 0x00F5, -16
    _WM_COMMAND = 0x0111
    _u32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    _u32.GetDlgCtrlID.argtypes = [wt.HWND]
    _pid_cache: dict[int, bool] = {}

    def _text(hwnd) -> str:
        buf = ctypes.create_unicode_buffer(_u32.GetWindowTextLengthW(hwnd) + 1)
        _u32.GetWindowTextW(hwnd, buf, len(buf))
        return buf.value

    def _cls(hwnd) -> str:
        buf = ctypes.create_unicode_buffer(64)
        _u32.GetClassNameW(hwnd, buf, 64)
        return buf.value

    def _is_reaper(hwnd) -> bool:
        pid = wt.DWORD()
        _u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value not in _pid_cache:
            handle = _k32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
            name = ""
            if handle:
                buf, size = ctypes.create_unicode_buffer(512), wt.DWORD(512)
                if _k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                    name = os.path.basename(buf.value).lower()
                _k32.CloseHandle(handle)
            _pid_cache[pid.value] = name.startswith("reaper")
        return _pid_cache[pid.value]

    def _children(hwnd) -> list[int]:
        out: list[int] = []
        cb = _ENUM(lambda h, _: out.append(h) or True)
        _u32.EnumChildWindows(hwnd, cb, 0)
        return out

    def _read(hwnd) -> dict | None:
        texts, buttons = [], []
        for child in _children(hwnd):
            cls, text = _cls(child), _text(child).replace("&", "").strip()
            if cls == "Static" and text:
                texts.append(text)
            elif cls == "Button" and text and (_u32.GetWindowLongW(child, _GWL_STYLE) & 0xF) in (0, 1):
                buttons.append({"id": int(child), "label": text})
        if not buttons or len(buttons) > 5:  # nem üzenetablak (pl. beállítások)
            return None
        return {"hwnd": int(hwnd), "title": _text(hwnd), "text": "\n".join(texts), "buttons": buttons}

    def list_dialogs() -> list[dict]:
        found: list[dict] = []

        def visit(hwnd, _):
            if _u32.IsWindowVisible(hwnd) and _cls(hwnd) == "#32770" and _is_reaper(hwnd):
                if (info := _read(hwnd)):
                    found.append(info)
            return True

        _u32.EnumWindows(_ENUM(visit), 0)
        return found

    def click(hwnd: int, button: int) -> None:
        ok = any(d["hwnd"] == hwnd and any(b["id"] == button for b in d["buttons"]) for d in list_dialogs())
        if not ok:
            raise BadInput("Az ablak közben eltűnt.")
        ctrl = _u32.GetDlgCtrlID(button)
        _u32.PostMessageW(hwnd, _WM_COMMAND, ctrl, button)  # BN_CLICKED (0) a felső szóban
else:
    def list_dialogs() -> list[dict]:
        return []

    def click(hwnd: int, button: int) -> None:
        raise BadInput("Ez csak Windowson működik.")