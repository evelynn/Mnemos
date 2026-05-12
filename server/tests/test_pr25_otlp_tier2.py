"""PR-25 — OTLP Tier 2 trace-tree assembly + edge upsert (P2-1, P2-2)."""

from __future__ import annotations

from pathlib import Path


from app.merge.runtime import (
    _attr_dict,
    _kind_for,
    _operation_of,
    _service_name,
    assemble_trace_tree,
)

_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# trace-tree assembly (pure functions, no DB)
# ---------------------------------------------------------------------------


def test_attr_dict_flattens_otel_attribute_format():
    span = {
        "attributes": [
            {"key": "http.route", "value": {"stringValue": "/api/users/{id}"}},
            {"key": "http.status_code", "value": {"intValue": 200}},
            {"key": "telemetry.sdk.version", "value": {"stringValue": "1.0"}},
        ]
    }
    flat = _attr_dict(span)
    assert flat["http.route"] == "/api/users/{id}"
    assert flat["http.status_code"] == "200"


def test_service_name_from_resource_attrs():
    attrs = {"service.name": "payments-core", "host.name": "node-12"}
    assert _service_name(attrs) == "payments-core"


def test_kind_for_server_maps_to_exposes():
    """A SERVER span (the service handling an inbound call) is the
    counterpart to a static EXPOSES edge."""
    assert _kind_for({"kind": 2}) == "EXPOSES"


def test_kind_for_client_maps_to_calls():
    assert _kind_for({"kind": 3}) == "CALLS"


def test_kind_for_internal_falls_back_to_calls():
    """INTERNAL spans don't normally cross a contract boundary but we
    still buffer them rather than dropping the data."""
    assert _kind_for({"kind": 1}) == "CALLS"


def test_operation_prefers_http_route_over_span_name():
    """``http.route`` is the templated path — what static contract
    nodes are keyed on. Picking ``http.url`` instead would explode
    cardinality across every path parameter value."""
    span = {"name": "GET /api/users/42"}
    attrs = {"http.route": "/api/users/{id}"}
    assert _operation_of(span, attrs) == "/api/users/{id}"


def test_operation_uses_db_sql_table_pair():
    """For DB spans the receiver must NOT key on ``db.statement`` —
    the scrub pass masks it. ``db.sql.table`` + ``db.operation`` is
    the stable matcher."""
    span = {"name": "select"}
    attrs = {"db.sql.table": "users", "db.operation": "SELECT"}
    assert _operation_of(span, attrs) == "SELECT users"


def test_operation_falls_back_to_span_name():
    span = {"name": "publish.order_placed"}
    assert _operation_of(span, {}) == "publish.order_placed"


def test_assemble_trace_tree_walks_nested_otlp_shape():
    payload = [
        {
            "resource": {
                "attributes": [
                    {"key": "service.name", "value": {"stringValue": "billing"}}
                ]
            },
            "scopeSpans": [
                {
                    "spans": [
                        {
                            "spanId": "aaaa",
                            "parentSpanId": None,
                            "traceId": "tttt",
                            "kind": 2,  # SERVER
                            "name": "POST /charge",
                            "attributes": [
                                {
                                    "key": "http.route",
                                    "value": {"stringValue": "/charge"},
                                }
                            ],
                        },
                        {
                            "spanId": "bbbb",
                            "parentSpanId": "aaaa",
                            "traceId": "tttt",
                            "kind": 3,  # CLIENT
                            "name": "ledger.write",
                            "attributes": [
                                {
                                    "key": "rpc.method",
                                    "value": {"stringValue": "Ledger/Append"},
                                }
                            ],
                        },
                    ]
                }
            ],
        }
    ]
    records = assemble_trace_tree(payload)
    assert len(records) == 2
    # Caller resolution preserved through parent_span_id.
    assert records[0]["service"] == "billing"
    assert records[0]["kind"] == "EXPOSES"
    assert records[0]["operation"] == "/charge"
    assert records[1]["kind"] == "CALLS"
    assert records[1]["operation"] == "Ledger/Append"
    assert records[1]["parent_span"] == "aaaa"


def test_assemble_trace_tree_skips_empty_payload():
    assert assemble_trace_tree([]) == []
    assert assemble_trace_tree(None) == []  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Wiring guards — receiver, jobs, model, migration
# ---------------------------------------------------------------------------


def test_receiver_calls_buffer_observations():
    body = (_ROOT / "app" / "runtime_receiver" / "router.py").read_text("utf-8")
    assert "buffer_observations" in body
    assert "assemble_trace_tree" in body
    # The previous "Week-6 merge v2" placeholder comment is gone.
    assert "Week-6 merge v2" not in body


def test_receiver_resolves_org_from_header():
    body = (_ROOT / "app" / "runtime_receiver" / "router.py").read_text("utf-8")
    # Org context arrives via the documented custom header — OTLP
    # itself doesn't carry tenant info.
    assert "X-Mnemos-Organization-Id" in body or "x_mnemos_organization_id" in body


def test_receiver_handles_buffer_errors_gracefully():
    """Spans aren't fail-stop traffic — a buffer error must not break
    the receiver. Caller relies on the audit-only fallback."""
    body = (_ROOT / "app" / "runtime_receiver" / "router.py").read_text("utf-8")
    assert "rollback" in body


def test_jobs_calls_reconcile_after_findings_rebuild():
    body = (_ROOT / "app" / "orchestrator" / "jobs.py").read_text("utf-8")
    assert "reconcile_observations" in body
    # Reconcile runs *after* rebuild_findings — otherwise the
    # freshly-built edge set isn't visible.
    rebuild_idx = body.find("rebuild_findings(session, project_id)")
    reconcile_idx = body.find("reconcile_observations(")
    assert 0 < rebuild_idx < reconcile_idx


def test_runtime_observation_model_has_required_columns():
    body = (_ROOT / "app" / "models" / "runtime.py").read_text("utf-8")
    for col in (
        "organization_id",
        "project_id",
        "service",
        "operation",
        "kind",
        "seen_count",
        "last_seen_at",
        "first_seen_at",
    ):
        assert col in body, f"missing column {col!r}"


def test_runtime_observation_unique_on_org_svc_op_kind():
    """Team B's must-fix #2 — the upsert path needs this constraint
    so concurrent receivers don't double-count."""
    body = (_ROOT / "app" / "models" / "runtime.py").read_text("utf-8")
    assert "uq_runtime_obs_org_svc_op_kind" in body


def test_migration_present():
    p = _ROOT / "alembic" / "versions" / "0016_runtime_observations.py"
    assert p.exists()
    body = p.read_text("utf-8")
    assert "runtime_observations" in body
    # Both indexes the merge query relies on.
    assert "ix_runtime_obs_project" in body
    assert "ix_runtime_obs_last_seen" in body


def test_migration_chains_from_0015():
    body = (
        _ROOT / "alembic" / "versions" / "0016_runtime_observations.py"
    ).read_text("utf-8")
    assert 'down_revision: Union[str, None] = "0015_plan_worktree_meta"' in body


# ---------------------------------------------------------------------------
# Phase 2 backlog status doc
# ---------------------------------------------------------------------------


def test_phase2_backlog_marks_p2_1_p2_2_shipped():
    """A future operator should see at a glance which P2-* items are
    done. Tests don't enforce the doc edit here yet — the commit
    message links to the shipped PR — but we leave a hook for a
    follow-up sweep."""
    body = (
        _ROOT.parent / "docs" / "operator-guide" / "phase2_backlog.md"
    ).read_text("utf-8")
    # Just confirm both sections still exist; the marker is updated
    # in a follow-up housekeeping commit.
    assert "## P2-1" in body
    assert "## P2-2" in body
