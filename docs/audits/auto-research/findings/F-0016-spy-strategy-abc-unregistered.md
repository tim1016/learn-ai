---
id: F-0016
severity: P2
status: open
area: inventory
canonical_file: PythonDataService/app/engine/strategy/algorithms/spy_strategy_{a,b,c}.py
reference: missing
first_seen: 2026-05-05
last_seen: 2026-05-05
phase: 1
---

## What

Three engine-level strategy algorithms exist with no registry rows:

- `spy_strategy_a.py` — EMA-gap + MACD + RSI-range; ADX(<15) exit
- `spy_strategy_b.py` — Supertrend + ADX(>threshold) + MACD + RSI-range; ADX(<20) exit
- `spy_strategy_c.py` — ADX(>threshold) + ADX-rising + RSI-range; ADX(<15) exit

All three subclass `_rsi_range_base.RsiRangeStrategy` (also unregistered). The registry's Strategies section currently lists SPY EMA Crossover, SPY ORB, QQQ ORB, RSI Mean Reversion, SMA Crossover, Momentum RSI/Stochastic, RSI Reversal — but not these.

## Where

- `PythonDataService/app/engine/strategy/algorithms/spy_strategy_a.py`
- `PythonDataService/app/engine/strategy/algorithms/spy_strategy_b.py`
- `PythonDataService/app/engine/strategy/algorithms/spy_strategy_c.py`
- `PythonDataService/app/engine/strategy/algorithms/_rsi_range_base.py` (base class — also unregistered)

## Why this severity

P2 — Strategies, not primitives. They appear to be experiments / variants for SPY. Whether they are user-runnable today (exposed via Engine Lab) or research-only is unclear from the file alone; the registry should classify them.

## Reproduction

```
git ls-files PythonDataService/app/engine/strategy/algorithms/ | grep spy_strategy
grep -c 'spy_strategy_a\|spy_strategy_b\|spy_strategy_c' docs/math-sources-of-truth.md   # 0
```

## Suggested resolution (NOT auto-applied)

Three rows in `math-sources-of-truth.md` § Strategies (or one row covering all three under "SPY RSI-range strategy variants A/B/C"):

| Concept | Canonical | Legacy / duplicates | Reference | Validated against | Status |
|---|---|---|---|---|---|
| SPY RSI-range A (EMA-gap + MACD) | `app/engine/strategy/algorithms/spy_strategy_a.py` | — | Internal (no external port reference) | (existing tests under `app/engine/tests/`, or `NONE — pending`) | canonical |
| SPY RSI-range B (Supertrend + ADX) | `spy_strategy_b.py` | — | Internal | (same) | canonical |
| SPY RSI-range C (ADX-rising) | `spy_strategy_c.py` | — | Internal | (same) | canonical |

If they are research-only and not exposed to user runs, mark `Status: research-only` with that designation defined.

## Provenance of the finding itself

Phase 1 / cursor: `app/engine/strategy/algorithms/spy_strategy_{a,b,c}.py` head reads.
