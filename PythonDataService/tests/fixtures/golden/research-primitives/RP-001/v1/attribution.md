# RP-001 — Information Coefficient (Spearman IC)

Generated: 2026-05-08
Oracle: literature_formula — scipy.stats.spearmanr per calendar day; mean ± t-stat
Canonical: PythonDataService/app/research/validation/ic.py::compute_information_coefficient

## Formula

IC_d = Spearman(rank(feature_d), rank(return_d))  for each trading day d
mean_IC = (1/N) × sum(IC_d)
t = mean_IC / (std_IC(ddof=1) / sqrt(N))

Reference: López de Prado, Advances in Financial Machine Learning (2018) §8.

## Input

4 trading days × 10 bars each = 40 bars.
Timestamps: 2024-01-02..2024-01-05, 09:30 ET, 15-min cadence (int64 ms UTC).
Feature: seeded N(0,1), seed=42. Target returns: seeded N(0,0.01), seed=43.

## Oracle computed values

mean_IC: -0.115151515
t-stat:  -1.675022233
N days:  4
daily ICs: ['-0.296969697', '-0.127272727', '0.030303030', '-0.066666667']

## Tolerance

atol=1e-9, rtol=0.0

## Justification

Initial generation.

## SHA-256

input.arrow:  70aa7cd04aa1bc2f10f343e7265da135337ef0ece59eabb7183c4ef9ca25ddbb
output.arrow: 1d84cbdcf6e07489b8d571ec4b2a807a93124be81eaa1398e4555935be2a676d
