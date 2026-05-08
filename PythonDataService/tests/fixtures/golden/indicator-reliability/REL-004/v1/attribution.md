# REL-004 — IC Decay Curve (EMA-10 Signal)

Generated: 2026-05-08
Oracle: literature_formula — direct Spearman rank-correlation per horizon,
  masking forward returns that cross a session boundary (mask_overnight=True)
Canonical: PythonDataService/app/research/indicator_reliability.py::compute_ic_decay_curve

## Formula

```text
For each horizon h in 1..5:
  fwd_return[i] = log(close[i+h] / close[i])  if same calendar day else NaN
  IC[h] = mean over days d of Spearman(ema[day_d], fwd_return[h][day_d])
```

EMA formula: k = 2/(1+10); s_t = k×close_t + (1-k)×s_{t-1}, s_0 = close_0.
Note: standard exponential smoothing (k=2/(1+period)), NOT Wilder smoothing (k=1/period).
RSI uses Wilder; EMA uses standard. See app/engine/indicators/ema.py:11.

## Input

5 trading days × 20 bars each = 100 bars.
Close: GBM(S₀=100, σ=0.005, seed=7). Timestamps: 2024-01-02..2024-01-08, 15-min cadence.

## Oracle computed values (mean IC per horizon)

  horizon=1: IC=-0.204561404
  horizon=2: IC=-0.280495356
  horizon=3: IC=-0.320098039
  horizon=4: IC=-0.335294118
  horizon=5: IC=-0.412857143

## Tolerance

atol=1e-9, rtol=0.0

## SHA-256

input.arrow:  a03cf8af86dbc3673e0486273504425f379f6496f6d73d8f654272cdfed86b2e
output.arrow: d88d46c84656d6d291ac6543ee0fda013b175bad121ce3ec521e55d8edb7cd94
