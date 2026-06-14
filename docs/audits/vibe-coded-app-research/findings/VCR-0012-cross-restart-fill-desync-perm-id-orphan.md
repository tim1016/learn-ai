---
id: VCR-0012
severity: P1
status: open
area: broker-ownership
canonical_file: PythonDataService/app/engine/live/live_engine.py::_convert_ibkr_fill
reference: docs/architecture/adrs/0008-durable-submit-protocol-order-identity-recovery.md
first_seen: 2026-06-14
last_seen: 2026-06-14
lens: broker-order-ownership-reconcile
dedupe_with_F: none
confidence: medium
---

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
