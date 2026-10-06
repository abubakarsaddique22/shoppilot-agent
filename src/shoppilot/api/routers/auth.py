"""Login (Step Q). Roles, rate limits and lockout are hardened in Step S.

POST /v1/auth/login   email + password -> JWT (30 minutes) with the user id, email and role inside
GET  /v1/auth/me      who am I, read from the token

A wrong password and an unknown email give the SAME answer and take about the same time (a dummy hash is checked when
the user does not exist), so the login form cannot be used to find out which emails have an account.
"""
from __future__ import annotations

from functools import cache

from fastapi import APIRouter
from sqlalchemy import select

from shoppilot.api.deps import ReaderUser, SessionFactoryDep, write_audit
from shoppilot.api.schemas import LoginIn, TokenOut, UserOut
from shoppilot.core.errors import AuthError
from shoppilot.core.logging import get_logger
from shoppilot.core.security import TOKEN_MINUTES, create_token, hash_password, verify_password
from shoppilot.db.models import UserRow

log = get_logger(__name__)
router = APIRouter(prefix="/v1/auth", tags=["auth"])


@cache
def _dummy_hash() -> str:
    return hash_password("not-a-real-password")


@router.post("/login")
def login(body: LoginIn, factory: SessionFactoryDep) -> TokenOut:
    email = body.email.strip().lower()  # scripts/create_user.py stores emails in lower case
    with factory() as session:
        user = session.scalar(select(UserRow).where(UserRow.email == email))

    password_ok = verify_password(body.password, user.password_hash if user else _dummy_hash())
    if user is None or not password_ok:
        log.warning("login failed")  # no email and no password in the log line
        write_audit(factory, "anonymous", "login_failed")
        raise AuthError("wrong email or password")

    write_audit(factory, str(user.id), "login_ok", role=user.role)
    return TokenOut(
        access_token=create_token(user.id, user.email, user.role),
        expires_in=TOKEN_MINUTES * 60,
        email=user.email,
        role=user.role,
    )


@router.get("/me")
def me(user: ReaderUser) -> UserOut:
    return user
