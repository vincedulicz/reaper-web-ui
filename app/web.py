"""HTTP layer: validates input, calls the service, returns JSON."""
import math

from app import dialogs
from . import filesystem
import time
from . import auth

from flask import Blueprint, abort, current_app, jsonify, render_template, request, session

from .errors import BadInput
from .models import Transport
from .services import RecorderService
from .units import MAX_DB, MIN_DB

bp = Blueprint("web", __name__)
bp.record_once(lambda state: setattr(state.app, "secret_key", auth.secret_key()))

_PUBLIC = {"web.index", "web.login", "web.logout", "web.me"}


@bp.before_request
def require_login():
    if request.endpoint in _PUBLIC:
        return None
    if not auth.current_user():
        return jsonify(error="Bejelentkezés szükséges"), 401
    return None


@bp.get("/api/me")
def me():
    user = auth.current_user()
    return jsonify(user={"username": user["username"], "name": user["name"]} if user else None)


@bp.post("/api/login")
def login():
    user = auth.check(_body_value("username", str), _body_value("password", str))
    if not user:
        time.sleep(0.6)
        return jsonify(error="Hibás felhasználónév vagy jelszó"), 401
    session.permanent = True
    session["user"] = user["username"]
    return jsonify(username=user["username"], name=user["name"])


@bp.post("/api/logout")
def logout():
    session.clear()
    return jsonify(ok=True)


_TRANSPORT_ACTIONS = ("play", "pause", "stop")


def _service() -> RecorderService:
    return current_app.extensions["recorder"]


def _flag(value) -> bool:
    if not isinstance(value, bool):
        raise TypeError("not a boolean")
    return value


def _str_list(value) -> list[str]:
    if not isinstance(value, list):
        raise TypeError("not a list")
    return [str(v) for v in value]


def _int_list(value) -> list[int]:
    if not isinstance(value, list):
        raise TypeError("not a list")
    return [int(v) for v in value]


def _body_value(key: str, cast):
    payload = request.get_json(silent=True) or {}
    try:
        value = cast(payload[key])
    except (KeyError, TypeError, ValueError):
        raise BadInput(f"Hiányzó vagy hibás mező: {key}") from None
    if isinstance(value, float) and not math.isfinite(value):
        raise BadInput(f"Hibás mező: {key}")
    return value


def _transport_json(transport: Transport):
    return jsonify(transport.to_dict())


@bp.get("/")
def index():
    return render_template("index.html", min_db=MIN_DB, max_db=MAX_DB, backend=_service().backend_name)


@bp.get("/api/transport")
def get_transport():
    return _transport_json(_service().transport())


@bp.get("/api/tracks")
def get_tracks():
    return jsonify([track.to_dict() for track in _service().tracks()])


@bp.get("/api/hw-outputs")
def get_hw_outputs():
    return jsonify(_service().hw_outputs())


@bp.get("/api/takes")
def get_takes():
    return jsonify([take.to_dict() for take in _service().takes()])


@bp.post("/api/takes/select")
def select_takes():
    takes = _service().select_takes(_body_value("names", _str_list))
    return jsonify([take.to_dict() for take in takes])


@bp.post("/api/takes/star")
def star_take():
    takes = _service().set_take_starred(_body_value("name", str), _body_value("starred", _flag))
    return jsonify([take.to_dict() for take in takes])


@bp.post("/api/takes/rename")
def rename_take():
    name, takes = _service().rename_take(_body_value("name", str), _body_value("new_name", str))
    return jsonify(name=name, takes=[take.to_dict() for take in takes])


@bp.post("/api/takes/delete")
def delete_take():
    takes = _service().delete_take(_body_value("name", str))
    return jsonify([take.to_dict() for take in takes])


@bp.get("/api/meters")
def get_meters():
    return jsonify([meter.to_dict() for meter in _service().meters()])


@bp.get("/api/peaks")
def get_peaks():
    buckets = min(8000, max(50, request.args.get("buckets", 600, type=int)))
    return jsonify(_service().peaks(buckets))


@bp.post("/api/record")
def start_recording():
    return _transport_json(_service().start_recording(_body_value("tracks", _int_list)))


@bp.post("/api/transport/<action>")
def transport_action(action: str):
    if action not in _TRANSPORT_ACTIONS:
        abort(404)
    return _transport_json(getattr(_service(), action)())


@bp.post("/api/seek")
def seek():
    return _transport_json(_service().seek(_body_value("position", float)))


@bp.post("/api/tracks/<int:index>/volume")
def set_volume(index: int):
    return jsonify(volume_db=_service().set_volume_db(index, _body_value("db", float)))


@bp.post("/api/tracks/<int:index>/mute")
def set_mute(index: int):
    return jsonify(on=_service().set_mute(index, _body_value("on", _flag)))


@bp.post("/api/tracks/<int:index>/solo")
def set_solo(index: int):
    return jsonify(on=_service().set_solo(index, _body_value("on", _flag)))


@bp.post("/api/tracks/<int:index>/master")
def set_master_send(index: int):
    return jsonify(on=_service().set_master_send(index, _body_value("on", _flag)))


@bp.post("/api/tracks/<int:index>/hw-output")
def set_hw_output(index: int):
    label = _body_value("label", str)
    return jsonify(on=_service().set_hw_output(index, label, _body_value("on", _flag)))

@bp.post("/api/tracks/<int:index>/arm")
def set_arm(index: int):
    return jsonify(on=_service().set_arm(index, _body_value("on", _flag)))

@bp.get("/api/settings")
def get_settings():
    return jsonify(jump_after=_service().jump_after)


@bp.post("/api/settings")
def set_settings():
    return jsonify(jump_after=_service().set_jump_after(_body_value("jump_after", float)))



@bp.get("/api/filesystem/dirs")
def list_dirs():
    path = request.args.get("path") or None
    try:
        return jsonify(filesystem.list_dirs(path, request.args.get("ext") or None))
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 400


@bp.post("/api/project/save")
def save_project():
    path = _service().save_project_as(
        _body_value("directory", str),
        _body_value("name", str),
        _body_value("create_subdirectory", bool),
        _body_value("move_media", bool),
    )
    return jsonify({"path": path})


@bp.get("/api/projects")
def get_projects():
    return jsonify([p.to_dict() for p in _service().projects()])


@bp.post("/api/projects/open")
def open_project():
    return jsonify([p.to_dict() for p in _service().open_project(_body_value("path", str))])


@bp.post("/api/projects/select")
def select_project():
    return jsonify([p.to_dict() for p in _service().select_project(_body_value("index", int))])


@bp.get("/api/projects/recent")
def get_recent_projects():
    return jsonify(_service().recent_projects())


@bp.get("/api/projects/templates")
def get_project_templates():
    return jsonify(_service().project_templates())


@bp.post("/api/projects/new-from-template")
def new_from_template():
    return jsonify([p.to_dict() for p in _service().new_from_template(_body_value("path", str))])


@bp.post("/api/tracks/<int:index>/input")
def set_input(index: int):
    return jsonify(value=_service().set_input(index, _body_value("value", int)))


@bp.get("/api/input-options")
def get_input_options():
    return jsonify([{"value": v, "label": l, "group": g} for v, l, g in _service().input_options()])


@bp.get("/api/dialogs")
def get_dialogs():
    return jsonify(dialogs.list_dialogs())


@bp.post("/api/dialogs/click")
def click_dialog():
    dialogs.click(_body_value("hwnd", int), _body_value("button", int))
    return jsonify(ok=True)


@bp.post("/api/reaper/launch")
def launch_reaper():
    payload = request.get_json(silent=True) or {}
    path = str(payload["path"]) if payload.get("path") else None
    _service().launch_reaper(path)
    return jsonify(ok=True)

