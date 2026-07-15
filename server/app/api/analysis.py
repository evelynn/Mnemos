import asyncio
import contextlib
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Cookie, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import case, func, select, union_all, update
from sqlalchemy.ext.asyncio import AsyncSession

import app.db as app_db
from app.api.graph_guard import require_readable_current_graph
from app.audit.logger import record as audit_record
from app.auth.deps import CurrentUser, resolve_active_session_user
from app.auth.org_scope import (
    require_project_org,
    require_run_org,
    resolve_run_org,
    same_org,
)
from app.auth.rbac import require_operator
from app.config import get_settings
from app.db import get_session
from app.extractor.validator import current_summary_claim_views
from app.graph_publication import GRAPH_HEAD_READY
from app.graph_overlays import (
    GraphOverlayFactNotFound,
    GraphOverlayUnavailable,
    edge_identity,
    edge_read_view,
    effective_certainty,
    load_edge_overlays,
    load_node_human_overlays,
    node_read_view,
    record_human_confirmation,
)
from app.models.auth import User
from app.models.graph import (
    ANALYSIS_RUN_TERMINAL_STATUSES,
    AnalysisRun,
    Edge,
    GraphHead,
    Node,
)
from app.models.projects import Project
from app.models.stages import AnalysisStage
from app.orchestrator.progress import ProgressBus
from app.orchestrator.queue import get_queue
from app.orchestrator.source_binding import (
    ProjectSourceBindingError,
    resolve_project_source_path,
)

router = APIRouter(prefix="/api/v1", tags=["analysis"])
log = logging.getLogger(__name__)
_settings = get_settings()
_TERMINAL_STATUSES = ANALYSIS_RUN_TERMINAL_STATUSES
_CANCELLABLE_STATUSES = frozenset({"queued", "running"})
_MAX_ERROR_LOG_CHARS = 4096
_SSE_DB_RECHECK_SEC = 5.0


def _bounded_error_log(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    if len(text) <= _MAX_ERROR_LOG_CHARS:
        return text
    return text[: _MAX_ERROR_LOG_CHARS - 16] + "\n...[truncated]"


class AnalysisTriggerRequest(BaseModel):
    git_sha: str = Field(default="HEAD")
    ref: str | None = Field(default=None, max_length=300)
    # Incremental is also correct for a project's first run: with no prior
    # manifest every registered producer is selected.  Making it the request
    # default prevents a routine refresh from accidentally forcing a full
    # repository walk; ``full`` remains the explicit repair/reconciliation
    # mode after a failed unpublished refresh.
    scope: str = Field(
        default="incremental",
        pattern="^(full|incremental|continuation)$",
    )
    source_path: str = Field(description="Absolute path visible to the worker")
    # Source indexing is deterministic and LLM-free by default.  Narrative
    # summaries are an explicit secondary pass over graph evidence.
    summarize: bool = Field(default=False)
    l1_limit: int = Field(default=25, ge=0, le=1000)
    l2_limit: int = Field(default=25, ge=0, le=1000)
    l3_limit: int = Field(default=25, ge=0, le=1000)
    # PR-140 — per-language file budget for Claude-Code extraction of
    # languages with no deterministic analyzer. 0 disables the path.
    agent_extract_limit: int = Field(default=0, ge=0, le=500)

    @model_validator(mode="after")
    def continuation_requires_narration(self) -> "AnalysisTriggerRequest":
        # Continuation does not re-index source.  With narration disabled it
        # has no useful work to perform and can otherwise sit in the queue
        # looking like an incremental source refresh.
        if self.scope == "continuation" and not self.summarize:
            raise ValueError("continuation_requires_summarize")
        return self


class AnalysisRunOut(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    status: str
    triggered_by: str
    git_sha: str
    scope: str
    started_at: datetime | None
    completed_at: datetime | None
    stats: dict[str, Any] | None
    error_log: str | None
    created_at: datetime


def _to_out(r: AnalysisRun) -> AnalysisRunOut:
    return AnalysisRunOut(
        id=r.id,
        project_id=r.project_id,
        status=r.status,
        triggered_by=r.triggered_by,
        git_sha=r.git_sha,
        scope=r.scope,
        started_at=r.started_at,
        completed_at=r.completed_at,
        stats=r.stats,
        error_log=_bounded_error_log(r.error_log),
        created_at=r.created_at,
    )


@router.post(
    "/projects/{project_id}/analyze",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_project_org()), Depends(require_operator)],
)
async def trigger_analysis(
    project_id: uuid.UUID,
    body: AnalysisTriggerRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> AnalysisRunOut:
    project = (
        await db.execute(select(Project).where(Project.id == project_id))
    ).scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail="project_not_found")

    # ``SOURCE_ALLOWED_ROOT`` alone is only a filesystem boundary, not a
    # tenant boundary. Bind this project to its operator-owned subdirectory
    # before creating an AnalysisRun or touching the queue.
    try:
        source_binding = resolve_project_source_path(
            project_id,
            body.source_path,
            allowed_root=_settings.source_allowed_root,
            project_roots_json=_settings.source_project_roots,
        )
    except ProjectSourceBindingError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    canonical_source_path = str(source_binding.source_path)

    run = AnalysisRun(
        id=uuid.uuid4(),
        project_id=project_id,
        status="queued",
        triggered_by=f"user:{user.id}",
        git_sha=body.git_sha,
        scope=body.scope,
    )
    job_id = f"analysis:{run.id}"
    run.stats = {
        "job_id": job_id,
        "queued_mode": "deterministic_index" if not body.summarize else "index_plus_ai_narration",
    }
    db.add(run)
    await db.commit()
    await db.refresh(run)

    try:
        queue = await get_queue()
        job = await queue.enqueue_job(
            "run_ingest",
            str(project_id),
            str(run.id),
            canonical_source_path,
            {
                "scope": body.scope,
                "ref": body.ref,
                "summarize": body.summarize,
                "l1_limit": body.l1_limit,
                "l2_limit": body.l2_limit,
                "l3_limit": body.l3_limit,
                "agent_extract_limit": body.agent_extract_limit,
            },
            _job_id=job_id,
        )
    except asyncio.CancelledError:
        run.status = "failed"
        run.completed_at = datetime.now(tz=timezone.utc)
        run.error_log = "analysis_enqueue_cancelled"
        await asyncio.shield(db.commit())
        raise
    except Exception as exc:  # noqa: BLE001 — persist a terminal run first
        log.exception("analysis enqueue failed run_id=%s", run.id)
        run.status = "failed"
        run.completed_at = datetime.now(tz=timezone.utc)
        run.error_log = _bounded_error_log(
            f"analysis_enqueue_failed:{type(exc).__name__}"
        )
        await db.commit()
    else:
        if job is None:
            run.status = "failed"
            run.completed_at = datetime.now(tz=timezone.utc)
            run.error_log = "analysis_job_not_enqueued"
            await db.commit()
    await audit_record(
        actor=f"user:{user.id}",
        action="analysis.enqueue",
        target=str(run.id),
        project_id=project_id,
        details={
            "git_sha": body.git_sha,
            "ref": body.ref,
            "scope": body.scope,
            "summarize": body.summarize,
            "agent_extract_limit": body.agent_extract_limit,
        },
    )
    return _to_out(run)


@router.get(
    "/analysis_runs/{run_id}",
    dependencies=[Depends(require_run_org())],
)
async def get_run(
    run_id: uuid.UUID,
    _: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> AnalysisRunOut:
    run = (
        await db.execute(select(AnalysisRun).where(AnalysisRun.id == run_id))
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail="not_found")
    return _to_out(run)


@router.get(
    "/projects/{project_id}/analysis_runs",
    dependencies=[Depends(require_project_org())],
)
async def list_runs(
    project_id: uuid.UUID,
    _: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> list[AnalysisRunOut]:
    rows = (
        await db.execute(
            select(AnalysisRun)
            .where(AnalysisRun.project_id == project_id)
            .order_by(AnalysisRun.created_at.desc())
            .limit(50)
        )
    ).scalars().all()
    return [_to_out(r) for r in rows]


async def _require_sse_run_access(
    run_id: uuid.UUID,
    session_token: Annotated[
        str | None,
        Cookie(alias=_settings.session_cookie_name),
    ] = None,
) -> User:
    """Authorize an SSE stream without a request-lifetime DB session.

    A normal ``get_session`` dependency is request-scoped for a streaming
    response, which can pin a pool connection for hours.  Authentication and
    org lookup are completed in one short explicit session before the stream
    starts; the generator opens similarly short sessions for terminal polls.
    """

    if not session_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="not_authenticated",
        )
    async with app_db.SessionLocal() as session:
        user = await resolve_active_session_user(session_token, session)
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid_session",
            )
        found, org_id = await resolve_run_org(session, run_id)
        if not found or not same_org(user, org_id):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="not_found",
            )
        return user


async def _load_run_state(run_id: uuid.UUID) -> tuple[str, str | None] | None:
    async with app_db.SessionLocal() as session:
        row = (
            await session.execute(
                select(AnalysisRun.status, AnalysisRun.error_log).where(
                    AnalysisRun.id == run_id
                )
            )
        ).one_or_none()
    if row is None:
        return None
    return str(row.status), _bounded_error_log(row.error_log)


def _terminal_payload(status_value: str, error_log: str | None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "event": f"run_{status_value}",
        "status": status_value,
    }
    if error_log:
        payload["error_log"] = error_log
        # Backward compatibility for the dashboard notification path.
        payload["error"] = error_log
    return payload


@router.get(
    "/analysis_runs/{run_id}/events",
    dependencies=[Depends(_require_sse_run_access)],
)
async def run_events(
    run_id: uuid.UUID,
    request: Request,
) -> StreamingResponse:
    bus = ProgressBus()

    async def _sse() -> Any:
        events = bus.subscribe(run_id)
        next_event: asyncio.Task | None = None
        yield f"event: open\ndata: {json.dumps({'run_id': str(run_id)})}\n\n"
        try:
            # Start subscribing before the authoritative DB re-read. If a
            # terminal publish lands in either direction of this race, the
            # periodic DB check below still observes the committed status.
            next_event = asyncio.create_task(anext(events))
            while not await request.is_disconnected():
                state = await _load_run_state(run_id)
                if state is None:
                    return
                current_status, error_log = state
                if current_status in _TERMINAL_STATUSES:
                    terminal = _terminal_payload(current_status, error_log)
                    yield f"event: progress\ndata: {json.dumps(terminal)}\n\n"
                    return

                if next_event is None:
                    await asyncio.sleep(_SSE_DB_RECHECK_SEC)
                    continue
                done, _ = await asyncio.wait(
                    {next_event}, timeout=_SSE_DB_RECHECK_SEC
                )
                if not done:
                    continue
                try:
                    event = next_event.result()
                except StopAsyncIteration:
                    return
                except Exception:  # noqa: BLE001 — retain DB terminal polling
                    log.exception("analysis SSE progress subscription failed")
                    next_event = None
                    continue

                event_name = event.get("event")
                if event_name in {
                    "run_completed",
                    "run_partial",
                    "run_failed",
                    "run_cancelled",
                }:
                    # Prefer committed DB state and its bounded error log.
                    state = await _load_run_state(run_id)
                    if state is not None and state[0] in _TERMINAL_STATUSES:
                        event = _terminal_payload(*state)
                    else:
                        event = dict(event)
                        for key in ("error", "error_log"):
                            if key in event:
                                event[key] = _bounded_error_log(event[key])
                    yield f"event: progress\ndata: {json.dumps(event)}\n\n"
                    return

                yield f"event: progress\ndata: {json.dumps(event)}\n\n"
                next_event = asyncio.create_task(anext(events))
        finally:
            if next_event is not None and not next_event.done():
                next_event.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await next_event
            with contextlib.suppress(RuntimeError):
                await events.aclose()

    return StreamingResponse(_sse(), media_type="text/event-stream")


@router.post(
    "/analysis_runs/{run_id}/cancel",
    dependencies=[Depends(require_run_org()), Depends(require_operator)],
)
async def cancel_run(
    run_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    # One conditional UPDATE owns the queued/running -> cancelled transition.
    # ``published`` is deliberately excluded: its graph receipt/head already
    # committed and cancellation may stop only optional post-processing.
    changed = (
        await db.execute(
            update(AnalysisRun)
            .where(
                AnalysisRun.id == run_id,
                AnalysisRun.status.in_(_CANCELLABLE_STATUSES),
            )
            .values(
                status="cancelled",
                completed_at=datetime.now(tz=timezone.utc),
            )
            .returning(AnalysisRun.project_id, AnalysisRun.stats)
        )
    ).one_or_none()
    cancel_event = "run_cancelled"
    response: dict[str, str] = {"status": "cancelled"}
    if changed is not None:
        project_id, run_stats = changed
    else:
        # Serialize the whole-JSON stats merge with retry bookkeeping and
        # postprocess finalization. Without the row lock, two readers can both
        # merge an old stats value and the later commit can erase either the
        # cancellation flag or the terminal postprocess receipt.
        current = (
            await db.execute(
                select(AnalysisRun)
                .where(AnalysisRun.id == run_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if current is None:
            raise HTTPException(status_code=404, detail="not_found")
        if current.status != "published":
            await db.rollback()
            return {"status": str(current.status)}
        project_id = current.project_id
        run_stats = current.stats
        prior_stats = dict(run_stats) if isinstance(run_stats, dict) else {}
        prior_postprocess = prior_stats.get("postprocess")
        postprocess = (
            dict(prior_postprocess) if isinstance(prior_postprocess, dict) else {}
        )
        postprocess.update(
            {
                "cancel_requested": True,
                "cancel_requested_at": datetime.now(tz=timezone.utc).isoformat(),
                "cancel_requested_by": f"user:{user.id}",
            }
        )
        run_stats = {**prior_stats, "postprocess": postprocess}
        current.stats = run_stats
        cancel_event = "run_cancel_requested"
        response = {"status": "published", "cancel_requested": "postprocess"}

    await db.commit()
    try:
        await ProgressBus().publish(
            run_id,
            {
                "event": cancel_event,
                "status": response["status"],
                **(
                    {"scope": "postprocess"}
                    if cancel_event == "run_cancel_requested"
                    else {}
                ),
            },
        )
    except Exception:  # noqa: BLE001 — DB cancellation is authoritative
        log.exception("analysis cancellation publish failed run_id=%s", run_id)

    job_id = (
        (run_stats or {}).get("job_id")
        if isinstance(run_stats, dict)
        else None
    )
    if isinstance(job_id, str) and job_id:
        try:
            queue = await get_queue()
            abort_job = getattr(queue, "abort_job", None)
            if abort_job is not None:
                await abort_job(job_id)
            else:
                from arq.jobs import Job

                await Job(job_id, queue).abort(timeout=2.0)
        except Exception:  # noqa: BLE001 — cooperative DB cancellation remains active
            log.exception("analysis job abort failed run_id=%s job_id=%s", run_id, job_id)
    await audit_record(
        actor=f"user:{user.id}",
        action=(
            "analysis.cancel_postprocess_requested"
            if cancel_event == "run_cancel_requested"
            else "analysis.cancel"
        ),
        target=str(run_id),
        project_id=project_id,
    )
    return response


@router.get(
    "/analysis_runs/{run_id}/stages",
    dependencies=[Depends(require_run_org())],
)
async def get_run_stages(
    run_id: uuid.UUID,
    _: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    rows = (
        await db.execute(
            select(AnalysisStage)
            .where(AnalysisStage.run_id == run_id)
            .order_by(AnalysisStage.position, AnalysisStage.created_at)
        )
    ).scalars().all()
    return [
        {
            "id": str(s.id),
            "position": s.position,
            "name": s.name,
            "language": s.language,
            "status": s.status,
            "items_total": s.items_total,
            "items_done": s.items_done,
            "started_at": s.started_at.isoformat() if s.started_at else None,
            "completed_at": s.completed_at.isoformat() if s.completed_at else None,
            "time_budget_sec": s.time_budget_sec,
            "stats": s.stats,
            "error_log": s.error_log,
        }
        for s in rows
    ]


@router.get(
    "/projects/{project_id}/graph/stats",
    dependencies=[
        Depends(require_project_org()),
        Depends(require_readable_current_graph, scope="function"),
    ],
)
async def graph_stats(
    project_id: uuid.UUID,
    _: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> dict[str, int]:
    """Simple stats used by the Analysis tab."""
    result = await db.execute(
        select(func.count())
        .select_from(Node)
        .where(Node.project_id == project_id, Node.valid_to.is_(None))
    )
    return {"nodes_current": int(result.scalar() or 0)}


@router.get(
    "/projects/{project_id}/graph/search",
    dependencies=[
        Depends(require_project_org()),
        Depends(require_readable_current_graph, scope="function"),
    ],
)
async def graph_search(
    project_id: uuid.UUID,
    _: CurrentUser,
    q: str = "",
    limit: int = 50,
    db: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 200))
    stmt = (
        select(Node)
        .where(Node.project_id == project_id, Node.valid_to.is_(None))
        .order_by(Node.id)
        .limit(limit)
    )
    if q:
        stmt = stmt.where(Node.id.ilike(f"%{q}%"))
    rows = (await db.execute(stmt)).scalars().all()
    overlays = await load_node_human_overlays(
        db, project_id=project_id, node_ids=(row.id for row in rows)
    )
    result: list[dict[str, Any]] = []
    for row in rows:
        view = node_read_view(row, overlays.get(row.id))
        result.append(
            {
                "id": row.id,
                "kind": row.kind,
                "data": view["data"],
                "certainty": view["certainty"],
                "source_certainty": view["source_certainty"],
                "effective_certainty": view["effective_certainty"],
                "confirmed": view["confirmed"],
            }
        )
    return result


@router.get(
    "/projects/{project_id}/graph/component_map",
    dependencies=[
        Depends(require_project_org()),
        Depends(require_readable_current_graph, scope="function"),
    ],
)
async def graph_component_map(
    project_id: uuid.UUID,
    _: CurrentUser,
    kind: str | None = None,
    limit: int = 200,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Light-weight payload for the dashboard's graph visualizer
    (PR-49). Returns up to ``limit`` nodes + the edges between them
    in a shape that fits a force-directed layout straight away:

      {
        "nodes": [{"id", "kind", "label", "certainty", "exercised"}],
        "edges": [{"source", "target", "kind", "certainty", "exercised"}],
      }

    The audit team's biggest "본질 가치" gap was that the platform
    stored 100K+ nodes but never *showed* the graph to a human.
    This endpoint is the data plane behind the new ``/graph`` tab.

    ``kind=Component`` is the canonical first view — operators
    almost always want the high-level component map. ``kind=null``
    returns the whole truncated graph (useful for small projects
    or for the "everything" overview).
    """
    limit = max(10, min(limit, 1000))
    # Rank nodes by graph degree (number of edges touching them) so a
    # truncated view shows the connected hubs, not an arbitrary
    # id-ordered slice that renders as a disconnected dot-cloud (a
    # single-repo Symbol graph put 200 unrelated nodes on screen with
    # one edge). Degree is one pass over the project's edges (cheap),
    # joined onto the node rows; un-connected nodes (degree 0) sort last.
    endpoints = union_all(
        select(Edge.source_id.label("nid")).where(
            Edge.project_id == project_id, Edge.valid_to.is_(None)
        ),
        select(Edge.target_id.label("nid")).where(
            Edge.project_id == project_id, Edge.valid_to.is_(None)
        ),
    ).subquery()
    degree = (
        select(endpoints.c.nid.label("nid"), func.count().label("deg"))
        .group_by(endpoints.c.nid)
        .subquery()
    )
    node_stmt = (
        select(Node)
        .outerjoin(degree, degree.c.nid == Node.id)
        .where(Node.project_id == project_id, Node.valid_to.is_(None))
    )
    if kind:
        node_stmt = node_stmt.where(Node.kind == kind)
    node_stmt = node_stmt.order_by(
        func.coalesce(degree.c.deg, 0).desc(), Node.id
    ).limit(limit)
    nodes = (await db.execute(node_stmt)).scalars().all()
    node_ids = {n.id for n in nodes}

    # Only edges where BOTH endpoints are inside the truncated
    # node set — otherwise the visualizer would draw arrows into
    # void.
    edge_rows = (
        await db.execute(
            select(Edge).where(
                Edge.project_id == project_id,
                Edge.valid_to.is_(None),
                Edge.source_id.in_(node_ids),
                Edge.target_id.in_(node_ids),
            )
        )
    ).scalars().all()
    node_overlays = await load_node_human_overlays(
        db, project_id=project_id, node_ids=node_ids
    )
    edge_overlays = await load_edge_overlays(
        db,
        project_id=project_id,
        identities=(edge_identity(edge) for edge in edge_rows),
    )
    node_views = {
        node.id: node_read_view(node, node_overlays.get(node.id)) for node in nodes
    }
    edge_views = {
        (edge.id, edge.valid_from): edge_read_view(
            edge,
            edge_overlays.human.get(edge_identity(edge)),
            edge_overlays.runtime.get(edge_identity(edge)),
        )
        for edge in edge_rows
    }

    def _label(n: Node) -> str:
        data = n.data or {}
        return str(data.get("name") or data.get("title") or n.id)

    def _exercised(data: dict) -> bool:
        # PR-25 OTLP Tier 2 marks live edges/nodes with this flag.
        return str((data or {}).get("exercised", "")).lower() == "true"

    # PR-196 — the reconcile marks CALLS *edges* exercised, not nodes, so a
    # node-only check showed every symbol as dead. A symbol is exercised when
    # its own flag is set OR any incident (visible) edge is exercised —
    # consistent with findings._subject_is_exercised. Derived from edge_rows
    # (no extra query); reflects the exercised edges actually drawn.
    exercised_node_ids: set[str] = set()
    for e in edge_rows:
        if edge_views[(e.id, e.valid_from)]["exercised"]:
            exercised_node_ids.add(e.source_id)
            exercised_node_ids.add(e.target_id)

    return {
        "nodes": [
            {
                "id": n.id,
                "kind": n.kind,
                "label": _label(n),
                "certainty": node_views[n.id]["certainty"],
                "source_certainty": node_views[n.id]["source_certainty"],
                "effective_certainty": node_views[n.id]["effective_certainty"],
                "exercised": _exercised(node_views[n.id]["data"])
                or n.id in exercised_node_ids,
                "confirmed": _confirmation_action(n.data or {})
                or node_views[n.id]["confirmed"],
            }
            for n in nodes
        ],
        "edges": [
            {
                "id": str(e.id),
                "source": e.source_id,
                "target": e.target_id,
                "kind": e.kind,
                "certainty": edge_views[(e.id, e.valid_from)]["certainty"],
                "source_certainty": edge_views[(e.id, e.valid_from)][
                    "source_certainty"
                ],
                "effective_certainty": edge_views[(e.id, e.valid_from)][
                    "effective_certainty"
                ],
                "exercised": edge_views[(e.id, e.valid_from)]["exercised"],
                "confirmed": _confirmation_action(e.data or {})
                or edge_views[(e.id, e.valid_from)]["confirmed"],
            }
            for e in edge_rows
        ],
        "truncated": len(nodes) >= limit,
    }


def _confirmation_action(data: dict) -> str | None:
    """Return ``"confirm"`` / ``"dispute"`` if a human has weighed in
    on this fact (PR-67, spec §1.2), else None."""
    hc = (data or {}).get("human_confirmation")
    return hc.get("action") if isinstance(hc, dict) else None


def _strengthened_certainty(action: str, current: str) -> str:
    """§1.2 + §2.3 — a human confirmation is one trustworthy source
    asserting the fact, so an ``inferred`` fact rises to ``asserted``.

    It never reaches ``verified``: that rung is reserved for the
    system observing the fact directly (a runtime trace, the DB
    catalogue). A dispute, or an already-asserted/verified fact,
    leaves the certainty untouched.
    """
    return effective_certainty(current, action if action in {"confirm", "dispute"} else None)


class ConfirmFactBody(BaseModel):
    target_kind: Literal["node", "edge"]
    target_id: str = Field(min_length=1, max_length=512)
    action: Literal["confirm", "dispute"]
    rationale: str = Field(min_length=10, max_length=2000)


@router.post(
    "/projects/{project_id}/graph/confirm_fact",
    dependencies=[
        Depends(require_project_org()),
        Depends(require_operator),
    ],
)
async def confirm_fact(
    project_id: uuid.UUID,
    body: ConfirmFactBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Record human evidence without rewriting analyzer-owned certainty.

    This mutation deliberately does not use the read-route snapshot guard:
    committing a confirmation advances ``overlay_generation``, so revalidating
    a pre-mutation stamp during dependency teardown would turn every successful
    write into a false retryable 409. The helper itself locks the graph head
    against promotion, re-reads the
    logical current fact, and stores a durable ``"human_confirmation"``
    overlay.  It also materializes that overlay into the exact current
    bitemporal row (the equivalent safety predicate is
    ``Edge.valid_from == row.valid_from``) for legacy readers.  The source
    certainty remains immutable; the response exposes a separate effective
    certainty.
    """
    if body.target_kind == "edge":
        try:
            uuid.UUID(body.target_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="bad_edge_id")
    try:
        result = await record_human_confirmation(
            db,
            project_id=project_id,
            target_kind=body.target_kind,
            target_id=body.target_id,
            action=body.action,
            actor=f"user:{user.id}",
            rationale=body.rationale,
        )
    except GraphOverlayUnavailable as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail="graph_not_ready") from exc
    except GraphOverlayFactNotFound as exc:
        await db.rollback()
        raise HTTPException(status_code=404, detail="fact_not_found") from exc
    await db.commit()

    await audit_record(
        actor=f"user:{user.id}",
        action="graph.fact_confirmed",
        target=f"{body.target_kind}:{body.target_id}",
        project_id=project_id,
        details={
            "action": body.action,
            "rationale": body.rationale,
            "source_certainty": result.source_certainty,
            "effective_certainty": result.effective_certainty,
        },
    )
    return {
        "target_kind": body.target_kind,
        "target_id": body.target_id,
        "action": body.action,
        "certainty_before": result.source_certainty,
        "certainty_after": result.effective_certainty,
        "source_certainty": result.source_certainty,
        "effective_certainty": result.effective_certainty,
    }


@router.get(
    "/projects/{project_id}/graph/certainty_breakdown",
    dependencies=[
        Depends(require_project_org()),
        Depends(require_readable_current_graph, scope="function"),
    ],
)
async def graph_certainty_breakdown(
    project_id: uuid.UUID,
    _: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> dict[str, dict[str, int]]:
    """Coverage metric (audit C3 — "how trustworthy is our graph?").

    Returns the count of nodes + edges per certainty value
    (``verified`` / ``asserted`` / ``inferred``), so the dashboard
    can show "12% of edges are still inferred — push more
    analyzers to lift confidence" instead of a single opaque
    "1 234 nodes" number.
    """
    node_rows = await db.execute(
        select(Node.certainty, func.count(Node.id))
        .where(Node.project_id == project_id, Node.valid_to.is_(None))
        .group_by(Node.certainty)
    )
    edge_rows = await db.execute(
        select(Edge.certainty, func.count(Edge.id))
        .where(Edge.project_id == project_id, Edge.valid_to.is_(None))
        .group_by(Edge.certainty)
    )
    return {
        "nodes": {c: int(n) for c, n in node_rows.all()},
        "edges": {c: int(n) for c, n in edge_rows.all()},
    }


@router.get(
    "/projects/{project_id}/pipeline_latency",
    dependencies=[Depends(require_project_org())],
)
async def pipeline_latency(
    project_id: uuid.UUID,
    _: CurrentUser,
    runs: int = 10,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Per-stage latency for the project's recent runs (PR-58,
    audit A8).

    The value audit's A8 finding: "Always-on latency 미측정 —
    각 stage 타이밍은 analysis_stages 에 기록되지만 운영자에게
    가시화 안 됨". Spec §1.5 promises "first full analysis in
    ≤ 8 hours" — but an operator had no way to *see* where the
    time goes.

    Returns:

      {
        "runs_analysed": int,
        "stages": [
          {"name": str,
           "mean_sec": float,
           "max_sec": float,
           "p95_sec": float,
           "samples": int},
          …
        ],
        "mean_total_sec": float | None,   # webhook→done wall clock
        "slowest_stage": str | None,
      }

    The slowest-stage callout tells an operator which analyzer to
    tune first.
    """
    # Most-recent N completed runs for the project.
    recent_runs = (
        await db.execute(
            select(AnalysisRun.id, AnalysisRun.started_at, AnalysisRun.completed_at)
            .where(AnalysisRun.project_id == project_id)
            .order_by(AnalysisRun.created_at.desc())
            .limit(max(1, min(runs, 100)))
        )
    ).all()
    run_ids = [r[0] for r in recent_runs]

    total_durations: list[float] = []
    for _id, started, completed in recent_runs:
        if started is not None and completed is not None:
            secs = (completed - started).total_seconds()
            if secs >= 0:
                total_durations.append(secs)

    per_stage: dict[str, list[float]] = {}
    if run_ids:
        stage_rows = (
            await db.execute(
                select(
                    AnalysisStage.name,
                    AnalysisStage.started_at,
                    AnalysisStage.completed_at,
                ).where(AnalysisStage.run_id.in_(run_ids))
            )
        ).all()
        for name, started, completed in stage_rows:
            if started is None or completed is None:
                continue
            secs = (completed - started).total_seconds()
            if secs < 0:
                continue
            per_stage.setdefault(name, []).append(secs)

    def _p95(values: list[float]) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        idx = min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))
        return round(ordered[idx], 2)

    stages = []
    for name, vals in sorted(
        per_stage.items(), key=lambda kv: -sum(kv[1]) / max(1, len(kv[1]))
    ):
        stages.append(
            {
                "name": name,
                "mean_sec": round(sum(vals) / len(vals), 2),
                "max_sec": round(max(vals), 2),
                "p95_sec": _p95(vals),
                "samples": len(vals),
            }
        )

    return {
        "runs_analysed": len(recent_runs),
        "stages": stages,
        "mean_total_sec": (
            round(sum(total_durations) / len(total_durations), 1)
            if total_durations
            else None
        ),
        "slowest_stage": stages[0]["name"] if stages else None,
    }


@router.get(
    "/projects/{project_id}/summaries",
    dependencies=[
        Depends(require_project_org()),
        Depends(require_readable_current_graph, scope="function"),
    ],
)
async def list_summaries(
    project_id: uuid.UUID,
    _: CurrentUser,
    level: int | None = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 50,
    offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
    db: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    """Hierarchical L1-L3 summaries and canonical L4 flows for the project.

    The value audit's C4 gap: L1-L3 summaries were *produced* by
    the analysis pipeline but never *surfaced* — no API, no GUI.
    The executive-report page reads ``?level=3`` for the
    system-level narrative; ``?level=`` (all) drives a drill-down.

    Only unsuperseded summaries (``superseded_by IS NULL``) are returned.
    Their stored claims are revalidated against the current project graph;
    prose whose evidence disappeared remains visible but is marked ``stale``.
    L4 source-window-bounded hypotheses expose ``flow``/``sections`` separately
    from graph-grounded ``claims`` and are explicitly marked ``hypothesis``;
    malformed persisted flow contracts are marked ``invalid`` with a safe
    path/type diagnostic.
    """
    from app.models.findings import Summary

    page_limit = max(1, min(int(limit), 50))
    page_offset = max(0, min(int(offset), 1_000_000))
    stmt = (
        select(Summary)
        .join(GraphHead, GraphHead.project_id == Summary.project_id)
        .where(
            Summary.project_id == project_id,
            Summary.superseded_by.is_(None),
            GraphHead.state == GRAPH_HEAD_READY,
            Summary.validated_graph_generation == GraphHead.generation,
            Summary.validated_overlay_generation == GraphHead.overlay_generation,
        )
        .order_by(
            Summary.level.desc(),
            Summary.generated_at.desc(),
            Summary.id.asc(),
        )
        .offset(page_offset)
        .limit(page_limit)
    )
    if level is not None:
        stmt = stmt.where(Summary.level == level)
    rows = (await db.execute(stmt)).scalars().all()
    claim_views = await current_summary_claim_views(
        db,
        project_id=project_id,
        summaries=list(rows),
    )

    result: list[dict[str, Any]] = []
    for summary, claim_view in zip(rows, claim_views, strict=True):
        result.append(
            {
                "id": str(summary.id),
                "target_id": summary.target_id,
                "level": summary.level,
                "summary": summary.summary,
                "detailed": summary.detailed,
                "claims": claim_view.claims,
                "flow": claim_view.flow,
                "source_snapshot": claim_view.source_snapshot,
                "sections": claim_view.sections or [],
                "contract_error": claim_view.contract_error,
                "open_questions": summary.open_questions or [],
                "model_used": summary.model_used,
                "fallback_reason": summary.fallback_reason,
                "grounding_status": claim_view.grounding_status,
                "narrative_certainty": "inferred",
                "analysis_run_id": (
                    str(summary.analysis_run_id)
                    if summary.analysis_run_id
                    else None
                ),
                "tokens_used": summary.tokens_used,
                "generated_at": (
                    summary.generated_at.isoformat()
                    if summary.generated_at
                    else None
                ),
            }
        )
    return result


@router.get(
    "/projects/{project_id}/llm_cost",
    dependencies=[
        Depends(require_project_org()),
        Depends(require_readable_current_graph, scope="function"),
    ],
)
async def project_llm_cost(
    project_id: uuid.UUID,
    _: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """LLM token spend for the project (PR-53, audit C5).

    Physical calls (including map/reduce partials and rejected model output)
    live in ``LLMCall``; Summary rows are products and would undercount them.
    This applies a coarse per-million-token rate. The rate is configurable via
    ``MNEMOS_LLM_USD_PER_MTOK`` (default 3.0, ~Sonnet input).
    """
    from app.extractor.cost import rate_usd_per_mtok
    from app.models.findings import LLMCall, Summary

    physical_call_count, total_tokens, unknown_token_calls = (
        await db.execute(
            select(
                func.count(LLMCall.id),
                func.coalesce(func.sum(LLMCall.tokens_used), 0),
                func.coalesce(
                    func.sum(case((LLMCall.tokens_used.is_(None), 1), else_=0)),
                    0,
                ),
            ).where(LLMCall.project_id == project_id)
        )
    ).one()
    summary_count = int(
        (
            await db.execute(
                select(func.count())
                .select_from(Summary)
                .join(GraphHead, GraphHead.project_id == Summary.project_id)
                .where(
                    Summary.project_id == project_id,
                    Summary.superseded_by.is_(None),
                    GraphHead.state == GRAPH_HEAD_READY,
                    Summary.validated_graph_generation == GraphHead.generation,
                    Summary.validated_overlay_generation
                    == GraphHead.overlay_generation,
                )
            )
        ).scalar_one()
    )
    physical_call_count = int(physical_call_count or 0)
    total_tokens = int(total_tokens or 0)
    unknown_token_calls = int(unknown_token_calls or 0)
    rate = rate_usd_per_mtok()
    est_usd = round((total_tokens / 1_000_000.0) * rate, 4)
    return {
        "summary_count": summary_count,
        "physical_call_count": physical_call_count,
        "unknown_token_calls": unknown_token_calls,
        "total_tokens": total_tokens,
        "rate_usd_per_mtok": rate,
        "estimated_usd": est_usd,
    }


@router.get(
    "/projects/{project_id}/llm_fallback_breakdown",
    dependencies=[
        Depends(require_project_org()),
        Depends(require_readable_current_graph, scope="function"),
    ],
)
async def project_llm_fallback_breakdown(
    project_id: uuid.UUID,
    _: CurrentUser,
    days: int | None = None,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """PR-138b — per-project breakdown of "why this summary missed the
    LLM". Pre-138 every fallback collapsed into ``model_used="stub"``.
    PR-138 stamped the reason into ``model_used`` and PR-138b adds the
    structured ``Summary.fallback_reason`` column. This endpoint
    aggregates those so the dashboard "Operational health" card can
    surface (a) how many summaries used the real LLM vs stub, and
    (b) WHY each fallback happened, without parsing model_used strings.

    Returns ``{ok: int, fallbacks: {reason: count}, total: int,
    fallback_rate: float, window_days: int | null}`` — the operator's
    "is the pipeline actually working?" answer at a glance.

    PR-138g — ``?days=N`` filters to the rolling window so an alert
    rule can ask "fallbacks in the last 7 days" instead of all-time
    (otherwise old burst events keep dominating the rate forever).
    Bounded 1–365; absent means all-time (back-compat).
    """
    from datetime import datetime, timedelta, timezone

    from app.models.findings import Summary

    where_clauses = [
        Summary.project_id == project_id,
        Summary.superseded_by.is_(None),
        GraphHead.state == GRAPH_HEAD_READY,
        Summary.validated_graph_generation == GraphHead.generation,
        Summary.validated_overlay_generation == GraphHead.overlay_generation,
    ]
    window_days: int | None = None
    if days is not None:
        window_days = max(1, min(int(days), 365))
        since = datetime.now(tz=timezone.utc) - timedelta(days=window_days)
        where_clauses.append(Summary.generated_at >= since)

    rows = (
        await db.execute(
            select(
                Summary.fallback_reason,
                func.count().label("n"),
            )
            .join(GraphHead, GraphHead.project_id == Summary.project_id)
            .where(*where_clauses)
            .group_by(Summary.fallback_reason)
        )
    ).all()
    ok = 0
    fallbacks: dict[str, int] = {}
    for reason, n in rows:
        n = int(n)
        if not reason:
            ok += n
        else:
            fallbacks[reason] = fallbacks.get(reason, 0) + n
    total = ok + sum(fallbacks.values())
    return {
        "ok": ok,
        "fallbacks": fallbacks,
        "total": total,
        "fallback_rate": (
            round(sum(fallbacks.values()) / total, 3) if total else 0.0
        ),
        "window_days": window_days,
    }
