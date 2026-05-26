"""PR-113 — in-tree docs surfaced via the dashboard.

Mnemos ships ~10 docs in ``docs/`` covering architecture, analyzer
contracts, operator guides, etc. Pre-PR, an operator had to ``cat``
them from the container filesystem or know the GitHub URL. spec §2.9
("every feature has a GUI") was being violated for documentation
itself.

This API exposes:
- ``GET /api/v1/docs``      — list of available docs with titles
- ``GET /api/v1/docs/{slug}`` — the markdown body of one doc

The doc list is built at startup from a static allowlist (we don't
walk the filesystem at request time — that would let an operator
``GET /api/v1/docs/../../etc/passwd``). The body is served verbatim
as text/markdown; rendering is the GUI's job.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse

router = APIRouter(tags=["docs"])

# Resolve once at import — guarantees we never serve from outside
# this directory, no matter what the request path looks like.
_DOCS_ROOT = (Path(__file__).resolve().parents[3] / "docs").resolve()

# Allowlist. ``slug`` is what the dashboard URL uses; ``path`` is
# relative to docs/. Add new docs here as they ship — that's the
# *only* way to expose them via this API.
_DOCS: dict[str, dict[str, str]] = {
    "getting-started": {
        "path": "operator-guide/getting-started.md",
        "title": "Getting started — operator's 30-minute tour",
        "category": "operator",
    },
    "deployment": {
        "path": "operator-guide/deployment.md",
        "title": "Deployment — docker-compose, env, TLS",
        "category": "operator",
    },
    "large-system-readiness": {
        "path": "operator-guide/large_system_readiness.md",
        "title": "Large-system readiness checklist",
        "category": "operator",
    },
    "performance-review": {
        "path": "operator-guide/performance-review.md",
        "title": "Performance review — load model + bottlenecks",
        "category": "operator",
    },
    "phase1-checklist": {
        "path": "operator-guide/phase1_checklist.md",
        "title": "Phase-1 launch checklist",
        "category": "operator",
    },
    "phase2-backlog": {
        "path": "operator-guide/phase2_backlog.md",
        "title": "Phase-2 backlog — what's next",
        "category": "operator",
    },
    "architecture": {
        "path": "architecture.md",
        "title": "Architecture — components, data flow, schemas",
        "category": "design",
    },
    "analysis-strategy": {
        "path": "analysis-strategy.md",
        "title": "Analysis strategy — extractor stages, summaries",
        "category": "design",
    },
    "analyzer-contract": {
        "path": "analyzer-contract.md",
        "title": "Analyzer contract — JSONL protocol, error shape",
        "category": "design",
    },
    "ultrareview": {
        "path": "ultrareview.md",
        "title": "Ultrareview — multi-agent diff review",
        "category": "design",
    },
}


def _resolve(slug: str) -> Path:
    """Map slug → on-disk path. Raises 404 on unknown slug AND on
    any path that escapes ``_DOCS_ROOT`` (defence-in-depth against
    a misconfigured allowlist)."""
    entry = _DOCS.get(slug)
    if entry is None:
        raise HTTPException(status_code=404, detail="unknown_doc")
    candidate = (_DOCS_ROOT / entry["path"]).resolve()
    # The .is_relative_to check stops a hypothetical ".." in the
    # allowlist entry from escaping the docs tree.
    try:
        candidate.relative_to(_DOCS_ROOT)
    except ValueError:
        raise HTTPException(status_code=404, detail="unknown_doc") from None
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="doc_not_built")
    return candidate


@router.get("/api/v1/docs")
async def list_docs() -> list[dict[str, str]]:
    """Sorted by category then title — operator docs first, design
    docs after, which matches the cognitive order an operator
    actually progresses through."""
    cat_order = {"operator": 0, "design": 1}
    return sorted(
        ({"slug": slug, **meta} for slug, meta in _DOCS.items()),
        key=lambda d: (cat_order.get(d["category"], 99), d["title"]),
    )


@router.get("/api/v1/docs/{slug}", response_class=PlainTextResponse)
async def get_doc(slug: str) -> PlainTextResponse:
    path = _resolve(slug)
    body = path.read_text(encoding="utf-8")
    return PlainTextResponse(content=body, media_type="text/markdown")
