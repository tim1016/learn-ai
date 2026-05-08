# RP-004 — Signal Z-score (Train-period Standardization)

Generated: 2026-05-08
Oracle: hand_computed — direct formula: z = (x - mu_train) / sigma_train
Canonical: PythonDataService/app/research/signal/standardize.py::compute_train_zscore

## Formula

mu_train    = mean(feature[train_mask])
sigma_train = std(feature[train_mask], ddof=1)    (pandas default ddof=1; NumPy default is ddof=0 — explicit ddof=1 required)
z           = (feature - mu_train) / sigma_train
z_flipped   = -z   (when flip_sign=True, for negative-IC signals)

## Input

50 bars. Feature: N(5, 2) seed=55. Train: first 35 bars.

## Oracle computed parameters

mu_train:    5.415703357
sigma_train: 2.325699978

## Tolerance

atol=1e-9, rtol=0.0

## Justification

Initial generation.

## SHA-256

input.arrow:  62a703e6a8dcc50cb24359537d713bc3aca7e82b1d6bed81472a2be487fc3e1c
output.arrow: 336ebc4d3c22ba473d3f061a1c29cbd10b2c1edf5a3774500996a2dde1ba5238
