"""PR-135 — single-process, docker-free launcher.

    python -m app.serve_local              # SQLite + fakeredis + uvicorn
    python -m app.serve_local --seed-demo  # + populate the demo dataset
    python -m app.serve_local --port 9000

Brings up the whole platform with **zero external services**: SQLite
for the database, in-process fakeredis for sessions/queues, jobs run
inline on the API event loop, analyzers as local subprocesses.

This module sets every environment default *before* importing any
``app.*`` module — ``app.db`` builds the SQLAlchemy engine at import
time and ``app.config`` lru-caches the settings, so the env must be
fixed first. Keep all ``from app...`` imports lazy (inside main()).
"""

from __future__ import annotations

import argparse
import os
import sys


def _bootstrap_env(db_path: str) -> None:
    """Set the docker-free defaults. ``setdefault`` so an operator can
    still override any of them (e.g. point at a different SQLite file
    or supply their own SECRET_KEY)."""
    os.environ.setdefault("MNEMOS_LOCAL_MODE", "1")
    # PR-153 — run the pure-stdlib in-repo analyzers (ggoss-py) without
    # Docker, so Python projects get verified deterministic extraction in
    # the basic config instead of the inferred Claude-Code fallback.
    os.environ.setdefault("MNEMOS_INREPO_ANALYZERS", "1")
    # Local dev/eval is not production; this also keeps the PR-97
    # placeholder-SECRET_KEY guard from firing on a generated key.
    os.environ.setdefault("MNEMOS_ENV", "local")
    os.environ.setdefault(
        "DATABASE_URL", f"sqlite+aiosqlite:///{db_path}"
    )
    # Generate ephemeral but valid crypto material so the operator
    # doesn't have to. These persist only for the process lifetime
    # unless the operator pins them in the environment.
    if not os.environ.get("SECRET_KEY"):
        import secrets

        os.environ["SECRET_KEY"] = secrets.token_urlsafe(48)
    if not os.environ.get("FERNET_KEY"):
        from cryptography.fernet import Fernet

        os.environ["FERNET_KEY"] = Fernet.generate_key().decode()
    # fakeredis stands in for Redis; the URL is never dialed but keep
    # it well-formed for any code that parses it.
    os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
    # Local HTTP — cookies can't require Secure or the browser drops
    # them over plain http://localhost. The Settings field is
    # ``session_cookie_secure`` → env ``SESSION_COOKIE_SECURE``.
    os.environ.setdefault("SESSION_COOKIE_SECURE", "false")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.serve_local",
        description="Run Mnemos with no Docker: SQLite + fakeredis + inline jobs.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument(
        "--db", default="./mnemos-local.db",
        help="SQLite file path (default ./mnemos-local.db).",
    )
    parser.add_argument(
        "--seed-demo", action="store_true",
        help="populate the demo dataset (org/project/graph/findings) on boot.",
    )
    parser.add_argument(
        "--reset", action="store_true",
        help="delete the SQLite file first for a clean slate.",
    )
    args = parser.parse_args(argv)

    if args.reset:
        for suffix in ("", "-wal", "-shm"):
            p = args.db + suffix
            try:
                os.remove(p)
            except OSError:
                pass

    _bootstrap_env(args.db)

    # ── now safe to import app.* (env is fixed) ────────────────────
    import asyncio

    from app.local_mode import ensure_sqlite_schema

    print("Mnemos local mode (no Docker)")
    print(f"  database : {os.environ['DATABASE_URL']}")
    print("  redis    : in-process fakeredis")
    print("  jobs     : inline (asyncio background tasks)")

    asyncio.run(ensure_sqlite_schema())

    if args.seed_demo:
        from app.seed_demo import seed_demo

        summary = asyncio.run(seed_demo(force=True))
        print("  demo     : seeded")
        if "password" in summary:
            print("  " + "=" * 56)
            print(f"    Login: {summary['user']} / {summary['password']}")
            print("  " + "=" * 56)

    print(f"\n  → http://{args.host}:{args.port}\n")

    import uvicorn

    from app.main import app

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
