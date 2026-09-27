# Strategy Lab metric-help formulas

Strategy Lab displays definitions for eight persisted run metrics. Angular does
not recompute them: the canonical implementation remains
`PythonDataService/app/engine/results/statistics.py`.

The golden input and outputs are frozen in
`contracts/fixtures/strategy-metric-help-golden-v2.json`. The fixture covers net
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

## Version 2: include the first evaluated session

[Issue #2448](https://github.com/tim1016/learn-ai/issues/2448) corrects platform
daily-return sampling: evaluation starting capital precedes the first evaluated
session close. The former metric-help v1 fixture captured the omitted-session
behavior. After that conflict was raised on
[PR #2525](https://github.com/tim1016/learn-ai/pull/2525#issuecomment-5859421278),
the owner directed the fix on 2026-09-27. Version 2 records the corrected
convention; the v1 fixture is retained byte-for-byte as historical evidence.
The estimator formulas and `1e-12` absolute, `0` relative tolerance do not change.
The catalog's platform formula identifiers remain stable because this corrects
their sampling bug, rather than introducing another estimator.

The unchanged synthetic equity observations are `[100000, 110000, 99000,
118800, 112860]`, each on a distinct ET date, and evaluation capital is
`100000`. The dates are synthetic observations, not a claim of exchange-session
coverage. Anchoring the first observation gives five returns:
`[0, 0.1, -0.1, 0.2, -0.05]`. In exact decimal arithmetic:

- Mean return: `0.03`.
- Sample variance: `0.058 / 4 = 0.0145`.
- Downside second moment: `(0.1² + 0.05²) / 5 = 0.0025`.
- Sharpe: `0.03 / sqrt(0.0145) * sqrt(252)` =
  `3.9549183696183702125388311436939317937193693850948`.
- Sortino: `0.03 / sqrt(0.0025) * sqrt(252)` =
  `9.5247047198325261258058167131013375325569330590970`.

Those two expectations were generated independently using Python's `Decimal`
at 50-digit precision, without importing the production statistics module.
`test_v2_return_metrics_have_an_independent_decimal_oracle` verifies the exact
intermediate values and the unchanged inputs, other six outputs, and tolerance.
The public-path golden test then compares the platform results with v2. The
`schema_version` remains `1`: the JSON structure is unchanged, while the filename
versions its numerical expectations.

To reproduce v2 from the repository root (writes v2 only):

```python
import json
from decimal import Decimal, localcontext
from itertools import pairwise
from pathlib import Path

root = Path("contracts/fixtures")
fixture = json.loads((root / "strategy-metric-help-golden-v1.json").read_text())
with localcontext() as context:
    context.prec = 50
    equity = [Decimal(fixture["inputs"]["initial_cash"])] + [
        Decimal(point["equity"]) for point in fixture["inputs"]["equity_points"]
    ]
    returns = [current / previous - 1 for previous, current in pairwise(equity)]
    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
    downside = sum(min(value, Decimal(0)) ** 2 for value in returns) / len(returns)
    fixture["expected"]["sharpe"] = float(mean / variance.sqrt() * Decimal(252).sqrt())
    fixture["expected"]["sortino"] = float(mean / downside.sqrt() * Decimal(252).sqrt())
(root / "strategy-metric-help-golden-v2.json").write_text(json.dumps(fixture, indent=2) + "\n")
```

LEAN-native statistics deliberately retain their Day-0/Day-1 skip and the
`lean-statistics-oracle-v1` receipt. All LEAN and ENG-00x fixtures remain
unchanged. No historical results are rewritten by this change.

Nonpositive evaluation capital cannot define an anchored percentage return.
For a retained positive equity curve, platform Sharpe, Sortino, annualized
volatility, and PSR are therefore unavailable; absolute P&L remains available.
The public summary tests cover both zero and negative starting capital. The
existing validation of a nonpositive reconstructed equity curve remains in
place when no retained curve is supplied.
