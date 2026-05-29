"""Analyzer subprocess env is allowlisted (spec §2.8; Team B finding A).

Catches the case where a future config field (Slack webhook URL, OIDC
client secret, Vault token, …) silently leaks into an analyzer binary
because the runner used to call ``{**os.environ, **extra}``.
"""

from __future__ import annotations


def test_build_env_drops_arbitrary_host_vars(monkeypatch):
    from app.analyzers.runner import _build_env

    # Stuff the host env with something secret-looking.
    monkeypatch.setenv("MNEMOS_FERNET_KEY", "do-not-leak")
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "do-not-leak")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://example.invalid/hook")

    env = _build_env({"MNEMOS_DB_CONN": "user/pwd@dsn"})

    # Caller-supplied keys pass through.
    assert env["MNEMOS_DB_CONN"] == "user/pwd@dsn"

    # Host vars that aren't allowlisted are gone.
    assert "MNEMOS_FERNET_KEY" not in env
    assert "OIDC_CLIENT_SECRET" not in env
    assert "SLACK_WEBHOOK_URL" not in env


def test_build_env_keeps_path_and_locale(monkeypatch):
    """PATH / LANG must survive so analyzers can actually run."""
    from app.analyzers.runner import _build_env

    monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin")
    monkeypatch.setenv("LANG", "C.UTF-8")
    env = _build_env(None)

    assert env["PATH"] == "/usr/local/bin:/usr/bin"
    assert env["LANG"] == "C.UTF-8"


def test_build_env_caller_overrides_host(monkeypatch):
    from app.analyzers.runner import _build_env

    monkeypatch.setenv("MNEMOS_MAX_ROWS", "9999")
    env = _build_env({"MNEMOS_MAX_ROWS": "500"})

    assert env["MNEMOS_MAX_ROWS"] == "500"
