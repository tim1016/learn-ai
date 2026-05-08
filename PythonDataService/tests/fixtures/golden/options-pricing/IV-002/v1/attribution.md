# IV-002 — SVI Total Variance Surface

## Evidence Layers

**Layer 1 — Market input provenance:** Synthetic. 3 SVI parameter sets × 7
log-moneyness values. Smile constructed from the oracle formula so that
`fit_svi` must recover the exact SVI parameters on a noiseless input.

**Layer 2 — Methodology provenance:** SVI raw parameterisation from Gatheral,
J. (2004). "A parsimonious arbitrage-free implied volatility parameterization
with application to the valuation of volatility derivatives." Presentation at
the Global Derivatives & Risk Management 2004.
Canonical: `app/volatility/fitting.py::fit_svi`.

**Layer 3 — Independent numerical oracle:** Direct formula evaluation:
`w(k) = a + b·(ρ·(k−m) + √((k−m)²+σ²))` without calling `fit_svi`.

## Formula

```text
w(k) = a + b · (ρ · (k−m) + √((k−m)² + σ²))

k     = log(K / F)     log-moneyness
w(k)  = σ²_IV · T     total implied variance
```

## SVI Parameter Sets

| set | a     | b    | ρ     | m     | σ_svi |
|-----|-------|------|-------|-------|-------|
| 0   | 0.020 | 0.30 | -0.70 | 0.00  | 0.20  |
| 1   | 0.040 | 0.50 | -0.40 | 0.05  | 0.15  |
| 2   | 0.010 | 0.20 | -0.50 | -0.05 | 0.25  |

k values: [-0.3, -0.15, -0.05, 0.0, 0.05, 0.15, 0.3]
forward = 100.0, ttm = 0.5

## Test Protocol

1. Group fixture rows by (a, b, rho, m, sigma_svi) — 3 groups of 7.
2. Build SmileSlice: strikes = forward * exp(k); ivs = sqrt(w_oracle / ttm).
3. Call `fit_svi(smile)`.
4. For each k, compute w_fit = result.volatility(strike)² * ttm.
5. Assert |w_fit - w_oracle| <= 1e-6.

## Canonical Implementation

`PythonDataService/app/volatility/fitting.py::fit_svi`

## Tolerance

atol=1e-6, rtol=0.0. Rationale: scipy least_squares converges to ~1e-7
residuals on noiseless SVI input; 1e-6 provides one order of headroom while
remaining tight enough to detect any solver regression.

## Regeneration

```bash
python scripts/generate_fixtures.py --id IV-002 --force \
  --justification "<reason>"
```

## Generation Metadata

Generated: 2026-05-08
Oracle: literature_formula — Gatheral (2004) SVI w(k) formula applied directly
Script: scripts/fixture_generators/volatility.py
Justification: Phase 2 initial generation
