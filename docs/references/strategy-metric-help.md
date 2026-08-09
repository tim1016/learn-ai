# Strategy Lab metric-help formulas

Strategy Lab displays definitions for eight persisted run metrics. Angular does
not recompute them: the canonical implementation remains
`PythonDataService/app/engine/results/statistics.py`.

The golden input and outputs are frozen in
`contracts/fixtures/strategy-metric-help-golden-v1.json`. The fixture covers net
profit, profit factor, expectancy, Sharpe, Sortino, maximum drawdown, win rate,
and completed-trade count. Its validating test is
`PythonDataService/tests/fixtures/test_strategy_metric_help_golden.py`, with
absolute tolerance `1e-12` and relative tolerance `0`.

Provenance follows the canonical module references: Sharpe (1994), “The Sharpe
Ratio,” *Journal of Portfolio Management* 21(1), section IV; and Bacon,
*Practical Portfolio Performance Measurement*, second edition, section 8.2 for
maximum drawdown. Trade-ledger formulas are validated directly against
`compute_trade_statistics`; equity-curve formulas are validated against
`compute_portfolio_statistics` and `max_drawdown` through that public path.
