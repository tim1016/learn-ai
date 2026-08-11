# Strategy Lab analytical manual v1 — claim ledger

This is an evidence ledger for the generated metric manual.  It is not a
second source of formulas or policy.  Python's generated catalog identifies
the canonical implementation and validation receipt for every rendered entry.

| Claim | Classification | Evidence and qualification |
| --- | --- | --- |
| LEAN-native statistics use the pinned source revision. | Verified | `references/` receipt and the native oracle fixture pin QuantConnect LEAN `261366a7e26ae942df858ab20df4fef8fa07de67`; `PythonDataService/tests/test_lean_statistics.py` checks the parity output with `abs <= 0.0000500001`, `rtol = 0`. |
| Platform Sharpe is distinct from LEAN-native Sharpe. | Verified with producer-specific qualification | The platform implementation and `contracts/fixtures/strategy-metric-help-golden-v1.json` govern the platform entry.  The LEAN entry follows its pinned source, daily preprocessing, and dated risk-free input.  The labels alone do not imply equality. |
| Square-root annualization is a convention rather than proof of independent returns. | Verified with qualification | The platform contract states its selected daily/trade fallback convention.  Serial correlation can limit interpretation; the manual does not claim it removes that limitation. |
| Probabilistic Sharpe and deflated Sharpe are interchangeable. | Corrected | The platform catalog documents only the produced probabilistic Sharpe contract.  Deflated Sharpe is not inferred or substituted. |
| The 17-input Backtest Evidence Grade establishes deployability or identifies a cause. | Product policy | `PythonDataService/app/services/run_verdict_service.py` owns the frozen v2 buckets.  The UI presents a research-evidence summary and an investigation action, not a causal conclusion or trading authorization. |
| Performance Memory provides independent confirmation. | Corrected | `PythonDataService/app/services/engine_validation_analytics.py` and its focused tests describe the same recorded run by horizon, timing, seasonality, and overlapping windows.  It is descriptive evidence, not an independent run. |
| Realized equity and the risk-statistic input curve are always the same. | Corrected | `PythonDataService/app/engine/results/equity_downsample.py` owns realized-equity presentation; the catalog separately describes the producing risk-input contract. |
| The dated Run 130 example is required for tests. | Deferred | The manual fixture is `PythonDataService/tests/fixtures/golden/strategy-lab-analytical-manual-v1/`; no runtime database row is a test dependency. |

## Trader-language research sources

The trader interpretations and cautions are synthesized explanations, not new
calculation contracts. They were checked against QuantConnect's official
[backtest Results documentation](https://www.quantconnect.com/docs/v2/cloud-platform/backtesting/results),
[Backtest Statistics API reference](https://www.quantconnect.com/docs/v2/cloud-platform/api-reference/backtest-management/read-backtest/backtest-statistics),
[Alpha indicator documentation](https://www.quantconnect.com/docs/v2/writing-algorithms/indicators/supported-indicators/alpha),
and [trading glossary](https://www.quantconnect.com/docs/v2/writing-algorithms/key-concepts/glossary).
When a producer has no retained formula contract, the manual identifies it as a
reported or policy value and does not invent a formula from general literature.

Primary implementation receipts are linked by the generated catalog.  General
literature supports interpretation only and never overrides a producer-specific
code contract.
