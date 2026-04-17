"""Contract-fidelity pass.

Looks at every ``+`` line in the diff that mentions an HTTP verb + path and
checks whether the resulting :func:`http_contract_id` already exists in the
graph. A *new* contract introduced by the diff is a `warning` (someone
should sanity-check the path pattern). Removing (line starts with ``-``)
an exposer for a contract that remains alive in the graph is a
``critical`` — that's a live endpoint about to be orphaned.
"""

from __future__ import annotations

import re
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.merge.contract_id import http_contract_id
from app.models.graph import Node
from app.safety.review.types import Finding

_HTTP_VERB = re.compile(
    r"(?:\[Http(Get|Post|Put|Delete|Patch)\(\"([^\"]+)\"\)\]|\bfetch\(\s*[\"']([^\"']+)[\"']"
    r"|Map(Get|Post|Put|Delete|Patch)\(\"([^\"]+)\")",
    re.IGNORECASE,
)


def _extract_paths(diff_line_body: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for m in _HTTP_VERB.finditer(diff_line_body):
        if m.group(1):  # [HttpGet("/x")]
            out.append((m.group(1), m.group(2)))
        elif m.group(3):  # fetch("/x"...)  — method unknown at this point
            out.append(("GET", m.group(3)))
        elif m.group(4):  # MapGet("/x"...)
            out.append((m.group(4), m.group(5)))
    return out


async def run(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    plan_id: uuid.UUID,  # noqa: ARG001
    diff: str,
) -> list[Finding]:
    findings: list[Finding] = []
    current_file = "?"
    for idx, raw in enumerate(diff.splitlines(), start=1):
        if raw.startswith("+++ "):
            current_file = raw[4:].strip()
            continue
        if raw.startswith("@@") or (not raw.startswith("+") and not raw.startswith("-")):
            continue
        if raw.startswith("+++") or raw.startswith("---"):
            continue
        body = raw[1:]
        paths = _extract_paths(body)
        for verb, path in paths:
            cid = http_contract_id(verb, path)
            node = (
                await session.execute(
                    select(Node).where(
                        Node.project_id == project_id,
                        Node.id == cid,
                        Node.kind == "Contract",
                        Node.valid_to.is_(None),
                    )
                )
            ).scalar_one_or_none()

            if raw.startswith("+") and node is None:
                findings.append(
                    Finding(
                        pass_name="contracts",
                        severity="warning",
                        rule="new_http_contract",
                        location=f"{current_file}:{idx}",
                        message=(
                            f"Introduces new HTTP contract {cid}. Confirm the "
                            f"path pattern and auth requirements."
                        ),
                        evidence=[{"kind": "node", "node_id": cid, "certainty": "asserted"}],
                    )
                )
            elif raw.startswith("-") and node is not None:
                findings.append(
                    Finding(
                        pass_name="contracts",
                        severity="critical",
                        rule="contract_exposer_removed",
                        location=f"{current_file}:{idx}",
                        message=(
                            f"Removes an exposer of live contract {cid}. "
                            f"Existing callers will break."
                        ),
                        evidence=[{"kind": "node", "node_id": cid, "certainty": node.certainty}],
                    )
                )
    return findings
