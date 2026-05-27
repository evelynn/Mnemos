"""PR-100 — docker-compose declares all five analyzer build targets.

Investigation toward 9.8 noted ``docker-compose.yml`` declared only
``analyzer-csharp`` under the ``analyzers`` profile. The platform's
``registry.py`` invokes five binaries (csharp / ts / sql-mssql /
sql-oracle / binary-dotnet) but only one image had a declared build
target — operators had to know to ``docker build`` the rest by
hand. PR-100 adds the missing four so
``docker compose --profile analyzers build`` is the single command.
"""

from __future__ import annotations

from pathlib import Path

_COMPOSE = Path(__file__).resolve().parents[2] / "docker-compose.yml"


def _read() -> str:
    return _COMPOSE.read_text(encoding="utf-8")


def test_all_analyzer_services_present():
    """PR-115 added ggoss-py; the set is now six analyzer services."""
    body = _read()
    for service in (
        "analyzer-csharp:",
        "analyzer-ts:",
        "analyzer-py:",
        "analyzer-sql-mssql:",
        "analyzer-sql-oracle:",
        "analyzer-binary-dotnet:",
    ):
        assert service in body, f"missing service: {service}"


def test_all_images_tagged_under_mnemos_prefix():
    body = _read()
    for img in (
        "image: mnemos/ggoss-csharp:latest",
        "image: mnemos/ggoss-ts:latest",
        "image: mnemos/ggoss-py:latest",
        "image: mnemos/ggoss-sql-mssql:latest",
        "image: mnemos/ggoss-sql-oracle:latest",
        "image: mnemos/ggoss-binary-dotnet:latest",
    ):
        assert img in body, f"missing image tag: {img}"


def test_all_under_analyzers_profile():
    """The profile gate stays — these images are large; the operator
    opts in. PR-98's graceful degradation handles the opt-out path."""
    body = _read()
    # Count the profile lines that scope analyzer services. PR-115
    # bumped the floor from 5 to 6 (added ggoss-py).
    profile_lines = [ln for ln in body.splitlines() if 'profiles: ["analyzers"]' in ln]
    assert len(profile_lines) >= 6


def test_image_tags_match_registry_binaries():
    """The compose images must match the binaries app/analyzers/
    registry.py invokes. Drift here means an operator can build the
    images and still have the platform fail to find them."""
    from app.analyzers.registry import _BINARIES

    body = _read()
    for binary in _BINARIES.values():
        # Image name is mnemos/<binary>:latest.
        assert f"image: mnemos/{binary}:latest" in body, (
            f"binary {binary!r} from registry has no compose image"
        )
