"""Which analyzers apply to which project languages."""

import shutil

from app.analyzers.runner import AnalyzerRunner, inrepo_script

# The binary may be shadowed by a docker-run wrapper in production; the
# platform only talks to it via the CLI contract in docs/analyzer-contract.md.
_BINARIES = {
    "csharp": "ggoss-csharp",
    "typescript": "ggoss-ts",
    # ggoss-ts walks .js/.jsx/.mjs/.cjs alongside .ts — a JS-only project
    # gets deterministic extraction instead of the agent fallback. When a
    # project lists BOTH typescript and javascript the orchestrator skips
    # the duplicate stage (same binary, same tree) with a recorded reason.
    "javascript": "ggoss-ts",
    "python": "ggoss-py",
    # PR-191 — deterministic C/C++ extraction (functions/structs/enums/
    # macros + CALLS); vendored/ trees excluded by the analyzer itself.
    "cpp": "ggoss-cpp",
    # PR-192 — deterministic Java extraction (types/methods + Spring/JAX-RS
    # HTTP contracts normalized to the same http.<M>.<path> node a TS fetch
    # client resolves to, so Java-server ↔ TS-client cross-service linking
    # works); closes the web+Java eval's empty-graph gap.
    "java": "ggoss-java",
    # PR-193 — web templates. ggoss-web extracts HTML form/link routes as HTTP
    # contracts (same normalized node a Java handler EXPOSES) so a template ↔
    # Spring-handler cross-service link forms; makes HTML/CSS targetable at all
    # (the web+Java eval's P2 gap). One binary walks html+css.
    "html": "ggoss-web",
    "css": "ggoss-web",
    "scss": "ggoss-web",
    # PR-194 — Kotlin (JVM web; modern Spring Boot). Separate analyzer, same
    # Spring annotations → same http.<M>.<path> node (cross-service linking).
    "kotlin": "ggoss-kotlin",
    "mssql": "ggoss-sql-mssql",
    "oracle": "ggoss-sql-oracle",
    "dotnet_binary": "ggoss-binary-dotnet",
}


# Languages with a deterministic ggoss analyzer. Languages outside this
# set fall back to Claude-Code agent extraction (PR-140) when eligible.
ANALYZER_LANGUAGES = frozenset(_BINARIES)


def binary_for(language: str) -> str | None:
    return _BINARIES.get(language)


def runner_for(language: str) -> AnalyzerRunner | None:
    binary = binary_for(language)
    return AnalyzerRunner(binary) if binary else None


def analyzer_available(language: str) -> bool:
    """True only when a deterministic analyzer is BOTH registered for the
    language AND its binary is on PATH. False means the platform cannot
    extract this language deterministically — e.g. an unregistered language
    (C++), or a registered one whose image isn't installed (the docker-free
    case, PR-144). Callers fall back to Claude-Code agent extraction so the
    graph is never left empty just because a binary is missing."""
    binary = binary_for(language)
    if binary is None:
        return False
    # Available if installed on PATH (docker/prod) or runnable from the
    # in-repo source (docker-free basic config, PR-153).
    return shutil.which(binary) is not None or inrepo_script(binary) is not None
