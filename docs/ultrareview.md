# Ultrareview-style review pipeline for Gate B

> This document records how Mnemos adopts the *ultrareview* philosophy for
> Gate B. We do **not** invoke any external ultrareview tool — we study its
> working pattern and reproduce the same process inside the platform, reusing
> the primitives we already built (graph, StageTracker, Validator, Extractor).

## 1. What the ultrareview pattern gets right

Observed across extremely-thorough code review practices — the `review` /
`security-review` skills, Anthropic's own "second opinion" pattern, and
heavyweight internal review tools — five ingredients recur:

1. **Many specialised passes, not one monolithic review.** Security,
   correctness, contract fidelity, data access, performance, style each
   have their own reviewer. Each pass sees a *slice* of the world it cares
   about, never the whole codebase.
2. **Evidence-grounded claims.** Every finding cites a concrete line, symbol,
   contract, or data-entity ID. Ungrounded "this might be wrong" comments are
   rejected at the source.
3. **Independent second opinion.** After the rule-based passes, a fresh
   reviewer reads the *diff + the findings so far* without the implementer's
   context and challenges them.
4. **Severity triage with gating.** Findings carry `info` / `warning` /
   `critical`. A critical finding blocks merge; it can only pass with an
   explicit operator override that lands in the audit log.
5. **Streaming visibility.** The reviewer runs as a pipeline of visible
   stages — reviewers and reviewees see progress, not a single late verdict.

## 2. How Mnemos reproduces it

Mnemos already has every primitive needed; the Gate-B path reuses them:

| Ultrareview ingredient     | Mnemos primitive it rides on                                       |
|----------------------------|--------------------------------------------------------------------|
| Specialised passes         | `app/safety/review/<pass>.py` modules, one per concern            |
| Evidence-grounded claims   | Reusable `Finding(severity, rule, location, evidence, message)`    |
| Evidence validator         | `app/extractor/validator.validate_claims` (reused)                 |
| Second opinion             | `Extractor` agent from Week-6 (same hash-stamping, same fallback)  |
| Severity gating            | `diff_submissions.status = blocked` unless override recorded       |
| Streaming pipeline         | `StageTracker` from the staged-analysis monitor                    |
| Audit                      | `audit_log` entries on submit, approve, override                   |

Pipeline shape:

```
submit_diff → [rules] → [contracts] → [data_access] → [impact]
                                         │
                                         ▼
                               [second_opinion (LLM)]
                                         │
                                         ▼
                               [evidence validator]
                                         │
                                         ▼
                                 verdict: clean | warn | blocked
```

Every arrow is a `ReviewStage` row with its own status + progress so the
Diffs tab can show a pipeline card identical in shape to the analysis
monitor.

## 3. Reviewer contracts

Each reviewer exports a single async function:

```python
async def run(session, diff: str, plan: Plan) -> list[Finding]: ...
```

Return value is a flat list of `Finding`s. Constraints:

- A reviewer MUST NOT access the network or call LLMs *unless* it is the
  `second_opinion` pass (which is specifically budgeted for that).
- Every non-style finding MUST include `evidence` — at least one node or
  edge ID from the graph. Findings without evidence are downgraded to
  `info` by the validator pass so they can still surface as hints but
  cannot block merge.
- Reviewers MUST be deterministic given the same diff + graph snapshot.
  Time, randomness, and hidden caches are forbidden.

## 4. The blocking rule

A diff submission's effective status after review is computed from the
union of findings:

| Findings present                | Effective submission status |
|---------------------------------|-----------------------------|
| no criticals, no warnings       | `clean`                     |
| no criticals, ≥1 warning        | `warn` (reviewer can approve) |
| ≥1 critical                     | `blocked`                   |

Approval of a `blocked` submission requires `POST .../approve` with
`override: true` and a `rationale` string ≥ 20 chars. Both end up in the
audit log keyed on the submission ID and the actor.

## 5. What this does *not* do

- Doesn't re-run the entire analysis. It reads the latest snapshot of the
  graph — exactly the shape the monitor already exposes.
- Doesn't write to production. It never leaves the worktree / DB.
- Doesn't replace human judgement. The operator remains the final
  decision-maker; the pipeline narrows the surface they must think about.

## 6. Extending the pipeline

New reviewers register themselves in `app/safety/review/__init__.py` as
`(name, position, callable)` tuples. They automatically appear as a stage
card in the Diffs tab and in the submission's `auto_review_findings`
payload. Removing a reviewer only requires deleting its module — stored
submissions keep the finding list they were approved against for audit.
