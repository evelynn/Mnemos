"""PR-109 — ``mnemos seed-demo`` populates a realistic demo dataset.

The single biggest adoption gap surfaced by the product-value
audit: a new operator can't see Mnemos do anything until they
register a GitLab project, configure secrets, trigger an
analysis, and wait minutes-to-hours for the analyzer pipeline
to populate the graph. SonarQube shows a first finding in 10
minutes. Mnemos's worth-trying threshold is much higher.

``seed-demo`` shortens that to 30 seconds:

    docker compose exec platform python -m app.cli seed-demo

The command creates:
- a ``demo`` organisation (or reuses if present)
- a ``demo`` admin user (password printed to stdout once)
- a ``demo-orders-service`` project — fake but realistic shape
- a small graph (~25 nodes, ~30 edges) covering all the kinds
  the dashboard renders: Component, Symbol, Contract, DataEntity
- 3 analysis runs (one running, one completed, one failed) so
  every status badge has data
- 8 findings spanning all detector kinds + priority buckets
  (one P1 so the new "Triage now" card lights up)
- L1 summaries for the top symbols so MCP get_module_summary
  returns prose, not stubs

Idempotency: re-running ``seed-demo`` clears prior demo rows
and re-creates them. ``--keep`` skips the destructive prelude.

Production guard: refuses to run when ``MNEMOS_ENV=production``
unless ``--force`` is passed. Demo data on a production graph
would mislead a real findings triage session.
"""

from __future__ import annotations

import secrets as _secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, select

from app.auth.passwords import hash_password
from app.db import SessionLocal
from app.models.audit import AuditLog
from app.models.auth import User
from app.models.findings import Finding, Summary
from app.models.graph import AnalysisRun, Edge, Node
from app.models.organization import Organization
from app.models.projects import Project

# All demo data is tagged with this slug so ``seed-demo`` can wipe
# its own prior insert without touching real org data.
DEMO_ORG_SLUG = "demo"
DEMO_PROJECT_NAME = "demo-orders-service"
DEMO_GITLAB_ID = 999999  # well above any real project id
DEMO_USER_NAME = "demo-admin"


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


async def _ensure_demo_org(session) -> Organization:
    org = (
        await session.execute(
            select(Organization).where(Organization.slug == DEMO_ORG_SLUG)
        )
    ).scalar_one_or_none()
    if org is None:
        org = Organization(slug=DEMO_ORG_SLUG, display_name="Demo Organisation")
        session.add(org)
        await session.flush()
    return org


async def _ensure_demo_user(session, org: Organization) -> tuple[User, str | None]:
    """Create the demo admin if missing. Returns (user, password_or_None).
    Password is only returned when freshly created — re-runs keep the
    existing user's password rather than rotating it."""
    user = (
        await session.execute(select(User).where(User.username == DEMO_USER_NAME))
    ).scalar_one_or_none()
    if user is not None:
        return user, None
    # Generate a printable random password — short enough to type
    # once, long enough to satisfy the ≥10-char policy.
    raw = "demo-" + _secrets.token_urlsafe(8)
    user = User(
        username=DEMO_USER_NAME,
        password_hash=hash_password(raw),
        role="admin",
        organization_id=org.id,
    )
    session.add(user)
    await session.flush()
    return user, raw


async def _wipe_prior_demo_rows(session, org_id: uuid.UUID) -> None:
    """Remove any prior demo project + cascading rows. Org and user
    are left in place so the operator's credential keeps working."""
    prior_projects = (
        await session.execute(
            select(Project.id).where(Project.organization_id == org_id)
        )
    ).scalars().all()
    for pid in prior_projects:
        await session.execute(delete(Finding).where(Finding.project_id == pid))
        await session.execute(delete(Summary).where(Summary.project_id == pid))
        await session.execute(delete(Edge).where(Edge.project_id == pid))
        await session.execute(delete(Node).where(Node.project_id == pid))
        await session.execute(
            delete(AnalysisRun).where(AnalysisRun.project_id == pid)
        )
        await session.execute(delete(Project).where(Project.id == pid))


def _node(
    *,
    id_: str,
    project_id: uuid.UUID,
    kind: str,
    name: str,
    extra: dict[str, Any] | None = None,
    certainty: str = "verified",
    exercised: bool = False,
) -> Node:
    data: dict[str, Any] = {"name": name}
    if extra:
        data.update(extra)
    if exercised:
        data["exercised"] = "true"
    return Node(
        id=id_,
        project_id=project_id,
        kind=kind,
        data=data,
        certainty=certainty,
        created_by=["seed-demo"],
    )


def _edge(
    *,
    project_id: uuid.UUID,
    source: str,
    target: str,
    kind: str,
    certainty: str = "verified",
    exercised: bool = False,
    extra: dict[str, Any] | None = None,
) -> Edge:
    data: dict[str, Any] = dict(extra or {})
    if exercised:
        data["exercised"] = "true"
    return Edge(
        project_id=project_id,
        source_id=source,
        target_id=target,
        kind=kind,
        data=data,
        certainty=certainty,
        created_by=["seed-demo"],
    )


def _build_graph(project_id: uuid.UUID) -> tuple[list[Node], list[Edge]]:
    """Hand-curated graph for the demo project. Covers each kind
    the dashboard renders so every panel has something to show."""
    nodes: list[Node] = []
    edges: list[Edge] = []

    # Two components — one TS frontend, one C# API.
    nodes += [
        _node(id_="comp:web-ui", project_id=project_id, kind="Component",
              name="web-ui", extra={"language": "typescript", "opacity": "open"}),
        _node(id_="comp:orders-api", project_id=project_id, kind="Component",
              name="orders-api", extra={"language": "csharp", "opacity": "open"}),
        _node(id_="comp:legacy-mailer", project_id=project_id, kind="Component",
              name="legacy-mailer",
              extra={"language": "unknown", "opacity": "binary"}),
    ]

    # Symbols.
    api_symbols = [
        ("sym:orders-api:CreateOrder",  "CreateOrder",  "method"),
        ("sym:orders-api:CancelOrder",  "CancelOrder",  "method"),
        ("sym:orders-api:GetOrder",     "GetOrder",     "method"),
        ("sym:orders-api:UpdateStatus", "UpdateStatus", "method"),
        ("sym:orders-api:OrdersRepo",   "OrdersRepo",   "class"),
        ("sym:orders-api:NotifyMailer", "NotifyMailer", "method"),
    ]
    ui_symbols = [
        ("sym:web-ui:OrderForm",  "OrderForm",  "function"),
        ("sym:web-ui:OrderList",  "OrderList",  "function"),
        ("sym:web-ui:ApiClient",  "ApiClient",  "class"),
    ]
    for sid, name, sub in api_symbols:
        nodes.append(_node(
            id_=sid, project_id=project_id, kind="Symbol", name=name,
            extra={"component_id": "comp:orders-api", "subkind": sub,
                   "signature": f"{name}(...)", "file": f"src/{name}.cs"},
            exercised=(name in {"CreateOrder", "GetOrder"}),
        ))
    for sid, name, sub in ui_symbols:
        nodes.append(_node(
            id_=sid, project_id=project_id, kind="Symbol", name=name,
            extra={"component_id": "comp:web-ui", "subkind": sub,
                   "signature": f"{name}(...)", "file": f"src/{name}.tsx"},
            exercised=(name in {"OrderForm", "OrderList"}),
        ))

    # Contracts (HTTP endpoints) — one of them duplicated to trip the
    # duplicate_endpoint detector.
    nodes += [
        _node(id_="contract:POST /orders", project_id=project_id,
              kind="Contract", name="POST /orders",
              extra={"protocol": "http", "method": "POST", "path": "/orders"}),
        _node(id_="contract:GET /orders/{id}", project_id=project_id,
              kind="Contract", name="GET /orders/{id}",
              extra={"protocol": "http", "method": "GET", "path": "/orders/{id}"}),
        _node(id_="contract:POST /orders/{id}/cancel", project_id=project_id,
              kind="Contract", name="POST /orders/{id}/cancel",
              extra={"protocol": "http", "method": "POST"}),
    ]

    # DataEntities — one sensitive, three not.
    nodes += [
        _node(id_="data:orders.dbo.Orders", project_id=project_id,
              kind="DataEntity", name="Orders",
              extra={"schema": "dbo", "sensitive": False,
                     "columns": ["id", "customer_id", "status", "created_at"]}),
        _node(id_="data:orders.dbo.Customers", project_id=project_id,
              kind="DataEntity", name="Customers",
              extra={"schema": "dbo", "sensitive": True,
                     "columns": ["id", "email", "phone", "rrn"]}),
        _node(id_="data:orders.dbo.OrderItems", project_id=project_id,
              kind="DataEntity", name="OrderItems",
              extra={"schema": "dbo", "sensitive": False,
                     "columns": ["order_id", "sku", "qty", "price"]}),
        # One schema_mismatch trigger — references a non-existent entity.
        _node(id_="data:orders.dbo.LegacyOrders", project_id=project_id,
              kind="DataEntity", name="LegacyOrders",
              extra={"schema": "dbo", "sensitive": False, "columns": ["id"]},
              certainty="inferred"),
    ]

    # EXPOSES — first one duplicated across two symbols (duplicate_endpoint).
    edges += [
        _edge(project_id=project_id, source="sym:orders-api:CreateOrder",
              target="contract:POST /orders", kind="EXPOSES", exercised=True),
        _edge(project_id=project_id, source="sym:orders-api:OrdersRepo",
              target="contract:POST /orders", kind="EXPOSES"),  # duplicate!
        _edge(project_id=project_id, source="sym:orders-api:GetOrder",
              target="contract:GET /orders/{id}", kind="EXPOSES", exercised=True),
        _edge(project_id=project_id, source="sym:orders-api:CancelOrder",
              target="contract:POST /orders/{id}/cancel", kind="EXPOSES"),
    ]
    # CALLS — frontend → contracts, methods → repo.
    edges += [
        _edge(project_id=project_id, source="sym:web-ui:OrderForm",
              target="contract:POST /orders", kind="CALLS", exercised=True),
        _edge(project_id=project_id, source="sym:web-ui:OrderList",
              target="contract:GET /orders/{id}", kind="CALLS", exercised=True),
        _edge(project_id=project_id, source="sym:web-ui:ApiClient",
              target="contract:POST /orders/{id}/cancel", kind="CALLS"),
        _edge(project_id=project_id, source="sym:orders-api:CreateOrder",
              target="sym:orders-api:OrdersRepo", kind="CALLS", exercised=True),
        _edge(project_id=project_id, source="sym:orders-api:CancelOrder",
              target="sym:orders-api:OrdersRepo", kind="CALLS"),
        _edge(project_id=project_id, source="sym:orders-api:CancelOrder",
              target="sym:orders-api:NotifyMailer", kind="CALLS",
              certainty="asserted",
              extra={"via_runtime": "true", "runtime_calls": 12, "runtime_errors": 4}),
        _edge(project_id=project_id, source="sym:orders-api:NotifyMailer",
              target="comp:legacy-mailer", kind="CALLS",
              certainty="asserted",
              extra={"runtime_calls": 12, "runtime_errors": 4}),
    ]
    # READS/WRITES — last one references LegacyOrders to trip schema_mismatch.
    edges += [
        _edge(project_id=project_id, source="sym:orders-api:CreateOrder",
              target="data:orders.dbo.Orders", kind="WRITES", exercised=True),
        _edge(project_id=project_id, source="sym:orders-api:CreateOrder",
              target="data:orders.dbo.OrderItems", kind="WRITES", exercised=True),
        _edge(project_id=project_id, source="sym:orders-api:GetOrder",
              target="data:orders.dbo.Orders", kind="READS", exercised=True),
        _edge(project_id=project_id, source="sym:orders-api:GetOrder",
              target="data:orders.dbo.Customers", kind="READS",
              extra={"sensitive_access": "true"}),
        _edge(project_id=project_id, source="sym:orders-api:CancelOrder",
              target="data:orders.dbo.LegacyOrders", kind="WRITES",
              certainty="inferred"),
    ]
    return nodes, edges


def _build_findings(project_id: uuid.UUID) -> list[Finding]:
    """Hand-shaped findings covering all priority buckets. One P1 so
    the dashboard's "Triage now" card has something to show."""
    now = _now()
    return [
        Finding(
            project_id=project_id, kind="duplicate_endpoint",
            severity="error", status="open",
            subject_node_id="contract:POST /orders",
            detail={"contract_id": "contract:POST /orders", "exposer_count": 2},
            risk_score=88, remediation="Pick one exposer; delete the other.",
            cwe_id="CWE-1041",
            first_seen_at=now - timedelta(days=2), last_seen_at=now,
        ),
        Finding(
            project_id=project_id, kind="opaque_component_failing",
            severity="warning", status="open",
            subject_node_id="comp:legacy-mailer",
            detail={"errors": 4, "calls": 12, "error_ratio": 0.333},
            risk_score=72, remediation="Investigate mailer error rate; add timeout/retry.",
            first_seen_at=now - timedelta(days=1), last_seen_at=now,
        ),
        Finding(
            project_id=project_id, kind="schema_mismatch",
            severity="warning", status="open",
            subject_node_id=None,
            detail={"source": "sym:orders-api:CancelOrder",
                    "missing_target": "data:orders.dbo.LegacyOrders",
                    "edge_kind": "WRITES"},
            risk_score=58, remediation="Rename code reference or recreate entity.",
            first_seen_at=now - timedelta(days=3), last_seen_at=now,
        ),
        Finding(
            project_id=project_id, kind="dynamic_call_detected",
            severity="warning", status="open",
            subject_node_id="sym:orders-api:NotifyMailer",
            detail={"source": "sym:orders-api:CancelOrder",
                    "target": "sym:orders-api:NotifyMailer"},
            risk_score=44, remediation="Materialise the call site or guard it.",
            first_seen_at=now - timedelta(days=5), last_seen_at=now,
        ),
        Finding(
            project_id=project_id, kind="unverified_claim",
            severity="info", status="acknowledged",
            subject_node_id=None,
            detail={"source": "sym:orders-api:CancelOrder",
                    "target": "data:orders.dbo.LegacyOrders", "kind": "WRITES"},
            risk_score=28, remediation="Run a fresh analysis to confirm.",
            first_seen_at=now - timedelta(days=12), last_seen_at=now - timedelta(days=1),
            acknowledged_at=now - timedelta(hours=6),
        ),
        Finding(
            project_id=project_id, kind="dead_path_suspected",
            severity="info", status="resolved",
            subject_node_id="sym:orders-api:UpdateStatus",
            detail={"source": "sym:orders-api:CreateOrder",
                    "target": "sym:orders-api:UpdateStatus"},
            risk_score=18,
            first_seen_at=now - timedelta(days=20), last_seen_at=now - timedelta(days=10),
            resolved_at=now - timedelta(days=8), resolved_by="user:demo-admin",
        ),
    ]


def _build_runs(project_id: uuid.UUID) -> list[AnalysisRun]:
    now = _now()
    return [
        AnalysisRun(
            project_id=project_id, status="completed",
            triggered_by="user:demo-admin", git_sha="abc123def4567890",
            started_at=now - timedelta(hours=2),
            completed_at=now - timedelta(hours=1, minutes=45),
            stats={"nodes": 25, "edges": 30, "findings": 6},
        ),
        AnalysisRun(
            project_id=project_id, status="failed",
            triggered_by="webhook:gitlab",
            git_sha="b0bcafe11deadbeef",
            started_at=now - timedelta(days=1),
            completed_at=now - timedelta(days=1) + timedelta(minutes=3),
            error_log="analyzer-csharp: timeout after 600s",
        ),
        AnalysisRun(
            project_id=project_id, status="running",
            triggered_by="webhook:gitlab", git_sha="ffeeddccbbaa9988",
            started_at=now - timedelta(minutes=3),
        ),
    ]


def _build_summaries(project_id: uuid.UUID) -> list[Summary]:
    """L1 summaries for top symbols so MCP get_module_summary returns
    real prose, not the no-key stub."""
    return [
        Summary(
            project_id=project_id, target_id="sym:orders-api:CreateOrder",
            level=1,
            summary=(
                "Handles POST /orders. Validates the payload, writes a row to "
                "dbo.Orders + N rows to dbo.OrderItems in one transaction, "
                "then returns the created order id."
            ),
            model_used="seed-demo",
        ),
        Summary(
            project_id=project_id, target_id="comp:orders-api", level=2,
            summary=(
                "REST API for the orders domain. Owns 3 endpoints (create / get / "
                "cancel) and the OrdersRepo. Talks to a legacy mailer over an "
                "opaque binary RPC — that hop has the highest error rate in the "
                "graph and is flagged."
            ),
            model_used="seed-demo",
        ),
    ]


async def seed_demo(*, force: bool = False, keep: bool = False) -> dict[str, Any]:
    """Populate the demo dataset. Idempotent — re-running wipes the
    prior demo project rows and re-inserts. Returns a small dict
    summarising what landed so the CLI can print it."""
    from app.config import get_settings

    s = get_settings()
    if (s.mnemos_env or "").lower() == "production" and not force:
        raise RuntimeError(
            "refusing to seed demo on MNEMOS_ENV=production "
            "(pass --force if this really is what you want)"
        )

    summary: dict[str, Any] = {}
    async with SessionLocal() as session:
        org = await _ensure_demo_org(session)
        user, raw_password = await _ensure_demo_user(session, org)
        summary["org_id"] = str(org.id)
        summary["user"] = user.username
        if raw_password is not None:
            summary["password"] = raw_password
        else:
            summary["password_note"] = "(existing demo user — password unchanged)"

        if not keep:
            await _wipe_prior_demo_rows(session, org.id)

        project = Project(
            name=DEMO_PROJECT_NAME,
            gitlab_project_id=DEMO_GITLAB_ID,
            gitlab_url="https://demo.local/orders-service",
            default_branch="main",
            languages=["csharp", "typescript", "sql"],
            organization_id=org.id,
        )
        session.add(project)
        await session.flush()

        nodes, edges = _build_graph(project.id)
        for n in nodes:
            session.add(n)
        for e in edges:
            session.add(e)
        for f in _build_findings(project.id):
            session.add(f)
        for r in _build_runs(project.id):
            session.add(r)
        for sm in _build_summaries(project.id):
            session.add(sm)
        session.add(AuditLog(
            actor=f"user:{user.id}",
            action="seed_demo.executed",
            target=str(project.id),
            project_id=project.id,
            details={"nodes": len(nodes), "edges": len(edges)},
        ))

        await session.commit()

        summary.update({
            "project_id": str(project.id),
            "project_name": project.name,
            "nodes": len(nodes),
            "edges": len(edges),
            "findings": 6,
            "runs": 3,
        })
    return summary
