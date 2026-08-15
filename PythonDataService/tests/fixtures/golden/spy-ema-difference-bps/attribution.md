# SPY EMA relative-difference basis-points fixture

- Reference: the normalized-gap definition recorded in
  `docs/references/spy-ema-normalized-gap-walk-forward.md`:
  `10,000 × (left − right) / right`.
- Generated: 2026-08-15.
- Generator: `python tests/fixtures/golden/spy-ema-difference-bps/generate.py`.
- Arithmetic authority used to generate expected values: Python
  `decimal.Decimal`, independently from the strategy-spec operand evaluator.
- Precision: exact decimal strings; the parity test requires exact equality.
- Assumptions: the denominator is non-zero. A zero denominator is a rejected
  domain input and is covered separately by the unit test.
