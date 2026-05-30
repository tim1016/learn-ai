# Reconciliation — SPY VWAP-band reversion (Python port ↔ QuantConnect)

## What was reconciled

`SpyVwapReversionAlgorithm` (Python port,
`app/engine/strategy/algorithms/spy_vwap_reversion.py`) against the
QuantConnect Cloud reference
(`references/quantconnect/spy_vwap_reversion/main.py`), via the
`qc_reconciler` taxonomy.

- **Reference orders:** `tests/fixtures/golden/spy-vwap-reversion-qc/qc_orders.json`
  (10 fills, 5 long round-trips).
- **Test window:** 2024-03-04 → 2024-03-08, SPY, 1-minute.
- **Our bars:** Polygon-sourced LEAN minute cache (committed fixture under
  the same dir). **QC's bars:** QuantConnect's own SPY minute feed.
- **Fill model:** `SIGNAL_BAR_CLOSE` — QC fills these market orders at the
  signal bar's close (verified: entry 1 matches to the cent, 512.18 @ 11:36).
- **Test:** `tests/integration/reconciliation/test_spy_vwap_reversion_qc.py`.

## Divergence count by category

| Category | Count | Gating? | Notes |
|---|---|---|---|
| DECISION_MISMATCH | 0 | yes | — |
| DIRECTION_MISMATCH | 0 | yes | — |
| QUANTITY_MISMATCH | 0 | yes | fixed 100 shares both sides |
| ORDER_TYPE_MISMATCH | 0 | yes | market both sides |
| FILL_PRICE_DRIFT (> $0.01) | 4 | **accepted** | data-source — see below |
| COMMISSION_DRIFT | n/a | non-gating | Branch-B (no fee in QC export) |

**Decision-level parity is exact:** every QC trade pairs to one of ours on
`(trading_date, side)` with identical signed quantity. The port's VWAP /
sigma / band / signal math is faithful.

## Accepted divergence — fill-price drift (data source)

8 of 10 fills land on the exact minute; prices differ by $0.005–$0.02
(QC-vs-Polygon vendor rounding). **2 entries fire one minute earlier in QC**
(drift $0.29 and $0.21) — and these are confirmed data-source, not a port
bug:

| Day | Our entry | QC entry | Receipt (our data) |
|---|---|---|---|
| 2024-03-06 | 10:02 | 10:01 | at 10:01 our close `509.8338` sat **+$0.0091** above our lower band `509.8247` — a hair from crossing; QC's marginally-lower print crossed one bar earlier |
| 2024-03-08 | 11:05 | 11:04 | at 11:04 our close `516.7050` sat **+$0.0077** above our lower band `516.6973` — same |

On both bars our close was **< 1¢** from the lower band; QC's sub-cent-
different SPY prices crossed the threshold one bar sooner. This is the
vendor data-source confound the shadow study (PRD-B Layer B `DATA_DRIFT`)
exists to quantify — not an engine defect.

**Accepted tolerance:** `fill_price_atol = $0.30` for this cross-vendor
reconciliation. Max observed drift $0.29 ≈ **0.06%** of SPY's ~$512 price —
small relative to range, data-source, documented (the three conditions
`.claude/rules/numerical-rigor.md` requires before loosening). The strict
$0.01 test still asserts the *only* category that appears is
FILL_PRICE_DRIFT (never a decision mismatch), so a real math regression
would still fail the suite.

## Reproduce

```
podman exec polygon-data-service python -m pytest \
  tests/integration/reconciliation/test_spy_vwap_reversion_qc.py -v
```
