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

**Status**: shipped in PR-25 (initial). The receiver now assembles a
trace tree, infers ``EXPOSES`` vs ``CALLS`` from ``span.kind``,
picks operation keys in the documented preference order
(``http.route`` → ``rpc.method`` → ``db.sql.table+db.operation`` →
``span.name``), and buffers triples in ``runtime_observations`` for
the merge stage to reconcile. The merge call site fires after every
``rebuild_findings`` pass.

Follow-up still open: distributed-trace assembly across multiple
receiver requests (currently a trace must arrive in one POST), and a
richer edge-match heuristic than the ``operation``/``path``/``route``
field probes — both are spec §7.6 work the round-5 audit explicitly
deferred past Phase 1.

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

**Status**: shipped in PR-25 (alembic migration 0016, ORM in
``server/app/models/runtime.py``). The UNIQUE constraint Team B
asked for (``organization_id, service, operation, kind``) backs the
``INSERT … ON CONFLICT DO UPDATE`` upsert path in
``app.merge.runtime.buffer_observations``. PR-29 wired the
14-day retention sweep into the existing ``retention_purge`` cron
(``DELETE FROM runtime_observations WHERE last_seen_at < cutoff``) —
the 9th-round audit caught that as the one missing piece.

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

**Status**: shipped initial wave in PR-26. ``ui.js`` carries a small
phrase-book (~30 entries covering toasts, onboarding text, empty
states, and button labels), the sidebar gets an EN / 한국어
switcher, and the locale persists to ``localStorage["mnemos_locale"]``
(falls back to ``navigator.language``). Pages opt in by tagging
elements with ``data-i18n="<english key>"`` or
``data-i18n-placeholder="<english key>"`` — the runtime translator
walks both attribute conventions on DOMContentLoaded and after every
``setLocale`` call.

Follow-up still open:

* Per-page audit — most templates still ship hard-coded English
  strings. The book needs to grow to cover findings.html, audit.html,
  diffs.html, plans.html, settings.html.
* Server-rendered Jinja strings (form labels, errors) — currently
  English-only. A future PR can either route them through the
  client-side translator (less ideal) or add a server-side i18n
  layer that reads ``Accept-Language``.
* DateTime + number formatting locale awareness — relativeTime
  already uses ``Intl.RelativeTimeFormat`` so it picks up the
  browser locale; other ad-hoc number formatting is still en-US.

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

**Status**: shipped in PR-20. ``MnemosUI.relativeTime(iso)`` +
``MnemosUI.hydrateRelativeTimes(scope)`` walk every
``<time data-ts="…">`` element via ``Intl.RelativeTimeFormat``;
auto-refresh every 60 s.

Every dashboard page renders raw ISO 8601 (`r.started_at`). Operators
want "3 minutes ago" / "yesterday" at a glance. Plan:

* `<time data-ts="2025-05-12T14:30:00Z">` markup in every template.
* `MnemosUI.relativeTime` helper in `static/ui.js` that walks
  `[data-ts]` elements on `DOMContentLoaded` and re-runs every minute.
* Locale-aware via `Intl.RelativeTimeFormat` — picks up the browser
  locale, no server changes.

---

## P2-5 — Colour-blind status glyphs on analysis badges

**Status**: shipped in PR-20. Every ``.badge.{queued,running,
completed,failed,cancelled,disabled,critical,high,medium,low,info}``
carries a distinct ``::before`` glyph; ``.sse-status.{live,
disconnected}`` likewise. The verdict pills already had this from
PR-9.

---

## P2-6 — Large-result pagination

**Status**: client-side shipped in PR-24. ``data.html`` and
``findings.html`` both render 100 rows per page via a
DocumentFragment-based ``_show*Page`` cursor. A real
server-side cursor for ``/data/query`` is the follow-up work that
stays open; the client-side fix unblocks every operator who hits
the existing 10 000-row clamp.

---

## P2-7 — Dashboard drill-down

**Status**: shipped in PR-22. Every actionable stat / metric card on
the dashboard is now an ``<a class="stat-card stat-link">`` with
the matching query string baked in; landing pages
(``findings.html``, ``analysis.html``, ``audit.html``,
``diffs.html``, ``settings.html``) auto-apply the filter and, where
appropriate, auto-dispatch the search.

---

## P2-8 — SSE status badge across tabs

**Status**: shipped in PR-23. ``BroadcastChannel("mnemos-sse")``
publishes ``live``/``disconnected``/``idle`` from
``analysis.html`` and a sticky ``#sse-cross-tab-strip`` in
``_layout.html`` reveals on every other tab when the state is
disconnected. Graceful fallback when ``BroadcastChannel`` is
unavailable (older WebViews).

---

## P2-9 — Auto-progression of the onboarding card

**Status**: shipped in PR-21. Step state lives in
``sessionStorage["mnemos_onboarding_step{1,2,3}_done"]``;
``projects.html`` marks step 1 on a successful create + reveals an
inline CTA pointing at ``/analysis?project=<id>&onboard=1``;
``analysis.html`` pre-fills the run form and marks step 2 on a
successful trigger; ``findings.html`` marks step 3 when at least one
row renders; ``dashboard.html`` updates the card's strikethroughs
live and hides the whole card once every step is done.

---

## P2-10 — Authlib / TOTP step-up auth, DPoP

**Status**: out of scope for Phase 2 too — single-operator threat
model doesn't change between Phase 1 and Phase 2.

The 2nd-round Team B report kept these on a Phase-2 list:

* ``pyotp`` for break-glass two-eyes step-up (defends against a single
  operator with two OIDC identities).
* Authlib DPoP (RFC 9449) once the platform fronts external clients.

Both stay out — they defuse threat models that don't exist in a
single-operator self-host deployment. If/when Mnemos starts shipping
as multi-tenant SaaS, this becomes a Phase 3 item; until then the
break-glass TTL grant + the audit log are sufficient.

---

## Out of scope entirely

Listed here so a future audit doesn't keep re-discovering them as
"missing":

* Multi-region deployment / read replicas — single-region by design.
* Marketplace / shared analyzers — analyzers live in this repo.
* Reverse-proxy LLM aggregator (BYO OpenAI) — Anthropic only.
* Mobile dashboard — desktop browser, period.
