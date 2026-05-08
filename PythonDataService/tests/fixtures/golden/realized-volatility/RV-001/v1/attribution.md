# RV-001 — Close-to-Close Realized Volatility

## Evidence Layers

**Layer 1 — Market input provenance:** Synthetic. 30-bar close
price series from a GBM log-normal walk (seed=7, σ_step=0.01,
S₀=100.0).

**Layer 2 — Methodology provenance:** Standard close-to-close RV estimator.
Parkinson (1980) credits it as the "classical" method. Canonical:
`app/engine/edge/features_realtime/realized_vol.py::close_to_close`.

**Layer 3 — Independent numerical oracle:** Pure-Python loop computing
rolling sample variance of log returns with ddof=1, annualized with ×252.

## Formula

```text
log_ret[i] = ln(close[i] / close[i-1])
var[i]     = Var(log_ret[i-9..i], ddof=1)  — rolling window=10
rv[i]      = √(var[i] · 252)               — annualized
NaN for i < 10
```

## NaN Convention

Bars 0..8: NaN. First non-NaN bar: 10.

## Canonical Implementation

`PythonDataService/app/engine/edge/features_realtime/realized_vol.py::close_to_close`

## Tolerance

atol=1e-9, rtol=0.0. Rationale: oracle and canonical use identical ddof=1
variance formulas on the same float64 data; observed max abs error < 1e-15.

## Regeneration

```bash
python scripts/generate_fixtures.py --id RV-001 --force \
  --justification "<reason>"
```

## Generation Metadata

Generated: 2026-05-08
Oracle: hand_computed — rolling var(log_ret, ddof=1)*252, pure Python
Script: scripts/fixture_generators/volatility.py
(initial generation)
