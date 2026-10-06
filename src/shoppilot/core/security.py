"""Passwords and tokens (Step Q, first part; rate limits, lockout and role tests are added in Step S).

- Passwords are hashed with argon2. The database never holds a password, only the hash.
- A login gives a JWT: the user id, email and role, signed with SHOP_JWT_SECRET, valid for 30 minutes.
- The role in the token is signed, so the user cannot change it. The API and the tools take the role from here,
  never from the request body and never from model output.

    hashed = hash_password("a long password")
    verify_password("a long password", hashed)      # True
    token = create_token(user_id=1, email="a@b.com", role="manager")
    claims = decode_token(token)                    # {"sub": "1", "email": ..., "role": "manager", ...}
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from shoppilot.core.config import settings
from shoppilot.core.errors import AuthError, ConfigError

ROLES = ("viewer", "support", "manager", "owner", "admin")  # the same list as the CHECK constraint on users.role
TOKEN_MINUTES = 30  # short on purpose (blueprint 13.2)
ALGORITHM = "HS256"
DEV_SECRET = "dev-only-change-me"  # the default in config.py: fine on a laptop, never in prod

_hasher = PasswordHasher()


# ------------------------------------------------------------------ passwords
def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """True when the password matches. A wrong password and a broken hash both give False, never an exception."""
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):  # VerifyMismatchError is a VerificationError
        return False


# ------------------------------------------------------------------ tokens
def _secret() -> str:
    if settings.env == "prod" and settings.jwt_secret == DEV_SECRET:
        raise ConfigError("SHOP_JWT_SECRET is still the development default: set a long random value in prod")
    return settings.jwt_secret


def create_token(user_id: int | str, email: str, role: str, minutes: int = TOKEN_MINUTES) -> str:
    if role not in ROLES:
        raise ConfigError(f"unknown role: {role}")
    now = datetime.now(UTC)
    claims = {"sub": str(user_id), "email": email, "role": role, "iat": now, "exp": now + timedelta(minutes=minutes)}
    return jwt.encode(claims, _secret(), algorithm=ALGORITHM)


def decode_token(token: str) -> dict[str, Any]:
    """The claims of a valid token. An expired, forged or malformed token raises AuthError (HTTP 401)."""
    try:
        claims = jwt.decode(token, _secret(), algorithms=[ALGORITHM], options={"require": ["sub", "role", "exp"]})
    except jwt.ExpiredSignatureError:
        raise AuthError("the token has expired: please log in again") from None
    except jwt.InvalidTokenError:
        raise AuthError("the token is not valid") from None
    if claims.get("role") not in ROLES:
        raise AuthError("the token is not valid")
    return claims
