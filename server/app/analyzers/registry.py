"""Which analyzers apply to which project languages."""

from app.analyzers.runner import AnalyzerRunner

# The binary may be shadowed by a docker-run wrapper in production; the
# platform only talks to it via the CLI contract in docs/analyzer-contract.md.
_BINARIES = {
    "csharp": "ggoss-csharp",
    "typescript": "ggoss-ts",
    "python": "ggoss-py",
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
