# IBKR broker-activity reconciliation — conceptual reference

**Audience:** engineers extending the broker-activity surface (publisher, reconciler, templates, schemas).
**Not:** operator documentation (see `docs/runbooks/live-trade-reconciliation.md`); not a re-statement of ADR 0014 (see `docs/architecture/adrs/0014-broker-authored-operator-view-backend-rendered-narratives.md`).
**Pairs with:** ADR 0014 (the decision), ADR 0008 + 2026-06-22 amendment (identity ladder & sibling WAL).

A conceptual map for engineers extending the reconciler. The decision (why server-authored, why one row per IBKR execution, why a closed verdict enum) lives in ADR 0014 and is assumed here. The mechanics live in code. This doc bridges the two: the *concepts* a contributor needs before reading either.

## The hybrid state-event model

A broker-activity row is the join of two streams:

- **The engine stream** — `LiveStateEnvelope.submitted_orders` (`app/engine/live/live_state_sidecar.py:80`), the durable projection of `intent_events.jsonl` (ADR 0008 §3). Each entry is an `intent_id` the engine *intended* to submit, with sizing provenance, timestamps, and the `order_ref` it stamped.
- **The broker stream** — `IbkrOrderEvent`s pulled off `ib_async`'s `IB.execDetailsEvent` and `IB.orderStatusEvent` callbacks, surfaced as a single `AsyncIterator` by `app.broker.ibkr.orders.stream_order_events` (`PythonDataService/app/broker/ibkr/orders.py:683`). Each event has, at most, the `order_ref` IBKR echoes back on the order.

**Row identity is broker-authored, overlay is engine-authored.** The broker decides whether a row exists at all (no execution → no row, with one exception below). The engine, when it can be joined, decides what *should have happened* — requested qty, requested price, sizing policy, and the four-phase latency clock. Authoring happens at that join, in `broker_activity_reconciler.author_row_from_event` (`PythonDataService/app/services/broker_activity_reconciler.py:413`).

The one exception is `ENGINE_ONLY_PENDING`: an unacked intent. The publisher synthesises a placeholder row via `author_pending_row` so the cockpit's "Working / Pending Orders" panel has something to show. It is broker-empty by construction and transitions out when a real broker event arrives.

## Latency anatomy

Intent-to-observation latency decomposes into four phases. The reconciler computes all four in `compute_lag_breakdown` (`broker_activity_reconciler.py:141`) and stores them on the row's `EngineOverlay.lag_breakdown` (`schemas/broker_activity.py:78`):

| Phase | From → To | What it measures |
|---|---|---|
| `intent_to_dispatch_ms` | `intent.intent_created_ms` → `intent.dispatched_ms` | Engine internal: signal-to-`placeOrder` |
| `dispatch_to_ack_ms` | `intent.dispatched_ms` → `intent.acked_ms` | Wire/broker-internal: `placeOrder` → first ack |
| `ack_to_exec_ms` | `intent.acked_ms` → `event.exec_time_ms` | Broker queue + exchange routing |
| `exec_to_observed_ms` | `event.exec_time_ms` → publisher's `ts_ms` | Callback delivery to our process |

The operator-facing chip surfaces a single derived number, `intent_to_exec_ms` (decision-to-trade lag), computed once at row-author time so the frontend never does arithmetic on raw phases. The breakdown is for drill-down forensics. A `None` phase means its bounding timestamps were not both available (e.g. all phases `None` for a foreign exec).

## Identity matching — the namespace exact-equality invariant

ADR 0008 §1 is non-negotiable here: `order_ref` is `{bot_order_namespace}:{intent_id}`, where `bot_order_namespace = learn-ai/{strategy_instance_id}/v1`. Ownership is decided **only** by exact equality on the namespace — the part before the *final* `:`. `learn-ai/foo/v10:…` must not match `learn-ai/foo/v1`. Never `startswith`.

`parse_order_ref` (`broker_activity_reconciler.py:100`) does the split with `rpartition(":")` precisely because of this — splitting on the final colon is what makes namespace versions safe. `match_identity` (`broker_activity_reconciler.py:116`) consumes the parsed pair and checks namespace equality before doing the `intent_id` lookup against `submitted_orders`. Any other state is "foreign" (returns `None`), which the verdict ladder routes to `REASON.UNMATCHED_EXECUTION`.

Do not relax this to `startswith` for "`/v2` rollouts" — cross-version recognition is handled by an explicit dual-read allowed-namespace *set* per ADR 0008 §7; equality on each element still holds.

## Divergence taxonomy — live vs. backtest

The live surface has its own divergence vocabulary, `ReasonCode` (`schemas/broker_activity.py:50`), a 12-value closed `StrEnum` driving template selection. The backtest reconciler has a separate vocabulary, `DivergenceCategory` (`app/research/parity/qc_reconciler.py`), 8 values per `.claude/rules/numerical-rigor.md` § "Trade-level reconciliation taxonomy".

These are deliberately separate enums because the consumer concerns differ:

| Aspect | Backtest `DivergenceCategory` | Live `ReasonCode` |
|---|---|---|
| When | Post-hoc parity vs a reference run | Per-execution narrative for an operator watching live |
| Cardinality | 8, exhaustive over trade-pair disagreements | 12, including happy-path codes (`NORMAL_FILL`, `PARTIAL_FILL`) and lifecycle states (`CANCELLATION`, `REJECTION`, `PENDING_ACKNOWLEDGEMENT`) that aren't "divergences" at all |
| Gating | Acceptance gate for a reconciliation report | Drives `Verdict` (`expected` / `expected_with_caveat` / `unexpected` / `engine_only_pending`) and template selection |
| Tolerance | Tolerances pinned per category, justified per `numerical-rigor.md` | Per-instance `ReconciliationTimingPolicy` for lag-driven verdicts; price/qty divergence is binary |

`ReasonCode` is a *superset in spirit* — it covers everything `DivergenceCategory` covers (quantity / price / unmatched / duplicate) plus the live-only concerns (timing, reconnect-recovery, missing commission, pending acks, partial fills). They are siblings, not duplicates: collapsing them would either force the backtest path to carry lifecycle codes it doesn't need or force the live path to drop operator-facing distinctions.

A new live divergence ships as: add a `ReasonCode`, register a matching template, update `select_template`'s priority order if needed — never reach across into `DivergenceCategory`.

## Template versioning

Every `(template_key, template_version)` pair is a frozen pure function from a `facts` dict to `(headline, narrative)` (`app/services/broker_activity_templates.py`). Versions exist so historical rows render exactly as the operator originally saw them.

The discipline:

- A v2 ships as a **new `Template` constant**. The v1 constant stays registered.
- The row persists its own `template_version` (`schemas/broker_activity.py:200`).
- `current_version(key)` (`broker_activity_templates.py:328`) returns the highest version registered — used when newly authoring rows.
- Read-side rendering resolves a row through the pair `(template_key, template_version)` it stored; v1 rows render v1 strings forever.

A template-library improvement (better wording, more specific reasons) re-renders only *new* rows. A row authored last week with v1 wording continues to show v1 wording today, even if a v2 has shipped. This preserves operator-view reproducibility and is the truthfulness contract on the WAL.

The persisted row carries **both** structured facts AND rendered strings — strings frozen at write time, never re-rendered on read. Enforced by `frozen=True` / `extra="forbid"` on the row schema and a property test that asserts `render(facts) == row.headline + row.narrative` for every recorded fixture (ADR 0014 §3 truthfulness contract).

## What this doc is NOT

This repo's broker layer uses the **TWS API via `ib_async`**. The 2026-06-22 design pass surveyed IBKR's Client Portal Web API for comparison; its mechanics are deliberately *not* in the implementation and *not* documented here. Out of scope: `/tickle`, `cOID`, `/portfolio/{accountId}/ledger`, the `sor+` / `str+` / `spl+` / `ech+hb` WebSocket topic syntax, the 10-req/sec global rate limit, any other CP Web API surface.

If you find yourself reading CP Web API docs while extending the reconciler, you are on the wrong track. The authoritative broker surface here is `app/broker/ibkr/`.

## Pointers

| Concern | Where |
|---|---|
| Decision rationale | `docs/architecture/adrs/0014-broker-authored-operator-view-backend-rendered-narratives.md` |
| Identity ladder, `order_ref` ownership | `docs/architecture/adrs/0008-durable-submit-protocol-order-identity-recovery.md` (§1) |
| Sibling WAL contract | ADR 0008 amendment 2026-06-22 |
| Pure reconciler | `PythonDataService/app/services/broker_activity_reconciler.py` |
| Versioned templates | `PythonDataService/app/services/broker_activity_templates.py` |
| Stateful publisher | `PythonDataService/app/services/broker_activity_publisher.py` |
| WAL writer | `PythonDataService/app/services/broker_activity_wal.py` |
| Row schemas + enums | `PythonDataService/app/schemas/broker_activity.py` |
| Resume cursor | `PythonDataService/app/engine/live/live_state_sidecar.py` (`last_broker_activity_wal_seq`) |
| SSE + REST surface | `PythonDataService/app/routers/broker_activity.py` |
| Operator runbook | `docs/runbooks/live-trade-reconciliation.md` |
