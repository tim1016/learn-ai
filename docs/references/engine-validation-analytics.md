# Engine Validation Analytics

## Scope

Engine Validation Analytics is the Engine Lab evidence layer for one completed
backtest run. It produces display-ready robustness views for:

- trailing performance windows: 2 weeks, 1 month, 3 months, 6 months, 1 year, 2 years
- weekday/hour entry expectancy buckets in America/New_York
- calendar-month seasonality
- trailing 20-trade stability
- rolling 20-session Sharpe versus cumulative native P&L divergence

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
- Daily return: `r_t = equity_t / equity_(t-1) - 1`, using the last native
  mark-to-market equity observation in each America/New_York session
- Rolling Sharpe: `sqrt(252) * mean(r) / sample_std(r)` over 20 sessions
- Cumulative P&L: `equity_t - equity_0`
- Divergence: over 20 eligible study observations, the ordinary-least-squares
  slope of cumulative P&L is positive while the slope of rolling Sharpe is
  negative

Trailing horizons only report `net_return` when the run's equity curve covers
the full requested window. Shorter runs return `has_full_coverage = false` and
`net_return = null`; the UI displays that as missing coverage.

The divergence flag is an exploratory decay diagnostic, not a significance
test or an entry/exit signal. It highlights periods in which the portfolio is
still accumulating dollars while its recent return per unit of volatility is
weakening. The chart uses each engine's native mark-to-market curve. This is
intentional: paired compatibility horizons continue to use their shared
return-normalized curve, so adding the study does not redefine existing parity
evidence.

## References

- Robert Pardo, *The Evaluation and Optimization of Trading Strategies*, 2nd ed.,
  chapter 4, for trade performance ratios and expectancy-style evaluation.
- Carl Bacon, *Practical Portfolio Performance Measurement*, 2nd ed., chapter 2,
  for period return measurement.
- William F. Sharpe, "The Sharpe Ratio," *Journal of Portfolio Management*,
  1994, for the reward-to-variability ratio.
- Andrew W. Lo, "The Statistics of Sharpe Ratios," *Financial Analysts
  Journal*, 2002, for Sharpe-estimator interpretation and sampling caution.

## Validation

- `PythonDataService/tests/services/test_engine_validation_analytics.py`
  verifies horizon coverage, one-year return, weekday/hour bucketing,
  calendar-month compounding, rolling stability, and rejection of non-monotonic
  equity curves.
- The same test module compares rolling Sharpe point-for-point against pandas
  rolling mean/sample-standard-deviation output with `abs=1e-12, rel=0`, checks
  New York session-close sampling, and verifies that native performance equity
  does not replace the compatibility horizon curve.
