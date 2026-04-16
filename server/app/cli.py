"""Bootstrap helpers usable as: python -m app.cli <command> ..."""

import asyncio
import sys

from sqlalchemy import select

from app.auth.passwords import hash_password
from app.db import SessionLocal
from app.models.auth import User


async def _create_admin(username: str, password: str) -> None:
    async with SessionLocal() as session:
        existing = (
            await session.execute(select(User).where(User.username == username))
        ).scalar_one_or_none()
        if existing is not None:
            print(f"user '{username}' already exists (id={existing.id})")
            return
        user = User(username=username, password_hash=hash_password(password), role="admin")
        session.add(user)
        await session.commit()
        await session.refresh(user)
        print(f"created admin user '{username}' (id={user.id})")


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: python -m app.cli <create-admin|...>")
        sys.exit(2)
    cmd = sys.argv[1]
    if cmd == "create-admin":
        if len(sys.argv) != 4:
            print("usage: python -m app.cli create-admin <username> <password>")
            sys.exit(2)
        asyncio.run(_create_admin(sys.argv[2], sys.argv[3]))
    else:
        print(f"unknown command: {cmd}")
        sys.exit(2)


if __name__ == "__main__":
    main()
