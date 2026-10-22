"""belépés: users.json + session cookie."""
import hmac
import json
import secrets
from pathlib import Path

from flask import session
from werkzeug.security import check_password_hash

USERS_FILE = Path(__file__).with_name("users.json")
SECRET_FILE = Path(__file__).with_name(".secret_key")

DEFAULT_USERS = {
    "users": [
        {"username": "rakuram", "name": "spongyabob", "pass": "kockanadrag"}
    ]
}


def secret_key() -> str:
    if not SECRET_FILE.exists():
        SECRET_FILE.write_text(secrets.token_hex(32))
    return SECRET_FILE.read_text().strip()


def load_users() -> list[dict]:
    if not USERS_FILE.exists():
        USERS_FILE.write_text(json.dumps(DEFAULT_USERS, indent=2, ensure_ascii=False), encoding="utf-8")
    return json.loads(USERS_FILE.read_text(encoding="utf-8")).get("users", [])


def _pass_ok(stored: str, given: str) -> bool:
    if stored.startswith(("scrypt:", "pbkdf2:")):
        return check_password_hash(stored, given)
    return hmac.compare_digest(stored.encode(), given.encode())


def check(username: str, password: str) -> dict | None:
    for user in load_users():
        if user["username"].lower() == username.strip().lower() and _pass_ok(str(user["pass"]), password):
            return user
    return None


def current_user() -> dict | None:
    name = session.get("user")
    return next((u for u in load_users() if u["username"] == name), None) if name else None