# PRD: Thermonuclear Review Remediation for PRs 719-741

**Status:** Active follow-up implementation
**Created:** 2026-06-30
**Scope:** Open stacked PRs #719-#741 authored by `tim1016`.

## 1. Context

PRs #719-#741 add lifecycle projection persistence, replay/tailing, trader
guidance, lifecycle timeline/receipt rendering, activity consistency warnings,
and watchdog recovery receipts. A parallel thermonuclear code-quality review was
run for each open PR in that range. The review bar was intentionally strict:
structural regressions, canonical-layer drift, large-file growth, spaghetti
branching, weak typed boundaries, and timestamp/session ambiguity are treated as
merge blockers when they can affect operator trust or future maintainability.

This PRD compiles those reviews into one follow-up remediation plan. It is the
single implementation contract for the follow-up PR stacked on
`codex/718-watchdog-recovery-receipts`.

## 2. Goals

1. Keep Python as the sole authority for lifecycle projection, trader guidance,
   activity consistency, operator surface, and chart facts.
2. Make persisted projection receipts audit-credible: source identity, ordering,
   template identity, hashes, and cursor state must mean what they claim.
3. Replace duplicated branch chains with typed decision/fact models where the
   same result is rendered, persisted, and tested from one source of truth.
4. Stop large-file and large-component growth where the reviewed PR caused a
   threshold crossing or added a self-contained subsystem.
5. Update authority docs so future agents do not follow stale or contradictory
   claims.

## 3. Non-Goals

- No new trading/math engine concept is introduced. `docs/math-sources-of-truth.md`
  is not expected to change.
- No .NET transport-layer behavior is introduced.
- No visual redesign beyond extracting already-shipped Angular presentation
  blocks into focused components.
- No broad cleanup of all historical 1k+ files. This PR fixes regressions and
  high-confidence decomposition opportunities found in the reviewed stack.

## 4. Accepted Remediation Requirements

### R1. Projection Store Ordering and Schema Honesty

Findings: #719.

- Persist or otherwise expose a canonical sort key that preserves the existing
  file-backed lifecycle ordering semantics: `ts_ms`, source rank, and
  source-local sequence.
- SQL timeline and safety-triage reads must order by the same semantics as the
  file projection.
- The lifecycle projection API must not expose dead fallback booleans. Either
  return a typed unavailable response or remove the booleans and let `503`
  carry the unavailable state.
- Durable schema must not ship future tables without writer/query ownership. If
  gate snapshot and node receipt tables remain, Python row models, write paths,
  read paths, and tests must own them.
- AccountOwner status snapshot persistence must use a typed model instead of
  unstructured `dict[str, Any]`.

### R2. Replay and Tailer Durability

Findings: #720, #734.

- DB-bound replay requires unique canonical `source_artifact` identities; no
  generic `account_events` or `intent_wal` defaults that can collide on
  `(source_artifact, source_seq)`.
- Replay must persist `run_id` for bot lifecycle rows when a run context exists.
- Account-event replay/tailing must not stamp every account-scoped row with a
  source-level `bot_id`; bot scope must be derived per event from typed evidence.
- Replay batch persistence must commit bot rows, account rows, and snapshots
  atomically before the cursor advances.
- Tailer cursor advancement must be atomic across concurrent source updates.
- `source_hash` must describe the same byte snapshot used to parse projected
  rows, or the field must be renamed/documented as a weaker high-water receipt.

### R3. Account Event Boundary Hardening

Findings: #733.

- Canonical account-event reads used by safety folds stay strict or return
  invalid-row diagnostics that fail closed. Legacy/tolerant parsing is isolated
  to projection/replay adapters.
- Account-event writes reject malformed timestamp-shaped fields, not only
  malformed top-level `ts_ms`. Timestamp fields persisted to files remain
  `int64 ms UTC`.

### R4. Trader Guidance Decision Model

Findings: #721.

- Trader submit readiness and remediation must be derived from one typed,
  prioritized finding list. Parallel branch chains cannot select different
  primary causes/remediations.
- The shipped closed contract must match ADR/design docs, or docs must be
  updated to bless the smaller split between `submit_readiness` and
  `trader_guidance`.
- Trader guidance logic should live in a focused Python service/module instead
  of expanding `operator_surface.py`.
- Tests must cover priority collisions, every shipped readiness/situation code,
  unknown states, and the disconnected-broker/missing-reconciliation case.

### R5. Lifecycle Chart Fact Ownership

Findings: #724, #728, #738, #741.

- `writer_guard` status is resolved in one place; AccountOwner
  phase/generation evidence cannot render differently depending on whether it
  came from surface fallback or lifecycle events.
- Receipt authoring is extracted from `bot_lifecycle_chart.py` into a focused
  module or fact builder so chart assembly stops owning timestamp projection,
  receipt payload keys, and evidence threading.
- Recovery status, evidence, `ts_ms`, `ts_ms_resolved`, and receipts are resolved
  atomically by one recovery fact helper. Mixed prior-run and watchdog authority
  cannot produce incoherent recovery nodes.
- Recovery placeholder tests cover the documented reachable causes, including
  active/stopping, blocked/incident, and poison/halt.

### R6. Activity/Lifecycle Consistency

Findings: #739.

- Activity/lifecycle comparisons use the same America/New_York session window
  that the endpoint and docs claim.
- Lifecycle/activity reconciliation policy moves out of the large router into a
  typed service boundary and reuses normalized lifecycle projection taxonomy
  instead of duplicating event-type/order-ref extraction locally.

### R7. Projection Safety Triage Contract

Findings: #735.

- Safety triage response rows are typed as warning/critical only in both Python
  and TypeScript. They must not alias the generic timeline response.
- Filter behavior is proven behaviorally or generated through a single filter
  spec so optional SQL predicates, router params, and frontend params cannot
  drift silently.

### R8. Rendered Template Receipts

Findings: #740.

- `rendered_template_id` is authored before lifecycle events are normalized
  into `(source, event_type)`. Distinct source templates that collapse to one
  event type must keep distinct stable template ids.
- Tests cover `ACK_FAILED_UNCERTAIN` and `SUBMIT_UNCERTAIN_HALTED` so they do
  not share a template id.

### R9. Angular Contract and Component Decomposition

Findings: #722, #723, #729, #730.

- Lifecycle projection types move out of `live-instances.types.ts` so the PR
  no longer pushes that file over 1k lines.
- Timeline loading is keyed to the current status identity or loaded as one
  typed snapshot so the UI cannot show fresh status with stale timeline rows.
- Trader timeline and node receipt panes are extracted into focused OnPush
  components with their own tests.
- Frontend operator-surface fixtures have one canonical home with named builders
  for monitor-only and ready-to-submit states.
- Trader remediation rendering is split from cockpit gate suggested-action
  rendering, and endpoint dispatch is explicit for trader remediations.
- Angular flow edges rely on `ngx-vflow` floating closest-handle routing instead
  of a bespoke coordinate-to-handle router.

### R10. Authority Document Reconciliation

Findings: #721, #724, #731, #739, #741.

- `docs/bot-lifecycle-account-owner-authority.md` reflects the final semantics
  for projection ordering, replay/tailer durability, writer guard ownership,
  recovery precedence, safety triage, and frontend render-only responsibilities.
- `docs/audits/bot-lifecycle-observability-2026-06-29.md` stops mixing stale
  original audit claims with corrected current-state claims.
- ADR/design docs for operator-surface trader guidance are updated if the
  shipped contract differs from the previous 17-code design.
- Operator notice/watchdog docs are updated if the final recovery precedence or
  incident-headline contract changes.

## 5. Deferred or Non-Blocking Findings

- PRs #736 and #737 produced no high-conviction blockers. Their residual
  file-size concerns are covered by R5.
- Existing large files that were already over 1k lines before this stack are not
  decomposed wholesale unless a requirement above touches the subsystem.
- Cosmetic-only review suggestions are not part of this PRD.

## 6. Acceptance Criteria

1. Tests prove the major bug classes: projection ordering parity, replay source
   identity/run id, atomic replay/cursor behavior, strict-vs-tolerant
   account-event reads, timestamp-field validation, trader priority collisions,
   NY-session activity consistency, template-id collision, and recovery
   precedence.
2. No changed file newly crosses from under 1k lines to over 1k lines.
3. Python route handlers remain transport orchestration; new lifecycle/activity
   policy lives in services.
4. Angular components introduced by this remediation use signals/input(),
   OnPush, modern control flow, and focused templates.
5. Authority docs match the shipped code and explicitly identify any weaker
   audit/provenance guarantees.
6. Focused Python and frontend tests pass locally, and project-level lint/test
   status is reported in the follow-up PR.

## 7. Implementation Status

This follow-up PR implements all accepted major findings from the collected
reviews. The remediation keeps Postgres as a rebuildable read model, preserves
Python as lifecycle/operator authority, and leaves R3 AccountOwner daemon/IPC
outside the shipped claim.

The only deliberately deferred items are broader historical decompositions and
cosmetic review suggestions already listed in section 5. No new trading math
authority or .NET transport behavior is introduced.
