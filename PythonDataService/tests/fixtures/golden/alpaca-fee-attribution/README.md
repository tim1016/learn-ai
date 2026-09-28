# Fee attribution oracle

Source: owner-defined fee policy in [PRD #2540](https://github.com/tim1016/learn-ai/issues/2540), September 27, 2026 revision, integer-cent Hamilton allocation. This is an independent rational-arithmetic conservation oracle, not a claim about the broker's proprietary allocation.

Generated 2026-09-27 with `python3 PythonDataService/tests/fixtures/golden/alpaca-fee-attribution/generate.py`. Generator uses only standard-library fractions and never imports production code. No time-zone, rate or market-data assumptions. Equality tolerance: exactly zero integer cents. Weights are unrounded predicted fees, never deployment size or fill notional substituted for fees.
