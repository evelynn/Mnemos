"""Call-resolution accuracy oracle — the honest in-project recall metric.

Raw resolution rate misleads: library calls (assertThat, json.dumps) are
correctly ``extern`` for both Mnemos and cbm's Hybrid LSP. The accuracy metric
that actually tracks the gap is:

    in-project recall = (in-project calls resolved) / (in-project calls total)

where an "in-project" call is one whose bare callee name matches a symbol
DEFINED in the repo. A missed in-project call (the name is defined here but we
couldn't resolve it) is the failure type-aware resolution closes; a call that
resolves to the WRONG same-named symbol is a precision failure (see the
per-language type-resolution tests).

Usage:
    python resolution_recall.py <analyzer_script> <repo_path> <extern_prefix>
e.g.
    python resolution_recall.py ../../analyzers/ggoss-java/src/ggoss_java.py \\
        /path/to/spring-petclinic java:extern:
"""

from __future__ import annotations

import collections
import json
import subprocess
import sys
from pathlib import Path

_DEF_KINDS = {"function", "method", "class", "struct", "type", "interface",
              "enum", "record", "object"}


def _run(script: str, verb: str, target: str) -> list[dict]:
    cp = subprocess.run(
        [sys.executable, script, verb, target],
        capture_output=True, text=True, timeout=600,
    )
    return [json.loads(x)["data"] for x in cp.stdout.splitlines() if x.strip()]


def _bare(name: str, extern_prefix: str) -> str:
    name = name.replace(extern_prefix, "")
    for sep in ("::", ".", "->"):
        if sep in name:
            name = name.rsplit(sep, 1)[-1]
    return name


def measure(script: str, repo: str, extern_prefix: str) -> dict:
    syms = _run(script, "symbols", repo)
    edges = _run(script, "calls", repo)
    name_count: collections.Counter = collections.Counter()
    for s in syms:
        if s.get("kind") in _DEF_KINDS:
            name_count[s["name"]] += 1
    proj = set(name_count)

    in_proj_total = in_proj_resolved = amb = uniq_miss = 0
    for e in edges:
        resolved = e["metadata"]["callee_resolved"]
        if resolved:
            in_proj_total += 1
            in_proj_resolved += 1
            continue
        callee = _bare(e["target_id"], extern_prefix)
        if callee in proj:
            in_proj_total += 1
            if name_count[callee] >= 2:
                amb += 1
            else:
                uniq_miss += 1
    recall = (in_proj_resolved / in_proj_total * 100.0) if in_proj_total else 100.0
    return {
        "total_calls": len(edges),
        "in_project_total": in_proj_total,
        "in_project_resolved": in_proj_resolved,
        "in_project_recall_pct": round(recall, 1),
        "missed_ambiguous": amb,       # need type/inheritance resolution
        "missed_unique": uniq_miss,    # should be recoverable
    }


def main() -> int:
    if len(sys.argv) < 4:
        sys.stderr.write(__doc__ or "")
        return 2
    report = measure(sys.argv[1], sys.argv[2], sys.argv[3])
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
