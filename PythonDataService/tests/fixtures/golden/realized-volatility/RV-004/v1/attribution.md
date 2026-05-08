# RV-004 — Model-Free Variance Replication (CBOE Formula)

## Evidence Layers

**Layer 1 — Market input provenance:** Synthetic. 3 test cases with BSM-priced
option chains (5–7 strikes, zero spread). One strike pct set each with varying
T, rate, and sigma. Chains designed to exercise K<K0 (put), K>K0 (call), and
K=K0 (average) logic.

**Layer 2 — Methodology provenance:** CBOE VIX 2019 whitepaper "VIX Index
Calculation: Step-by-Step." The underlying mathematics: Demeterfi, Derman,
Kamal, Zou (1999) "More Than You Ever Wanted to Know About Volatility Swaps."
Canonical: `app/volatility/vix_replication.py::replicate_expiry_variance`.

**Layer 3 — Independent numerical oracle:** Direct CBOE formula in Python
without calling `replicate_expiry_variance`. Same algorithm, independent code.
Oracle stores `sigma_sq_T` (annualized variance) as the output.

## Formula

```text
F   = K* + e^(rT) · (C(K*) − P(K*))     (put-call parity forward)
K0  = max{K : K ≤ F}
Q   = put_mid  for K < K0
    = call_mid for K > K0
    = (call_mid + put_mid)/2  at K0

ΔK  = (K_{i+1} − K_{i-1})/2  interior; forward/back diff at edges

σ²(T) = (2/T)·Σᵢ(ΔKᵢ/Kᵢ²)·e^(rT)·Q(Kᵢ) − (1/T)·(F/K₀ − 1)²
```

## Canonical Implementation

`PythonDataService/app/volatility/vix_replication.py::replicate_expiry_variance`

## Tolerance

atol=1e-6, rtol=0.0. Two distinct error sources apply here:

1. **Oracle vs canonical agreement**: oracle and canonical implement the same
   discrete CBOE sum using the same `math.exp`/`math.log` calls; float64
   rounding only, keeping oracle-vs-canonical disagreement < 1e-15.

2. **Discrete formula vs continuous BSM variance**: the CBOE sum approximates
   a continuous integral; for chains with few strikes (5–7 here) the
   discretization error between σ²(T)_discrete and σ²(T)_continuous can reach
   ~1e-4. This is a model-accuracy gap, not an oracle-vs-canonical gap.

The `atol=1e-6` floor is a deliberate safety margin above float rounding
noise (not above the discretization error). A future maintainer should not
loosen atol to ~1e-4; that would mask oracle-canonical regressions.

## Regeneration

```bash
python scripts/generate_fixtures.py --id RV-004 --force \
  --justification "<reason>"
```

## Generation Metadata

Generated: 2026-05-08
Oracle: literature_formula — CBOE VIX 2019 whitepaper formula in pure Python
Script: scripts/fixture_generators/volatility.py
Justification: Phase 2 initial generation
