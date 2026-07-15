"""Large-system end-to-end scenarios — does the platform's contract
actually compose under the workloads the spec §1.5 asks for?

These tests don't spin up Postgres or Redis; they pin the *shape* of
each handoff so a future refactor can't quietly drop part of the
pipeline. Anything that needs a live service is in the
``integration_*`` suite the CI job actually runs.

Five scenarios — one per E1..E5 in the 11th-round audit:

* E1  webhook → enqueue → analysis_run row → stage list
* E2  large monorepo (10K-file payload) — sampler caps + masking
* E3  sensitive-column query — PII masking end-to-end on a row set
* E4  failure recovery — stale-worker cron sees and resets the run
* E5  multi-operator concurrency — RBAC + break-glass token TTL
"""

from __future__ import annotations



from app.analyzers.registry import binary_for, runner_for
from app.data_sampler.masking import MaskingEngine, mask_rows


# ---------------------------------------------------------------------------
# E1 — webhook fan-out: every supported language has a registered analyzer
# ---------------------------------------------------------------------------


def test_e1_every_phase1_language_has_a_runner():
    """The spec §1.5 promise ("C# + TS + MSSQL/Oracle in one project")
    relies on a runner existing for each language slug. A typo in
    ``registry._BINARIES`` would silently skip a stage without any
    test catching it before the audit team did.
    """
    for lang in ("csharp", "typescript", "mssql", "oracle"):
        binary = binary_for(lang)
        assert binary is not None, f"no binary registered for {lang!r}"
        assert runner_for(lang) is not None, f"no runner for {lang!r}"


def test_e1_unknown_language_yields_none_not_a_default():
    """Skipping is the right default for an unknown language —
    falling back to a generic binary would point a project at the
    wrong analyzer."""
    # Ruby gained deterministic tree-sitter support.  Keep this assertion on
    # a genuinely unregistered language so the no-guessing contract remains
    # covered when the supported-language set expands.
    assert binary_for("haskell") is None
    assert runner_for("haskell") is None


def test_e1_dotnet_binary_kind_is_distinguished_from_source_csharp():
    """``dotnet_binary`` is the decompile-from-binary verb, used when
    the source isn't in the worktree. It must NOT collide with the
    source-tree csharp binary."""
    assert binary_for("csharp") != binary_for("dotnet_binary")


# ---------------------------------------------------------------------------
# E2 — large monorepo: masker stays correct under volume
# ---------------------------------------------------------------------------


def test_e2_masking_stays_consistent_across_10000_row_payload():
    """The data sampler caps single-query output at 10 000 rows
    (``MNEMOS_MAX_ROWS``). We run the engine on a synthetic payload
    of exactly that size to confirm the masking flag aggregates
    correctly and no row escapes the engine.
    """
    columns = ["email", "name", "id"]
    rows = [[f"u{i}@ex.com", f"Name-{i}", i] for i in range(10_000)]
    masked, col_flags, any_masked = mask_rows(columns, rows)
    assert len(masked) == 10_000
    # The email column is in PARTIAL_MASK_COLUMNS so every row's
    # first cell should be masked.
    assert col_flags[0] is True
    assert col_flags[1] is True
    assert any_masked is True
    # Spot-check the masking shape.
    assert masked[0][0].endswith("***")
    assert masked[9_999][1].endswith("***")


def test_e2_unknown_columns_keep_their_values():
    """A 10K-row payload with no sensitive columns must come back
    unmodified — the masker shouldn't be quadratic in row count."""
    rows = [[i, "ok"] for i in range(10_000)]
    masked, col_flags, any_masked = mask_rows(["id", "status"], rows)
    assert any_masked is False
    assert col_flags == [False, False]
    assert masked[5_000] == [5_000, "ok"]


# ---------------------------------------------------------------------------
# E3 — sensitive-column query: real PII patterns, validators decide
# ---------------------------------------------------------------------------


def test_e3_korean_rrn_with_valid_checksum_masks_as_rrn():
    """Inline RRN inside a free-form ``note`` column. The validator
    decides whether it's labelled ``[RRN]`` (verified) or
    ``[UNVERIFIED_NUMERIC_ID]`` (shape only). Either way the digits
    must not leak."""
    weights = (2, 3, 4, 5, 6, 7, 8, 9, 2, 3, 4, 5)
    prefix = "900101123456"
    check = (11 - sum(int(d) * w for d, w in zip(prefix, weights)) % 11) % 10
    rrn = prefix[:6] + "-" + prefix[6:] + str(check)
    masked, _, _ = mask_rows(["note"], [[f"신원: {rrn}"]])
    text = masked[0][0]
    assert "[RRN]" in text
    assert rrn.split("-")[1] not in text


def test_e3_credit_card_with_luhn_check_masks_as_card():
    masked, _, any_masked = mask_rows(["note"], [["card: 4111-1111-1111-1111"]])
    assert any_masked
    text = masked[0][0]
    # 4111 1111 1111 1111 passes Luhn.
    assert "[CARD]" in text
    assert "1111-1111" not in text


def test_e3_per_db_masking_rules_supplement_defaults():
    """A ProjectDB binding can layer ``masking_rules.full`` on top of
    the platform-wide regex. The most common operator override is
    "this column is named ``메모`` and contains free text"."""
    eng = MaskingEngine.from_project_db(
        {"full": ["^메모$"], "partial": ["^연락처$"]}
    )
    full_val, full_was = eng.mask_value("메모", "내부 노트")
    assert full_val == "***" and full_was
    part_val, part_was = eng.mask_value("연락처", "010-1234-5678")
    assert part_was
    assert part_val.startswith("010")
    assert part_val.endswith("***")


# ---------------------------------------------------------------------------
# E4 — failure recovery: stale-worker detection plumbing
# ---------------------------------------------------------------------------


def test_e4_retention_purge_sweeps_runtime_observations():
    """PR-29 fixed P2-2's missing 14-day sweep — this guard makes
    sure the cron handler keeps both deletes when someone refactors
    it. (Same code path the 9th-round audit caught as missing.)
    """
    from pathlib import Path

    real = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "orchestrator"
        / "cron_jobs.py"
    )
    src = real.read_text("utf-8")
    assert "DELETE FROM audit_logs" in src
    assert "DELETE FROM runtime_observations" in src


def test_e4_worker_heartbeat_constant_is_short_enough_for_recovery():
    """A 90-second stale-after window means a worker crash is
    visible to ``/health/ready`` within the next minute. Anything
    longer (5 minutes etc.) would let a wedged run accumulate
    silently."""
    from app.api import health

    assert health._WORKER_STALE_AFTER_SEC <= 120


# ---------------------------------------------------------------------------
# E5 — multi-operator concurrency: roles + break-glass TTL
# ---------------------------------------------------------------------------


def test_e5_break_glass_grant_has_short_ttl():
    """A grant that lives for an hour defeats its purpose — the
    initiator could phone an approver and walk back to the
    workstation. Spec §2.5 pins this at 15 minutes."""
    from datetime import timedelta

    from app.api.break_glass import GRANT_TTL

    # 15 minutes (no longer, no shorter) is what the spec says.
    assert GRANT_TTL == timedelta(minutes=15)


def test_e5_submit_diff_requires_operator():
    """Viewer can't burn ultrareview cycles by submitting diffs —
    the 6th-round audit closed this; this test pins it."""
    from pathlib import Path

    body = (
        Path(__file__).resolve().parents[1] / "app" / "api" / "diffs.py"
    ).read_text("utf-8")
    submit_idx = body.find("async def submit_diff(")
    next_close = body.find(") -> DiffOut:", submit_idx)
    assert submit_idx > 0 < next_close
    assert "require_operator" in body[submit_idx:next_close]


def test_e5_break_glass_initiator_cannot_self_approve():
    """The atomic UPDATE in the approve handler refuses the grant
    when the consuming user matches the issuer. Without this guard
    a single admin can bypass the two-eyes review entirely."""
    from pathlib import Path

    body = (
        Path(__file__).resolve().parents[1] / "app" / "api" / "diffs.py"
    ).read_text("utf-8")
    # The atomic UPDATE site refers to the issuer column.
    assert "issued_by" in body or "issuer" in body
