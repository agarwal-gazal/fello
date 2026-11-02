"""Passwords (scrypt), signed session cookie (HMAC), CSRF tokens. Standard library only."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time

N, R, P = 2 ** 14, 8, 1
SESSION_SECONDS = 8 * 3600
_SECRET = (os.environ.get("SECRET_KEY") or secrets.token_hex(32)).encode()     # no SECRET_KEY: sessions end when the process restarts


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(password.encode(), salt=salt, n=N, r=R, p=P, dklen=32)
    return f"scrypt${N}${R}${P}${salt.hex()}${h.hex()}"


_DUMMY = hash_password("not-a-real-password")


def verify_password(password: str, stored: str | None) -> bool:
    """Always does the scrypt work, even for unknown users, so response time does not reveal which usernames exist."""
    stored = stored or _DUMMY
    try:
        _, n, r, p, salt, h = stored.split("$")
        got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(got.hex(), h) and stored is not _DUMMY
    except Exception:
        return False


def _sign(payload: str) -> str:
    return hmac.new(_SECRET, payload.encode(), hashlib.sha256).hexdigest()


def make_session(username: str, now: float | None = None) -> str:
    exp = int((now or time.time()) + SESSION_SECONDS)
    payload = base64.urlsafe_b64encode(f"{username}|{exp}".encode()).decode()
    return f"{payload}.{_sign(payload)}"


def read_session(cookie: str | None, now: float | None = None) -> str | None:
    if not cookie or "." not in cookie:
        return None
    payload, sig = cookie.rsplit(".", 1)
    if not hmac.compare_digest(sig, _sign(payload)):
        return None
    try:
        username, exp = base64.urlsafe_b64decode(payload.encode()).decode().rsplit("|", 1)
        return username if int(exp) > (now or time.time()) else None
    except Exception:
        return None


def csrf_token(username: str) -> str:
    return _sign("csrf|" + username)[:32]


def csrf_ok(username: str, token: str | None) -> bool:
    return bool(token) and hmac.compare_digest(token, csrf_token(username))
