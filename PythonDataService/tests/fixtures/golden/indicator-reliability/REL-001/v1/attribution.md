# REL-001 — IC Hit Rate (Win-rate Stability)

Generated: 2026-05-08
Oracle: hand_computed — count(daily_ic × sign(mean_ic) > 0) / N
Canonical: PythonDataService/app/research/validation/ic.py::compute_information_coefficient (hit_rate field)

## Formula

expected_sign = sign(mean_IC) = -1  (negative (mean_ic < 0))
hit_rate = count(daily_ic_d × expected_sign > 0) / N

## Input

Same synthetic data as RP-001: 4 days × 10 bars each.

## Oracle computed values

mean_ic:  -0.115151515
hit_rate: 0.750000000
n_days:   4

## Tolerance

atol=1e-9, rtol=0.0

## SHA-256

input.arrow:  70aa7cd04aa1bc2f10f343e7265da135337ef0ece59eabb7183c4ef9ca25ddbb
output.arrow: bf2f04a54c6dfcf52aef0d08c1a7451e534a60dfda93fed195d85ddbd0eff85b
