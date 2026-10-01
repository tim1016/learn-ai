# Fill-Model Parity Spike — 2026-05-19

**Purpose:** Determine which Engine Lab `FillMode` (`SIGNAL_BAR_CLOSE` or `NEXT_BAR_OPEN`)
matches LEAN's actual `EquityFillModel.MarketFill` behavior for the EMA crossover template.
This decision gates Task 1.1 (adding the template source).

---

## 1. LEAN fill observed

**Run:** `fill-spike-20250106` (trusted-run, `trusted_default` template, buy-and-hold)
**Window:** 2025-01-06 (single trading day, minute resolution)
**Symbol:** SPY (synthetic bars, close = 100.00 + bar-index × 0.01)

From `MyAlgorithm-order-events.json`:

```json
{
  "orderId": 1,
  "orderEventId": 2,
  "time": 1736173860.0,
  "status": "filled",
  "fillPrice": 100.0,
  "fillQuantity": 997.0,
  "direction": "buy"
}
```

**Decoded:**

| Field | Value |
|---|---|
| `time` (epoch s) | `1736173860.0` |
| `ms_utc` | `1736173860000` |
| UTC wall-clock | `2025-01-06T14:31:00Z` |
| ET wall-clock | `2025-01-06T09:31:00-05:00` |
| `fill_price` | `100.0` |

**First minute bar the algorithm received:**

| Field | Value |
|---|---|
| `bar.Time` (ET) | `09:30:00` |
| `bar.EndTime` (ET) | `09:31:00` |
| `bar.EndTime` (ms) | `1736173860000` |
| `bar.Close` | `100.0` |

**Conclusion:** LEAN filled at `bar.EndTime = 09:31:00 ET` with `price = bar.Close = 100.0`.
The fill time and price exactly match `bar.EndTime` / `bar.Close` of the signal bar.

Delta from session open (09:30 ET = `1736173800000 ms`): **60 seconds = 1 minute**
(This 1-minute offset is exactly the bar's duration — it is the bar `EndTime`, not the bar `Time`.)

---

## 3. Decision

**Chosen approach: (a) retain `SIGNAL_BAR_CLOSE` — no custom `EquityFillModel` required.**

### Rationale

LEAN's observed fill:
- Time = `bar.EndTime` of the signal bar (`1736173860000 ms` = 09:31 ET for the 09:30 minute bar)
- Price = `bar.Close` of the signal bar (`100.0`)

Engine Lab `SIGNAL_BAR_CLOSE`:
- `fill_time = signal_bar.end_time`
- `fill_price = signal_bar.close`

These are semantically identical. The 1-minute delta between LEAN fill time and bar open
(`09:31` vs `09:30`) is not a delay — it is definitional: `bar.EndTime = bar.Time + bar_duration`.

Engine Lab `NEXT_BAR_OPEN` adds **another** 1-minute delay on top of `bar.EndTime`, filling at
`10:01 ET` instead of `10:00 ET`. That is a 60-second misalignment from LEAN's actual behavior.

**Why the docstring in `fill_model.py` already says this is the right choice:**
The existing docstring (line 5–9) explicitly states:
> "This reproduces the bookkeeping inside LEAN's `SpyEmaCrossoverAlgorithm.OnFifteenMinuteBar`,
> where `_entryPrice` is set to `bar.Close` on the signal bar."

This spike provides the numerical receipt that validates that claim against actual LEAN output.

---

## 5. Session-boundary note (15-min consolidation)

The buy-and-hold spike runs at **minute resolution** (not 15-min consolidated), so the signal
bar is a 1-minute bar. The EMA crossover template uses a 15-min `TradeBarConsolidator`. The
semantic is the same regardless of bar period:

```text
fill_time  = signal_bar.EndTime
fill_price = signal_bar.Close
```

For a 15-min bar: `EndTime` = bar boundary (e.g., 10:00 ET, 10:15 ET, …). The Engine Lab's
consolidator writes `end_time = bar_open + 15 min` and the fill model reads `signal_bar.end_time`,
matching LEAN's `bar.EndTime` contract exactly.

### 2026-05-23 addendum — stale consolidated bars after a session gap

The original spike covered on-time bars: the consolidated bar fires when the next minute belongs
to the next 15-minute bucket in the same session (`signal_bar.end_time == current_minute.time`).
The W6mo matrix exposed the missing case: a consolidated bar can be emitted only when the next
available regular-session minute arrives after an overnight/weekend gap.

Empirical receipt from the failed QQQ W6mo run before the fix:

| Order | Signal context | LEAN fill | Data receipt |
|---|---|---|---|
| `QQQ` order 16, sell | Entry 2026-01-02 14:45 ET, 5-bar exit signal on the stale 2026-01-02 15:45-16:00 bar | `2026-01-05 09:31 ET`, price `619.32` | `observations.csv` first Monday minute has `time=09:30`, `end_time=09:31`, `open=619.32`, `close=619.59` |

LEAN therefore does **not** fill this stale signal at Friday's 16:00 close. It uses the current
minute's market data after the exchange reopens: fill price = current minute `open`, fill time =
current minute `end_time`. Engine Lab now exposes this as an opt-in `FillModel` flag:
`fill_stale_signal_at_current_open=True`. The cross-engine matrix runner enables it alongside
`IbkrEquityCommissionModel`; the default remains disabled so legacy research runs keep their
bar-close simplification.

Regression receipts:

- `PythonDataService/tests/engine/test_fill_model.py::test_signal_bar_close_stale_signal_uses_current_open_when_enabled`
- `PythonDataService/tests/engine/test_engine_fill_modes.py::test_signal_bar_close_lean_stale_signal_fills_at_current_minute_open`
- `PythonDataService/tests/lean_sidecar/test_cross_runner_fee_wiring.py::test_cross_runner_constructs_backtest_engine_with_ibkr_fee_model`

---

## References

- LEAN order-events: `artifacts/lean-sidecar/fill-spike-20250106/workspace/output/MyAlgorithm-order-events.json`
- Normalized result: `artifacts/lean-sidecar/fill-spike-20250106/normalized/result.json`
- Stale-signal receipt: `PythonDataService/tests/fixtures/golden/cross-engine-studies/cells/QQQ_W6mo_2025-11-03_to_2026-04-30/lean/orders.json`
- Stale-signal observations: `PythonDataService/tests/fixtures/golden/cross-engine-studies/cells/QQQ_W6mo_2025-11-03_to_2026-04-30/lean/observations.csv`
- Engine Lab fill model: `PythonDataService/app/engine/execution/fill_model.py`
- Trusted sample: `PythonDataService/app/lean_sidecar/trusted_samples/buy_and_hold.py`
