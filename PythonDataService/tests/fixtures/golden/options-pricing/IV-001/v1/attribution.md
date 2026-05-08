# IV-001 — Implied Volatility Solver Round-Trip

## Evidence Layers

**Layer 1 — Market input provenance:** Synthetic. 12-case grid of
(spot, strike, rate, ttm, dividend, sigma_known) spanning ITM/ATM/OTM,
short/long TTM, calls and puts, with and without dividend.
`market_price` = BSM formula applied to sigma_known — guaranteed solvable.

**Layer 2 — Methodology provenance:** Three-stage cascade in
`app/volatility/solver.py::implied_volatility` — Newton-Raphson →
QuantLib → scipy Brent. Hull (2022) §19.11; Brent (1973).

**Layer 3 — Independent numerical oracle:** `sigma_oracle` = `sigma_known`.
Market price was generated from that exact sigma; any convergent solver
must recover it to within floating-point noise. The oracle is the input
sigma itself — no external library needed.

## Formula

```text
market_price = BSM(spot, strike, ttm, rate, dividend, sigma_known, is_call)
oracle: sigma_oracle = sigma_known
test: |implied_volatility(market_price, ...).iv - sigma_oracle| <= 1e-6
```

## Cases

| row | spot  | strike | ttm  | rate | div  | sigma | call? |
|-----|-------|--------|------|------|------|-------|-------|
| 0   | 100.0 | 90.0   | 0.25 | 0.05 | 0.00 | 0.20  | T     |
| 1   | 100.0 | 100.0  | 0.25 | 0.05 | 0.00 | 0.20  | T     |
| 2   | 100.0 | 110.0  | 0.25 | 0.05 | 0.00 | 0.20  | T     |
| 3   | 100.0 | 90.0   | 1.00 | 0.05 | 0.00 | 0.25  | T     |
| 4   | 100.0 | 100.0  | 1.00 | 0.05 | 0.00 | 0.25  | T     |
| 5   | 100.0 | 110.0  | 1.00 | 0.05 | 0.00 | 0.25  | T     |
| 6   | 100.0 | 90.0   | 0.25 | 0.02 | 0.01 | 0.30  | T     |
| 7   | 100.0 | 100.0  | 0.25 | 0.02 | 0.01 | 0.30  | T     |
| 8   | 100.0 | 110.0  | 0.25 | 0.02 | 0.01 | 0.30  | T     |
| 9   | 100.0 | 90.0   | 1.00 | 0.02 | 0.01 | 0.35  | F     |
| 10  | 100.0 | 100.0  | 1.00 | 0.02 | 0.01 | 0.35  | F     |
| 11  | 100.0 | 110.0  | 1.00 | 0.02 | 0.01 | 0.35  | F     |

## Canonical Implementation

`PythonDataService/app/volatility/solver.py::implied_volatility`

## Tolerance

atol=1e-6, rtol=0.0. Rationale: the solver's internal convergence criterion is
1e-7 (Newton-Raphson) or 1e-10 (Brent); the 1e-6 floor provides one order of
headroom while remaining tight enough to catch any solver regression.

## Regeneration

```bash
python scripts/generate_fixtures.py --id IV-001 --force \
  --justification "<reason>"
```

## Generation Metadata

Generated: 2026-05-08
Oracle: hand_computed — BSM pricing formula; sigma_known is the round-trip answer
Script: scripts/fixture_generators/volatility.py
Justification: Phase 2 initial generation
