# IV-003 — IV30 Constant-Maturity (Variance-Time Interpolation)

## Evidence Layers

**Layer 1 — Market input provenance:** Synthetic. 3 test cases with 2-expiry
BSM-priced option chains (5 strikes each, zero spread, calls for K>F / puts
for K<F / average at K=K0). Chains generated from known BSM sigma values.

**Layer 2 — Methodology provenance:** CBOE VIX 2019 whitepaper "VIX Index
Calculation: Step-by-Step" for the per-expiry variance integral; variance-time
interpolation as in Demeterfi, Derman, Kamal, Zou (1999).
Canonical: `app/volatility/vix_replication.py::vix_style_iv30`.

**Layer 3 — Independent numerical oracle:** CBOE formula applied directly in
Python (same algorithm, independently coded). Variance-time interpolation then
applied directly. No call to `vix_style_iv30`.

## Formula

```text
σ²(T) = (2/T)·Σᵢ(ΔKᵢ/Kᵢ²)·e^(rT)·Q(Kᵢ) − (1/T)·(F/K₀ − 1)²

IV30:
  w = (T2 − T_target) / (T2 − T1)
  σ²_target · T_target = w·σ²_T1·T1 + (1−w)·σ²_T2·T2
  iv30 = √(σ²_target)
```

## Test Protocol

1. Reconstruct two `OptionQuote` lists per row (call mid = Q for K>F, put mid = Q for K<F).
2. Call `vix_style_iv30(chain1, chain2, rate=rate, T1_calendar_days=T1, T2_calendar_days=T2)`.
3. Assert |result − iv30_oracle| <= 1e-6.

## Canonical Implementation

`PythonDataService/app/volatility/vix_replication.py::vix_style_iv30`

## Tolerance

atol=1e-6, rtol=0.0. Rationale: both canonical and oracle implement the same
CBOE formula; the 1e-6 floor exceeds the ~1e-12 observed float64 discrepancy
between a pure-Python and numpy implementation of the same arithmetic, and
accounts for any platform variance in scipy's exp/log transcendentals.

## Regeneration

```bash
python scripts/generate_fixtures.py --id IV-003 --force \
  --justification "<reason>"
```

## Generation Metadata

Generated: 2026-05-08
Oracle: hand_computed — CBOE per-expiry formula + variance-time interpolation in Python
Script: scripts/fixture_generators/volatility.py
(initial generation)
