"""Bootstrap helpers usable as: python -m app.cli <command> ...

Commands:
- create-user --username X --role {admin,operator,viewer}
    Prompts for a password (interactive). Exit code 2 on bad args,
    1 when the username is already taken.
- create-admin <username> <password>
    Legacy shorthand kept for scripts. Prefer ``create-user``.
- rotate-fernet-key --old-key K1 --new-key K2 [--dry-run]
    Re-encrypt every stored secret and encrypted LLM semantic candidate
    from the old Fernet key to the new one. Safe to run while the platform
    is live (one transaction); prefer a maintenance window so no new row is
    missed mid-rotation.
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
from app.models.llm import LLMSemanticCandidate

_VALID_ROLES = {"admin", "operator", "viewer"}


async def _rotate_fernet_key(old_key: str, new_key: str, dry_run: bool) -> int:
    old_fernet = Fernet(old_key.encode() if isinstance(old_key, str) else old_key)
    new_fernet = Fernet(new_key.encode() if isinstance(new_key, str) else new_key)
    rewrapped = 0
    candidates_rewrapped = 0
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
        candidates = await session.execute(select(LLMSemanticCandidate))
        for candidate in candidates.scalars().all():
            try:
                plaintext = old_fernet.decrypt(candidate.payload_ciphertext)
            except InvalidToken:
                skipped += 1
                print(
                    f"skip candidate {candidate.attempt_id}: decrypt_failed "
                    "(already rotated?)",
                    file=sys.stderr,
                )
                continue
            new_ciphertext = new_fernet.encrypt(plaintext)
            if not dry_run:
                candidate.payload_ciphertext = new_ciphertext
            candidates_rewrapped += 1
        if not dry_run:
            await session.commit()
    verb = "would rewrap" if dry_run else "rewrapped"
    print(
        f"{verb} {rewrapped} secrets and {candidates_rewrapped} "
        f"LLM candidates (skipped {skipped})"
    )
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


async def _verify() -> int:
    """One-command boot self-test (PR-102). Mirrors /health/ready but
    runs from the CLI on a freshly-built image, so an operator can
    catch a bad env / missing analyzer / wrong-rotated FERNET_KEY
    BEFORE the first webhook arrives. Returns 0 if every hard-fail
    check passes; 1 otherwise. Soft warnings (missing analyzer
    binaries) print but don't change the exit code."""
    import shutil as _shutil
    import sys as _sys

    from app.config import get_settings

    failures: list[str] = []

    # 1) config — get_settings re-runs the PR-97 SECRET_KEY guard.
    try:
        s = get_settings()
    except RuntimeError:
        print("FAIL  config: invalid_configuration", file=_sys.stderr)
        return 1
    print(f"ok    config: mnemos_env={s.mnemos_env}")

    # 2) DB connect.
    try:
        from app.db import SessionLocal
        from sqlalchemy import text

        async with SessionLocal() as db:
            await db.execute(text("SELECT 1"))
        print("ok    database")
    except Exception:  # noqa: BLE001
        print("FAIL  database: database_unavailable", file=_sys.stderr)
        failures.append("database")

    # 3) Redis connect.
    try:
        from app.db import get_redis

        r = await get_redis()
        pong = await r.ping()
        if not pong:
            raise RuntimeError("no pong")
        print("ok    redis")
    except Exception:  # noqa: BLE001
        print("FAIL  redis: redis_unavailable", file=_sys.stderr)
        failures.append("redis")

    # 4) Crypto round-trip — catches a wrong-rotated FERNET_KEY.
    try:
        from app.safety.crypto import decrypt, encrypt

        probe = "mnemos-verify-probe"
        ct, iv = encrypt(probe)
        if decrypt(ct, iv) != probe:
            raise RuntimeError("round_trip_mismatch")
        print("ok    crypto: round_trip")
    except Exception:  # noqa: BLE001
        print("FAIL  crypto: crypto_unavailable", file=_sys.stderr)
        failures.append("crypto")

    # 5) Analyzer binaries on PATH (advisory).
    from app.analyzers.registry import _BINARIES

    missing = [b for b in _BINARIES.values() if _shutil.which(b) is None]
    if missing:
        print(
            f"warn  analyzers missing on PATH: {','.join(missing)} "
            "(stages will skip; run `docker compose --profile analyzers build`)"
        )
    else:
        print("ok    analyzers: all present")

    if failures:
        print(f"\nverify FAILED: {','.join(failures)}", file=_sys.stderr)
        return 1
    print("\nverify OK")
    return 0


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
        help=(
            "re-encrypt stored secrets and LLM candidates from --old-key "
            "to --new-key"
        ),
    )
    rotate.add_argument("--old-key", required=True, help="current FERNET_KEY")
    rotate.add_argument("--new-key", required=True, help="new FERNET_KEY to store under")
    rotate.add_argument(
        "--dry-run", action="store_true", help="count without writing changes"
    )

    sub.add_parser(
        "verify",
        help=(
            "boot self-test: env (FERNET_KEY/SECRET_KEY/MNEMOS_ENV), "
            "DB + Redis connect, crypto round-trip, analyzer binaries "
            "on PATH. Non-zero exit on a hard fail."
        ),
    )

    seed = sub.add_parser(
        "seed-demo",
        help=(
            "PR-109 — populate a realistic demo dataset (org + project "
            "+ graph + findings + runs) so a new operator can explore "
            "the dashboard without registering GitLab. Idempotent. "
            "Refuses on MNEMOS_ENV=production unless --force."
        ),
    )
    seed.add_argument(
        "--force", action="store_true",
        help="allow seeding on MNEMOS_ENV=production (use with care).",
    )
    seed.add_argument(
        "--keep", action="store_true",
        help="skip the destructive prelude — keep prior demo rows.",
    )

    serve = sub.add_parser(
        "serve-local",
        help=(
            "PR-135 — run the whole platform with NO Docker: SQLite + "
            "in-process fakeredis + inline jobs + local analyzer "
            "binaries. Single process, zero external services."
        ),
    )
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", default="8080")
    serve.add_argument("--db", default="./mnemos-local.db")
    serve.add_argument("--seed-demo", action="store_true")
    serve.add_argument("--reset", action="store_true")

    args = parser.parse_args()

    if args.cmd == "serve-local":
        # serve_local must control env (DATABASE_URL etc) BEFORE any
        # app.* import. This module already imported app.db, so hand
        # off to a clean process via execv rather than importing here.
        import os

        cmd = [
            sys.executable, "-m", "app.serve_local",
            "--host", args.host, "--port", str(args.port), "--db", args.db,
        ]
        if args.seed_demo:
            cmd.append("--seed-demo")
        if args.reset:
            cmd.append("--reset")
        os.execvp(sys.executable, cmd)

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
    if args.cmd == "verify":
        rc = asyncio.run(_verify())
        sys.exit(rc)
    if args.cmd == "seed-demo":
        from app.seed_demo import seed_demo

        try:
            summary = asyncio.run(seed_demo(force=args.force, keep=args.keep))
        except RuntimeError as exc:
            print(f"seed-demo refused: {exc}", file=sys.stderr)
            sys.exit(2)
        print("seed-demo OK:")
        for k, v in summary.items():
            print(f"  {k}: {v}")
        if "password" in summary:
            print("")
            print("=" * 60)
            print(f"  Login: {summary['user']} / {summary['password']}")
            print("  ↑ printed ONCE — save it before clearing the terminal.")
            print("=" * 60)
        sys.exit(0)


if __name__ == "__main__":
    main()
