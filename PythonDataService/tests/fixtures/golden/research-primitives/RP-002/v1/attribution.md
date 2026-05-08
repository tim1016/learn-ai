# RP-002 — Quantile Monotonicity

Generated: 2026-05-08
Oracle: hand_computed — pd.qcut into 5 quantiles, mean return per bin
Canonical: PythonDataService/app/research/validation/quantile.py::compute_quantile_analysis

## Formula

bins = pd.qcut(feature, q=5)
bin_mean[i] = mean(target[feature in bin_i])
monotonicity_ratio = count(bin_mean[i+1] > bin_mean[i]) / (5 - 1)
is_monotonic = monotonicity_ratio >= 0.75

## Input

200 observations. Feature: N(0,1) seed=77.
Target: feature×0.003 + N(0,0.005) — weakly monotonic by design.

## Oracle computed values

is_monotonic: True
monotonicity_ratio: 1.000000000
bin means: ['-0.005197735', '-0.002894925', '0.000518001', '0.002073041', '0.004342342']

## Tolerance

atol=1e-9, rtol=0.0

## SHA-256

input.arrow:  73c16d4b565de82de5d49eed5a62b44d5f8f205e49dd745431a99cd7308a9c5a
output.arrow: f88405acebde3d4425a83cc17a012341ca0c60d4d9d1e53b36a2a4e8e39df31a
