"""Finding risk scoring + remediation hints (PR-50).

The 19th-round value audit's B-area finding: a Mnemos finding
told the operator *what* but never *how important* or *what to do*.
This module is the deterministic answer to both — no LLM call, so
it's free, instant, and reproducible.

``score_finding`` produces a 0-100 ``risk_score`` from three
factors:

* **severity weight** — the base. info / warning / error map to
  10 / 40 / 75.
* **exercised multiplier** — a finding on a code path an OTLP
  trace has confirmed is *live in production* is worth more than
  one on dead-looking static code. ×1.3 when exercised.
* **blast-radius bonus** — a finding on a node with many inbound
  / outbound edges affects more of the system. +0..20 scaled by
  the edge count the merge layer passes in.

``remediation_for`` returns a deterministic fix hint + CWE id
keyed off the finding ``kind``. This is the first rung of the
"Finding → Fix" automation ladder — it doesn't write the diff,
but it tells the operator the canonical next move so they don't
have to reverse-engineer the finding's intent.
"""

from __future__ import annotations

_SEVERITY_WEIGHT = {
    "info": 10,
    "warning": 40,
    "error": 75,
    # Defensive default for an unknown severity string — treat it
    # as a warning rather than silently scoring it 0.
    "_default": 40,
}

_EXERCISED_MULTIPLIER = 1.3


def score_finding(
    *,
    severity: str,
    exercised: bool = False,
    blast_radius: int = 0,
) -> int:
    """Return a 0-100 integer risk score.

    ``blast_radius`` is the count of graph edges touching the
    finding's subject node — the merge layer computes it once and
    passes it in so this function stays pure.
    """
    base = _SEVERITY_WEIGHT.get(severity, _SEVERITY_WEIGHT["_default"])
    score = float(base)
    if exercised:
        score *= _EXERCISED_MULTIPLIER
    # Blast-radius bonus: log-ish ramp so a node with 3 edges and a
    # node with 30 edges don't differ by 10× — capped at +20.
    if blast_radius > 0:
        bonus = min(20.0, 4.0 * (blast_radius ** 0.5))
        score += bonus
    return max(0, min(100, round(score)))


# ---------------------------------------------------------------------------
# Remediation hints — deterministic, keyed off finding kind.
# ---------------------------------------------------------------------------

# Each entry: (remediation hint, CWE id or None).
_REMEDIATION: dict[str, tuple[str, str | None]] = {
    "duplicate_endpoint": (
        "Two or more components expose the same HTTP contract. Pick the "
        "canonical exposer — usually the one with the most verified "
        "callers — and route the others through it, or delete the "
        "redundant handlers. Open a Plan to consolidate.",
        "CWE-1041",  # Use of Redundant Code
    ),
    "unverified_claim": (
        "This edge is 'inferred' — the analyzers guessed it but nothing "
        "confirmed it. Add a unit test that exercises the path, or point "
        "an OTLP collector at the running service so a runtime trace "
        "lifts the certainty to 'verified'.",
        None,
    ),
    "dynamic_call_detected": (
        "A reflective / dynamic dispatch was found — the static graph "
        "can't see where it lands. Replace the dynamic call with an "
        "explicit interface, or annotate the call site so the analyzer "
        "can record the intended target.",
        "CWE-470",  # Unsafe Reflection
    ),
    "dead_path_suspected": (
        "No caller reaches this code on any analysed path. Confirm it is "
        "genuinely unreachable (check reflective entry points and DI "
        "wiring first), then delete it — dead code is a maintenance and "
        "audit-surface cost.",
        "CWE-561",  # Dead Code
    ),
    "schema_mismatch": (
        "The code's view of this data entity disagrees with the live "
        "database schema. Reconcile them: either migrate the DB or fix "
        "the model. A schema mismatch is a runtime-error waiting to "
        "happen.",
        "CWE-1078",  # Inappropriate Source Code Style / structure
    ),
    "opaque_component_failing": (
        "A component the analyzers could not see inside is on a failing "
        "path. Add an analyzer for its language, or wrap it in a "
        "contract (an explicit API boundary) so its behaviour becomes "
        "observable.",
        None,
    ),
}

_DEFAULT_REMEDIATION = (
    "Review the finding detail and open a Plan describing the intended "
    "fix; the ultrareview pipeline will gate the resulting diff.",
    None,
)


def remediation_for(kind: str) -> tuple[str, str | None]:
    """Return ``(hint, cwe_id)`` for a finding kind.

    Unknown kinds get a generic but still actionable default —
    "open a Plan" — rather than an empty string.
    """
    return _REMEDIATION.get(kind, _DEFAULT_REMEDIATION)


def priority_label(risk_score: int) -> str:
    """Bucket a numeric score into a human label for the GUI badge.

    P1 ≥ 75, P2 ≥ 50, P3 ≥ 25, P4 below. Operators sort by
    ``risk_score`` but skim by label.
    """
    if risk_score >= 75:
        return "P1"
    if risk_score >= 50:
        return "P2"
    if risk_score >= 25:
        return "P3"
    return "P4"
