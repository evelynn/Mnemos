"""PR-97 — production fail-closed on a placeholder SECRET_KEY.

A pessimistic cross-comparison flagged ``SECRET_KEY="change-me-in-
production"`` (config.py:19 default) and ``"change-me-to-a-random-
32-byte-secret"`` (.env.example) as Day-1 forgeable-cookie risks:
anyone who has read the README can sign a session for any user. The
platform now refuses to boot in production with any of the known
placeholders, mirroring the FERNET_KEY guard already in safety/kms.

The test suite is exempt — conftest sets ``MNEMOS_ENV=test`` so
``Settings()`` loads with whatever placeholder is configured.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_CONFIG = Path(__file__).resolve().parents[1] / "app" / "config.py"


def _reload_settings(monkeypatch, **env):
    """Bust the lru_cache on get_settings and re-import with new env."""
    import importlib

    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import app.config as cfg
    importlib.reload(cfg)
    return cfg


def test_production_with_default_secret_key_refuses(monkeypatch):
    cfg = _reload_settings(
        monkeypatch,
        MNEMOS_ENV="production",
        SECRET_KEY="change-me-in-production",
    )
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        cfg.get_settings()


def test_production_with_env_example_placeholder_refuses(monkeypatch):
    """The .env.example placeholder must also be rejected."""
    cfg = _reload_settings(
        monkeypatch,
        MNEMOS_ENV="production",
        SECRET_KEY="change-me-to-a-random-32-byte-secret",
    )
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        cfg.get_settings()


def test_production_with_empty_secret_refuses(monkeypatch):
    cfg = _reload_settings(
        monkeypatch, MNEMOS_ENV="production", SECRET_KEY="",
    )
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        cfg.get_settings()


def test_production_with_real_secret_loads(monkeypatch):
    cfg = _reload_settings(
        monkeypatch,
        MNEMOS_ENV="production",
        SECRET_KEY="J9k_2Hf-9qmRPxV4nL7eYbT3D8wQzAaE5cM1iU0xKj6vS_pNoFhBuGr",
    )
    s = cfg.get_settings()
    assert s.secret_key.startswith("J9k_")


def test_dev_env_lets_placeholder_through(monkeypatch):
    """A developer running locally with MNEMOS_ENV=dev can still boot
    with the default — only production fails closed."""
    cfg = _reload_settings(
        monkeypatch,
        MNEMOS_ENV="dev",
        SECRET_KEY="change-me-in-production",
    )
    s = cfg.get_settings()
    assert s.mnemos_env == "dev"


def test_known_placeholder_list_documented():
    body = _CONFIG.read_text(encoding="utf-8")
    assert "_INSECURE_SECRET_KEYS" in body
    # The two README-visible placeholders are in the list.
    assert '"change-me-in-production"' in body
    assert '"change-me-to-a-random-32-byte-secret"' in body
