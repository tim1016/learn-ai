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

The one exception is `ENGINE_ONLY_PENDING`: an unacked intent. The publisher synthesises a placeholder row via `author_pending_row` so the bot control page's "Working / Pending Orders" panel has something to show. It is broker-empty by construction and transitions out when a real broker event arrives.

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

## Account Truth projection

The Account Truth board is the account-wide sibling of the per-bot Activity stream. It lives in `PythonDataService/app/broker/ibkr/account_truth.py`, is exposed as `GET /api/broker/account-truth`, and emits the `AccountTruthResponse` schema in `PythonDataService/app/schemas/account_truth.py`.

Its job is different from Activity:

| Surface | Unit of truth | Primary question |
|---|---|---|
| Per-bot Activity | One broker callback/execution joined to one bot's intent evidence | Did this bot's broker outcome match its intended order? |
| Account Truth | The whole connected IBKR account joined against every known bot namespace and manual namespace | Is every live account fact assigned, or should bot submits stay blocked? |

The projection gathers live TWS/Gateway evidence from account summary, positions, open orders, completed orders, and execution sweeps. Bot ownership is derived from the durable account instance registry (`accounts/<account_id>/instance_registry.jsonl`), not the in-process publisher registry. It uses two namespace views: the attribution view contains every namespace ever bound to the account, including retired bindings, while the active-known/current-risk view contains currently-bound `DEPLOYED` or `ACTIVE` namespaces. Terminal facts use the attribution view; live open orders and current positions use the current-risk view and surface retired owners as named live-exposure anomalies. Orders and executions classify as bot/manual/foreign-or-unclaimed; current positions can additionally classify as mixed-known when more than one known owner has filled evidence for the same contract:

| Owner class | Evidence accepted |
|---|---|
| `bot` | `order_ref` namespace exactly equals a registry-known `learn-ai/{strategy_instance_id}/v1` namespace. Terminal facts can be attributed to retired bindings; live working orders or current positions under a retired binding emit `owner_binding_state=RETIRED`, severity `critical`, and blocker `retired_owner_live_exposure`. |
| `manual` | App-submitted manual paper order with a reserved `manual/{operator_or_session}/v1:{intent_id}` `order_ref`. The current MVP uses `manual/operator/v1` until an authenticated operator/session principal exists at this route. |
| `mixed_known` | Positions only: a current position is explained by more than one known bot/manual owner for the same contract. |
| `foreign_or_unclaimed` | Missing `order_ref`, unparseable `order_ref`, never-registered namespace, TWS hand-click, other API session, stale process, or any broker fact not proved by a known namespace. A retired-but-known namespace is attributable and is not collapsed into this class. |

`clientId` is forensic context only. A TWS click on `clientId=0` with no namespace is still `foreign_or_unclaimed`; it is not auto-classified as manual. This preserves the same fail-closed contract as the broker-activity reconciler: absence of identity is evidence of uncertainty, not evidence of safety.

Account Truth emits backend-authored invariant rows rather than leaving Angular to infer pass/fail:

| Invariant | Failure meaning |
|---|---|
| `broker_liveness_proven` | The connected broker session is not live enough to prove account truth. |
| `open_orders_known` | At least one live working broker order is foreign/unclaimed, owned by a retired binding, or the open-order sweep is unavailable. Critical. |
| `completed_orders_known` | Recent terminal order evidence is incomplete or contains unclaimed rows. Warning in the MVP because terminal history is lower live-risk than working orders. |
| `all_executions_assigned` | At least one execution sweep row is foreign/unclaimed, or execution evidence is unavailable. Critical. |
| `positions_match_known_ownership` | A current position is not explained by known bot/manual evidence, or is attributed only to a retired owner. Critical. |
| `commission_complete` | One or more executions are missing IBKR `commissionReport` evidence. Warning; missing fees stay `None`, never fabricated as zero. |
| `flex_audit_match` | `not_applicable` until the delayed Flex import slice ships. |
| `duplicate_exec_id_suppressed` | IBKR redelivered an `execId`; the projection keeps the first observation, backfills missing fields from later observations, and reports the duplicate as a caveat. |

Rows preserve broker identifiers for audit: `execId` dedupes executions, `permId` groups an order lifecycle when available, `order_ref` assigns ownership, and `orderId` is displayed only as broker evidence.

`GET /api/broker/account-truth` also stores the latest composed projection in the process-local Account Truth snapshot cache (`PythonDataService/app/services/account_truth_snapshot.py`). Bot Control status reads consume only that cached projection by account id. A missing cache entry, a cache entry older than the hard readiness TTL, or a cached `final_verdict != clean` folds into `operator_surface.submit_readiness` as `broker_state_unproven` with `ACCOUNT_TRUTH_*` blocking reason codes. `LivePortfolio.submit_pending_orders` consumes the same cached projection through the `account.account_truth` `GateResult` and blocks non-pass results before any broker call or AccountOwner handoff. The readiness fold and submit gate perform no IBKR sweep and write no freeze artifact.

### Manual adoption boundary

Post-hoc adoption of foreign/unclaimed rows is deliberately not part of the MVP projection. When it ships, it must be an append-only adoption ledger folded over raw broker facts, keyed by durable identity (`permId` for orders, `execId` for executions). Adoption is a human claim layered over broker-foreign identity; it must not rewrite raw broker evidence or silently mark a row safe. Live-working order adoption is especially sensitive because it can clear the unknown-open-order bot-submit block.

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
| Account-wide truth projection | `PythonDataService/app/broker/ibkr/account_truth.py` |
| Account Truth schema | `PythonDataService/app/schemas/account_truth.py` |
| Account Truth REST surface | `GET /api/broker/account-truth` in `PythonDataService/app/routers/broker_account_truth.py` |
| Resume cursor | `PythonDataService/app/engine/live/live_state_sidecar.py` (`last_broker_activity_wal_seq`) |
| SSE + REST surface | `PythonDataService/app/routers/broker_activity.py` |
| Operator runbook | `docs/runbooks/live-trade-reconciliation.md` |
