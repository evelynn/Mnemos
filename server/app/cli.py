"""Bootstrap helpers usable as: python -m app.cli <command> ...

Commands:
- create-user --username X --role {admin,operator,viewer}
    Prompts for a password (interactive). Exit code 2 on bad args,
    1 when the username is already taken.
- create-admin <username> <password>
    Legacy shorthand kept for scripts. Prefer ``create-user``.
- rotate-fernet-key --old-key K1 --new-key K2 [--dry-run]
    Re-encrypt every stored secret from the old Fernet key to the new
    one. Safe to run while the platform is live (writes row-by-row in
    its own transaction); prefer a scheduled maintenance window anyway
    so a newly-created secret isn't missed mid-rotation.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select

from app.auth.passwords import hash_password
from app.db import SessionLocal
from app.models.auth import Secret, User

_VALID_ROLES = {"admin", "operator", "viewer"}


async def _rotate_fernet_key(old_key: str, new_key: str, dry_run: bool) -> int:
    old_fernet = Fernet(old_key.encode() if isinstance(old_key, str) else old_key)
    new_fernet = Fernet(new_key.encode() if isinstance(new_key, str) else new_key)
    rewrapped = 0
    skipped = 0
    async with SessionLocal() as session:
        result = await session.execute(select(Secret))
        for secret in result.scalars().all():
            try:
                plaintext = old_fernet.decrypt(secret.ciphertext).decode()
            except InvalidToken:
                skipped += 1
                print(f"skip {secret.id}: decrypt_failed (already rotated?)", file=sys.stderr)
                continue
            new_ciphertext = new_fernet.encrypt(plaintext.encode())
            if not dry_run:
                secret.ciphertext = new_ciphertext
            rewrapped += 1
        if not dry_run:
            await session.commit()
    verb = "would rewrap" if dry_run else "rewrapped"
    print(f"{verb} {rewrapped} secrets (skipped {skipped})")
    return 0 if skipped == 0 else 2


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

    rotate = sub.add_parser(
        "rotate-fernet-key",
        help="re-encrypt stored secrets from --old-key to --new-key",
    )
    rotate.add_argument("--old-key", required=True, help="current FERNET_KEY")
    rotate.add_argument("--new-key", required=True, help="new FERNET_KEY to store under")
    rotate.add_argument(
        "--dry-run", action="store_true", help="count without writing changes"
    )

    args = parser.parse_args()

    if args.cmd == "create-user":
        password = args.password or _prompt_password()
        rc = asyncio.run(_create_user(args.username, password, args.role))
        sys.exit(rc)
    if args.cmd == "create-admin":
        rc = asyncio.run(_create_user(args.username, args.password, "admin"))
        sys.exit(rc)
    if args.cmd == "rotate-fernet-key":
        rc = asyncio.run(
            _rotate_fernet_key(args.old_key, args.new_key, args.dry_run)
        )
        sys.exit(rc)


if __name__ == "__main__":
    main()
