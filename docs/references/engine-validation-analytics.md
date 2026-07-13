# Engine Validation Analytics

## Scope

Engine Validation Analytics is the Engine Lab evidence layer for one completed
backtest run. It produces display-ready robustness views for:

- trailing performance windows: 2 weeks, 1 month, 3 months, 6 months, 1 year, 2 years
- weekday/hour entry expectancy buckets in America/New_York
- calendar-month seasonality
- trailing 20-trade stability

The canonical implementation is
`PythonDataService/app/services/engine_validation_analytics.py`. Angular is
render-only and must not recompute these values.

## Formulas

- Horizon return: `equity_end / equity_start - 1`
- Bucket expectancy: arithmetic mean of closed-trade `pnl_pct`
- Bucket win rate: `winning_trades / total_trades`
- Monthly seasonality: `product(1 + pnl_pct_i) - 1` per year-month, then
  median across observations for the same calendar month
- Rolling stability: expectancy and win rate over each trailing 20-trade window

Trailing horizons only report `net_return` when the run's equity curve covers
the full requested window. Shorter runs return `has_full_coverage = false` and
`net_return = null`; the UI displays that as missing coverage.

## References

- Robert Pardo, *The Evaluation and Optimization of Trading Strategies*, 2nd ed.,
  chapter 4, for trade performance ratios and expectancy-style evaluation.
- Carl Bacon, *Practical Portfolio Performance Measurement*, 2nd ed., chapter 2,
  for period return measurement.

## Validation

- `PythonDataService/tests/services/test_engine_validation_analytics.py`
  verifies horizon coverage, one-year return, weekday/hour bucketing,
  calendar-month compounding, rolling stability, and rejection of non-monotonic
  equity curves.
