"""Bootstrap helpers usable as: python -m app.cli <command> ...

Commands:
- create-user --username X --role {admin,operator,viewer}
    Prompts for a password (interactive). Exit code 2 on bad args,
    1 when the username is already taken.
- create-admin <username> <password>
    Legacy shorthand kept for scripts. Prefer ``create-user``.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys

from sqlalchemy import select

from app.auth.passwords import hash_password
from app.db import SessionLocal
from app.models.auth import User

_VALID_ROLES = {"admin", "operator", "viewer"}


async def _create_user(username: str, password: str, role: str) -> int:
    async with SessionLocal() as session:
        existing = (
            await session.execute(select(User).where(User.username == username))
        ).scalar_one_or_none()
        if existing is not None:
            print(f"user '{username}' already exists (id={existing.id})")
            return 1
        user = User(
            username=username, password_hash=hash_password(password), role=role
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        print(f"created {role} user '{username}' (id={user.id})")
        return 0


def _prompt_password() -> str:
    while True:
        p1 = getpass.getpass("Password: ")
        if len(p1) < 10:
            print("password must be at least 10 characters", file=sys.stderr)
            continue
        p2 = getpass.getpass("Password (again): ")
        if p1 != p2:
            print("passwords do not match", file=sys.stderr)
            continue
        return p1


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)

    create_user = sub.add_parser("create-user", help="create a new user")
    create_user.add_argument("--username", required=True)
    create_user.add_argument(
        "--role", required=True, choices=sorted(_VALID_ROLES)
    )
    create_user.add_argument(
        "--password",
        help=(
            "only for non-interactive use (CI, automation). Prefer omitting "
            "this and entering the password at the prompt."
        ),
    )

    legacy = sub.add_parser("create-admin", help="legacy: create an admin user")
    legacy.add_argument("username")
    legacy.add_argument("password")

    args = parser.parse_args()

    if args.cmd == "create-user":
        password = args.password or _prompt_password()
        rc = asyncio.run(_create_user(args.username, password, args.role))
        sys.exit(rc)
    if args.cmd == "create-admin":
        rc = asyncio.run(_create_user(args.username, args.password, "admin"))
        sys.exit(rc)


if __name__ == "__main__":
    main()
