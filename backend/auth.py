from datetime import datetime, timedelta, timezone

import hashlib
import hmac
import os

import jwt

SECRET = os.environ.get("JWT_SECRET", "germination-protocol-dev-secret")
SALT = "germination-protocol-v1"


def _digest(password: str) -> str:
    return hashlib.sha256(f"{SALT}:{password}".encode()).hexdigest()


# role: writer 能出数、能判定霉粒；reader 只能核对。
# title 就是霉粒列表里记的判定人身份（主检 / 副检 / 核算员）。
USERS = {
    "admin": {"role": "writer", "digest": _digest("123456"), "title": "核算员"},
    "witness": {"role": "reader", "digest": _digest("wit123456"), "title": "见证员"},
    "primary": {"role": "writer", "digest": _digest("pri123456"), "title": "主检"},
    "deputy": {"role": "writer", "digest": _digest("dep123456"), "title": "副检"},
}


def authenticate(username: str, password: str):
    user = USERS.get(username)
    if not user:
        return None
    if not hmac.compare_digest(_digest(password), user["digest"]):
        return None
    return {"username": username, "role": user["role"], "title": user["title"]}


def issue_token(user: dict) -> str:
    exp = datetime.now(timezone.utc) + timedelta(hours=8)
    return jwt.encode(
        {"sub": user["username"], "role": user["role"], "exp": exp},
        SECRET,
        algorithm="HS256",
    )


def read_user(authorization: str | None):
    if not authorization or not authorization.startswith("Bearer "):
        return None
    try:
        payload = jwt.decode(authorization[7:].strip(), SECRET, algorithms=["HS256"])
    except jwt.InvalidTokenError:
        return None
    username = payload.get("sub")
    user = USERS.get(username)
    if not user:
        return None
    return {"username": username, "role": user["role"], "title": user["title"]}
