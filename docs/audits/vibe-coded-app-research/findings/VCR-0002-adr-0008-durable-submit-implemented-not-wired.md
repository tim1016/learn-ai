---
id: VCR-0002
severity: P0
status: open
area: broker-ownership
canonical_file: PythonDataService/app/engine/live/live_engine.py:1086
reference: docs/architecture/adrs/0008-durable-submit-protocol-order-identity-recovery.md
first_seen: 2026-06-14
last_seen: 2026-06-14
lens: broker-order-ownership-reconcile
dedupe_with_F: none
confidence: high
---

## What

ADR 0008 ("Durable submit protocol, order identity, recovery") defines the post-restart reconciliation contract that closes the relaunch-poisoning bug class: an intent WAL of `PENDING_INTENT → SUBMITTED → ACK_OK/ACK_FAILED_UNCERTAIN → FILL`; a content-addressed `orderRef` stamped onto every IBKR order; a cold-start reconciler that verifies open orders, in-flight intents, and persisted positions on engine boot; a reconciliation classifier; and `broker_ownership_query` with a positive-allowlist guard so foreign orders are never touched.

All of these modules **exist as fully-implemented Python**, with unit tests, and are referenced only by tests and by each other. The production live-engine path **never imports or invokes them**:

- `live_engine.py` and `run.py` do not import `ColdStartReconciler`, `IntentWal`, `submit_state_machine`, `reconciliation_classifier`, `build_order_ref`, or `VerifiedBrokerOwnershipQuery`.
- `live_engine.py:1086` reads, verbatim: *"RECONCILE is a runtime no-op — the ColdStartReconciler is the..."* The other three docstring references (lines 725, 976, 987) describe *what would happen* if the cold-start reconciler were wired — none of those passages corresponds to an actual call site.
- The IBKR order-placement path still uses the legacy `client_order_id` idempotency cache (`broker/ibkr/orders.py:49-73`), which ADR 0008 § 0 explicitly flags as the relaunch-poisoning bug class. `order.orderRef` is never set on `IB.placeOrder`.
- `broker_ownership_query.require_durable_submit_activation` correctly refuses every caller until a `VerifiedBrokerOwnershipQuery` subclass is registered — but no production path registers one, so the guard *always* refuses, which means there is no live ownership-query path at all.

The clean code is real and the tests pass. What is missing is the wiring that makes the durable-submit invariants gate the actual order flow. Until that wiring lands, every claim ADR 0008 makes about post-restart safety is unenforced.

## Where

- `PythonDataService/app/engine/live/live_engine.py:1086` — explicit "RECONCILE is a runtime no-op — the ColdStartReconciler is the…" docstring.
- `PythonDataService/app/engine/live/live_engine.py:725,976,987` — three additional docstrings that describe ColdStartReconciler intent but no call site exists.
- `PythonDataService/app/engine/live/live_engine.py` — no import of `cold_start_reconciler`, `intent_wal`, `submit_state_machine`, `reconciliation_classifier`, `order_identity`, or `broker_ownership_query`.
- `PythonDataService/app/engine/live/run.py` — same: no import of any ADR 0008 module.
- `PythonDataService/app/broker/ibkr/orders.py:49-73` — legacy `client_order_id` idempotency cache still active; ADR 0008 § 0 names this as the bug.
- `PythonDataService/app/engine/live/cold_start_reconciler.py` — `verify()` callable; only test consumers.
- `PythonDataService/app/engine/live/intent_wal.py` — `IntentWal.append/read_tail` correct, no production caller.
- `PythonDataService/app/engine/live/submit_state_machine.py` — `next_action()` with RETRY_CAP=1 and `NOT_PROVABLE → HALT`, no production caller.
- `PythonDataService/app/engine/live/reconciliation_classifier.py` — `classify()` with per-perm_id/exec_id precedence, no production caller.
- `PythonDataService/app/engine/live/order_identity.py` — `build_order_ref`, `parse_order_ref`, no production caller.
- `PythonDataService/app/engine/live/broker_ownership_query.py` — `require_durable_submit_activation` correctly refuses bare callers, but no `VerifiedBrokerOwnershipQuery` subclass is registered, so the guard always refuses.

## Why this severity

PRD §7 P0: "can silently corrupt live/paper trading, position sizing, fills, P&L, **timestamps, broker state, reconciliation**, or ingestion. Can cause unintended orders or prevent expected flattening."

Without the durable-submit + cold-start-reconciler path:

- A crash mid-submit leaves an `ACK_FAILED_UNCERTAIN`-class state that no production code can recover from. On restart, the engine has no view of what made it to the broker; it cannot safely either retry or cancel.
- A fill that arrives between sessions (`perm_id` known to the broker but not to in-memory `_order_meta`) is silently dropped — the broker books the position, the engine portfolio does not (see also VCR-0003).
- Foreign account activity is technically unsafe even though `broker_ownership_query` is built to gate it: there is no wired ownership-precedence check at the order-cancellation surface; the legacy `_owned_order_ids` set in `IbkrBrokerAdapter` is the only gate, and it is in-memory only (lost on restart).
- The shadow-mode adapter (`NoSubmitBrokerAdapter`) is built per ADR 0002 and has structural invariants, but those invariants are not asserted at runtime by any check that runs in the production loop.

This is silent broker-state corruption on the exact paths the ADR was written to protect. Paper-only blast radius today, same code path on live tomorrow.

## Trading impact

- **Post-restart drift**: an open IBKR order placed before a crash has no `orderRef` and no WAL entry. After restart, the engine cannot identify it as its own; it will either ignore it (engine portfolio stale) or potentially cancel it if a sibling guard fires (un-owned cancel).
- **Unrecoverable submit-mid-ack failures**: ADR 0008's `ACK_FAILED_UNCERTAIN` taxonomy and HALT-on-NOT_PROVABLE rule are not in flight; a network glitch during submit becomes a permanent uncertain state.
- **Reconciliation by name only**: existing reconcile.py paths cross-reference broker positions to in-memory state by symbol + sign, not by intent. Two strategies on the same symbol cannot be disambiguated.

## Reproduction

Static trace, confirmed:

```bash
# No ADR 0008 modules are imported by the live engine:
grep -nE "ColdStartReconciler|IntentWal|submit_state_machine|build_order_ref" \
  PythonDataService/app/engine/live/{live_engine,run}.py
# All matches are docstrings, no `from … import …`

# Legacy client_order_id cache still in use:
grep -nE "client_order_id|IDEMPOTENCY_CACHE" \
  PythonDataService/app/broker/ibkr/orders.py | head

# Confirm RECONCILE is documented as runtime no-op:
sed -n '1080,1090p' PythonDataService/app/engine/live/live_engine.py
```

## Suggested resolution (NOT auto-applied)

This is multi-PR work. Sequence:

1. **Wire `IntentWal.append` on every order placement** (`broker/ibkr/orders.py::place_paper_order` writes `PENDING_INTENT` before submit; writes `SUBMITTED` after ACK; writes `FILL` from execution callbacks). Drop the legacy `_IDEMPOTENCY_CACHE` once intent IDs are the dedup key.
2. **Stamp `order.orderRef = build_order_ref(...)` on every IB.placeOrder** so post-restart ownership is verifiable from the broker alone.
3. **Invoke `ColdStartReconciler.verify()` from `cmd_start`** before any user signal can produce an order. Halt on any unclassified divergence.
4. **Register a `VerifiedBrokerOwnershipQuery` subclass** for IBKR so `broker_ownership_query.require_durable_submit_activation` admits live callers.
5. **Add `submit_state_machine.next_action` to the submit retry loop** so RETRY_CAP=1 and `NOT_PROVABLE → HALT` actually gate behaviour.
6. **Move reconcile.py to delegate to `reconciliation_classifier.classify`** so ownership precedence is uniform.

Until that work is done, the live trading surface should be banner-gated to "ADR 0008 not wired — restart requires manual reconciliation" rather than presenting the current optimistic readiness verdict.

## Provenance of the finding

Lens: `broker-order-ownership-reconcile` (workflow `wf_def78013-ce4`). Lens summary identified the gap; main-loop verified by direct read of `live_engine.py:1086` (the verbatim "runtime no-op" comment) plus grep against `live_engine.py`/`run.py`/`broker/ibkr/orders.py` confirming no import or invocation of the ADR 0008 module set.
