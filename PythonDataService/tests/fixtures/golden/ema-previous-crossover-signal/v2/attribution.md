# ema_previous_crossover SignalSession trace corpus v2

Generated: 2026-10-23. Program identity: `ema-previous-crossover/3`.

## Reference and inputs

This is an internal, byte-exact replay commitment under ADR 0043, not an
independent validation of the whole strategy. The ten cells, symbols,
resolved parameters and trace counts are recorded in `trace-corpus.json`.
Inputs are the committed Polygon-captured minute bars at
`cross-engine-studies/cells/<cell>/lean/observations.csv`. Historical
attribution and the previous corpus remain in `../v1/`.

The corrected indicators use LEAN commit
`7986ed0aade3ae5de06121682409f05984e32ff7`, vendored under
`references/lean/`: `ExponentialMovingAverage.cs`,
`RelativeStrengthIndex.cs`, `WilderMovingAverage.cs`, and
`MovingAverageTypeExtensions.cs`. IND-001/v2 and IND-003/v2 separately
prove indicator values at `atol=1e-9, rtol=0`; the period-14 Decimal
operation-order regression is in `tests/fixtures/test_indicator_fixtures.py`.

## Why v2

PR #3484 corrects EMA/RSI warmup publication and preserves Wilder's
`sample*k + previous*(1-k)` evaluation order, including the first ready
update. Changing that arithmetic changes the serialized RSI decimals in
`reason_evidence`.

A bar-by-bar comparison against the indicators at
`b2fb4a8dca6b43baaf244c79d4811ce12757f7d2` reproduced every v1 root
before comparing the corrected implementation. Across this program's
35,900 evaluations, 19,251 evidence payloads changed. Ready RSI
values differ by at most `1.2e-25`. Before changing the version, every
other trace field was identical: readiness, convergence, relation and
signal facts, staged candidates, action requests, timestamps and counts.
This corpus demonstrates those inputs only; it is not a universal
no-decision-change claim for arbitrary prices or threshold settings.

The new program version also changes evaluation IDs by design. Previous
seals remain historical; Start/Resume requires deploying a fresh instance
under the new identity. No persisted seals or trading records are rewritten.

## Regeneration and qualification

From `PythonDataService/`:

```sh
python -m scripts.generate_signal_program_trace_corpus --program ema_previous_crossover --output tests/fixtures/golden/ema-previous-crossover-signal/v2/trace-corpus.json
python -m scripts.run_signal_program_build_qualification
```

The replay uses `BacktestEngine.for_decision_identity`: every staged
candidate is committed, and the strategy's own configured date window
bounds the input. Session protocol and validated settings are unchanged.
The corpus root is SHA-256 over exact ordered trace payloads; no tolerance
or field exclusion is used to retain an old identity. Qualification emits
a receipt only after the generated corpus passes its replay test.
