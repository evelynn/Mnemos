"""Policy enforcement (AWR consent, maintenance windows, sensitive tables)."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.data_sampler.project_db import (
    PolicyViolation,
    enforce_policy,
    requires_awr,
)


def _pdb(**overrides):
    return SimpleNamespace(
        allow_awr=overrides.get("allow_awr", False),
        maintenance_windows=overrides.get("maintenance_windows", []),
        sensitive_tables=overrides.get("sensitive_tables", []),
        masking_rules=overrides.get("masking_rules", {}),
    )


def test_requires_awr_detects_dba_hist():
    assert requires_awr("SELECT * FROM DBA_HIST_ACTIVE_SESS_HISTORY")
    assert requires_awr("select * from gv$session")
    assert not requires_awr("SELECT * FROM dual")


def test_awr_blocked_without_consent():
    with pytest.raises(PolicyViolation) as exc:
        enforce_policy(_pdb(allow_awr=False), awr_required=True)
    assert exc.value.code == "awr_not_consented"


def test_awr_allowed_with_consent():
    enforce_policy(_pdb(allow_awr=True), awr_required=True)


def test_missing_pdb_is_permissive():
    # No row yet -> fall through to defaults; nothing raises.
    enforce_policy(None, awr_required=True, sql="SELECT * FROM Users")


def test_sensitive_table_blocks():
    with pytest.raises(PolicyViolation) as exc:
        enforce_policy(
            _pdb(sensitive_tables=["Users"]),
            sql="SELECT * FROM Users",
        )
    assert exc.value.code == "sensitive_table_blocked"


def test_outside_maintenance_window_blocks():
    # This window covers only minute 0 of hour 0 — almost always outside.
    with pytest.raises(PolicyViolation) as exc:
        enforce_policy(_pdb(maintenance_windows=["0 0 1 1 *"]))
    assert exc.value.code == "outside_maintenance_window"


def test_empty_windows_are_permissive():
    enforce_policy(_pdb(maintenance_windows=[]))


def test_wildcard_windows_are_permissive():
    enforce_policy(_pdb(maintenance_windows=["* * * * *"]))
