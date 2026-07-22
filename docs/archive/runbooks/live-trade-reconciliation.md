> **Status:** Archived / superseded (2026-07-22).
> **Do not use as implementation authority or an operator procedure.**
> **Current authority:** `docs/bot-control-operator-manual.md`, ADR-0030, and `docs/references/ibkr-reconciliation.md` for engineering concepts.
> **Archived because:** Its recovery and callback statements predate the Clerk-backed account-cockpit workflow.

# Live-trade reconciliation — operator runbook

**Audience:** operators monitoring a live-paper bot via the bot control Activity view at `/broker/bots/:id`.
**Pairs with:** `docs/architecture/adrs/0014-broker-authored-operator-view-backend-rendered-narratives.md` (decision), `docs/references/ibkr-reconciliation.md` (engineer-facing reference).

The Activity tab shows one row per IBKR execution (and, once the pending-row gap below is closed, per unacked pending intent), authored server-side. The frontend renders verbatim — it does not derive verdicts, compose narratives, or compute lag chips. Every chip color and every word was decided by the backend when the row was written.

## Account Truth board

The broker pages now expose an account-wide Account Truth projection in addition to the per-bot Activity tab. Use it when the question is "is the whole IBKR account safe for bots to submit again?"

| Page | What to check first |
|---|---|
| Account Monitor | Overall Account Truth verdict, blockers, owner rollups, symbol exposure, buying power, margin, NLV, and P&L. |
| Reconciliation | Invariant cards: broker liveness, open orders known, completed orders known, executions assigned, positions explained, commission complete, Flex audit status. |
| Orders | Broker order ledger with open and recently completed/cancelled/rejected orders, owner label, evidence tier, lifecycle stage, `order_ref`, and IBKR evidence. |
| Per-bot Activity | The bot-specific intent and fill narrative after Account Truth says a row belongs to a bot namespace. |

If a trade is "in limbo":

1. Start on **Orders**. A live broker order whose lifecycle is `submitted`, `acknowledged`, or `limbo` remains visible in the ledger even before it fills. If it has no known namespace, it is `foreign_or_unclaimed`.
2. Check **Reconciliation**. `open_orders_known` fails critical for unknown live working orders. `all_executions_assigned` fails critical for an unknown execution. `positions_match_known_ownership` fails critical for unexplained current exposure.
3. Check **Account Monitor** for whether the unowned fact affects current exposure, margin, or buying power.
4. If the row is bot-owned, drill into that bot's Activity/Audit trail. If it is manual, the row should show app-minted manual evidence. If it is foreign/unclaimed, treat it as unresolved broker risk.

A hand-clicked TWS order is not automatically manual. If it has no app-minted `manual/{operator_or_session}/v1` `order_ref`, it remains foreign/unclaimed until the future audited adoption workflow ships. This is intentional: `clientId=0` tells you where the order was seen, not who meant to own it.

Completed/cancelled/rejected orders come from TWS/Gateway's completed-order surface. That surface helps the live ledger remember terminal orders after they leave the open-order list, but it is not the delayed official statement. Flex import remains the future official audit for settled executions, commissions, cash, and positions; until then `flex_audit_match` is `not_applicable`.

## What you see by default

Every row carries a single **verdict chip**. There are four values:

| Verdict | Color | Operator action |
|---|---|---|
| `expected` | Green | None. A normal fill within policy. |
| `expected_with_caveat` | Amber | Read the narrative. No intervention required, but the row is telling you *why* it's not boringly normal (timing, partial fill, reconnect window, missing fee). |
| `unexpected` | Red | Investigate. The headline + narrative names the divergence; the drill-down has the structured facts. |
| `engine_only_pending` | Grey | Transient — the engine submitted an intent that the broker has not yet acked. Surfaced in the "Working / Pending Orders" panel, not the main activity table. **Known gap:** the current publisher does not yet author these rows (see "Where the data lives" below). |

Forensic detail (price delta, lag in ms, sizing provenance) lives in the row's drill-down. It does *not* multiply the verdict — the row is `unexpected` or it isn't.

## What each headline means

The Activity tab uses 12 templates registered in `app/services/broker_activity_templates.py`. One paragraph per template:

**`normal_fill`** — Filled in full at the broker price; commission reported. Informational, no action.

**`pending_acknowledgement`** — Intended for "intent submitted, awaiting broker ack" rows in the Working panel. **Current shipped path caveat:** before the host-runner raw callback WAL lands, `BrokerActivityPublisher._handle_event` only authors rows in response to a broker event arriving from the live IBKR event stream (fill / cancel / error / matching status transition); it does *not* walk `LiveStateEnvelope.pending_intents` or call `author_pending_row`. After ADR 0014's 2026-06-25 raw-callback amendment is implemented, the publisher projects from `broker_callbacks.jsonl` and pending rows are superseded by the first matching callback row. To inspect unacked intents in the current shipped path, read `LiveStateEnvelope.pending_intents` directly from `live_state.json` under the instance's artifacts directory and check Diagnostics for an `ACK_FAILED_UNCERTAIN` incident (ADR 0008 §4).

**`partial_fill`** — Filled some shares; remaining still working. Informational while the order is open. A terminal order with remaining > 0 produces a subsequent cancel/expire row.

**`timing_caveat`** — Filled fine but intent-to-exec lag crossed the instance's `caveat_lag_ms` (default 2s). No other divergence. Raise the threshold or investigate broker latency if persistent.

**`reconnect_recovery`** — Intended for fills captured via a `reqExecutions()` sweep on broker resume; the lag chip reflects observation time, not exchange time. Under the post-2026-06-25 ownership model, reconnect-sweep callbacks are captured into `broker_callbacks.jsonl` and projected idempotently into authored rows. In older shipped builds where the publisher reads only live IBKR events, authoritative timing for the missing window lives in the durable mutation log.

**`missing_commission`** — Fill arrived but IBKR has not yet reported the fee. Row's `commission` is `null`. Under the raw-callback model, a later `commissionReport` callback is captured in `broker_callbacks.jsonl`; whether it authors a new row or enriches a rebuild is decided by the projector invariant in ADR 0014 §5.

**`price_divergence`** — Filled at a price not equal to the engine's requested limit. Only fires for LMT orders.

**`quantity_divergence`** — Filled a different share count than the engine requested.

**`unmatched_execution`** — IBKR reported an execution whose `order_ref` namespace does not match this instance's `bot_order_namespace`. No engine intent for this fill.

**`duplicate_execution`** — IBKR redelivered an execution the publisher already authored. Logged and suppressed from SSE; no action.

**`cancellation`** — Order cancelled before fill. No position change.

**`rejection`** — Broker rejected. No position change. The durable mutation log carries the broker's reason code.

## Escalation by template

For the verdicts that require action (`unexpected`) or attention (`expected_with_caveat`):

- **`unmatched_execution`** — CHECK, in order: manual TWS click on this DU account? Stale order from a prior run? Another client_id active under the DU account? If none: namespace logic may have regressed — open a bug; the row's `order_ref` names the foreign namespace.

- **`duplicate_execution`** — INFO. Publisher dedupes; WAL records the duplicate.

- **`quantity_divergence`** — INVESTIGATE. Broker-side: cash buffer, position cap, insufficient buying power. Engine-side: sizing rounding bug — compare `engine_overlay.sizing_provenance` against the requested fraction.

- **`price_divergence`** — INVESTIGATE. Market moved during transit (check `intent_to_exec_ms`), or order-type mis-port (spec said MKT but LMT was placed, or vice versa).

- **`missing_commission`** — INFO. Fee appears in the durable mutation log; no row re-author.

- **`reconnect_recovery`** — INFO. Cross-reference Diagnostics for the reconnect window. If `intent_to_exec_ms` is large *and* no reconnect incident lines up on the timeline, the recovery flag is wrong — open a bug.

- **`timing_caveat`** — INFO if isolated. If routine for an instance, the configured `caveat_lag_ms` is too tight — adjust `reconciliation_timing_policy.caveat_lag_ms` in the instance config.

## Where the data lives

- **Account Truth projection:** `GET /api/broker/account-truth` — backend-authored account-wide invariants, blockers, caveats, owner summaries, order rows, execution rows, and position rows.
- **Completed-order sweep:** `GET /api/broker/orders/completed` — recent terminal TWS orders from `reqCompletedOrdersAsync(apiOnly=false)`.
- **What-if preview:** `POST /api/broker/orders/what-if` — non-submitting paper preview used by the Orders confirmation dialog before manual paper submit.
- **Raw broker-callback record:** `<run_dir>/broker_callbacks.jsonl` — host-runner-owned append-only WAL and first-capture authority for broker callbacks (ADR 0014 amendment 2026-06-25). The data-plane publisher must be able to rebuild authored activity from this file.
- **Authored operator-view record:** `<run_dir>/broker_activity.jsonl` — append-only projection WAL carrying backend-authored `BrokerActivityRow`s. Rows projected from a raw callback carry `source_callback_seq`, `source_callback_type`, and the raw idempotency key; pending rows with no callback carry `source_callback_seq = null`.
- **Projection cursors:** `LiveStateEnvelope.last_broker_callbacks_wal_seq` is the highest raw callback `seq` projected into authored rows; `LiveStateEnvelope.last_broker_activity_wal_seq` is the highest authored row `seq` exposed through REST/SSE. If the authored projection is rebuilt, reset both cursors to `0`, replay `broker_callbacks.jsonl`, and recreate `broker_activity.jsonl`.
- **Live channel:** `/api/live-instances/{strategy_instance_id}/broker-activity/stream` (SSE).
- **Backfill:** `/api/live-instances/{strategy_instance_id}/broker-activity` (paginated REST by `seq`).
- **Row drill-down:** `engine_overlay` (intent_id, sizing provenance, lag breakdown by phase) + `divergence_facts` (price delta, qty delta, lag total). The headline is a summary; the drill-down has everything used to author it.

## How to disable or pause

Operator actions (pause, flatten-and-pause, stop) are governed by ADR 0010 (`docs/architecture/adrs/0010-operator-action-contract-flatten-pause-stop.md`). Use the sticky banner action toolbar; do not stop the Activity-tab SSE separately. Stopping the instance drains subscribers and closes the WAL cleanly.

The Activity tab cannot be hidden without stopping the instance. Intentional: it is the operator's only view of broker truth, and a "hide it" affordance would invite flattening blind.
