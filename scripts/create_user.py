"""Create or update a user (Step Q). Needs Postgres running (`make up`) and the schema (`alembic upgrade head`).

    uv run python scripts/create_user.py --email manager@example.com --role manager      # asks for the password
    uv run python scripts/create_user.py --email owner@example.com --role owner --password "a long password"
    uv run python scripts/create_user.py --demo                                          # one fake user per role

An email that already exists gets the new password and role. Passwords are stored as argon2 hashes only.
--demo is for your laptop: all demo users share one fake password and it refuses to run when SHOP_ENV=prod.
"""
import argparse
import getpass

from sqlalchemy import select

from shoppilot.core.config import settings
from shoppilot.core.security import ROLES, hash_password
from shoppilot.db.models import UserRow
from shoppilot.db.session import make_engine, make_session_factory

MIN_PASSWORD_LENGTH = 8
DEMO_PASSWORD = "ShopPilot-demo-1"  # fake, development only


def save_user(sf, email: str, password: str, role: str) -> str:
    """Create the user, or update the password and role of an existing one. Returns 'created' or 'updated'."""
    email = email.strip().lower()
    with sf() as session:
        user = session.scalar(select(UserRow).where(UserRow.email == email))
        action = "updated" if user else "created"
        if user is None:
            user = UserRow(email=email, password_hash="", role=role)
            session.add(user)
        user.password_hash = hash_password(password)
        user.role = role
        session.commit()
    return action


def main() -> None:
    parser = argparse.ArgumentParser(description="Create or update a ShopPilot user")
    parser.add_argument("--email", help="login email")
    parser.add_argument("--role", choices=ROLES, help="viewer | support | manager | owner | admin")
    parser.add_argument("--password", help="leave it out to be asked (the typing is hidden)")
    parser.add_argument("--demo", action="store_true", help="create one fake user per role (development only)")
    args = parser.parse_args()

    sf = make_session_factory(make_engine())

    if args.demo:
        if settings.env == "prod":
            parser.error("--demo is not allowed when SHOP_ENV=prod")
        for role in ROLES:
            email = f"{role}@example.com"
            print(f"{save_user(sf, email, DEMO_PASSWORD, role):8} {email}  ({role})")
        print(f"Password for all of them: {DEMO_PASSWORD}")
        return

    if not args.email or not args.role:
        parser.error("use --demo, or give --email and --role")
    password = args.password or getpass.getpass("Password: ")
    if len(password) < MIN_PASSWORD_LENGTH:
        parser.error(f"the password needs at least {MIN_PASSWORD_LENGTH} characters")
    print(f"{save_user(sf, args.email, password, args.role)} {args.email.strip().lower()} ({args.role})")


if __name__ == "__main__":
    main()
