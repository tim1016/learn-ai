# Math Rigor — Options System Upgrade Plan

Two items of the original ten-upgrade plan stay here because code
provenance blocks cite them by name. Both have shipped:
`PythonDataService/app/research/options/iv_builder.py::_interpolate_iv`
interpolates in total variance, and `iv_builder.py` reads its rate from
`PythonDataService/app/services/fred_service.py`. The rest of the plan is in
git history.

## Upgrade 1: Variance Interpolation (not Volatility)

### Problem

The original interpolation in `iv_builder.py` was linear in σ:

```
σ_30 = w_low · σ_low + w_high · σ_high
```

This is first-order correct but introduces **downward bias** when the term structure has curvature. By Jensen's inequality, for a convex function (√x):

```
√(w₁x₁ + w₂x₂) ≥ w₁√x₁ + w₂√x₂
```

So linear-in-vol systematically underestimates the true 30-day IV.

### Correct Form

Interpolate in **total variance** (σ²T), then extract σ:

```
σ²(T_30) · T_30 = w_low · σ²_low · T_low + w_high · σ²_high · T_high

where:
  w_low  = (T_high - T_30) / (T_high - T_low)
  w_high = (T_30 - T_low)  / (T_high - T_low)
  T_x    = DTE_x / 365

σ_30 = √[ (w_low · σ²_low · T_low + w_high · σ²_high · T_high) / T_30 ]
```

This is **variance-time** interpolation — the industry standard for constructing constant-maturity vol surfaces.

## Upgrade 4: Dynamic Risk-Free Rate from FRED

### Problem

The original IV solver hardcoded `r = 0.043`. IV sensitivity to r:

```
∂σ/∂r ≈ −(∂C/∂r) / vega = K·T·e^(−rT)·N(d₂) / vega
```

For a 60-DTE ATM option: ~50bps rate error → ~0.3-0.5 vol point IV error. This is **systematic** — it biases the entire term structure in one direction.
