---
id: VCR-0002
severity: P0
status: phase_5c_structural_complete_operator_gated
area: broker-ownership
canonical_file: PythonDataService/app/engine/live/live_engine.py:1086
reference: docs/architecture/adrs/0008-durable-submit-protocol-order-identity-recovery.md
first_seen: 2026-06-14
last_seen: 2026-06-14
remediation_progress:
  - "#496 — Phase 4 — Operator-trust mitigation (UI banner + RECONCILE accepted_noop)"
  - "#497 — Phase 5A — Intent identity foundation (intent_id, order_ref, PENDING_INTENT / SUBMITTED / ACK_FAILED_UNCERTAIN WAL)"
  - "aae1cf2c — Phase 5B — Require IntentWal + order_ref for real IBKR submits (ColdStartReconciler gate)"
  - "#533 — Phase 5D — submit_state_machine wired into submit_pending_orders (RETRY_CAP=1, NOT_PROVABLE→HALT, SUBMIT_UNCERTAIN_HALTED WAL event, SubmitUncertainHaltError)"
  - "#535 — Phase 5D Resume WAL guard — cmd_resume refuses on unresolved ACK_FAILED_UNCERTAIN with --force override"
  - "#536 — Phase 5E — _convert_ibkr_fill cross-restart classifier via folded intent WAL keyed by perm_id"
  - "#539 — Phase 5C — IbkrBrokerOwnershipQuery(VerifiedBrokerOwnershipQuery) subclass implementing namespace-scoped open_orders + executions queries against ib_async caches; passes require_durable_submit_activation structural gate"
  - "#543 — Phase 5C — LiveConfig.durable_submit_enabled activation flag wired into LiveEngine.__init__; default False keeps backward compatibility"
  - "#545 — Phase 5C — LiveEngine._flatten cancel-confirm timeout (CANCEL_CONFIRM_TIMEOUT_S=5.0); on timeout writes halt.flag + raises CancelConfirmTimeoutHaltError, refuses liquidation"
  - "#546 — Phase 5C — _recovery_flatten (managed: halt on timeout) + cmd_emergency_flatten (force: log EMERGENCY_FLATTEN_WITH_UNCONFIRMED_CANCELS audit row and proceed); all three flatten paths now cancel-confirm-protected"
follow_up_required:
  - "Phase 5C activation flip — operator enables require_durable_submit_activation(enabled=True, ownership_query=IbkrBrokerOwnershipQuery(client), verified_order_ref_cap=VERIFIED_ORDER_REF_CAP) after the deployment_validation paper deploy proves IBKR returns prior-run orders/executions carrying orderRef across reconnect (Acceptance Gate #2 behavioral receipt). This is the operator-controlled flip; the structural prerequisites are now all in place."
  - "OWNERSHIP_QUERY_UNAVAILABLE_HALT — emitted via halt.flag mechanism when activation is on and the ownership query subclass cannot return for a managed flatten path (deferred until operator flips activation, since the halt taxonomy doesn't fire without the gated query active)"
lens: broker-order-ownership-reconcile
dedupe_with_F: none
confidence: high
---

## Phase 5A progress (#497) — intent identity foundation

Wired the ``intent_id ↔ order_ref ↔ attempted broker order`` invariant:

- ``LivePortfolio`` accepts optional ``intent_wal`` + ``bot_order_namespace``.
  ``set_holdings`` mints an ``intent_id`` **only after** sizing resolves to
  ``delta != 0`` (a skip never reserves an identity).
- ``submit_pending_orders`` builds ``order_ref = build_order_ref(namespace,
  intent_id)``, stamps it on ``IbkrOrderSpec.order_ref`` (new field), and
  fsyncs ``PENDING_INTENT`` BEFORE ``broker.place_order`` is called.
- On success: ``SUBMITTED`` is appended with ``order_id`` and ``perm_id`` (if
  the ack carries one). On exception: ``ACK_FAILED_UNCERTAIN`` with error
  context (no silent swallow — the submit is genuinely uncertain and the
  WAL preserves that).
- ``app/broker/ibkr/orders.py::_build_order`` stamps the spec's
  ``order_ref`` onto ``ib_async.order.orderRef`` so the IBKR Gateway
  echoes it on every order callback. The runtime can now join fills /
  cancels by the deterministic token across restarts.
- Existing in-memory ``sizing_resolutions`` list is unchanged so the
  Sizing card keeps rendering during the transition (PRD §5A); Phase 8
  swaps the list for a WAL fold over ``SIZING_RESOLVED`` / ``SIZING_SKIP``.

The full closure of VCR-0002 needs Phases 5B–5E (cold-start reconciler,
ownership classifier, submit-retry state machine) plus Phase 8 (sizing
WAL fold). Phase 5A is the load-bearing identity layer those phases
build on.

Regression tests in ``tests/engine/live/test_intent_identity_wiring.py``:

- ``test_ibkr_order_spec_accepts_order_ref``
- ``test_set_holdings_with_zero_delta_does_not_mint_intent_id``
- ``test_set_holdings_with_non_zero_delta_mints_intent_id``
- ``test_submit_pending_orders_stamps_order_ref_on_spec``
- ``test_submit_pending_orders_writes_pending_intent_before_submit``
- ``test_submit_pending_orders_writes_submitted_after_success``
- ``test_submit_pending_orders_writes_ack_failed_uncertain_on_exception``
- ``test_legacy_portfolio_without_wal_keeps_working``

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

## Acceptance Gate #2 — behavioral receipt (captured 2026-06-16)

Captured during a live HITL paper-broker run against IBKR (account `DUM284968`, paper port 4002) from master `d4b1953a` + a not-yet-committed in-place patch (see "Production-wiring gap" below). Receipt path: deploy → first SUBMITTED → engine stop → engine restart → cold-start reconciler queries IBKR via the namespaced `executions_for_namespace` and matches the prior session's `orderRef` tokens byte-identical.

### What was observed

```
BUY  1 SPY @ 753.35   permId=567189495   orderId=22
  orderRef = learn-ai/dep_val_smoke_002/v1:78F5yVtDSaiz8AQBjO0SzQ

SELL 1 SPY @ 753.76   permId=567189496   orderId=24
  orderRef = learn-ai/dep_val_smoke_002/v1:vRNr9yzMQkece9Zaf-Lkrg
```

Both fills carried the constructed `order_ref` token. IBKR returned them on `execDetails` callbacks immediately after the new (post-restart) engine connected, before any new bars were processed. The orderRef was preserved byte-identical across the engine restart.

### What this proves

1. IBKR preserves `Order.orderRef` through `place_order → ack → fill → executions` for paper STK / MKT orders. The `orderRef` we stamped via `_build_order` (the `ib_async.MarketOrder(orderRef=...)` constructor) survived the full lifecycle.
2. IBKR returns `orderRef` on the `execDetails` callback consumed by the new engine's cold-start reconciler — `ib_async.IB.fills()[…].execution.orderRef` carries the same token across session restart.
3. `Order.permId` materialised on both executions (`567189495` BUY, `567189496` SELL) — the durable IBKR fingerprint that survives reconnects and is the join key for the Phase 5E cross-restart classifier (#536).
4. Namespace + intent_id schema (`learn-ai/<strategy_instance_id>/v1:<22-char base64 intent_id>`) round-trips cleanly. Total length: **50 characters**.

### Verified orderRef cap

The observed orderRef length was **50 characters**. Recommended cap for activation: **`durable_submit_verified_order_ref_cap=60`** (gives 10-character headroom for namespace schema or intent_id encoding evolution before the cap needs re-validation).

### Production-wiring gap (must ship before activation flip)

This receipt was captured with an **in-place patch** on the host venv's `app/engine/live/{live_engine.py, run.py}` that wires the IntentWal into LivePortfolio at engine construction time. The patch is required because Phase 5B (`aae1cf2c`) added a fail-fast `LivePortfolio.__post_init__` check (real broker + no IntentWal → `ValueError`), but the production `run.cmd_start → LiveEngine` path was never updated to thread `intent_wal_path` through to `LivePortfolio(intent_wal=..., bot_order_namespace=...)`.

**Patch summary (NOT YET COMMITTED — must land as a PR before the activation flip):**
- `live_engine.py:629` — `LivePortfolio(self._broker)` → `LivePortfolio(self._broker, intent_wal=IntentWal(self._intent_wal_path), bot_order_namespace=f"learn-ai/{self._strategy_instance_id}/v1")` when `intent_wal_path` is set.
- `run.py:1192-1217` — `LiveEngine(...)` constructor call gains `intent_wal_path=args.run_dir / "intent_events.jsonl"`.

Without this patch, the production cmd_start path crashes on construction with `ValueError: ADR 0008 / Phase 5B: LivePortfolio with a real-broker adapter (IbkrBrokerAdapter) cannot be constructed without an IntentWal`. The patch closes that gap; activation flip requires this patch to be in master.

### Operational notes observed during capture

- **IBKR Gateway disconnect frequency.** During the ~60-minute capture window the IBKR session dropped twice (`Error 1100: Connectivity between IBKR and Trader Workstation has been lost`) at roughly 20-25 minute intervals. The `IBKRBarStreamError` is raised fatally at the bar-stream source and is not intercepted by Phase 3 reconnect re-validation (#542). Daemon restarted the engine each time. Worth investigating Gateway settings (`Auto restart`, idle timeout) before flipping `durable_submit_enabled=true`, since the cascade is more sensitive to mid-position restarts.
- **VCR-P3-K timestamp drift.** The cockpit Failures panel displayed event timestamps offset by 5 hours (CDT host time parsed as UTC, then re-converted to CDT for display). Cosmetic only; the engine's `live.log` timestamps were correct. Tracked separately as VCR-P3-K.
- **No `halt.flag` written**, no `poisoned.flag` written, no `ACK_FAILED_UNCERTAIN` events — the durable submit cascade behaved exactly as ADR 0008 specifies on the happy path.

### Sidecar projection

`live_state.json` at `artifacts/live_state/dep_val_smoke_002/live_state.json` after the run shows the two `submitted_orders` keyed by `client_order_id` (`live-1`, `live-2`) with `perm_id` populated, `known_perm_ids = [567189495, 567189496]`, position flat. This is the sidecar shape the next session's ColdStartReconciler reads — and on the actual restart it correctly matched all of these against IBKR's response.

### Status flip readiness

With this receipt:

- ✅ Acceptance Gate #1 (structural): require_durable_submit_activation passes with IbkrBrokerOwnershipQuery (#539, #543).
- ✅ Acceptance Gate #2 (behavioral): IBKR echoed orderRef on `execDetails` across engine restart (this section).
- ❌ Production-wiring patch (above): must land in master as a separate PR before the activation flip is safe.
- ❌ Gateway stability (above): two disconnects in 60 minutes is abnormal; investigate before flipping to avoid mid-position activation hangs.

Once those two are resolved, the operator can ship a deploy with `live_config = {"durable_submit_enabled": true, "durable_submit_verified_order_ref_cap": 60, "sizing": {...}}` and the full Phase 5C cascade activates.
