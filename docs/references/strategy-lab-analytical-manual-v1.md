# Strategy Lab analytical manual v1 — trader-language sources

Python's generated catalog
(`PythonDataService/app/research/documentation/analytical_metric_catalog.py`)
identifies the canonical implementation and validation receipt for every
rendered entry. This note holds only the outside sources the manual's
trader language was checked against.

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
