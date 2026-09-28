# Golden fixture — LEAN factor-file dividend pricing (AAPL, Aug–Sep 2020)

## Reference source

QuantConnect/Lean at commit `7986ed0aade3ae5de06121682409f05984e32ff7`
(vendored under `references/lean/7986ed0aade3ae5de06121682409f05984e32ff7/`):

- `Data/equity/usa/factor_files/aapl.csv` — QuantConnect's own published AAPL
  factor file, the oracle for this fixture.
- `Common/Data/Auxiliary/CorporateFactorRow.cs` (`GetDividend`) and
  `Common/Data/Market/Dividend.cs` (`ComputeDistribution`) — how the LEAN
  engine turns two adjacent factor rows back into the cash distribution a
  raw-mode backtest pays: `round(ref × (1 − pf_i/pf_{i+1}), 2)`.
- `ToolBox/FactorFileGenerator.cs` (`CalculateNextDividendFactor`) — the
  generator our builder originally ported; its dividend term multiplies the
  cash by the cumulative later-split factor, an input convention for a
  split-adjusted dividend feed that Polygon's raw `cash_amount` does not
  satisfy (see `docs/references/lean-factor-file-dividend-pricing.md`).

## Generated

- Date: 2026-09-28 (issue #2479)
- The expected output is **derived from the reference, not from our builder**:

  - The 2020-08-07 $0.82 dividend (ex-date) and 2020-08-31 4:1 split are the
    two corporate actions; their reference sessions (2020-08-06, 2020-08-28)
    carry the closes published in the reference file's own `reference_price`
    column (455.61, 499.23).
  - The dividend event row's `price_factor` is `1 − 0.82 / 455.61 =
    0.9982002151` (10 dp) — the per-event multiplier the reference encodes:
    its adjacent rows `20200806,0.9949942,0.25,455.61` and
    `20200828,0.9967882,0.25,499.23` give `0.9949942 / 0.9967882 =
    0.9982002…`, agreeing with the exact arithmetic to 4.4e-9 (the published
    values are rounded to 7 significant digits).
  - The split event row carries `split_factor = 1/4 = 0.25` (the reference's
    own convention: its 20200828 row has `split_factor` 0.25, and rows after
    the ex-date return to 1) and `price_factor = 1` (a split does not change
    the price factor; the reference's 0.9967882 there is the accumulation of
    later dividends outside this capture).
  - The two anchor rows follow the builder's documented anchor contract
    (`factor_files.py`: first/last captured session, fully-cumulated factors
    on the first anchor, identity on the last, nearest available close as the
    reference price when the anchor session itself has none).

## Regenerate

```bash
curl -sL "https://raw.githubusercontent.com/QuantConnect/Lean/7986ed0aade3ae5de06121682409f05984e32ff7/Data/equity/usa/factor_files/aapl.csv"
# rows 20200806 and 20200828 are the reference for the two event rows
```

The captured sessions (2020-08-03..2020-09-04) are the canonical calendar's
NYSE sessions (`app.lean_sidecar.trading_calendar.expected_sessions`) — one
contiguous span containing both ex-dates with `first < ex-date <= last`.

## Tolerances

- Built-bytes comparison: exact equality (deterministic builder, Decimal
  arithmetic, 10-dp quantization).
- Reference-row reconciliation (`pf_i/pf_{i+1}` against the published pair):
  `atol=1e-6, rtol=0`. The published factors carry 7-significant-digit
  rounding (±5e-8 relative each), which displaces their ratio by up to
  ~1e-7; 1e-6 gives an order of magnitude of headroom while still excluding
  the old formula's `1 − 0.82 × 0.25 / 455.61 = 0.99955…` (2.4e-3 away).
- Raw-mode dividend cash: `atol=1e-9, rtol=0` on the unrounded distribution
  `455.61 × (1 − pf_i/pf_{i+1}) = 0.82` (LEAN rounds to 2 dp at payout
  time, which is downstream of the value asserted here).

## Assumptions

- Polygon `cash_amount` is the raw per-share cash amount and the lake's
  `reference_close` is the raw prior-session close — the same basis as the
  reference file's `reference_price` column (AAPL really closed at $455.61
  on 2020-08-06 and paid $0.82 with ex-date 2020-08-07).
- The dividend precedes the split in time; the file's cumulative
  `split_factor` at the dividend row (0.25) matches the reference file's
  own layout, where a row dated D carries the factors for data ≤ D.
