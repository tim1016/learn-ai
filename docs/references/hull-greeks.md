# Hull §19 — Greek Letters (reference extract)

**Source**: Hull, John C. *Options, Futures, and Other Derivatives.* 10th edition. Pearson, 2018. Chapter 19 "The Greek Letters."

## Definitions used in learn-ai

| Greek | Symbol | Definition (Hull §19) | Unit (learn-ai canonical) |
|---|---|---|---|
| Delta | Δ | ∂C/∂S — sensitivity of option price to underlying spot | Per 1.0 underlying unit |
| Gamma | Γ | ∂²C/∂S² — rate of change of delta with respect to spot | Per 1.0 underlying unit per 1.0 spot move |
| Theta | Θ | ∂C/∂t — time decay (negative for long options) | Per **calendar day** (annual / 365) |
| Vega | ν | ∂C/∂σ — sensitivity to volatility | Per **1% IV move** (raw / 100) |
| Rho | ρ | ∂C/∂r — sensitivity to risk-free rate | Per **1% rate move** (raw / 100) |

The unit conventions on `BSGreeks` and `GreeksResult` (in `bs_greeks.py` and `quantlib_pricer.py`) match these — the modules apply the per-day / per-1% scaling at the boundary so downstream consumers see identical units regardless of which pricing engine produced the result.

## Closed-form formulas (Hull §15.8 + §19)

For European call C with spot S, strike K, time-to-expiry T (years), risk-free rate r, continuous dividend yield q, volatility σ:

```
d1 = [ln(S/K) + (r - q + σ²/2)·T] / (σ·√T)
d2 = d1 - σ·√T

C = S·e^(-q·T)·N(d1) - K·e^(-r·T)·N(d2)
P = K·e^(-r·T)·N(-d2) - S·e^(-q·T)·N(-d1)

Δ_call = e^(-q·T)·N(d1)
Δ_put  = e^(-q·T)·[N(d1) - 1]
Γ      = e^(-q·T)·N'(d1) / (S·σ·√T)
ν      = S·e^(-q·T)·√T·N'(d1)
Θ_call = -[S·e^(-q·T)·N'(d1)·σ / (2·√T)] - r·K·e^(-r·T)·N(d2) + q·S·e^(-q·T)·N(d1)
Θ_put  = -[S·e^(-q·T)·N'(d1)·σ / (2·√T)] + r·K·e^(-r·T)·N(-d2) - q·S·e^(-q·T)·N(-d1)
ρ_call = K·T·e^(-r·T)·N(d2)
ρ_put  = -K·T·e^(-r·T)·N(-d2)
```

where N(·) is the standard normal CDF and N'(·) is the standard normal PDF.

## Where the formulas land in the codebase

- **Closed-form variant**: `PythonDataService/app/services/bs_greeks.py` — implements all of the above with Decimal/numpy precision; cross-engine parity at atol=1e-10.
- **QuantLib variant**: `PythonDataService/app/services/quantlib_pricer.py` — same math via QuantLib's compiled C++ analytic_bs engine.
- **Cross-engine parity test**: `PythonDataService/tests/services/test_bs_cross_engine_parity.py` — 360-case grid (Phase 1.4 shipped 2026-04-26 + precision-leak fix 69d2bfe).
- **Strategy aggregation**: `PythonDataService/app/services/strategy_engine.py::AnalyzeOptionsStrategy` composes per-leg Greeks into strategy-level Greek curves.
- **Portfolio scenario**: `PythonDataService/app/services/portfolio_scenario.py` recomputes Greeks at every scenario point (no shock-propagation from stored entry Greeks).
- **Render-helper-only TS path**: `Frontend/src/app/utils/black-scholes.ts` — same math via Abramowitz & Stegun normal CDF approximation; legacy-ok with two intentional callers (pricing-lab, strategy-builder); not a math authority.

## Registry rows that cite this reference

- Black-Scholes price (European call/put)
- Greeks — Delta, Gamma, Theta, Vega, Rho
- Portfolio scenario / what-if (theoretical option value across spot, time, IV grid)
- Portfolio live Greeks (current-time delta/gamma/theta/vega per position)
- Options strategy analysis (payoff, POP, current-time PnL curve, Greek curves, per-leg diagnostics) — also cites Hull §11–12 for payoff diagrams

## Notes on assumptions

The canonical Hull §19 formulas assume:
- European exercise (no early exercise — for American options, an exercise boundary correction or an alternative numerical scheme like Cox-Ross-Rubinstein binomial tree is needed).
- Lognormal underlying (constant volatility — for surfaces with skew, the IV-per-strike interpolation in `app/volatility/solver.py` and `app/volatility/fitting.py` provides per-contract σ).
- Continuous dividend yield q (discrete dividends require an alternative treatment — escrowed dividend or cash-flow adjustment; not currently implemented in `bs_greeks.py`).

If any of these assumptions break for a future use case, the canonical implementation file's docstring must call it out, and the registry row should reflect it.
