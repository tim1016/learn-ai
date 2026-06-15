---
id: VCR-0012
severity: P1
status: remediated
area: broker-ownership
canonical_file: PythonDataService/app/engine/live/live_engine.py::_convert_ibkr_fill
reference: docs/architecture/adrs/0008-durable-submit-protocol-order-identity-recovery.md
first_seen: 2026-06-14
last_seen: 2026-06-14
regrounded_on: 2026-06-14
regrounded_to: high
phase_0_verdict: confirmed_valid
remediated_in: "Phase 5E — _convert_ibkr_fill consults the durable intent WAL when in-memory _order_meta misses, reconstructs _OrderMeta from the SubmittedOrderView's order_spec (symbol/action/quantity) keyed by perm_id. Cross-restart bot-owned fills now record into portfolio."
lens: broker-order-ownership-reconcile
dedupe_with_F: none
confidence: high
---

## Phase 0 re-grounding (2026-06-14)

**Verdict:** CONFIRMED VALID. Survives Phase 5A; Phase 5A built the durable WAL but the converter doesn't read it.

**Evidence at current HEAD:**

- `_convert_ibkr_fill` at `live_engine.py:1591-1608` is gated **exclusively** on `self._order_meta.get(int(fill.order_id))`. If `meta is None`, the fill is logged-and-dropped (lines 1602-1608). No perm_id fallback.
- `reconciliation_classifier.classify` at `reconciliation_classifier.py:125-232` implements the correct precedence (perm_id → exec_id → exact-namespace order_ref match). **Zero call sites** in the production fill path.
- `parse_order_ref` / `build_order_ref` at `order_identity.py:129-141` exist and are used at submit time (Phase 5A stamping) but not consulted at fill conversion time.
- Phase 5A stamps `intent_id`/`order_ref` on outbound `IbkrOrderSpec` at `live_portfolio.py:544, 558, 571-574` and writes SUBMITTED WAL with `perm_id` (`intent_events.py:80`). Durable artifacts exist. The converter just never folds them back.

**Current behavior on cross-restart fill:**

- Restart loses `_order_meta` (in-memory only).
- An IBKR fill carrying a `perm_id` that was SUBMITTED by a prior session arrives with `order_id` unknown to the fresh `_order_meta`.
- Line 1605-1608 drops the fill → engine portfolio never records it → broker position diverges from engine forever.

**Phase 5E plug-in point:**

Replace lines 1602-1608 with a fallback to `classify_ownership(order_ref, perm_id, exec_id, allowed_namespaces, known_intent_ids, known_perm_ids, known_exec_ids)` driven by the intent WAL view. When ownership ≠ NONE, reconstruct `meta` from the WAL's SUBMITTED event (or `IntentEvent.order_spec`) instead of dropping. Detailed example pseudo-code in `docs/audits/vibe-coded-app-remediation-prd.md` §5E.

## What

`LiveEngine._convert_ibkr_fill` drops fills whose IBKR `order_id` is absent from the in-memory `_order_meta` dict — even when the fill's `perm_id` proves it is bot-owned from a prior session. The dropped fill is one the broker has booked; the engine portfolio does not record it. On the next `set_holdings` (or any portfolio-state-aware decision), the engine computes deltas against a stale view.

The `reconciliation_classifier.classify` module (which uses `perm_id`/`exec_id` precedence then exact-namespace match) is the correct ownership-precedence path, but it is not wired into the production fill path (see VCR-0002). The legacy `_order_meta` dict is what gates the fill recording today, and it is in-memory only — lost on every restart.

## Where

- `PythonDataService/app/engine/live/live_engine.py::_convert_ibkr_fill` — drops fills missing from `_order_meta`.
- `PythonDataService/app/engine/live/reconciliation_classifier.py::classify` — correct precedence (perm_id, exec_id, exact-namespace match), unused in production.
- `PythonDataService/app/engine/live/order_identity.py::parse_order_ref` — `rpartition(":")` ownership trick, unused in production.

## Why this severity

PRD §7 P1: "silent corruption of broker state, reconciliation". The cross-restart desync is exactly the bug class ADR 0008 was written to close. Reading the fill (and the recordedness of the position) determines every subsequent sizing decision; if the engine view diverges from the broker view, every order placed downstream is from a wrong base. Paper-only blast radius today.

Not P0 only because the desync window opens at engine boot — operators could in principle re-deploy and reconcile manually before any orders flow.

## Trading impact

- Engine restart with an outstanding bot order. Broker fills it during downtime. Engine on restart drops the fill (no `_order_meta` entry). Engine portfolio cash and position diverge from broker forever (or until the operator manually reconciles).
- Subsequent `set_holdings(symbol, 1.0)` computes target shares against stale engine view; the engine submits an order to "buy what the broker thinks we already own".

## Reproduction

```bash
grep -n '_convert_ibkr_fill\|_order_meta' PythonDataService/app/engine/live/live_engine.py | head
# Confirm: a fill with perm_id known to the broker but order_id absent in _order_meta → dropped (no recording).

grep -n 'classify\|perm_id\|exec_id' PythonDataService/app/engine/live/reconciliation_classifier.py | head
# Confirm: the correct precedence path exists, unused.
```

## Suggested resolution (NOT auto-applied)

Companion to VCR-0002. Specifically:

1. Wire `_convert_ibkr_fill` to consult `reconciliation_classifier.classify` instead of the bare `_order_meta` lookup. The classifier's `perm_id`/`exec_id` precedence handles bot-owned fills from prior sessions correctly.
2. Persist `_order_meta` (or its replacement, the intent ledger) durably so restarts recover ownership without needing to recompute.
3. Add a test that simulates a fill arriving with `order_id` absent but `perm_id` matching a prior-session bot order → asserts the engine records the position.

## Provenance of the finding

Lens: `broker-order-ownership-reconcile` (workflow `wf_def78013-ce4`). Surfaced in lens summary; specific `_convert_ibkr_fill` line range not re-verified by main loop. `medium` confidence pending direct read.
