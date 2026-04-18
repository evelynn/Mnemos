"""Role ordering checks (pure logic, no FastAPI machinery)."""

import pytest

from app.auth.rbac import _rank, check_role


def test_rank_ordering():
    assert _rank("admin") > _rank("operator") > _rank("viewer")
    assert _rank("unknown") == -1


def test_admin_passes_viewer_gate():
    assert check_role("admin", "viewer")


def test_operator_passes_operator_gate():
    assert check_role("operator", "operator")


def test_viewer_blocked_from_admin():
    assert not check_role("viewer", "admin")


def test_viewer_blocked_from_operator():
    assert not check_role("viewer", "operator")


def test_unknown_role_fails_closed():
    assert not check_role("robot", "viewer")


def test_unknown_min_role_raises():
    with pytest.raises(AssertionError):
        check_role("admin", "super")
