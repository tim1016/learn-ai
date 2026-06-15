---
id: VCR-0009
severity: P1
status: remediated
area: halt-state-machine
canonical_file: PythonDataService/app/engine/live/run.py::cmd_emergency_flatten
reference: PRD §12.8
first_seen: 2026-06-14
last_seen: 2026-06-14
regrounded_on: 2026-06-14
regrounded_to: high
phase_0_verdict: confirmed_valid
remediated_in: "fix(vcr-0009) — cmd_emergency_flatten now calls broker.cancel_open_orders before the liquidation loop, matching the sibling _recovery_flatten / LiveEngine._flatten pattern. Force-flatten carve-out: cancel failure is logged loudly but liquidation proceeds (leaving open positions during a panic is worse than acting without cancel confirmation)."
follow_up_required:
  - "Full Phase 5C ownership-query gating (OWNERSHIP_QUERY_UNAVAILABLE_HALT + cancel-confirm timeout + EMERGENCY_FLATTEN_WITH_UNCONFIRMED_CANCELS carve-out) — separate remediation"
  - "Cross-restart fill-conversion classifier (VCR-0012) — Phase 5E"
lens: halt-pause-stop-flatten-poison
dedupe_with_F: none
confidence: high
---

## Phase 0 re-grounding (2026-06-14)

**Verdict:** CONFIRMED VALID. The claim survives direct evidence at current HEAD.

**Evidence:**

- `cmd_emergency_flatten` at `PythonDataService/app/engine/live/run.py:1547-1663` (inner `_flatten()` at 1609-1649): the path is `broker.fetch_positions()` (line 1619) → liquidation loop → `broker.place_order(spec)` (line 1645). **No `cancel_open_orders` call** appears before the liquidation orders fire.
- Sibling paths *do* cancel-first: `_recovery_flatten` at `run.py:413-507` calls `cancelled = await broker.cancel_open_orders()` at line 469 before the liquidation loop at line 484. `live_engine.py::_flatten` at lines 1347-1387 calls the same at line 1369. The asymmetry called out in the original finding holds.
- The companion `_convert_ibkr_fill` gap at `live_engine.py:1591-1639` (drops fills with unknown `order_id`) is the same issue tracked separately as VCR-0012.
- Phase 6A (#498 / commit 24bb655d) shipped flatten-and-pause but did **not** touch `cmd_emergency_flatten`'s cancel sequencing.

**Outstanding work for this finding:**

1. `cmd_emergency_flatten` needs `cancel_open_orders` before the liquidation loop, per the Phase 5C contract ("Cancel-then-liquidate ordering" — PRD §5C step 4). The emergency-flatten carve-out (`force=True`) per Phase 5C may proceed past cancel-confirm timeout with audit event `EMERGENCY_FLATTEN_WITH_UNCONFIRMED_CANCELS`, but it must still **attempt** the cancel.
2. Cross-restart fill ownership (`_convert_ibkr_fill` perm_id fallback) is Phase 5E.

## What

`cmd_emergency_flatten` places market liquidations **without first calling `broker.cancel_open_orders()`**. Sibling paths (`recovery_flatten`, `force_flat`) both cancel-then-liquidate. The asymmetry means an open bot order (e.g., a SELL limit at $X) can race with the emergency SELL market and double-sell: the limit ACKs and fills; the market ACKs and fills; net position goes short by one fill size.

There is also a related runtime gap (lens 5): `_convert_ibkr_fill` drops fills whose `order_id` is absent from in-memory `_order_meta`, even when `perm_id` proves the fill is bot-owned from a prior session. The combination of this and the missing cancel-before-liquidate makes a crash-during-emergency-flatten unsafe — the engine portfolio loses the over-sell while the broker books it.

## Where

- `PythonDataService/app/engine/live/run.py::cmd_emergency_flatten` — liquidation path, no `cancel_open_orders` call.
- `PythonDataService/app/engine/live/run.py::recovery_flatten` / `force_flat` — cancel-then-liquidate pattern (the contrast).
- `PythonDataService/app/engine/live/live_engine.py::_convert_ibkr_fill` — drops perm_id-bot-owned fills if `order_id ∉ _order_meta`.

## Why this severity

PRD §7 P1: "can cause unintended orders or prevent expected flattening." Not P0 because the operator would notice the over-sell at the next snapshot (broker account shows -1×Q while expected is flat). But the panic-button path of last resort is allowed to over-sell.

## Trading impact

- Operator presses Emergency Flatten with an active SELL limit on the same symbol → both orders fire → net position goes short.
- Combined with the cross-restart fill desync, a mid-flatten crash leaves the broker account holding state the engine cannot reconstruct.

## Reproduction

```bash
grep -n 'cmd_emergency_flatten\|recovery_flatten\|force_flat' PythonDataService/app/engine/live/run.py
# Confirm: recovery_flatten / force_flat call cancel_open_orders;
# cmd_emergency_flatten does not.
grep -n 'cancel_open_orders' PythonDataService/app/engine/live/run.py
grep -n '_convert_ibkr_fill\|_order_meta' PythonDataService/app/engine/live/live_engine.py | head
```

## Suggested resolution (NOT auto-applied)

1. Add `await broker.cancel_open_orders(owned_only=True)` as the first step of `cmd_emergency_flatten`, mirroring `recovery_flatten` / `force_flat`.
2. Block the liquidation submit until ACK_OK on every cancel (or HALT on `ACK_FAILED_UNCERTAIN` per ADR 0008 contract — see VCR-0002 for the prerequisite wiring).
3. Wire `_convert_ibkr_fill` to consult `perm_id`-based ownership (the `reconciliation_classifier.classify` path already exists; see VCR-0002).
4. Add a test that simulates an open SELL limit + emergency-flatten and asserts no double-sell.

## Provenance of the finding

Lens: `halt-pause-stop-flatten-poison` + `broker-order-ownership-reconcile` (workflow `wf_def78013-ce4`). Surfaced in two lens summaries; specific line ranges for `cmd_emergency_flatten` not re-verified by the main loop. `medium` confidence pending direct read of that function body before remediation.
