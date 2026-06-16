---
id: VCR-0019
severity: P0
status: open
area: halt-state-machine
canonical_file: PythonDataService/app/engine/live/run.py::_recovery_flatten
reference: ADR 0008 § 5 (durable submit) + PRD §12.8
first_seen: 2026-06-16
last_seen: 2026-06-16
lens: halt-pause-stop-flatten-poison
dedupe_with_F: none
confidence: high
---

## What

`_recovery_flatten` and the engine's shutdown-time recovery path consult the
engine's **in-memory portfolio snapshot** to decide what to flatten — not the
broker's actual position. When the strategy's normal exit SELL has filled
at the broker but the engine's internal `portfolio.positions` hasn't yet
been refreshed (the broker-side fill callback hadn't propagated through
the engine's bar loop before the shutdown began), recovery_flatten reads
`position=1`, submits a SECOND `SELL 1`, and **takes the account net-short**.

Observed live during the 2026-06-16 HITL VCR-0002 receipt run on
`dep_val_smoke_002`:

```
10:20:05.367  Strategy issued normal exit SELL — order 45, intent 1w8v8RycS5ix9ZGnX7DCLA
10:20:05.448  Order 45 filled → broker position = 0 (FLAT)
10:20:25.351  recovery_flatten ALSO issued SELL — order 47, intent -Si5_qOsRoqReN7IcS0LkQ
10:20:25.366  Engine disconnect
```

20 seconds elapsed between the broker fill and the duplicate SELL. The engine
should have observed `position=0` in that window; it didn't. The net effect
was a `-1 SPY @ 752.01` short on a paper account that the operator then had
to manually flatten via the `POST /api/broker/orders` endpoint (the documented
`cmd_emergency_flatten` surface itself bombed — see VCR-0020).

## Where

- `PythonDataService/app/engine/live/run.py::_recovery_flatten` (lines 413-507) —
  iterates `snapshot.positions` from `broker.fetch_positions()` (line 416-417)
  but **then** uses that snapshot for the liquidation loop without
  re-querying. If the snapshot is taken before the strategy's prior SELL
  fill is reflected by IBKR, the loop liquidates a position that's already
  closed at the broker.
- `PythonDataService/app/engine/live/live_engine.py::_flatten` — same shape;
  uses `portfolio.positions` rather than a fresh broker refresh.

## Why this severity

P0 because it puts a real, undesired position on the account every time
the engine shuts down with a recently-issued strategy SELL that hasn't yet
been reflected in the engine's snapshot. On paper this costs commissions
and exposes the account to direction risk; on a live mode flip, it would
mean wrong direction on the operator's account during the most safety-
critical code path (shutdown / poison-state recovery).

The receipt run captured exactly the failure mode: 5 clean trade cycles,
then the final shutdown introduced the net-short via this race. The bug
is structural and would fire every time the timing aligns; it's not a
once-in-a-blue-moon edge.

## Trading impact

- **Today (paper):** observed once during the HITL receipt run. -$2.48
  realized loss on the duplicate sell + extra commission. Manual cleanup
  required because `cmd_emergency_flatten` (VCR-0020) was also broken.
- **Tomorrow (live mode flip):** a similarly-timed engine shutdown would
  put real money on the wrong side of a position during the shutdown
  flow. Combined with VCR-0020 (the documented cleanup surface broken),
  this is a meaningful safety regression vs. the user's mental model of
  "stop = flat".

## Reproduction

```bash
# 1. Deploy + start dep_val_smoke_002 with FixedShares(1).
# 2. Wait for a full BUY+SELL cycle (strategy auto-exits after 3 hold
#    bars, takes ~5-10 min).
# 3. Issue a Stop request via the cockpit immediately after the SELL
#    fills (within ~30s).
# 4. Observe a second SELL in intent_events.jsonl seq+2 / seq+3 with no
#    preceding SIZING_RESOLVED — recovery_flatten fired with stale state.
# 5. Confirm the account is now net-short via /api/broker/positions.
```

## Suggested resolution (NOT auto-applied)

1. **Refresh broker positions before deciding to flatten.** In both
   `_recovery_flatten` and `LiveEngine._flatten`, call `await
   broker.fetch_positions()` immediately before iterating; ignore the
   engine's in-memory `portfolio.positions` for the flatten decision.
2. **Guard the loop body too:** for each position, re-check the broker's
   current quantity before submitting (`if abs(broker_qty) < epsilon:
   continue`). The double-refresh costs one extra IBKR round-trip per
   flatten and closes the race even when the first refresh observed the
   stale value.
3. **Tests:** integration test that issues a normal SELL, waits for the
   broker callback to update IBKR's view, but pins the engine's internal
   portfolio at the old state — asserts recovery_flatten does NOT
   submit a second order. The Phase 5C ownership-query subclass (#539)
   is the natural seam.

## Provenance of the finding

Live observation during the 2026-06-16 HITL VCR-0002 Acceptance Gate #2
receipt run; see also VCR-0002 finding's "Operational notes" section for
the timeline and `dep_val_smoke_002` WAL evidence at
`PythonDataService/artifacts/live_runs/99b57b0decd474554776db1d41b06ad82290c148a2f894e47555c8fff01a08f7/intent_events.jsonl`
(seq 24-27 inclusive). The receipt itself completed cleanly before this
fired; the bug surfaced on engine shutdown.
