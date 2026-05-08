# RV-002 — HF Two-Component Realized Volatility (ABDL)

## Evidence Layers

**Layer 1 — Market input provenance:** Synthetic. 5 trading
days × 4 ETH bars per day = 20 bars total.
GBM intraday log-returns (seed=13, σ=0.005/bar), S₀=100.
Sessions: ETH 04:00–20:00 ET. Timestamps: UTC int64 ms.
Days: 2024-01-02 through 2024-01-08.

**Layer 2 — Methodology provenance:** Andersen, Bollerslev, Diebold, Labys
(2003) "Modeling and Forecasting Realized Volatility," Econometrica 71(2).
Two-component: intraday squared returns + overnight squared return per day.
Canonical: `app/engine/edge/features_realtime/hf_realized_vol.py::hf_realized_vol_trd252`.

**Layer 3 — Independent numerical oracle:** Pure-Python loop grouping bars by
trading day, computing intraday squared-return sums + overnight², rolling over
window_trading_days, annualizing × 252/W, then ffilling onto the bar grid.

## Formula

```text
For each trading day d:
  RV2_d = sum_i ln(close_i / close_{i-1})^2   (intraday)
          + ln(close_first_d / close_last_{d-1})^2  (overnight)

Rolling (window=3 days):
  rv_hf_d = sqrt(sum RV2_{d-W+1..d} * 252 / W)

NaN for days 0..1. Ffilled onto bar grid.
```

## NaN Convention

8 of 20 output bars are NaN (first 2 days < window).

## Canonical Implementation

`PythonDataService/app/engine/edge/features_realtime/hf_realized_vol.py::hf_realized_vol_trd252`

## Tolerance

atol=1e-8, rtol=0.0. Rationale: oracle uses Python's built-in `math.log` while
canonical uses numpy's `np.log`; observed max abs error from float64 accumulation
across 4 bars per day is < 1e-13. The 1e-8 floor provides
five orders of headroom over the observed error while remaining tight enough to
detect any formula regression.

## Regeneration

```bash
python scripts/generate_fixtures.py --id RV-002 --force \
  --justification "<reason>"
```

## Generation Metadata

Generated: 2026-05-08
Oracle: hand_computed — per-day ABDL two-component formula, pure Python
Script: scripts/fixture_generators/volatility.py
Justification: Phase 2 initial generation
