# SPY EMA relative-difference basis-points fixture

- Reference: the U.S. Securities and Exchange Commission's Investor.gov
  [basis-point definition](https://www.investor.gov/introduction-investing/investing-basics/glossary/basis-point):
  one basis point is 0.01 percentage point. Applied to the protocol's relative
  difference `(left − right) / right`, this fixes the 10,000 scale factor.
- Generated: 2026-08-15.
- Generator: `python tests/fixtures/golden/spy-ema-difference-bps/generate.py`.
- Oracle method: expected strings are fixed literals derived independently as
  exact rational arithmetic. The non-trivial row uses a denominator of 125, so
  `(125.056789 − 125) / 125 × 10,000 = 4.5431200` terminates exactly. The
  generator copies these pinned literals and does not restate or execute the
  production formula.
- Precision: exact decimal strings; the parity test requires exact equality.
- Zero-denominator policy: reject the operand with ``ZeroDivisionError``;
  the generated fixture includes and pins that domain-error case.
