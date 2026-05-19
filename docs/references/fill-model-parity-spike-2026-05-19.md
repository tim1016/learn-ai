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

## 2. Engine Lab fill modes compared

Spec strategy: `spy_ema_crossover.spec.json` (EMA5/EMA10 crossover, RSI gate, 15-min bars)
Window: 2025-01-06–2025-04-30 (29 trades produced)

**Trade 1 — first entry, 2025-01-13:**

| Mode | `entry_time_ms` | ET wall-clock | `entry_price` |
|---|---|---|---|
| `signal_bar_close` | `1736780400000` | `2025-01-13T10:00:00-05:00` | `577.87` |
| `next_bar_open` | `1736780460000` | `2025-01-13T10:01:00-05:00` | `577.48` |
| Delta | `60 000 ms` | 1 minute | `0.39` |

`signal_bar_close` fills at **10:00 ET** — the `EndTime` of the 15-min bar spanning 09:45–10:00.
`next_bar_open` fills at **10:01 ET** — the `Time` of the next minute bar (1 minute later).

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

## 4. Numerical receipts

### LEAN order-events raw (ms_utc)

| `order_event_id` | `ms_utc` | `status` | `fill_price` |
|---|---|---|---|
| 1 | `1736173860000` | `submitted` | `0.0` |
| 2 | `1736173860000` | `filled` | `100.0` |

Both events share the same millisecond. Submission and fill are simultaneous in LEAN's
backtesting transaction handler — there is no separate "next bar" delay for market orders
when the bar data is already available in the data stream.

### Engine Lab trade 1 by mode (ms_utc)

| Mode | `entry_time` | `entry_price` | `exit_time` | `exit_price` |
|---|---|---|---|---|
| `signal_bar_close` | `1736780400000` | `577.87` | `1736784900000` | `577.125` |
| `next_bar_open` | `1736780460000` | `577.48` | `1736784960000` | `577.39` |

Exit timestamps also differ by 60 000 ms (1 minute), consistent with the mode shifting
every fill 60 seconds later.

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

---

## 6. What was NOT investigated

- **`MarketOnOpenOrder`** — LEAN supports `MarketOnOpenOrder` which fills at the next session's
  opening auction. The buy-and-hold template uses `SetHoldings` → implicit `MarketOrder`, not
  `MarketOnOpenOrder`. The EMA crossover template will also use `SetHoldings`. `MarketOnOpenOrder`
  was therefore out of scope for this spike.
- **Custom `EquityFillModel`** overriding `MarketFill` — not needed. The default LEAN fill model
  fills at the signal bar's price data (confirmed empirically). A custom model would be required
  only if LEAN's native model produced a different price, which it does not.

---

## 7. Task 1.1 implication

The EMA crossover template's `SetFillModel(...)` call (if present) should use LEAN's **default**
fill model — no override. Engine Lab must run with `fill_mode=signal_bar_close`. The
cross-reconciler's `FILL_PRICE_DRIFT` category should gate at `atol=$0.01` per the repo default
(`.claude/rules/numerical-rigor.md`).

---

## References

- LEAN order-events: `artifacts/lean-sidecar/fill-spike-20250106/workspace/output/MyAlgorithm-order-events.json`
- Normalized result: `artifacts/lean-sidecar/fill-spike-20250106/normalized/result.json`
- Engine Lab fill model: `PythonDataService/app/engine/execution/fill_model.py`
- Spec router: `PythonDataService/app/routers/spec_strategy.py`
- Trusted sample: `PythonDataService/app/lean_sidecar/trusted_samples/buy_and_hold.py`
