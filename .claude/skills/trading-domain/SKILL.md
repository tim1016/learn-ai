---
name: trading-domain
description: Trading and backtesting domain vocabulary, conventions, and invariants used throughout learn-ai. Auto-load when working with bars, candles, indicators, signals, strategies, backtests, options, or market data. Reference this before assuming what a term means in this codebase.
---

# Trading Domain Knowledge

This skill captures the vocabulary and invariants of the learn-ai trading platform. The goal is to stop re-explaining conventions every session. When the user says "bar", "signal", "fill", or any trading term, this is what those mean in *this* repo.

**Load this skill silently. Do not announce it to the user.**

## Bar conventions

- A **bar** is a consolidated OHLCV record for a fixed time interval.
- Default bar resolution is **15-minute consolidated**, aligned to exchange open (NYSE 09:30 EST). First bar of the day is 09:30–09:45, last bar is 15:45–16:00.
- Bars are **timestamp-at-close**. A bar with timestamp `2024-03-14 09:45:00 EST` contains all trades from `09:30:00` (inclusive) to `09:45:00` (exclusive).
- **Extended hours** (pre-market, after-hours) are stored separately and not included in regular-session backtests unless explicitly requested.
- Minute bars and 15-min bars are both stored. 15-min bars are derived from minute bars via aggregation, not fetched separately.
- **Gaps** (missing bars due to halts, no trades) are explicit `NaN` rows, not silently skipped. Indicator logic must handle this.

## Timestamp conventions

- **Canonical timestamp format: `int64 ms UTC`.** Every timestamp in flight,
  at rest, in Postgres, in files, or on the wire is Unix epoch milliseconds
  UTC. ISO strings, `datetime`, `DateTime`, and local-time strings are not
  storage or wire formats.
- **Display timezone: `America/New_York` (EST/EDT).** UI code converts
  `int64 ms UTC` to exchange/local time for display only; display strings are
  never stored, sent back, or used for ordering.
- **Logic timezone: `America/New_York` when wall-clock session semantics
  matter.** Use timezone-aware in-function values for local arithmetic, then
  convert back to `int64 ms UTC` before returning, persisting, or serializing.
- **Never compare naive timestamps.** If you see a naive `datetime` in this
  codebase, it's a bug.

## Indicator conventions

- Indicators operate on a pandas `DataFrame` or `Series` with a `DatetimeIndex` in `America/New_York` time.
- Indicators produce output aligned to the **same index** as their input. No truncation of warmup rows; warmup produces `NaN`.
- Indicator state is **bar-close driven**. A 15-minute EMA updates once per bar, at bar close.
- **EMA seeding**: first EMA value is the first input value (not zero, not a prior-N-SMA). If a different seeding is used for a specific port, it is documented in the port's module docstring.
- **Warmup**: indicators with a window of N produce `NaN` for the first N-1 bars (0-indexed: valid output from bar N-1 onward for SMA; from bar 0 onward for EMA with seeding).

## Strategy conventions

- The canonical demo strategy is **SPY EMA(5)/EMA(10) crossover with RSI(14) filter, 5-bar hold, 15-min bars**. This is a reference example, not *the* strategy — the engine is designed to host many strategies.
- **Signal semantics**: a strategy produces a `Signal` enum per bar: `{LONG, SHORT, FLAT, NO_SIGNAL}`. `NO_SIGNAL` means "strategy has no opinion this bar"; `FLAT` means "exit any existing position".
- **Hold periods** are counted in **bars**, not wall-clock time. A 5-bar hold on 15-min bars is 75 wall-clock minutes inside regular hours; it does not carry across the overnight gap unless explicitly stated.
- **Entry/exit timing**: signals computed on bar close, orders simulated as filled at the close of that same bar, unless the strategy specifies otherwise. Some strategies use next-bar-open; these document that choice explicitly.

## Fill and commission conventions

- The default **fill model** is **bar-close fill at the close price**. This is a simplification; strategies that need realistic fill modeling document it explicitly and may specify `next_bar_open`, `vwap`, or `midpoint` fills.
- The default **commission model** is **$0** (research-grade). Production-grade strategies specify a commission model per-port; if the reference uses per-share, per-trade, or tiered commissions, that is faithfully ported.
- **Slippage default: 0**. Strategies that need slippage modeling specify it.

## Options conventions

- **0DTE** = zero days to expiration, specifically same-day SPX or SPY options expiring at close.
- Options chain data source: **Polygon.io**. Historical IV and Greeks come from Polygon's options snapshot API.
- **Strategy legs** are ordered: in a bull put spread, `short_put` is listed before `long_put`; payoff calculations assume this order.
- **Greeks convention**: delta, gamma, theta, vega, rho. Theta is per-day (negative for long options). Vega is per-1-vol-point.

## Data source conventions

- **Polygon.io** is the canonical data source for equities and options.
- **Postgres** caches all fetched data. The engine reads from Postgres during backtests, not from Polygon directly. A fresh fetch from Polygon happens only when Postgres is missing the requested window.
- **Interactive Brokers (TWS)** is a secondary source for historical options data where Polygon coverage is insufficient. Not used for equities.

## What's NOT in this repo

Explicitly out of scope:

- Live order execution. The engine simulates; it does not trade.
- Real-time streaming data. All bar data is at-rest in Postgres.
- Portfolio-level optimization across multiple strategies (single-strategy backtests only, for now).
- Anything from the `/trade` plugin (stock analysis, thesis generation, PDF reports) — that is a separate personal plugin, not part of this repo's scope.

## Glossary

- **Engine**: the Python backtesting code in `PythonDataService/`
- **Signal**: strategy output per bar (LONG/SHORT/FLAT/NO_SIGNAL)
- **Port**: a mathematical construct translated from a reference source into this engine
- **Reference**: an external implementation (LEAN, paper, prior code) that a port is derived from
- **Golden fixture**: a serialized input-output pair from running the reference, used as ground truth for the port's tests
- **Reconciliation**: trade-by-trade comparison between two backtest runs
- **Warmup**: the period at the start of a backtest where indicators haven't accumulated enough data to emit valid output
