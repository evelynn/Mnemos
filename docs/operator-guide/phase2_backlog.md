# Phase 2 backlog

Items the 5 self-review rounds across PR-1 ~ PR-18 deliberately
deferred. Living document — when one of these is picked up, move the
detail into a new spec section (or a dedicated doc) and replace the
entry here with a one-line "shipped in PR-N".

## Why a backlog file at all

Round 6 of the self-review flagged a real risk: deferred items were
scattered across (a) PR commit messages, (b) inline TODO comments
in source, and (c) the Phase 1 checklist. Anyone arriving fresh saw
a green test suite and had no way to find out what was *intentionally*
not built yet. This file is the single index.

---

## P2-1 — OTLP Tier 2: trace → exercised-edge merge

**Status**: deferred.

**Source**: spec §7.6 "exercised edge"; commit `e892182` (PR-11) added
metric emit but left the merge unimplemented; PR-17 commit message
explicitly defers the trace-tree assembly.

**What it has to do**:

* Subscribe to the OTLP span receiver (`runtime_receiver/router.py`).
* Walk the span tree per trace (resource attrs → `service.name`, scope
  span name → component slug, `parent_span_id` → caller resolution).
* For each `SERVER` span, upsert the matching `EXPOSES` edge with
  `data.exercised = "true"` and `data.last_seen_at = <iso>`.
* For each `CLIENT` span, upsert the matching `CALLS` edge.
* For `db.statement` / `db.sql.table` spans, upsert `READS` / `WRITES`
  edges keyed by `(db.sql.table, db.operation)` — `db.statement` alone
  is masked and unreliable.
* Spans whose caller/callee don't resolve to known components land in
  a `runtime_observations(organization_id, service, operation,
  seen_count, last_seen_at)` table; the next analysis run's merge
  stage replays them.

**Why deferred**: trace-tree assembly is a multi-week change with its
own concurrency story (spans of one trace can arrive across many
HTTP requests). Team B's 5th-round critique #1 was explicit that
shipping the simple `(service, operation)` matcher would create false
positives we'd have to live with.

---

## P2-2 — RuntimeObservation table

**Status**: deferred (depends on P2-1).

Schema sketch:

```
CREATE TABLE runtime_observations (
  id              uuid PRIMARY KEY,
  organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  project_id      uuid REFERENCES projects(id) ON DELETE CASCADE,  -- nullable until reconciled
  service         text NOT NULL,
  operation       text NOT NULL,
  seen_count      int  NOT NULL DEFAULT 1,
  last_seen_at    timestamptz NOT NULL,
  UNIQUE (organization_id, service, operation)
);
```

Reconcile hook: end of `orchestrator/stages.py` analysis pipeline calls
`merge.runtime.reconcile_observations(project_id)`.

Retention: 14 days, swept by the existing `retention_purge` cron job.

---

## P2-3 — Korean UI (i18n)

**Status**: deferred.

Round 4-6 audits all flagged that PII *masking* is Korea-aware (RRN
validators, 휴대폰 prefixes, Korean column-name keywords in
`PARTIAL_MASK_COLUMNS`) but the **UI** is English-only. README labels
are bilingual; templates aren't.

Path forward:

* Adopt `babel` + `Flask-Babel`-style gettext on top of Jinja2, or
* Switch to a static `i18n.json` map and let Jinja look up keys.

Either way the spec §0.4 ("UI 한국어 컨텍스트") stays open until this
ships. Helper text and toasts are the highest-leverage starting points.

---

## P2-4 — Relative timestamps

**Status**: deferred.

Every dashboard page renders raw ISO 8601 (`r.started_at`). Operators
want "3 minutes ago" / "yesterday" at a glance. Plan:

* `<time data-ts="2025-05-12T14:30:00Z">` markup in every template.
* `MnemosUI.relativeTime` helper in `static/ui.js` that walks
  `[data-ts]` elements on `DOMContentLoaded` and re-runs every minute.
* Locale-aware via `Intl.RelativeTimeFormat` — picks up the browser
  locale, no server changes.

---

## P2-5 — Colour-blind status glyphs on analysis badges

`app.css` already adds `✓ ⚠ ⛔` glyphs to verdict pills (PR-9). The
same treatment needs to land on the `.badge.{queued,running,completed,
failed,cancelled}` set used by the analysis tab. Round 6 audit A4.

---

## P2-6 — Large-result pagination in `data.html`

When `q-max-rows` is left blank we ship up to 10 000 rows back at
once and render every one of them into a `<table>`. Operators with
real-world workloads will need:

* Server: cursor-paginated `/data/query` (or a streaming variant).
* UI: virtual scroll table — vanilla JS is enough; no framework.

Round 5 audit E3.

---

## P2-7 — Dashboard drill-down

Each stat / metric card lights up red/yellow but most have no link to
the contextual list. "Failed runs (24h): 3" should go to a
pre-filtered `/analysis?status=failed&since=24h`. Round 6 audit A3.

---

## P2-8 — SSE status badge across tabs

The `#sse-status` live/disconnected pill only renders on
`/analysis`. An operator with the platform open on `/findings` won't
know their monitoring stream went away. Cross-tab notification via
`BroadcastChannel("mnemos-sse")` was sketched in round 5; needs the
companion listener in `_layout.html` and a sticky strip at the top
of the content area. Round 6 audit A4, S2.

---

## P2-9 — Auto-progression of the onboarding card

`#onboarding-card` lists three steps but does not move the user
through them — register → analyse → review is a manual click chain.
Plan:

* Store step state in `sessionStorage` keyed by the active project.
* After project register, redirect with `?onboard=1` so analysis tab
  highlights the "Trigger run" form.
* After first run finishes, the dashboard onboarding card hides
  permanently for that user.

Round 6 audit S5 / B1.

---

## P2-10 — Authlib / TOTP step-up auth, DPoP

The 2nd-round Team B report kept these on a Phase-2 list:

* `pyotp` for break-glass two-eyes step-up (defends against a single
  operator with two OIDC identities).
* Authlib DPoP (RFC 9449) once the platform fronts external clients.

Both stay out of Phase 1 — single-operator self-host doesn't see the
threat models these defuse.

---

## Out of scope entirely

Listed here so a future audit doesn't keep re-discovering them as
"missing":

* Multi-region deployment / read replicas — single-region by design.
* Marketplace / shared analyzers — analyzers live in this repo.
* Reverse-proxy LLM aggregator (BYO OpenAI) — Anthropic only.
* Mobile dashboard — desktop browser, period.
