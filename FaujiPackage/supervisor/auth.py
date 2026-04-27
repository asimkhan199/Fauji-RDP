"""Single-user password auth with JWT cookie."""
from __future__ import annotations
import json
import secrets
import time
from typing import Any

import bcrypt
import jwt
from .paths import AUTH_PATH

ALGO = "HS256"
COOKIE_NAME = "fauji_session"
TOKEN_TTL_SECONDS = 60 * 60 * 24 * 7  # 7 days


def _read() -> dict[str, Any]:
    if not AUTH_PATH.exists():
        return {}
    with AUTH_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def _write(d: dict[str, Any]) -> None:
    AUTH_PATH.parent.mkdir(parents=True, exist_ok=True)
    with AUTH_PATH.open("w", encoding="utf-8") as f:
        json.dump(d, f, indent=2)


def is_password_set() -> bool:
    d = _read()
    return bool(d.get("password_hash") and d.get("jwt_secret"))


def set_password(plain: str) -> None:
    if len(plain) < 6:
        raise ValueError("Password must be at least 6 characters.")
    h = bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    d = _read()
    d["password_hash"] = h
    if not d.get("jwt_secret"):
        d["jwt_secret"] = secrets.token_urlsafe(48)
    _write(d)


def verify_password(plain: str) -> bool:
    d = _read()
    h = d.get("password_hash")
    if not h:
        return False
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), h.encode("utf-8"))
    except Exception:
        return False


def issue_token() -> str:
    d = _read()
    secret = d.get("jwt_secret")
    if not secret:
        raise RuntimeError("Auth not initialized.")
    payload = {"iat": int(time.time()), "exp": int(time.time()) + TOKEN_TTL_SECONDS}
    return jwt.encode(payload, secret, algorithm=ALGO)


def verify_token(token: str | None) -> bool:
    if not token:
        return False
    d = _read()
    secret = d.get("jwt_secret")
    if not secret:
        return False
    try:
        jwt.decode(token, secret, algorithms=[ALGO])
        return True
    except jwt.PyJWTError:
        return False
