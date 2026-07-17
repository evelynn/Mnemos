"""Cross-tier flow analysis bound to one immutable analysis snapshot.

Both flow endpoints accept only project-relative paths and read them from
the exact Git commit retained by a completed analysis run.  Request-supplied
host paths are never opened.  Persisted flow summaries carry that run and
revision as provenance so a later checkout cannot silently change their
evidence.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import case, literal, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.logger import record as audit_record
from app.auth.deps import CurrentUser
from app.auth.org_scope import require_project_org
from app.auth.rbac import require_operator
import app.db as app_db
from app.db import get_session
from app.api.graph_guard import require_readable_current_graph
from app.extractor.agent_flow import (
    FLOW_LEVEL,
    FLOW_RESULT_CONTRACT,
    FlowAttemptState,
    FlowContractError,
    analyze_flow_via_agent_sdk,
    build_flow_summary_content,
    flow_candidate_binding_fingerprint,
    is_agent_sdk_available,
    normalize_flow_payload,
)
from app.extractor.cost import (
    BudgetExceeded,
    LLMRunBudget,
    RunBudgetExceeded,
)
from app.graph_publication import GraphPublicationError
from app.models.findings import Summary
from app.llm.contracts import LLMPurpose, ResultStatus
from app.llm.lifecycle import (
    AttemptCallbacks,
    AttemptOutcome,
    AttemptReplayResult,
    AttemptStartMetadata,
    AttemptTicket,
    BudgetScopeKind,
    LLMLifecycleError,
    SemanticCandidate,
    SemanticCandidateReplay,
    SemanticCandidateReplayRequest,
    begin_attempt,
    classify_attempt_result,
    finalize_attempt,
    fingerprint_input,
    load_attempt_replay,
    load_terminal_semantic_candidate,
    lock_semantic_candidate_for_product,
    use_attempt_callbacks,
)
from app.models.graph import (
    ANALYSIS_RUN_SOURCE_PUBLISHED_STATUSES,
    AnalysisRun,
    Edge,
    Node,
)
from app.mcp.file_read import read_project_file
from app.source_snapshot import (
    GitSourceSnapshot,
    SourceSnapshotError,
    normalize_project_path,
    revalidate_current_source_snapshot,
    resolve_git_source_snapshot,
    snapshot_error_fields,
)
from app.summary_freshness import lock_ready_summary_generation

router = APIRouter(
    prefix="/api/v1/projects/{project_id}",
    tags=["flow"],
    dependencies=[
        Depends(require_project_org()),
        Depends(require_readable_current_graph, scope="function"),
    ],
)

# Path/extension → (tier, language) heuristics. The tier hint in the path
# wins (frontend/backend/db dirs); otherwise the extension decides.
_FRONTEND_EXT = {".js", ".jsx", ".ts", ".tsx", ".vue", ".svelte"}
_DB_EXT = {".sql", ".ddl"}
_LANG_BY_EXT = {
    ".py": "python", ".cs": "csharp", ".java": "java", ".go": "go",
    ".rb": "ruby", ".php": "php", ".rs": "rust", ".ts": "typescript",
    ".tsx": "typescript", ".js": "javascript", ".jsx": "javascript",
    ".sql": "sql", ".ddl": "sql", ".cpp": "cpp", ".cc": "cpp", ".h": "cpp",
}


def _classify(path: Path) -> tuple[str, str]:
    lowered = {p.lower() for p in path.parts}
    ext = path.suffix.lower()
    lang = _LANG_BY_EXT.get(ext, "unknown")
    if ext in _DB_EXT or {"db", "database", "sql", "schema", "migrations"} & lowered:
        return "database", "sql"
    if ext in _FRONTEND_EXT or {"frontend", "front", "ui", "client", "web"} & lowered:
        return "frontend", lang
    return "backend", lang


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:60] or "flow"


class TraceFlowRequest(BaseModel):
    entry: str = Field(min_length=1, max_length=300, description="Process to trace")
    source_paths: list[str] = Field(
        min_length=1, max_length=20,
        description=(
            "Project-relative FE/BE/DB paths in the selected completed analysis "
            "snapshot; absolute and parent-traversal paths are rejected"
        ),
    )
    analysis_run_id: uuid.UUID | None = Field(
        default=None,
        description="Completed analysis run to use; defaults to the latest completed run",
    )
    persist: bool = Field(default=True)
    max_file_bytes: int = Field(default=60_000, ge=1, le=400_000)


def _source_snapshot_http_error(exc: SourceSnapshotError) -> HTTPException:
    """Expose an actionable provenance error without a server filesystem path."""

    return HTTPException(status_code=409, detail=snapshot_error_fields(exc))


def _truncate_utf8(text: str, max_bytes: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


async def _load_snapshot_sources(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    snapshot: GitSourceSnapshot,
    project_paths: list[str],
    max_file_bytes: int,
) -> tuple[list[dict], list[str]]:
    """Read bounded UTF-8 source windows from one pinned Git revision.

    The shared snapshot reader performs path normalization, Git object
    resolution, streaming limits, binary detection, and revision checks.  We
    normalize first so an absolute path never reaches any filesystem API.
    """

    sources: list[dict] = []
    skipped: list[str] = []
    seen_paths: set[str] = set()
    for index, raw in enumerate(project_paths):
        try:
            project_path = normalize_project_path(raw)
        except SourceSnapshotError:
            skipped.append(f"input[{index}]:invalid_project_path")
            continue
        if project_path in seen_paths:
            skipped.append(f"{project_path}:duplicate_source_path")
            continue
        seen_paths.add(project_path)

        window = await read_project_file(
            db,
            project_id=project_id,
            file_path=project_path,
            run_id=snapshot.run_id,
        )
        if (
            window.get("error")
            or window.get("encoding") != "utf-8"
            or window.get("run_id") != str(snapshot.run_id)
            or not str(window.get("revision") or "").startswith(
                snapshot.revision.lower()
            )
        ):
            skipped.append(project_path)
            continue
        raw_content = str(window.get("content") or "")
        code = _truncate_utf8(raw_content, max_file_bytes)
        tier, lang = _classify(Path(project_path))
        sources.append(
            {
                "tier": tier,
                "language": lang,
                "label": project_path,
                "code": code,
                "truncated": bool(window.get("truncated")) or code != raw_content,
            }
        )
    return sources, skipped


@router.post("/trace_flow", dependencies=[Depends(require_operator)])
async def trace_flow(
    project_id: uuid.UUID,
    body: TraceFlowRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> dict:
    if not is_agent_sdk_available():
        raise HTTPException(status_code=503, detail="agent_sdk_unavailable")

    try:
        snapshot = await resolve_git_source_snapshot(
            db,
            project_id=project_id,
            run_id=body.analysis_run_id,
        )
    except SourceSnapshotError as exc:
        raise _source_snapshot_http_error(exc) from exc
    sources, skipped = await _load_snapshot_sources(
        db,
        project_id=project_id,
        snapshot=snapshot,
        project_paths=body.source_paths,
        max_file_bytes=body.max_file_bytes,
    )

    if not sources:
        raise HTTPException(
            status_code=400,
            detail="no_readable_source_paths_in_analysis_snapshot",
        )

    return await _analyze_and_persist(
        db,
        project_id,
        user,
        body.entry,
        sources,
        body.persist,
        skipped,
        snapshot,
    )


async def _gather_files_from_graph(
    db: AsyncSession, project_id: uuid.UUID, entry: str, max_files: int
) -> list[str]:
    """Resolve an entry to the set of relative source files involved in the
    flow, using the knowledge graph: symbols whose name/id/signature match
    the entry (any tier — so a query like "place order" pulls the frontend
    ``placeOrder`` and the backend ``handle_create_order`` alike), plus the
    nodes one CALLS/READS/WRITES hop away (which reaches the DataEntity =
    SQL-schema files via data-access edges). Returns distinct project-relative
    ``data.location.file`` values, newest/most-relevant first, capped at
    ``max_files``. ``data.file`` remains a legacy fallback only."""
    terms = [t for t in re.split(r"[^a-z0-9]+", entry.lower()) if len(t) >= 3]
    if not terms:
        return []
    term_matches = []
    for t in terms:
        like = f"%{t}%"
        term_matches.append(
            or_(
                Node.data["name"].astext.ilike(like),
                Node.data["id"].astext.ilike(like),
                Node.data["signature"].astext.ilike(like),
            )
        )
    relevance = sum(
        (case((term_match, 1), else_=0) for term_match in term_matches),
        start=literal(0),
    )
    seeds = (
        await db.execute(
            select(Node)
            .where(
                Node.project_id == project_id,
                Node.valid_to.is_(None),
                or_(*term_matches),
            )
            .order_by(relevance.desc(), Node.id.asc())
            .limit(20)
        )
    ).scalars().all()
    seed_ids = [n.id for n in seeds]
    node_by_id = {n.id: n for n in seeds}

    # One hop out along any edge (CALLS / CONTAINS / READS / WRITES) so the
    # tables a handler touches (and its immediate callers/callees) come too.
    if seed_ids:
        edges = (
            await db.execute(
                select(Edge).where(
                    Edge.project_id == project_id,
                    Edge.valid_to.is_(None),
                    or_(Edge.source_id.in_(seed_ids), Edge.target_id.in_(seed_ids)),
                )
                .order_by(
                    Edge.kind.asc(),
                    Edge.source_id.asc(),
                    Edge.target_id.asc(),
                    Edge.id.asc(),
                )
                .limit(200)
            )
        ).scalars().all()
        neighbour_ids = {e.source_id for e in edges} | {e.target_id for e in edges}
        neighbour_ids -= set(seed_ids)
        if neighbour_ids:
            extra = (
                await db.execute(
                    select(Node).where(
                        Node.project_id == project_id,
                        Node.valid_to.is_(None),
                        Node.id.in_(neighbour_ids),
                    )
                    .order_by(Node.id.asc())
                )
            ).scalars().all()
            for n in extra:
                node_by_id.setdefault(n.id, n)

    files: list[str] = []
    for n in node_by_id.values():
        data = n.data or {}
        location = data.get("location") if isinstance(data.get("location"), dict) else {}
        f = location.get("file") or data.get("file")
        if f and f not in files:
            files.append(f)
    return files[:max_files]


async def _analyze_and_persist(
    db: AsyncSession,
    project_id: uuid.UUID,
    user,  # noqa: ANN001
    entry: str,
    sources: list[dict],
    persist: bool,
    skipped: list[str],
    snapshot: GitSourceSnapshot,
) -> dict:
    run_budget = LLMRunBudget.from_env()
    attempt_state = FlowAttemptState()

    async def _start_attempt(
        metadata: AttemptStartMetadata,
    ) -> AttemptTicket:
        return await begin_attempt(
            db,
            project_id=project_id,
            # The immutable source run remains on the Summary product. This
            # user-triggered provider budget is a fresh request scope.
            analysis_run_id=None,
            purpose=LLMPurpose.FLOW,
            target_id=f"flow:{_slug(entry)}",
            level=FLOW_LEVEL,
            run_budget=run_budget,
            scope_kind=BudgetScopeKind.REQUEST,
            metadata=metadata,
            pre_reserved=None,
            require_atomic_dollar_reservation=True,
        )

    async def _finish_attempt(
        ticket: AttemptTicket,
        outcome: AttemptOutcome,
    ) -> None:
        await finalize_attempt(db, ticket=ticket, outcome=outcome)

    async def _finish_candidate(
        ticket: AttemptTicket,
        outcome: AttemptOutcome,
        candidate: SemanticCandidate,
    ) -> None:
        await finalize_attempt(
            db,
            ticket=ticket,
            outcome=outcome,
            candidate=candidate,
        )

    async def _replay_candidate(
        request: SemanticCandidateReplayRequest,
    ) -> SemanticCandidateReplay:
        # Snapshot/source reads on the request session may already own a
        # transaction. Candidate replay is a read-only exact-identity lookup
        # and deliberately uses a fresh accounting session.
        async with app_db.SessionLocal() as accounting_db:
            return await load_terminal_semantic_candidate(
                accounting_db,
                operation_id=request.operation_id,
                attempt_no=request.attempt_no,
                project_id=project_id,
                purpose=LLMPurpose.FLOW,
                input_fingerprint=request.input_fingerprint,
                contract_name=request.contract_name,
                binding_fingerprint=request.binding_fingerprint,
                normalizer=request.normalizer,
            )

    async def _replay_attempt(
        request: SemanticCandidateReplayRequest,
    ) -> AttemptReplayResult:
        async with app_db.SessionLocal() as accounting_db:
            return await load_attempt_replay(
                accounting_db,
                operation_id=request.operation_id,
                attempt_no=request.attempt_no,
                project_id=project_id,
                purpose=LLMPurpose.FLOW,
                input_fingerprint=request.input_fingerprint,
                contract_name=request.contract_name,
                binding_fingerprint=request.binding_fingerprint,
                normalizer=request.normalizer,
            )

    callbacks = AttemptCallbacks(
        start=_start_attempt,
        finish=_finish_attempt,
        finish_candidate=_finish_candidate,
        replay_candidate=_replay_candidate,
        replay_attempt=_replay_attempt,
        mark_provider_dispatch=lambda: None,
    )

    visibility_identity = (
        "current"
        f":{snapshot.graph_stamp.generation}"
        f":{snapshot.graph_stamp.overlay_generation}"
        if snapshot.graph_stamp is not None
        else "historical"
    )
    operation_namespace = uuid.uuid5(
        project_id,
        "\x00".join(
            (
                LLMPurpose.FLOW.value,
                str(snapshot.run_id),
                snapshot.revision,
                visibility_identity,
                f"flow:{_slug(entry)}",
            )
        ),
    )
    semantic_binding_identity = fingerprint_input(
        "mnemos.flow.consumer.v2",
        str(project_id),
        str(snapshot.run_id),
        snapshot.revision,
        visibility_identity,
        entry,
    )

    try:
        async with use_attempt_callbacks(callbacks):
            envelope = await analyze_flow_via_agent_sdk(
                entry=entry,
                sources=sources,
                run_budget=run_budget,
                attempt_state=attempt_state,
                operation_id=operation_namespace,
                semantic_binding_identity=semantic_binding_identity,
            )
    except BudgetExceeded as exc:
        raise HTTPException(status_code=429, detail="flow_budget_exceeded") from exc
    except RunBudgetExceeded as exc:
        raise HTTPException(status_code=429, detail=exc.reason) from exc
    except LLMLifecycleError as exc:
        raise HTTPException(status_code=503, detail="flow_accounting_failed") from exc

    async def _reject_invalid_pending_candidate(failure_code: str) -> None:
        binding = attempt_state.candidate_binding_fingerprint
        attempt_id = attempt_state.attempt_id
        if (
            attempt_state.result_status != ResultStatus.PENDING
            or not isinstance(attempt_id, uuid.UUID)
            or not isinstance(binding, str)
        ):
            return
        try:
            await lock_semantic_candidate_for_product(
                db,
                attempt_id=attempt_id,
                project_id=project_id,
                purpose=LLMPurpose.FLOW,
                contract_name=FLOW_RESULT_CONTRACT,
                binding_fingerprint=binding,
                normalizer=normalize_flow_payload,
            )
            await classify_attempt_result(
                db,
                attempt_id=attempt_id,
                result_status=ResultStatus.SCHEMA_REJECTED,
                failure_code=failure_code,
            )
        except LLMLifecycleError as exc:
            await db.rollback()
            raise HTTPException(
                status_code=503,
                detail="flow_accounting_failed",
            ) from exc

    if not isinstance(envelope, dict) or set(envelope) != {"flow", "source_scope"}:
        await _reject_invalid_pending_candidate("flow_envelope_invalid")
        raise HTTPException(status_code=502, detail="flow_analysis_failed")
    flow = envelope.get("flow")
    if not isinstance(flow, dict) or flow.get("contract") != FLOW_RESULT_CONTRACT:
        await _reject_invalid_pending_candidate("flow_envelope_invalid")
        raise HTTPException(status_code=502, detail="flow_analysis_failed")
    source_scope = envelope.get("source_scope")
    scope_files = source_scope.get("files") if isinstance(source_scope, dict) else None
    if not isinstance(scope_files, list) or not scope_files:
        await _reject_invalid_pending_candidate("flow_source_scope_invalid")
        raise HTTPException(status_code=502, detail="flow_source_scope_missing")
    included_labels = [
        item.get("label")
        for item in scope_files
        if isinstance(item, dict) and isinstance(item.get("label"), str)
    ]
    source_by_label = {source["label"]: source for source in sources}
    if (
        len(included_labels) != len(scope_files)
        or len(set(included_labels)) != len(included_labels)
        or any(label not in source_by_label for label in included_labels)
    ):
        await _reject_invalid_pending_candidate("flow_source_scope_invalid")
        raise HTTPException(status_code=502, detail="flow_source_scope_invalid")
    analyzed_sources = [source_by_label[label] for label in included_labels]
    omitted_scope = source_scope.get("omitted_files")
    omitted_scope = omitted_scope if isinstance(omitted_scope, list) else []
    attempt_id = attempt_state.attempt_id
    if attempt_id is None:
        raise HTTPException(status_code=503, detail="flow_accounting_failed")
    candidate_binding_fingerprint = flow_candidate_binding_fingerprint(
        semantic_binding_identity,
        source_scope,
    )

    async def _lock_exact_candidate():  # noqa: ANN202
        replay = await lock_semantic_candidate_for_product(
            db,
            attempt_id=attempt_id,
            project_id=project_id,
            purpose=LLMPurpose.FLOW,
            contract_name=FLOW_RESULT_CONTRACT,
            binding_fingerprint=candidate_binding_fingerprint,
            normalizer=normalize_flow_payload,
        )
        if (
            replay.candidate_status not in {"candidate", "accepted"}
            or replay.result_status
            not in {ResultStatus.PENDING, ResultStatus.ACCEPTED}
            or replay.payload != flow
        ):
            raise LLMLifecycleError(
                "flow candidate does not match grounded product input"
            )
        return replay

    try:
        summary_content = build_flow_summary_content(
            flow,
            analysis_run_id=snapshot.run_id,
            revision=snapshot.revision,
            source_scope=source_scope,
        )
    except FlowContractError as exc:
        try:
            await _lock_exact_candidate()
            await classify_attempt_result(
                db,
                attempt_id=attempt_id,
                result_status=ResultStatus.SCHEMA_REJECTED,
                failure_code="flow_product_contract_invalid",
            )
        except LLMLifecycleError:
            await db.rollback()
            raise HTTPException(
                status_code=503, detail="flow_accounting_failed"
            ) from exc
        raise HTTPException(status_code=502, detail="flow_analysis_failed") from exc

    # The provider may run long enough for a new source or overlay generation
    # to publish. Do not authorize its prose against a different current graph.
    try:
        await revalidate_current_source_snapshot(db, snapshot=snapshot)
    except SourceSnapshotError as exc:
        try:
            await _lock_exact_candidate()
            await classify_attempt_result(
                db,
                attempt_id=attempt_id,
                result_status=ResultStatus.GROUNDING_REJECTED,
                failure_code="flow_source_snapshot_changed",
            )
        except LLMLifecycleError:
            await db.rollback()
            raise HTTPException(
                status_code=503, detail="flow_accounting_failed"
            ) from exc
        raise _source_snapshot_http_error(exc) from exc

    summary_id: str | None = None
    evidence_hash = hashlib.sha256(
        json.dumps(
            {
                "analysis_run_id": str(snapshot.run_id),
                "revision": snapshot.revision,
                "entry": entry,
                "files": included_labels,
                "file_windows": scope_files,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    projection = {
        "summary": flow["summary"],
        "detailed": flow["detailed"],
        "claims": summary_content.sections,
        "open_questions": flow["open_questions"],
    }
    projection_sha256 = hashlib.sha256(
        json.dumps(
            projection,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    target_id = f"flow:{_slug(entry)}"
    row_id = uuid.uuid5(attempt_id, "mnemos.flow.summary-product.v1")
    validated_generation: int | None = None
    validated_overlay_generation: int | None = None
    validated_at: datetime | None = None
    current_stamp = snapshot.graph_stamp
    try:
        if persist and current_stamp is not None:
            await lock_ready_summary_generation(
                db,
                project_id=project_id,
                expected_generation=current_stamp.generation,
                expected_overlay_generation=current_stamp.overlay_generation,
            )
            validated_generation = current_stamp.generation
            validated_overlay_generation = current_stamp.overlay_generation
            validated_at = datetime.now(tz=timezone.utc)
        elif persist:
            # Historical products have no GraphHead marker. Serialize all
            # Flow publications for the immutable run so two different
            # provider inputs cannot both remain unsuperseded for one target.
            historical_run = (
                await db.execute(
                    select(AnalysisRun)
                    .where(
                        AnalysisRun.id == snapshot.run_id,
                        AnalysisRun.project_id == project_id,
                        AnalysisRun.status.in_(
                            ANALYSIS_RUN_SOURCE_PUBLISHED_STATUSES
                        ),
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if historical_run is None:
                raise LLMLifecycleError(
                    "historical Flow publication run is unavailable"
                )

        receipt = await _lock_exact_candidate()
        if not persist:
            await classify_attempt_result(
                db,
                attempt_id=attempt_id,
                result_status=ResultStatus.ACCEPTED,
            )
        else:
            existing_rows = (
                await db.execute(
                    select(Summary)
                    .where(
                        or_(
                            Summary.id == row_id,
                            Summary.llm_attempt_id == attempt_id,
                        )
                    )
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalars().all()
            if len(existing_rows) > 1:
                raise LLMLifecycleError(
                    "flow attempt owns multiple Summary products"
                )
            existing = existing_rows[0] if existing_rows else None
            await classify_attempt_result(
                db,
                attempt_id=attempt_id,
                result_status=ResultStatus.ACCEPTED,
                commit=False,
            )
            model_used = receipt.resolved_model or receipt.requested_model
            expected_product = (
                row_id,
                project_id,
                target_id,
                FLOW_LEVEL,
                snapshot.run_id,
                validated_generation,
                validated_overlay_generation,
                projection["summary"],
                projection["detailed"],
                projection["claims"],
                projection["open_questions"],
                evidence_hash,
                model_used,
                receipt.total_tokens,
                None,
                attempt_id,
                candidate_binding_fingerprint,
                projection_sha256,
                None,
            )
            if existing is not None:
                actual_product = (
                    existing.id,
                    existing.project_id,
                    existing.target_id,
                    existing.level,
                    existing.analysis_run_id,
                    existing.validated_graph_generation,
                    existing.validated_overlay_generation,
                    existing.summary,
                    existing.detailed,
                    existing.claims,
                    existing.open_questions,
                    existing.evidence_hash,
                    existing.model_used,
                    existing.tokens_used,
                    existing.fallback_reason,
                    existing.llm_attempt_id,
                    existing.semantic_binding_fingerprint,
                    existing.projection_sha256,
                    existing.superseded_by,
                )
                if actual_product != expected_product:
                    raise LLMLifecycleError(
                        "flow product replay projection changed"
                    )
                await db.commit()
                summary_id = str(existing.id)
            else:
                if current_stamp is not None:
                    supersede_scope = (
                        Summary.validated_graph_generation.is_not(None),
                    )
                else:
                    supersede_scope = (
                        Summary.validated_graph_generation.is_(None),
                        Summary.validated_overlay_generation.is_(None),
                        Summary.analysis_run_id == snapshot.run_id,
                    )
                await db.execute(
                    update(Summary)
                    .where(
                        Summary.project_id == project_id,
                        Summary.target_id == target_id,
                        Summary.level == FLOW_LEVEL,
                        Summary.superseded_by.is_(None),
                        Summary.id != row_id,
                        *supersede_scope,
                    )
                    .values(superseded_by=row_id)
                )
                db.add(
                    Summary(
                        id=row_id,
                        project_id=project_id,
                        target_id=target_id,
                        level=FLOW_LEVEL,
                        analysis_run_id=snapshot.run_id,
                        validated_graph_generation=validated_generation,
                        validated_overlay_generation=validated_overlay_generation,
                        validated_at=validated_at,
                        summary=projection["summary"],
                        detailed=projection["detailed"],
                        claims=projection["claims"],
                        open_questions=projection["open_questions"],
                        evidence_hash=evidence_hash,
                        model_used=model_used,
                        tokens_used=receipt.total_tokens,
                        llm_attempt_id=attempt_id,
                        semantic_binding_fingerprint=(
                            candidate_binding_fingerprint
                        ),
                        projection_sha256=projection_sha256,
                    )
                )
                await db.commit()
                summary_id = str(row_id)
    except GraphPublicationError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail="graph_snapshot_changed_retry",
        ) from exc
    except LLMLifecycleError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=503,
            detail="flow_product_provenance_invalid",
        ) from exc

    await audit_record(
        actor=f"user:{user.id}",
        action="flow.trace",
        target=(
            "entry_sha256:"
            + fingerprint_input("mnemos.audit.flow-entry.v1", entry)
        ),
        project_id=project_id,
        details={
            "entry_chars": len(entry),
            "tiers": sorted({s["tier"] for s in analyzed_sources}),
            "files_provided": len(sources),
            "files_analyzed": len(analyzed_sources),
            "source_scope_truncated": bool(source_scope.get("truncated")),
            "steps": len(flow["steps"]),
            "flags": len(flow["flags"]),
        },
    )
    return {
        "entry": entry,
        "analysis_run_id": str(snapshot.run_id),
        "revision": snapshot.revision,
        "tiers_analyzed": sorted({s["tier"] for s in analyzed_sources}),
        "files_analyzed": included_labels,
        "source_scope": source_scope,
        "skipped_paths": [
            *skipped,
            *[
                f"{item.get('label', '?')}:{item.get('reason', 'source_budget')}"
                for item in omitted_scope
                if isinstance(item, dict)
            ],
        ],
        "summary_id": summary_id,
        "flow": flow,
    }


class TraceFlowAutoRequest(BaseModel):
    entry: str = Field(min_length=1, max_length=300, description="Process to trace")
    source_root: str | None = Field(
        default=None,
        description=(
            "Deprecated and ignored; source is read only from the immutable "
            "completed analysis snapshot"
        ),
    )
    persist: bool = Field(default=True)
    max_files: int = Field(default=8, ge=1, le=30)
    max_file_bytes: int = Field(default=60_000, ge=1, le=400_000)


@router.post("/trace_flow/auto", dependencies=[Depends(require_operator)])
async def trace_flow_auto(
    project_id: uuid.UUID,
    body: TraceFlowAutoRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> dict:
    """Trace a process from just an entry point: the relevant FE/BE/DB
    source files are gathered from the knowledge graph (PR-147), so the
    operator no longer has to know which files implement the flow."""
    if not is_agent_sdk_available():
        raise HTTPException(status_code=503, detail="agent_sdk_unavailable")

    try:
        snapshot = await resolve_git_source_snapshot(
            db,
            project_id=project_id,
        )
    except SourceSnapshotError as exc:
        raise _source_snapshot_http_error(exc) from exc

    rel_files = await _gather_files_from_graph(
        db, project_id, body.entry, body.max_files
    )
    if not rel_files:
        raise HTTPException(status_code=404, detail="no_graph_files_for_entry")

    sources, skipped = await _load_snapshot_sources(
        db,
        project_id=project_id,
        snapshot=snapshot,
        project_paths=rel_files,
        max_file_bytes=body.max_file_bytes,
    )

    if not sources:
        raise HTTPException(
            status_code=400,
            detail="no_readable_source_paths_in_analysis_snapshot",
        )

    result = await _analyze_and_persist(
        db,
        project_id,
        user,
        body.entry,
        sources,
        body.persist,
        skipped,
        snapshot,
    )
    result["auto_collected_files"] = rel_files
    return result
