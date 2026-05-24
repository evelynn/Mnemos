"""PR-102 — ``python -m app.cli verify`` boot self-test.

Investigation toward 9.8: PR-97/98/99/100 all close real Day-1
gaps but the only way for an operator to discover them is to send
the first webhook and wait for the failure. PR-99 made
``/health/ready`` deep-check, but ``/health/ready`` runs in the
serving process — an operator hitting it post-boot misses any boot
error that already crashed the process.

This PR adds a CLI command that runs the same checks against the
local env: config, DB, Redis, crypto round-trip, analyzer binaries
on PATH. Hard-failure (config / DB / Redis / crypto) returns exit
1; missing analyzer binaries are advisory.

Usage: ``docker compose exec platform python -m app.cli verify``.
"""

from __future__ import annotations

from pathlib import Path

_CLI = Path(__file__).resolve().parents[1] / "app" / "cli.py"


def _read() -> str:
    return _CLI.read_text(encoding="utf-8")


def test_verify_subcommand_registered():
    body = _read()
    assert 'sub.add_parser(\n        "verify"' in body
    assert "if args.cmd == \"verify\":" in body
    # Wired to the _verify async helper.
    assert "asyncio.run(_verify())" in body


def test_verify_function_runs_each_check():
    body = _read()
    idx = body.find("async def _verify(")
    slab = body[idx:idx + 3500]
    # Five subsystems probed: config / db / redis / crypto / analyzers.
    for marker in (
        "get_settings()",
        "SessionLocal",
        "get_redis",
        "encrypt(probe)",
        "_BINARIES",
    ):
        assert marker in slab, f"verify missed: {marker!r}"


def test_verify_exits_nonzero_on_hard_failure():
    body = _read()
    idx = body.find("async def _verify(")
    slab = body[idx:idx + 3500]
    # The function appends failure names then returns 1 — and the
    # caller (main) sys.exits with that code.
    assert "failures.append(" in slab
    assert "return 1" in slab
    assert "return 0" in slab


def test_verify_analyzers_advisory_not_fatal():
    body = _read()
    idx = body.find("async def _verify(")
    slab = body[idx:idx + 3500]
    # Missing analyzers print a 'warn' but don't append to failures.
    warn_at = slab.find("warn  analyzers")
    fail_append_at = slab.rfind("failures.append(")
    assert warn_at > 0
    # The warn block doesn't append to failures (analyzer block is
    # after the last failures.append).
    assert warn_at > fail_append_at


def test_help_text_present():
    body = _read()
    assert "boot self-test" in body
    assert "FERNET_KEY/SECRET_KEY/MNEMOS_ENV" in body
