# IV-004 — IV Rank Rolling 60-Day Window

## Evidence Layers

**Layer 1 — Market input provenance:** Synthetic. 80-bar IV series
simulated via mean-reverting random walk (seed=42, mean=0.20,
σ_step=0.012). Represents a realistic ATM IV30 time series.

**Layer 2 — Methodology provenance:**
`app/research/features/options_features.py::OptionsFeatures.compute_iv_rank`
Uses `pd.Series.rolling(window=60, min_periods=30).min()/.max()`.

**Layer 3 — Independent numerical oracle:** Pure-Python loop computing the
rolling min/max and rank formula without calling `compute_iv_rank` or pandas.

## Formula

```text
rolling_min[i] = min(iv[max(0, i-59)..i])  if count >= 30 else NaN
rolling_max[i] = max(iv[max(0, i-59)..i])  if count >= 30 else NaN
denom = rolling_max - rolling_min
rank[i] = (iv[i] - rolling_min[i]) / denom  if denom > 1e-10 else 0.5
         NaN                                 if count < 30
```

## NaN Convention

Bars 0..28 (first 29): NaN (below min_periods=30).
First non-NaN bar: 0 (bar index 29, 0-indexed).

## Canonical Implementation

`PythonDataService/app/research/features/options_features.py::OptionsFeatures.compute_iv_rank`

## Tolerance

atol=1e-9, rtol=0.0. Rationale: the oracle and canonical both apply the same
arithmetic (subtraction, division); float64 ULP differences are < 1e-15.
The 1e-9 floor provides ample headroom for any platform variance.

## Regeneration

```bash
python scripts/generate_fixtures.py --id IV-004 --force \
  --justification "<reason>"
```

## Generation Metadata

Generated: 2026-05-08
Oracle: hand_computed — rolling (iv-min)/(max-min) in pure Python
Script: scripts/fixture_generators/volatility.py
(initial generation)
