# Gate-B review pipeline

> Mnemos does not invoke an external “ultrareview” product. It implements the
> useful pattern—separate passes, grounded findings, independent second opinion,
> fail-closed coverage, and audited approval—inside the source-analysis product.
> This document describes the current code, not the original design sketch.

## 1. Current pipeline

`app.safety.review.pipeline.run_pipeline()` runs these passes in order:

```text
rules → contracts → data_access → impact
                                ↓
                    second_opinion (Anthropic)
                                ↓
             deterministic graph-evidence validator
                                ↓
               clean | warn | blocked + coverage
```

| Pass | Contract |
|---|---|
| `rules` | Offline regex/self-review rules; legacy `error` becomes `critical` |
| `contracts` | Checks changed HTTP contracts against the project graph |
| `data_access` | Checks changed data-entity access against graph evidence |
| `impact` | Uses graph relationships to flag blast-radius concerns |
| `second_opinion` | Bounded independent review of exact changed-line evidence |
| `validator` | Validates deterministic node/edge evidence against the current project graph |

The four deterministic reviewers are registered explicitly in
`pipeline.py::_PASSES`. Second Opinion and the validator have different input
and evidence contracts, so they are intentionally invoked outside that list.

The runner accepts an optional `progress_cb(PassResult)`. The current submission
path does not persist `ReviewStage` rows and does not claim analysis-monitor-style
durable stage progress. `DiffSubmission` persists the flat validated finding list,
status, and the exact canonical review revision: run id, source generation,
overlay generation, and Git SHA.

## 2. Reviewer contract

Each deterministic reviewer has this effective signature:

```python
async def run(
    session: AsyncSession,
    *,
    project_id: UUID,
    plan_id: UUID,
    diff: str,
) -> list[Finding]: ...
```

`Finding` is a dataclass whose severity enum is checked at runtime:

```text
pass_name, severity(info|warning|critical), rule,
location, message, evidence[]
```

Rules findings may legitimately have no graph evidence and retain their severity.
For deterministic findings that do cite node/edge evidence, a missing project-scoped
graph fact is tagged with an `_unverified` rule and downgraded one severity step
(`critical→warning`, `warning→info`, `info→info`). It is not silently discarded.
A deterministic pass exception produces a privacy-safe `critical`
`<pass>_review_unavailable` finding and marks coverage incomplete.

Second Opinion does not reuse the node/edge validator. It accepts only the canonical
`mnemos.second_opinion.v1` schema and exact supplied diff-line references:
project-relative path, old/new side, line number, and line digest. Schema-invalid,
unreferenced, or mismatched model findings are excluded from the accepted finding
list. Any rejected finding lowers product/pass coverage to incomplete, so the final
verdict fails closed as `blocked` rather than treating the surviving subset as clean.

## 3. Second Opinion accounting and replay

Second Opinion is the only review pass allowed to call an LLM. Its current transport
is the price-attested direct Anthropic Messages API; there is no Agent SDK or generic
fallback.

Before network dispatch it requires:

- a canonical plan/source/overlay/Git review revision;
- at least one bounded diff-line evidence item (the evidence pack may still report
  incomplete overall coverage);
- a positive project-dollar policy;
- an atomic worst-case reservation for the exact model/official API route;
- a committed durable `STARTED` attempt and remaining DB-owned wall time.

The provider client uses the contract-derived official Anthropic base, a
provider-enforced output limit, and SDK retry 0. Usage, resolved model, finish reason,
JSON schema, and diff-line grounding are validated. The encrypted normalized
candidate is bound to the exact review revision. Candidate acceptance,
`SecondOpinionProduct` publication, and attempt classification commit atomically.
A terminal retry replays the stored candidate without provider redispatch.

If input, policy, provider, schema, grounding, persistence, or replay is incomplete,
the pass reports `coverage="incomplete"`; it does not invent a clean opinion.
An incomplete evidence pack with usable bounded lines may still be sent to the
provider, but it can never yield complete Gate-B authority.

## 4. Coverage and verdict

Coverage is authority, not display metadata. If any required pass is incomplete,
the pipeline verdict is `blocked` even when its available subset has no critical
finding.

When coverage is complete:

| Validated findings | Verdict |
|---|---|
| at least one `critical` | `blocked` |
| no critical, at least one `warning` | `warn` |
| info only or no findings | `clean` |

`submit_diff` rechecks that the reviewed diff still equals the plan worktree diff,
then stores status `blocked` for a blocked report or `pending_approval` otherwise.
The canonical graph revision stays pinned through verdict persistence. Approval
revalidates the same revision and byte-identical worktree diff before an MR side
effect is authorized.

## 5. Break-glass is not an override switch

The old `override: true` shortcut does not exist. A blocked submission can proceed
only through the following fail-closed workflow:

1. An administrator requests
   `POST /api/v1/diff_submissions/{id}/break_glass_grant` with a 200–10,000 character
   rationale.
2. The server reruns the complete pipeline against the current diff and canonical
   graph revision. Incomplete or still-blocked review issues no grant.
3. A successful grant is valid for 15 minutes and one submission/review revision,
   is single-use, and stores only the SHA-256 token hash.
4. A different operator—not the issuing administrator—passes the raw token to the
   approval endpoint.
5. One conditional `UPDATE ... RETURNING` atomically verifies the token, submission,
   expiry, non-consumption, 2-eyes rule, and exact review revision.
6. The reviewed diff and plan worktree are checked again. A late mismatch rolls the
   uncommitted grant consumption back.

Issue, consume, approval, and MR outcome are audited. This workflow never authorizes
a diff while the rerun verdict remains blocked.

## 6. Scope and extension

- The pipeline reviews a submitted diff against an already published source-analysis
  graph; it does not rerun the whole repository analysis. Submissions are bounded at
  ingress: `DiffSubmit.diff` rejects past `DIFF_INPUT_MAX_CHARS` (422), and
  `run_pipeline` fail-closes its non-HTTP callers (break-glass rerun, MCP dev tools)
  with `DiffInputTooLarge` before any deterministic pass scans the string. Second
  Opinion's evidence selection is additionally bounded on its own.
- Deterministic passes must not access the network. Second Opinion is the only
  price-attested exception.
- Model output remains inferred review guidance, not verified source truth.
- Add or reorder deterministic passes only by editing `pipeline.py::_PASSES` and
  supplying tests for findings, failure coverage, evidence validation, and verdict.
- A new model pass or evidence dialect needs its own schema, grounding, durable
  candidate, price/route contract, and product-publication invariants; it must not be
  slipped into the generic deterministic reviewer interface.
