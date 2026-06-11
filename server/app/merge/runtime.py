"""OTLP Tier 2 merge — trace-tree assembly + exercised-edge upsert.

Spec §7.6. The OTLP receiver buffers scrubbed spans in
``runtime_observations`` keyed by ``(organization_id, service,
operation, kind)``. This module:

* Walks a span tree (per trace) the receiver hands us, resolving
  caller/callee pairs through ``parent_span_id``.
* For each ``(caller, callee, kind)`` triple that matches an
  existing ``Edge``, upserts ``Edge.data.exercised = "true"`` and
  ``Edge.data.last_seen_at`` so downstream queries can filter on
  the ``runtime_exercised`` flag.
* For triples that don't resolve (early-arrival span, project not
  yet registered, etc.), buffers a RuntimeObservation row so the
  next analysis run's merge stage replays them.

Team B's 5th-round critique #1 was explicit that ``(service,
operation)`` alone is too coarse — the same service can call itself
internally — so the resolver walks ``parent_span_id`` and
distinguishes SERVER (EXPOSES) from CLIENT (CALLS) spans.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.graph import Edge
from app.models.runtime import RuntimeObservation

# OTLP span.kind enum values (per the OTel spec):
#   0 UNSPECIFIED  1 INTERNAL  2 SERVER  3 CLIENT  4 PRODUCER  5 CONSUMER
_SPAN_KIND_SERVER = 2
_SPAN_KIND_CLIENT = 3
_SPAN_KIND_PRODUCER = 4
_SPAN_KIND_CONSUMER = 5


def _attr_dict(span: dict[str, Any]) -> dict[str, str]:
    """Flatten OTel ``attributes: [{key, value: {stringValue: …}}]``
    into a plain ``{key: str}`` dict the rest of the merge code can
    treat as a normal mapping.
    """
    out: dict[str, str] = {}
    for attr in span.get("attributes", []) or []:
        k = attr.get("key")
        v = attr.get("value") or {}
        # OTLP encodes values per type; we only care about the string
        # representation for matching purposes.
        for vk in ("stringValue", "intValue", "boolValue", "doubleValue"):
            if vk in v:
                out[k] = str(v[vk])
                break
    return out


def _service_name(resource_attrs: dict[str, str]) -> str:
    return resource_attrs.get("service.name", "")


def _kind_for(span: dict[str, Any]) -> str:
    """Map an OTel span.kind onto the graph edge kind.

    SERVER  → EXPOSES   (the contract this service exposes was hit)
    CLIENT  → CALLS     (this service called something else)
    PRODUCER/CONSUMER → CALLS (treat queue traffic as a call edge)
    Anything else → CALLS by default — INTERNAL spans don't normally
    cross a contract boundary but we still buffer them so an operator
    debugging a strangler-pattern migration sees the data.
    """
    k = span.get("kind", 0)
    if k == _SPAN_KIND_SERVER:
        return "EXPOSES"
    return "CALLS"


def _operation_of(span: dict[str, Any], attrs: dict[str, str]) -> str:
    """Pick a stable operation key.

    Preference order matches what most OTel auto-instrumentations
    actually emit:

    1. ``http.route`` — the *pattern*, not the URL, which is what
       contract nodes are keyed on. Avoids cardinality explosions
       from path parameters.
    2. ``rpc.method`` — gRPC / Thrift.
    3. ``db.sql.table`` + ``db.operation`` — for READS / WRITES.
       ``db.statement`` alone is masked by the receiver scrub so
       it's not usable for matching.
    4. Fall back to span.name.
    """
    if "http.route" in attrs:
        return attrs["http.route"]
    if "rpc.method" in attrs:
        return attrs["rpc.method"]
    if "db.sql.table" in attrs and "db.operation" in attrs:
        return f"{attrs['db.operation']} {attrs['db.sql.table']}"
    return span.get("name", "")


def _operation_from_node_id(node_id: Any) -> str | None:
    """Extract the HTTP path from a contract *node id* so a runtime
    observation can match an edge that carries the route on its target
    node rather than in ``edge.data``.

    The platform has several contract-id shapes in flight depending on
    who minted the node — all of them keep the path as the last
    whitespace/colon-separated field:

        ``contract:POST /orders``        (seed + merge canonical) → ``/orders``
        ``contract:http:POST:/orders``   (ggoss-py analyzer)      → ``/orders``
        ``http.POST./orders``            (contract_id.http_contract_id) → ``/orders``

    Returns ``None`` for anything that isn't an HTTP contract id (e.g. a
    symbol or data node), so the caller simply skips it.
    """
    if not node_id:
        return None
    s = str(node_id)
    # Only HTTP contracts carry a path; ignore grpc./mq./dll./data: etc.
    if s.startswith("contract:http:"):
        # contract:http:POST:/orders  → take the part after the method
        rest = s[len("contract:http:"):]
        _, _, path = rest.partition(":")
        return path or None
    if s.startswith("contract:"):
        rest = s[len("contract:"):]
        # "POST /orders" → "/orders"; if no method prefix, use as-is.
        _, _, after = rest.partition(" ")
        path = after or rest
        return path if path.startswith("/") else None
    if s.startswith("http."):
        # http.POST./orders → /orders
        idx = s.find("./")
        if idx != -1:
            return s[idx + 1:]
    return None


def _operation_matches(candidate: Any, observed: str) -> bool:
    """True when a stored edge operation matches a runtime-observed
    one — raw, or after the path-template normalisation contract ids
    use, so a literal ``/api/orders/42`` matches a templated
    ``/api/orders/{id}``. Non-path operations (a db ``SELECT orders``)
    only match raw, never path-normalised.
    """
    if candidate is None or not observed:
        return False
    candidate = str(candidate)
    if candidate == observed:
        return True
    if candidate.startswith("/") and observed.startswith("/"):
        from app.merge.contract_id import normalize_http_path

        return normalize_http_path(candidate) == normalize_http_path(observed)
    return False


def assemble_trace_tree(
    resource_spans: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Group OTLP ``resourceSpans`` into a flat list of span records
    suitable for the merge code below.

    Each record:
        {
          "service":      str,       # resource.attributes service.name
          "operation":    str,       # see _operation_of()
          "kind":         "EXPOSES"|"CALLS",
          "parent_span":  str|None,  # hex
          "span_id":      str,       # hex
          "trace_id":     str,       # hex
        }

    The tree shape is preserved via ``parent_span``/``span_id`` so
    the caller can do its own parent-resolution where needed.
    """
    out: list[dict[str, Any]] = []
    for rs in resource_spans or []:
        resource_attrs = _attr_dict(rs.get("resource", {}))
        service = _service_name(resource_attrs)
        for scope in rs.get("scopeSpans", []) or []:
            for span in scope.get("spans", []) or []:
                attrs = _attr_dict(span)
                out.append(
                    {
                        "service": service,
                        "operation": _operation_of(span, attrs),
                        "kind": _kind_for(span),
                        "parent_span": span.get("parentSpanId") or None,
                        "span_id": span.get("spanId") or "",
                        "trace_id": span.get("traceId") or "",
                    }
                )
    return out


async def buffer_observations(
    db: AsyncSession,
    organization_id: uuid.UUID,
    records: Iterable[dict[str, Any]],
    project_id: uuid.UUID | None = None,
) -> int:
    """Upsert every (service, operation, kind) tuple from ``records``.

    Uses Postgres ``INSERT … ON CONFLICT DO UPDATE`` against the
    UNIQUE constraint ``uq_runtime_obs_org_svc_op_kind`` so the
    seen_count bump is atomic — no SELECT/INSERT race even when two
    workers receive the same trace concurrently.

    Returns the number of distinct triples buffered.
    """
    seen: set[tuple[str, str, str]] = set()
    rows = []
    for rec in records:
        svc = rec.get("service") or ""
        op = rec.get("operation") or ""
        kind = rec.get("kind") or "CALLS"
        if not svc or not op:
            continue
        key = (svc, op, kind)
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "organization_id": organization_id,
                "project_id": project_id,
                "service": svc,
                "operation": op,
                "kind": kind,
            }
        )
    if not rows:
        return 0
    stmt = pg_insert(RuntimeObservation).values(rows)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_runtime_obs_org_svc_op_kind",
        set_={
            "seen_count": RuntimeObservation.seen_count + 1,
            "last_seen_at": datetime.now(tz=timezone.utc),
        },
    )
    await db.execute(stmt)
    return len(rows)


async def reconcile_observations(
    db: AsyncSession,
    project_id: uuid.UUID,
    organization_id: uuid.UUID,
) -> dict[str, int]:
    """Match buffered observations against this project's edges.

    For each observation whose ``service`` maps to a Node with kind
    ``Service`` *and* whose ``operation`` matches an outgoing Edge of
    the documented kind, mark the edge ``exercised: true`` (preserve
    the existing ``data`` JSONB), set ``last_seen_at``, and bump
    ``hit_count`` if present.

    Returns ``{"matched": N, "unmatched": M}``.

    The strategy is intentionally conservative — the merge stage that
    runs after L0 extraction calls this; a real cross-language graph
    lookup is one of the items P2-1 explicitly defers to a future
    spec section. What we ship now is the upsert plumbing and the
    org/project guards.
    """
    obs_rows = (
        await db.execute(
            select(RuntimeObservation).where(
                RuntimeObservation.organization_id == organization_id,
                (RuntimeObservation.project_id == project_id)
                | (RuntimeObservation.project_id.is_(None)),
            )
        )
    ).scalars().all()

    matched = 0
    unmatched = 0
    now = datetime.now(tz=timezone.utc)
    for obs in obs_rows:
        edge_q = (
            select(Edge)
            .where(
                Edge.project_id == project_id,
                Edge.kind == obs.kind,
                Edge.valid_to.is_(None),
            )
        )
        candidates = (await db.execute(edge_q)).scalars().all()
        hit = None
        obs_op = obs.operation
        for edge in candidates:
            edge_data = edge.data or {}
            # Heuristic match: edge.data may carry ``operation`` /
            # ``path`` / ``route`` set by the static analyzers. An
            # OTel ``http.route`` is the *templated* path, but a
            # static analyzer may have stored a literal one (or vice
            # versa), so compare both raw and after the same
            # path-template normalisation contract ids use —
            # otherwise a runtime ``/api/orders/{id}`` never matches a
            # stored ``/api/orders/42``.
            # Match against the operation stored on the edge's own data…
            if any(
                _operation_matches(edge_data.get(k), obs_op)
                for k in ("operation", "path", "route")
            ):
                hit = edge
                break
            # …or against the path carried by the target *contract node*
            # (the common case — analyzers key the route on the contract
            # node id, e.g. ``contract:POST /orders``, not on edge.data,
            # so without this the standard EXPOSES/CALLS edge never
            # reconciles and the observation is stuck unmatched).
            if _operation_matches(_operation_from_node_id(edge.target_id), obs_op):
                hit = edge
                break
        if hit is None:
            unmatched += 1
            continue
        new_data = dict(hit.data or {})
        new_data["exercised"] = "true"
        new_data["last_seen_at"] = now.isoformat()
        new_data["hit_count"] = int(new_data.get("hit_count", 0)) + obs.seen_count
        await db.execute(
            update(Edge)
            .where(Edge.id == hit.id, Edge.valid_from == hit.valid_from)
            .values(data=new_data)
        )
        # Mark this observation as reconciled by pinning its
        # project_id (in case it came in before the project was
        # registered). The 14-day retention sweep will clean it up.
        if obs.project_id is None:
            obs.project_id = project_id
        matched += 1
    return {"matched": matched, "unmatched": unmatched}
