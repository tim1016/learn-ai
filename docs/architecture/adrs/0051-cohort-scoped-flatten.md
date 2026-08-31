# ADR 0051 — Cohort-scoped flatten is N attributed legs behind one affordance

**Status:** Accepted 2026-08-31
**Provenance:** Decision ticket [#1802](https://github.com/tim1016/learn-ai/issues/1802) (T3). Source: `docs/audits/bot-fleet-stress-2026-08-26.md` §3 T3 — the churn wave caught all four QQQ `deployment_validation` bots mid-position with one cohort-targeted stop, producing 4× `RESUME_CARRYOVER_UNSUPPORTED` simultaneously; the proven remedy (reconcile → flatten → resume, A9) is N×3 clicks and N grows with the fleet.
**Decision drivers:** Same-symbol bots running the same program enter and exit in lockstep *by design*, so a stop wave lands mid-hold for every member at once. Every piece is correct in isolation (stop = decisions-stop; carryover FORBID; the per-bot remedy works); the failure is emergent at cohort scale. #1802 frames the needed shape precisely: the **inverse-scoped sibling of the Two-Tap account-hold rule (#1773)** — where Two-Tap says an account-scoped fact must not drive per-bot behaviour, this says a per-bot remedy applied to an account-scoped event must not require per-bot ceremony.
**Related:** PRD #1752 (user story 2: flattening one bot must never touch another's position on the same account; user story 11: every leg carries its own decision receipt with a distinct idempotency identity), ADR 0047 (no surfaced action may be structurally unarmable — presented actions carry their executability facts), ADR 0045 (exposure lifecycle closure — the per-bot `flatten_stop` this decision composes), #1776 (reads project the sweep's verdict; the presentation GET adds no second reconciler).
**Vocabulary:** none owed. No new per-bot action id, no new reason code, no operator-copy change — the legs are the existing `flatten_stop`, and per-leg refusals reuse the existing typed action-error shape. The cohort wrapper is transport and orchestration, not a new operator-domain concept, so the ADR 0041 Button Reference is unchanged.

## Context

Every panel action today is per-bot: `POST /{broker}/accounts/{account_id}/bots/{sid}/actions` executes one `PanelActionRequest` under three invariants (action-scoped concurrency token, idempotency ledger, channel-derived identity — `action_execution_service.execute_action`). `flatten_stop` is Clerk-attributed by construction: the performer reduces exactly the requesting bot's attributed exposure and cancels its working entries, with per-bot receipts. Nothing in the stack can flatten more than one bot per request, and nothing presents a cohort-level affordance.

## Decision

### 1. A cohort flatten is a batch of per-bot `flatten_stop` legs — never an account-level position flatten

Each leg runs the **existing, unchanged** per-bot pipeline: the same availability guard, the same action-scoped concurrency token, the same idempotency ledger, the same performer, the same Clerk-attributed reduction and per-bot receipts. The cohort layer adds orchestration only. PRD #1752 user story 2 is inherited rather than re-proven: a leg cannot touch another bot's position because the per-bot action cannot.

### 2. Membership is explicit in the request; the backend never infers it at execution time

`POST /{broker}/accounts/{account_id}/bots/cohort-flatten` names the exact legs (`strategy_instance_id` + that leg's presented `concurrency_token` and `revision`). The backend executes precisely the named legs — membership, not exclusion. Inferring membership server-side at act time ("flatten everything in cohort X") would be an account-scoped fact driving per-bot behaviour: the defect family #1773 exists to kill, and the fact-scope rule (a fact must not drive consumers with a different admission contract) applies to affordances too.

### 3. The presentation is backend-authored and carries executability facts

`GET /{broker}/accounts/{account_id}/bots/cohort-flatten` groups the account's roster by `(strategy_key, symbol)` and, for every multi-member cohort, presents each member's **actual** `flatten_stop` action facts — `enabled`, the action-scoped `concurrency_token`, the panel `revision`, and the first blocker headline when disabled — extracted from the same per-bot panel projection the single-bot surface presents. ADR 0047's invariant holds by construction: a leg is presented armed only when the per-bot action is armed, because it *is* the per-bot action. The GET is a pure read (no broker contact, no reconciliation — it projects what the panel projects) and is fetched on demand, not polled.

### 4. Per-leg idempotency identity is derived: `{cohort_key}:{sid}`

The request carries one operator-supplied cohort idempotency key; each leg executes under `f"{cohort_key}:{sid}"` — distinct per leg (#1752 US11), deterministic under retry. A re-POST of the same cohort key replays applied legs as idempotent no-ops (`applied=false`, the existing ledger semantics) and retries only legs whose reservation was released (pre-execution refusals), which is exactly the recovery an operator wants after a partial wave.

### 5. A leg's failure never aborts the batch, and legs execute sequentially

Each leg resolves to a typed outcome: `applied`, `replayed` (idempotent no-op), or a typed refusal/failure reusing the existing `PanelActionErrorResponse` shape (`conflict` / `failure` / `unknown` + `reason_code` + backend-authored prose). A bot that became active again refuses its own leg (stale token or availability guard) without touching its siblings — #1802's constraint verbatim. Legs run sequentially in request order: the account authority is a single writer, so parallelism buys no wall time and would make the receipt order nondeterministic.

### 6. No new per-bot action id; the operator surface is a follow-up slice

The wire surface is two scoped routes plus their schemas. The roster-side affordance (cohort grouping UI, selection, the Two-Tap-style confirmation with blast-radius copy, per-leg outcome rendering) is deliberately its own implementation issue — it composes this mechanism and the panel's existing confirmation patterns, and shipping the mechanism first keeps the affordance honest (it can present only what the GET presents).

## Considered and rejected

**An account-level flatten at the broker** (one order per symbol netting the account flat). Violates PRD #1752 US2 directly: it collapses attribution, and a manual-operator position or a non-cohort bot's position on the same symbol would be flattened by a command aimed at neither.

**The frontend loops the existing per-bot endpoint.** Reduces clicks but: no backend-authored presentation of batch executability (the client would synthesize cohort-armed state — a client-derived fact ADR 0013/0047 forbid), N uncoordinated requests racing the sweep and each other's projections, and no cohort-scoped idempotent retry story (each loop iteration mints its own key; a crashed loop leaves no receipt naming the batch).

**Server-side membership resolution at POST time.** Rejected per Decision 2 — and it re-creates T3's own shape in reverse: an account-scoped command whose per-bot blast radius is computed after the operator committed to it.

**Parallel leg execution.** The account is single-writer; concurrency would interleave receipts without saving time.

**A new `cohort_flatten` per-bot ActionId in the vocabulary.** The per-bot action *is* `flatten_stop`; a second id for the same mutation would duplicate operator copy and split the idempotency/receipt history of the identical command.

## Consequences

- T3's remedy drops from N×3 clicks to: open the cohort surface → confirm one batch (with per-leg receipts and typed per-leg refusals coming back in one response). Reconcile/resume remain per-bot where they belong — they are admission decisions, not exposure reductions.
- The mechanism is symbol-agnostic and program-agnostic: a "fleet flatten" is the same POST with more legs; no new machinery is owed for the fleet case.
- **Follow-up owed (filed from #1802):** the roster/triage cohort affordance consuming these routes.
- The OpenAPI contract, generated frontend types, and their snapshots move with the new schemas in the same PR (repo contract-gate rule).
