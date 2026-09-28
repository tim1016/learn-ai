# LEAN factor-file dividend pricing — port attribution

## Target

`PythonDataService/app/data_lake/factor_files.py::build_factor_file_bytes` —
the canonical factor-file builder. The dividend price factor is
`1 - cash_amount / reference_close`: **raw Polygon cash over the raw
prior-session close**, with no split term. Fixed by #2479 (2026-09-28); the
pre-fix builder multiplied the cash by the cumulative later-split factor.

## Reference

QuantConnect/Lean at commit `7986ed0aade3ae5de06121682409f05984e32ff7`
(Apache-2.0), vendored under `references/lean/7986ed0…/`:

- **The oracle — QuantConnect's own published factor file**,
  `Data/equity/usa/factor_files/aapl.csv`. Its rows
  `20200806,0.9949942,0.25,455.61` and `20200828,0.9967882,0.25,499.23`
  encode the 2020-08-07 $0.82 dividend — paid three weeks *before* the
  2020-08-31 4:1 split — as the per-event multiplier
  `0.9949942 / 0.9967882 = 0.9982002… = 1 − 0.82 / 455.61`. Raw cash over
  raw close. The same convention holds for every dividend row in the file
  (e.g. the 2020-11-05 row against 2021-02-04 pays
  `119.03 × (1 − 0.9967882/0.9985079) = $0.205`, the actual Nov-2020
  payout).
- **What the engine does with the rows** —
  `Common/Data/Auxiliary/CorporateFactorRow.cs::GetDividend` builds
  `Dividend.Create(symbol, nextTradingDay, ReferencePrice, PriceFactor /
  nextRow.PriceFactor, 2)` and `Common/Data/Market/Dividend.cs::
  ComputeDistribution` pays `round(close × (1 − ratio), 2)` per share. So
  the factor file is the *sole* carrier of the dividend cash amount in a
  LEAN backtest: whatever ratio the rows encode is what every raw-mode
  backtest pays.
- **What we did not port verbatim** —
  `ToolBox/FactorFileGenerator.cs::CalculateNextDividendFactor`:
  `priceFactor = prev.PriceFactor * (1 − dividend.Value *
  prev.SplitFactor / previousClosingPrice.Close)`. The generator walks
  newest-to-oldest from a terminal `(1, 1)` row exactly as our builder
  does, and the split term exists to convert a **split-adjusted dividend
  feed** back to raw: for the published AAPL row to come out of this
  formula, its `dividend.Value` input must be `0.82 / 0.25 = 3.28`, not
  the raw $0.82. Polygon's `cash_amount` is raw (and our `reference_close`
  is the raw prior close), so porting the term verbatim (#2452 report)
  priced each dividend at `1 − cash × S / close` with `S` the product of
  every *later* split in the file.

## Why it was wrong, in both directions

With AAPL's real 2020 sequence (dividend, then 4:1 split):

- **Adjusted ratios**: our dividend row came out `0.9995500538` instead of
  `0.9982002151` — each affected dividend day's return was off by
  `0.82 × (1 − 0.25) / 455.61 ≈ 0.135` percentage points. Since #2452 the
  factor file covers every captured session, not the request window, so a
  split anywhere later in the lake rescaled earlier dividend days for
  *every* study of that symbol.
- **Raw-mode cash**: LEAN pays `ref × (1 − pf_i/pf_{i+1})` per share, so
  our rows made a LEAN raw-mode backtest pay `455.61 × 0.00045 = $0.205`
  per share instead of `$0.82` — the payout shrunk by exactly the later
  split factor `S` (`cash × S` in general).

## Tolerance

- Built bytes vs. the fixture: exact equality (deterministic Decimal
  builder, 10-dp factor quantization).
- Reconciliation against the published rows: `atol=1e-6, rtol=0`. The
  published factors are rounded to 7 significant digits, which displaces
  their ratio by up to ~1e-7 (measured: 4.4e-9 for the AAPL pair); the old
  formula sits 2.4e-3 away, so the tolerance separates the two by three
  orders of magnitude.
- Raw-mode dividend cash: the 2-dp rounded payout is asserted exactly
  (`$0.82`); the unrounded value at `atol=1e-7` (the 10-dp factor
  quantization's worst-case displacement is `455.61 × 1e-10 ≈ 4.6e-8`).

## Validated by

- Golden fixture `PythonDataService/tests/fixtures/golden/lean-factor-file-aapl/`
  (input actions + closes, expected rows, this attribution) exercised by
  `PythonDataService/tests/data_lake/test_factor_files_golden.py`.
- `tests/data_lake/test_factor_files_coverage.py::
  test_a_later_split_leaves_an_earlier_dividends_ratio_unchanged` — the
  #2452 pin flipped: a later split no longer moves an earlier dividend
  day's ratio.
- Read-side parity (unchanged by this fix):
  `tests/data_lake/test_factor_files.py` against LEAN's
  `CorporateFactorProvider.GetScalingFactors` walk.

Registry: `docs/math-sources-of-truth.md` → "LEAN factor-file
corporate-action factors".
