---
id: F-0006
severity: P1
status: fixed-verified
area: inventory
canonical_file: PythonDataService/app/engine/results/statistics.py
reference: docs/math-sources-of-truth.md (Backtesting engine and statistics section, vague path)
first_seen: 2026-05-05
last_seen: 2026-05-05
phase: 1
---

## What

`docs/math-sources-of-truth.md` rows for **Max drawdown** (line 46), **Sharpe ratio** (line 47), and **Bar consolidation, event replay, fill models** (line 44) all cite the canonical as `PythonDataService/app/engine/` — a directory, not a file. The actual file is `PythonDataService/app/engine/results/statistics.py` (TradeStatistics, EquityPoint, `TRADING_DAYS_PER_YEAR=252`, ratio computations). The row that lists "37 files, LEAN-ported" is unactionable for a reviewer who wants to find the specific Sharpe formula used.

## Where

- Registry rows: `docs/math-sources-of-truth.md:44, 46, 47`
- Actual file: `PythonDataService/app/engine/results/statistics.py`
  - Module docstring confirms it computes per-trade and per-period metrics (Sharpe, drawdown, etc.)
  - `TRADING_DAYS_PER_YEAR = 252` (annualization constant — should be a pinpoint provenance citation)

## Why this severity

P1 — A registry that points at `PythonDataService/app/engine/` cannot serve its purpose (telling the contract which file to point at). The registry's *job* is to be the pinpoint lookup; broad-path entries defeat it. Bumping to P0 is also defensible because Sharpe and max drawdown are user-facing performance numbers — but P1 fits the "missing pinpoint provenance" pattern over P0 "actively wrong".

## Reproduction

```
grep -nE "PythonDataService/app/engine/\s*\|" docs/math-sources-of-truth.md   # rows with directory-only canonicals
test -f PythonDataService/app/engine/results/statistics.py    # exit 0
head -25 PythonDataService/app/engine/results/statistics.py   # confirms it owns Sharpe + drawdown
```

## Suggested resolution (NOT auto-applied)

Tighten the three rows in `math-sources-of-truth.md`:

- **Max drawdown** canonical → `PythonDataService/app/engine/results/statistics.py` (specific function name)
- **Sharpe ratio** canonical → same file (specific function name)
- **Bar consolidation, event replay, fill models** canonical → split into specific rows for `app/engine/consolidators/trade_bar_consolidator.py`, `app/engine/engine.py`, `app/engine/execution/fill_model.py`, `app/engine/execution/intrabar_resolver.py`, `app/engine/execution/portfolio.py`

Each tightened row should carry the existing `Validated against` test pointer (currently `Backend.Tests/Unit/Services/BacktestServiceTests.cs` — service-level, not great; pending-fixture is honest).

Cross-reference: this overlaps with the bigger §4 of the migration plan ("BacktestService.cs is being retired in Phase 3"). The .NET row already says it's pending-migration; the Python side is what needs sharpening.

## Provenance of the finding itself

Phase 1 / cursor: registry row scan. File presence verified with `Read`.

## Closure (2026-05-06)

`docs/math-sources-of-truth.md` registry rows updated:

- **Max drawdown** canonical → `PythonDataService/app/engine/results/statistics.py`. Legacy column expanded to also list `SnapshotService.cs::ComputeDrawdownSeries` per F-0011.
- **Sharpe ratio** canonical → `PythonDataService/app/engine/results/statistics.py` with `TRADING_DAYS_PER_YEAR = 252` annualization noted.
- **Bar consolidation, event replay, fill models** canonical → split into specific files: `app/engine/engine.py` (orchestration), `app/engine/consolidators/trade_bar_consolidator.py` (consolidation), `app/engine/execution/{fill_model,intrabar_resolver,portfolio,order,execution_config}.py` (execution + accounting).

The remaining work (4-field provenance block on each file) is tracked separately in F-0027 and is touch-driven per the registry's burn-down rule.

