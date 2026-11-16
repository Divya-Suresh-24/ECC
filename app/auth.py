"""Simple username/password accounts with two roles (student, faculty).

Kept deliberately small and isolated so it can be swapped for Microsoft Entra ID (ISU NetID)
single sign-on later: the rest of the app only uses `current_user` / `require_faculty`.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from collections import defaultdict

from fastapi import Depends, HTTPException, Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from . import db
from .config import settings

COOKIE_NAME = "ecc_session"
_serializer = URLSafeTimedSerializer(settings.secret_key, salt="ecc-session")

# ------------------------------------------------------------------ passwords (scrypt, stdlib only)

_N, _R, _P = 2 ** 14, 8, 1


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        _, n, r, p, salt_b64, dk_b64 = stored.split("$")
        dk = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt_b64), n=int(n), r=int(r), p=int(p),
                            dklen=len(base64.b64decode(dk_b64)))
        return hmac.compare_digest(dk, base64.b64decode(dk_b64))
    except Exception:
        return False


def password_problem(password: str) -> str | None:
    if len(password) < 10:
        return "Password must be at least 10 characters."
    has_letter = any(c.isalpha() for c in password)
    has_other = any(not c.isalpha() for c in password)
    if len(password) < 16 and not (has_letter and has_other):
        return "Use letters plus numbers/symbols, or a passphrase of 16+ characters."
    return None


def temp_password() -> str:
    words = secrets.token_urlsafe(9).replace("-", "x").replace("_", "y")
    return f"ecc-{words}"


# ------------------------------------------------------------------ login throttling

_failures: dict[str, list[float]] = defaultdict(list)


def throttle_check(key: str) -> None:
    window = time.time() - 15 * 60
    _failures[key] = [t for t in _failures[key] if t > window]
    if len(_failures[key]) >= 8:
        raise HTTPException(429, "Too many failed sign-in attempts. Wait 15 minutes and try again.")


def throttle_fail(key: str) -> None:
    _failures[key].append(time.time())


def throttle_clear(key: str) -> None:
    _failures.pop(key, None)


# ------------------------------------------------------------------ sessions

def make_session_cookie(user: dict) -> str:
    return _serializer.dumps({"uid": user["id"], "sv": user["session_version"]})


def set_session(response, user: dict) -> None:
    response.set_cookie(
        COOKIE_NAME, make_session_cookie(user),
        max_age=settings.session_hours * 3600, httponly=True, samesite="lax",
        secure=settings.cookie_secure, path=(settings.root_path or "/"),
    )


def clear_session(response) -> None:
    response.delete_cookie(COOKIE_NAME, path=(settings.root_path or "/"))


def user_from_request(request: Request) -> dict | None:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    try:
        data = _serializer.loads(token, max_age=settings.session_hours * 3600)
    except (BadSignature, SignatureExpired):
        return None
    user = db.row("SELECT * FROM users WHERE id=? AND active=1", (data.get("uid"),))
    if not user or user["session_version"] != data.get("sv"):
        return None
    return user


def current_user(request: Request) -> dict:
    user = user_from_request(request)
    if not user:
        raise HTTPException(401, "Please sign in.")
    return user


def require_faculty(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "faculty":
        raise HTTPException(403, "Faculty access required.")
    return user


def public_user(user: dict) -> dict:
    return {k: user[k] for k in ("id", "username", "display_name", "role", "must_change_pw")}


# ------------------------------------------------------------------ user management

def create_user(username: str, display_name: str, role: str, password: str | None = None,
                email: str | None = None, must_change: bool = True) -> tuple[dict, str]:
    username = username.strip().lower()
    if not username or not all(c.isalnum() or c in "._-@" for c in username):
        raise ValueError("Usernames may contain letters, numbers, dots, dashes, underscores and @.")
    if role not in ("student", "faculty"):
        raise ValueError("Role must be student or faculty.")
    if db.row("SELECT id FROM users WHERE username=?", (username,)):
        raise ValueError(f"User '{username}' already exists.")
    pw = password or temp_password()
    db.get_conn().execute(
        """INSERT INTO users (username, display_name, email, role, pw_hash, must_change_pw, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (username, display_name.strip() or username, email, role, hash_password(pw), 1 if must_change else 0, db.now()),
    )
    return db.row("SELECT * FROM users WHERE username=?", (username,)), pw


def bootstrap_admin() -> None:
    if db.row("SELECT id FROM users LIMIT 1"):
        return
    if settings.initial_admin_username and settings.initial_admin_password:
        create_user(settings.initial_admin_username, "Faculty admin", "faculty",
                    settings.initial_admin_password, must_change=False)
        print(f"[ecc] Created initial faculty account '{settings.initial_admin_username}'.")
    else:
        print("[ecc] No users yet. Create one with:  python -m app.cli create-user <username> --faculty")
