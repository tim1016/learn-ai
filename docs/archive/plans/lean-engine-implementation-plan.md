> **Status:** Archived — CONFLICT with `docs/architecture/engine-authority-map.md`.
> **Do not use as implementation authority.**
> Current authority: `docs/architecture/engine-authority-map.md` (engine is canonical and shipped).
> Archived because: this plan frames the engine as a future build goal; `engine-authority-map.md` (protected, reviewed 2026-05-04) documents the same engine as fully shipped and canonical.

# LEAN-Compatible Backtest Engine — Implementation Plan

## Goal

Build a new Python backtest engine inside `PythonDataService/app/engine/` that
**reproduces the trades produced by QuantConnect LEAN's `SpyEmaCrossoverAlgorithm`
bit-exactly** as the first validation milestone, then generalize from that
foundation.

This document supersedes the speculative phasing in `lean-pipeline-research-plan.md`
with concrete decisions, file layouts, and a validation procedure tied to the
actual LEAN reference output we now have on disk.

---

## 1. The Validation Target

LEAN ran the SPY EMA crossover strategy from **2024-03-28 → 2026-03-27** on
$100,000 starting cash. The output we are matching lives at:

- `C:\Users\inkan\Lean\Launcher\bin\Debug\SpyEmaCrossoverAlgorithm-log.txt`
- `C:\Users\inkan\Lean\Launcher\bin\Debug\SpyEmaCrossoverAlgorithm.json`
- `C:\Users\inkan\Lean\Launcher\bin\Debug\SpyEmaCrossoverAlgorithm-order-events.json`
- `C:\Users\inkan\Lean\Launcher\bin\Debug\SpyEmaCrossoverAlgorithm-summary.json`

The trade log contains **63 trades** with these fields per row:

```
EntryTime, EntryPrice, ExitTime, ExitPrice, EMA5, EMA10, RSI14, PnLPts, PnLPct, Result
```

Headline numbers from `SpyEmaCrossoverAlgorithm.json`:

| Metric | Value |
|---|---|
| Total trades | 63 |
| Wins / Losses | 44 / 19 |
| Win rate | 69.84% |
| Net profit | $11,274.73 (11.27%) |
| Profit factor | 2.7527 |
| Total fees | $126.03 |
| Largest win | $2,798.25 |
| Largest loss | -$1,097.26 |

### 1.1 Reference algorithm summary

`SpyEmaCrossoverAlgorithm.cs` (lines 39–135):

- Symbol: `SPY`, Resolution `Minute`, normalization `Raw`
- Consolidator: `TradeBarConsolidator(TimeSpan.FromMinutes(15))`
- Indicators (all updated manually inside `OnFifteenMinuteBar`):
  - `ExponentialMovingAverage("EMA5", 5)`
  - `ExponentialMovingAverage("EMA10", 10)`
  - `RelativeStrengthIndex("RSI14", 14, MovingAverageType.Wilders)`
- Position management: `_inPosition`, `_barsUntilExit` (5)
- Entry rules (long only, no pyramiding):
  - `freshCrossover = currentAbove && !_prevEma5AboveEma10`
  - `gapOk = (ema5 - ema10) >= 0.20m`
  - `rsiOk = rsi >= 50 && rsi <= 70`
- Order: `SetHoldings(_spy, 1.0)` (full portfolio)
- Exit: when `_barsUntilExit == 0`, `Liquidate(_spy)`
- Logged `_entryPrice` / exit price = `bar.Close` of the 15-minute consolidated bar
  at signal time. **This is what the trade log records**, not the underlying fill
  model output.

---

## 2. Reproducibility Decisions

These are the calculation details that must match LEAN exactly. Each one is a
non-obvious trap I want flagged up front.

### 2.1 15-minute bar alignment

LEAN's `TradeBarConsolidator(TimeSpan.FromMinutes(15))` rounds `bar.Time` down
using `dateTime.Ticks % interval.Ticks`. With no offset, this aligns 15-min bars
to **wall-clock boundaries**: `:00, :15, :30, :45`.

Because the regular session opens at 09:30 (which sits on a 15-min boundary),
the first bar of the day covers minute bars 09:30 → 09:44 inclusive and fires
when the 09:45 minute bar arrives. The bar's `Time` is 09:30, `EndTime` is 09:45.

Our consolidator must match this exactly:
- Input: minute bars with `time` (start) and `end_time = time + 1m`
- Output: 15-min bars whose `time = floor_to_15min(first_minute.time)` and
  `end_time = last_minute.end_time`
- The consolidated bar's `close` = the close of the **last minute bar contained**
  in the period (the 09:44 bar in the example above)
- The consolidated bar fires when we receive the first minute bar of the *next*
  15-min window — never partially

Note: the algorithm log timestamps (`2024-04-11 12:00`) are bar `EndTime`
values. So entry "12:00" means the 11:45–12:00 consolidated bar.

### 2.2 EMA seeding

LEAN's `ExponentialMovingAverage`:
- Smoothing constant `k = 2.0 / (1 + period)`
- Internally feeds the first `period` samples to a rolling SMA
- At sample `period`, `Current.Value = SMA(first N values)` (this becomes the seed)
- From sample `period + 1` onward: `EMA = input * k + prev_EMA * (1 - k)`
- `IsReady` becomes `true` once `Samples >= period`

Our streaming `Ema` must match this seeding behavior, not the simpler "start
from the first value" or pandas-ta `adjust=True` formulation.

### 2.3 Wilders RSI

LEAN's `RelativeStrengthIndex` with `MovingAverageType.Wilders`:
- Per-step gain: `max(0, input - prev_input)` (uses `>=` for the equality case
  → equality is treated as a zero gain rather than zero loss)
- Per-step loss: `max(0, prev_input - input)` when `input < prev_input`, else 0
- Wilders smoothing on both averages: `avg = (avg * (period - 1) + new_value) / period`
- Initial averages = SMA of the first `period` gain/loss samples
- `IsReady` when `Samples >= period + 1` (one extra sample is needed for the
  first delta)
- `RS = avgGain / avgLoss`, `RSI = 100 - 100 / (1 + RS)`
- Edge case: if `round(avgLoss, 10) == 0`, RSI = 100

### 2.4 Indicator update timing

Inside `OnFifteenMinuteBar`, the algorithm calls:

```csharp
_ema5.Update(bar.EndTime, bar.Close);
_ema10.Update(bar.EndTime, bar.Close);
_rsi14.Update(bar.EndTime, bar.Close);
```

So indicators are updated **once per consolidated bar with that bar's close
price**, timestamped at `EndTime`. We must mirror this — not update on minute
bars, not update on bar `Time`.

### 2.5 Crossover state machine

The crossover detection has a subtlety: while indicators are warming up,
`_prevEma5AboveEma10` is updated to the current relationship even though no
trading happens. This means the **first tradeable bar** uses a previous-state
that was set during warmup, not `false`.

```csharp
if (!_ema5.IsReady || !_ema10.IsReady || !_rsi14.IsReady)
{
    _prevEma5AboveEma10 = _ema5.IsReady && _ema10.IsReady
        && _ema5.Current.Value > _ema10.Current.Value;
    return;
}
```

Our strategy must reproduce this exactly to avoid spurious "first crossover"
signals after warmup ends.

### 2.6 Entry/exit price recording

To match the trade log bit-exactly:

- `entry_price = consolidated_bar.close` at the entry signal bar
- `exit_price  = consolidated_bar.close` at the exit signal bar (5 bars after entry)
- The entry/exit `time` recorded in the log is `bar.EndTime`
- `pnl_pts = exit_price - entry_price` (per share, formatted to 2 decimals in the log)
- `pnl_pct = pnl_pts / entry_price` (formatted to 6 decimals as `%.6f`)

### 2.7 Fill model — separate concern

LEAN's actual `EquityFillModel.MarketFill` would fill the SetHoldings order at
the *next minute bar's close* (the 09:46 minute bar after a 09:45 signal),
because `GetBestEffortTradeBar` requires `bar.EndTime > order.Time`. This makes
the **portfolio P&L in `SpyEmaCrossoverAlgorithm.json`** different from the
**per-trade P&L in `SpyEmaCrossoverAlgorithm-log.txt`**:

- The log uses `bar.Close` of the signal bar (algorithm bookkeeping)
- The portfolio uses the actual fill price (fill model output)

**Decision:** the validation target is the trade log. Our engine will support a
configurable fill mode (`SignalBarClose` and `NextBarOpen`), but for the SPY
validation we use `SignalBarClose` to reproduce the log. We will independently
implement LEAN's `NextBarOpen` fill model and verify that the portfolio
statistics in `SpyEmaCrossoverAlgorithm.json` (end equity, fees) match in a
secondary validation.

---

## 3. Engine Architecture

### 3.1 Package layout

```
PythonDataService/app/engine/
├── __init__.py
├── data/
│   ├── __init__.py
│   ├── lean_format.py          # LEAN zip read/write, deci-cent encoding
│   ├── postgres_export.py      # Postgres StockAggregate → LEAN zip exporter
│   ├── data_source.py          # DataSource ABC + implementations
│   └── trade_bar.py            # TradeBar dataclass
├── consolidators/
│   ├── __init__.py
│   ├── base.py                 # ConsolidatorBase
│   └── trade_bar_consolidator.py
├── indicators/
│   ├── __init__.py
│   ├── base.py                 # Indicator ABC (Current, IsReady, Samples, Update)
│   ├── ema.py                  # ExponentialMovingAverage (LEAN-faithful seed)
│   ├── sma.py                  # SimpleMovingAverage
│   └── rsi.py                  # RelativeStrengthIndex with Wilders smoothing
├── strategy/
│   ├── __init__.py
│   ├── base.py                 # Strategy ABC (initialize, on_bar, on_fill, on_end)
│   └── algorithms/
│       └── spy_ema_crossover.py
├── execution/
│   ├── __init__.py
│   ├── order.py                # Order, OrderType, Direction
│   ├── fill_model.py           # FillMode enum, SignalBarCloseFillModel, NextBarOpenFillModel
│   └── portfolio.py            # Portfolio (cash, holdings, SetHoldings)
├── results/
│   ├── __init__.py
│   ├── trade.py                # CompletedTrade
│   ├── statistics.py           # Win rate, profit factor, drawdown, Sharpe, Sortino
│   └── lean_compat.py          # Output JSON matching LEAN's schema (subset)
├── engine.py                   # BacktestEngine (lifecycle orchestration)
└── tests/
    ├── test_consolidator.py
    ├── test_ema.py
    ├── test_rsi.py
    ├── test_spy_validation.py  # the bit-exact LEAN validation
    └── fixtures/
        └── spy_lean_trades.csv # extracted from SpyEmaCrossoverAlgorithm-log.txt
```

### 3.2 Key types

```python
# data/trade_bar.py
@dataclass(frozen=True)
class TradeBar:
    symbol: str
    time: datetime          # bar start (exchange tz, tz-aware)
    end_time: datetime      # bar end
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
```

Use `Decimal` (not `float`) to avoid drift over 2 years of EMA recursion. LEAN
uses `decimal` throughout for the same reason.

```python
# indicators/base.py
class Indicator(ABC):
    name: str
    samples: int
    current_time: datetime | None
    current_value: Decimal | None

    @property
    def is_ready(self) -> bool: ...
    def update(self, time: datetime, value: Decimal) -> bool: ...
    def reset(self) -> None: ...
```

```python
# strategy/base.py
class Strategy(ABC):
    def initialize(self, ctx: StrategyContext) -> None: ...
    def on_bar(self, bar: TradeBar) -> None: ...     # called per consolidated bar
    def on_order_event(self, event: OrderEvent) -> None: ...
    def on_end(self) -> None: ...
```

```python
# engine.py
class BacktestEngine:
    def __init__(self, config: BacktestConfig, data_source: DataSource): ...
    def add_strategy(self, strategy: Strategy) -> None: ...
    def run(self) -> BacktestResult: ...
```

### 3.3 Engine main loop

```
BacktestEngine.run():
    portfolio = Portfolio(initial_cash)
    strategy.initialize(context)
    for minute_bar in data_source.iter_bars(start, end):
        # 1. Push to consolidators registered for this symbol
        for consolidator in consolidators[minute_bar.symbol]:
            fired_bars = consolidator.update(minute_bar)
            for consolidated in fired_bars:
                # 2. Strategy receives the consolidated bar
                strategy.on_bar(consolidated)
                # 3. Drain pending orders through the fill model
                for order in portfolio.pending_orders():
                    fill = fill_model.fill(order, consolidated, next_minute_bar)
                    if fill:
                        portfolio.apply_fill(fill)
                        strategy.on_order_event(fill)
    strategy.on_end()
    return result_builder.build()
```

The strategy never sees minute bars directly — it only consumes consolidated
bars. This matches LEAN's `OnFifteenMinuteBar` model.

---

## 4. Data Pipeline

### 4.1 Postgres → LEAN zip exporter

A new module `engine/data/postgres_export.py` converts the existing
`StockAggregate` rows for a ticker/date range into LEAN's on-disk format:

```
data/equity/usa/minute/{ticker}/{YYYYMMDD}_trade.zip
└── {YYYYMMDD}_{ticker}_minute_trade.csv
    Format: ms_since_midnight,open*10000,high*10000,low*10000,close*10000,volume
    No header. ET timezone. Integer prices (deci-cents).
```

This is invoked from the existing strategy-lab UI workflow:
1. User picks ticker + date range in UI
2. Backend fetches from Polygon (existing flow), stores in Postgres
3. New "Export to LEAN" action calls the exporter
4. Engine reads the zips for backtesting

The output directory is configurable (default `learn-ai/data/`).

### 4.2 LEAN format reader

`engine/data/lean_format.py` provides:

```python
class LeanMinuteDataReader:
    def __init__(self, data_root: Path): ...
    def iter_bars(self, symbol: str, start: date, end: date) -> Iterator[TradeBar]: ...
```

The reader handles deci-cent decoding (`Decimal(int_price) / Decimal(10000)`),
ms-since-midnight → ET-aware datetime conversion, and skipping non-trading days.

### 4.3 Reference: convert_spy_data.py

LEAN already ships a `convert_spy_data.py` that does the same transformation
from a generic OHLCV CSV. We will read it for format reference but write our
own implementation that pulls directly from Postgres.

---

## 5. Validation Procedure

### 5.1 Fixture extraction

From `SpyEmaCrossoverAlgorithm-log.txt`, extract the 63-row trade table into
`engine/tests/fixtures/spy_lean_trades.csv` with columns:

```
trade_no, entry_time, entry_price, exit_time, exit_price,
ema5_at_entry, ema10_at_entry, rsi_at_entry, pnl_pts, pnl_pct, result
```

### 5.2 Test cases (in priority order)

1. **`test_consolidator_alignment`** — feed synthetic minute bars, assert 15-min
   bars fire on `:00 :15 :30 :45` boundaries with correct OHLCV aggregation
2. **`test_ema_against_lean_seed`** — feed a known sequence, assert EMA matches
   LEAN's seed-from-SMA behavior
3. **`test_rsi_wilders_against_known_values`** — use Welles Wilder's original
   1978 example (known RSI test vector) to verify the smoothing
4. **`test_spy_first_trade`** — run the engine over April 2024 SPY data only,
   assert the first trade matches `2024-04-11 12:00 ENTRY @ 515.34, EXIT @ 13:15
   @ 516.97, EMA5=514.1906, EMA10=513.9322, RSI=57.33, PnL=1.63`
5. **`test_spy_full_validation`** — run the full 2024-03-28 → 2026-03-27 backtest,
   assert all 63 trades match the fixture row-by-row, with these tolerances:
   - Timestamps: exact match
   - Prices: exact match (Decimal)
   - EMA5/EMA10: match to 4 decimals (LEAN log precision)
   - RSI14: match to 2 decimals (LEAN log precision)
   - PnL points: match to 2 decimals
   - PnL pct: match to 6 decimals

If any of these fail, the failure points to the specific reproducibility issue
to fix (consolidator alignment, EMA seeding, RSI smoothing, etc.).

### 5.3 Secondary validation (fill model)

After the trade-log validation passes, run the same backtest with
`FillMode.NextBarOpen` and assert that the resulting portfolio statistics match
`SpyEmaCrossoverAlgorithm.json`:
- End equity within $1 of $111,274.73
- Total fees match $126.03 (depends on commission model — LEAN's default
  equity fee is roughly $1/trade for this strategy)
- Trade count = 63

---

## 6. API Integration

A new FastAPI router at `PythonDataService/app/routers/engine.py`:

```
POST /engine/backtest
  body: { strategy: str, ticker: str, start: date, end: date,
          parameters: dict, fill_mode: str }
  response: BacktestResult JSON (LEAN-compatible subset)

POST /engine/export-lean-data
  body: { ticker: str, start: date, end: date, output_root: str }
  response: { zip_files: [paths], bars_exported: int }
```

The frontend's strategy-lab gets a new mode toggle (`Legacy` vs `LEAN Engine`)
that routes the backtest request to the appropriate endpoint. Existing
strategies continue to work via the legacy route during the transition.

---

## 7. Phasing

### Phase 1 — SPY validation (this milestone)

1. Scaffold `engine/` package with empty modules
2. Implement `TradeBar` + `LeanMinuteDataReader`
3. Implement `Postgres → LEAN zip` exporter
4. Implement `TradeBarConsolidator` (with the alignment + tests from §5.2)
5. Implement `Indicator` base + `ExponentialMovingAverage` + `RelativeStrengthIndex`
6. Implement `Portfolio` + `Order` + `SignalBarCloseFillModel`
7. Implement `BacktestEngine` lifecycle
8. Implement `Strategy` base + `SpyEmaCrossoverAlgorithm` port
9. Run all validation tests in §5.2 until they pass
10. Implement `NextBarOpenFillModel` and run §5.3 secondary validation

**Done definition for Phase 1:** all 63 trades in the LEAN log are reproduced
bit-exactly by `test_spy_full_validation`, AND the secondary validation passes
within tolerance.

### Phase 2 — Generalization

Once Phase 1 passes, generalize beyond SPY:
- Port the existing strategies (SMA crossover, RSI mean reversion,
  EMA-crossover-RSI rule-based, momentum RSI stochastic) to the new `Strategy`
  base class
- Add the `/engine/backtest` API endpoint and frontend toggle
- Wire results into the existing `StrategyExecution` / `BacktestTrade` Postgres
  tables for UI compatibility

### Phase 3 — Realism (formerly LEAN-plan Phase 2)

- Slippage models
- Commission models matching LEAN's `InteractiveBrokersFeeModel` etc.
- Extended statistics: Sortino, Calmar, MAE/MFE, profit factor

### Phase 4 — Framework (formerly LEAN-plan Phase 3)

- Alpha / Portfolio / Risk / Execution module separation
- Scheduled events (`Schedule.On(DateRules, TimeRules, callback)`)
- Multi-symbol support, signal standardization

### Phase 5 — Data infrastructure (formerly LEAN-plan Phase 4)

- Map files / factor files for corporate actions
- Symbol change tracking
- Multi-source data abstraction

---

## 8. Open Questions

These are deferred until we have concrete code, but flagging now:

1. **Decimal vs float performance.** LEAN uses C# `decimal` which is hardware-supported.
   Python `Decimal` is ~50x slower than `float`. If the SPY backtest takes minutes
   instead of seconds, we may need a hybrid: `Decimal` for bookkeeping, `float64`
   for indicator inner loops, with verified equivalence on the validation set.

2. **Holiday / early close handling.** LEAN's data files just don't exist for
   non-trading days. Our exporter must respect the same convention. For early
   closes (e.g., day after Thanksgiving), the data file ends at 13:00 ET — the
   consolidator should not produce a partial 12:45–13:00 bar unless that minute
   bar exists.

3. **Daylight savings.** LEAN stores ms-since-midnight in ET (which means the
   absolute UTC offset shifts twice a year). We must store and compare in ET-aware
   datetimes throughout to avoid off-by-one-hour bugs around DST transitions.

4. **Warmup data.** The SPY backtest starts 2024-03-28, but the first trade is
   2024-04-11. That's ~10 trading days for the indicators to warm up. Our engine
   should explicitly fetch and process pre-start data during warmup, the same
   way LEAN does via its history requests.

---

## 9. References

| Component | LEAN file |
|---|---|
| SPY algorithm | `Algorithm.CSharp/SpyEmaCrossoverAlgorithm.cs` |
| EMA | `Indicators/ExponentialMovingAverage.cs` |
| RSI | `Indicators/RelativeStrengthIndex.cs` |
| Indicator base | `Indicators/IndicatorBase.cs` |
| Consolidator | `Common/Data/Consolidators/TradeBarConsolidator.cs` |
| Period base | `Common/Data/Consolidators/PeriodCountConsolidatorBase.cs` |
| TradeBar parser | `Common/Data/Market/TradeBar.cs` |
| Fill model | `Common/Orders/Fills/EquityFillModel.cs` |
| Statistics | `Common/Statistics/StatisticsBuilder.cs` |
| Trade builder | `Common/Statistics/TradeBuilder.cs` |
| Reference output | `Launcher/bin/Debug/SpyEmaCrossoverAlgorithm*.{json,txt}` |
| Data converter | `convert_spy_data.py` |
