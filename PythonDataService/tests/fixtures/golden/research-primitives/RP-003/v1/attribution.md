# RP-003 — Phipson-Smyth Permutation P-value

Generated: 2026-05-08
Oracle: literature_formula — Phipson-Smyth (2010) p = (1+count(null≥obs))/(N+1)
Canonical: PythonDataService/app/research/baselines/runner.py::_empirical_position

## Formula

percentile = count(null < parent) / N          (fraction strictly less than)
p_value    = (1 + count(null >= parent)) / (N + 1)   — Phipson-Smyth small-sample

Reference: Phipson, B. & Smyth, G.K. (2010). Permutation p-values should never
be zero: calculating exact p-values when permutations are randomly drawn.
Statistical Applications in Genetics and Molecular Biology 9(1), Article 39.

## Input

Observed IC: 0.12
Null distribution: 200 values, N(0, 0.05) seed=99.

## Oracle computed values

percentile: 0.995000000
p_value:    0.009950249

## Tolerance

atol=1e-9, rtol=0.0

## SHA-256

input.arrow:  43d5af3d86f030fb251438cfc46041d3d73a4aba1d695856abf8c0e074968524
output.arrow: ee6cd02c1aadca0995ddef4bfc7a97f9003d8cbda13c518e5fd9eeffe25df084
