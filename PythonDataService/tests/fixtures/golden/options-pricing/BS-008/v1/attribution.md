# BS-008 — the Strategy Builder's browser Black-Scholes agrees with the Python canonical

## Source

Reference: `PythonDataService/app/services/bs_greeks.py` (closed-form Black-Scholes-Merton price and Greeks, Hull 10e §15.8 and §19) and `PythonDataService/app/services/strategy_engine.py` (`compute_payoff_curve`, `compute_current_curve`, `compute_greek_curves`: a strategy's expiry payoff, current-time P&L with its what-if shifts, and aggregate Greeks). Both are the canonical implementations (ADR 0068). The generator imports them; it carries no formula of its own.

Subject under test: `Frontend/src/app/utils/black-scholes.ts` and `strategy-payoff.ts`, the browser copy ADR 0068 names as exception 1 (latency: the builder re-prices a 1,200-point grid on every leg edit).

## Evidence layers

1. Market input: synthetic. A 360-case single-option grid and five hand-picked strategies.
2. Methodology: the closed-form formulas above. The canonical's own equivalence to independent implementations is pinned by BS-001..BS-007 (py_vollib) and `tests/services/test_bs_cross_engine_parity.py` (QuantLib, `atol=1e-10`).
3. Oracle: `reference_kind=cross_engine`. The browser copy is checked against the Python canonical. `tests/fixtures/test_bs_browser_parity_fixture.py` re-evaluates the canonical on every row and requires this file to equal it, so the expected values cannot drift from the canonical.

## Cases

- `cases`: spot {80, 100, 120} x strike 100 x ttm {7, 30, 90, 180, 365} days x vol {0.10, 0.20, 0.40} x rate {0, 0.05} x dividend {0, 0.02} x call/put = 360. Each has the price; the 180 with dividend 0 also carry delta, gamma, theta, vega and rho (the browser copy has no dividend input; the page and the server both use q = 0).
- `strategies`: a 95/105 bull call spread, a 95 call with no IV (priced at intrinsic by the server, #3221), a two-contract iron condor, a ratio call spread whose short leg has no IV, and a straddle under 10% vol (the IV-10% what-if floors its IVs at zero, #3221). Each is evaluated at the server's curve grid for the expiry payoff, the current-time P&L unshifted and under the page's three what-ifs (T+5d, IV+10%, IV-10%), and the aggregate delta, gamma, theta and vega.
- Breakevens, max profit and max loss are pinned by `Frontend/src/testing/payoff-parity/cases.json` (#3186), not repeated here.

## Tolerance

Held per quantity, in `output.json`'s `tolerance` block, with the reason for each. The browser copy's error floor is its Abramowitz & Stegun 7.1.26 normal CDF (`|error| < 1.5e-7`); the canonical's `scipy.stats.norm.cdf` is exact to machine precision. Each `atol` sits between the largest difference measured over this fixture and the most the CDF's error can reach through that quantity: price `2e-5`, delta and rho `1e-7`, theta `2e-9`, gamma and vega `1e-12` (no CDF term). The strategy figures also carry the rounding of what the canonical returns (4 dp for payoff and P&L, 6 dp for aggregate Greeks), so P&L is held to one 4 dp step and strategy gamma, theta and vega to one 6 dp step.

## Timestamps

None. Time left is `ttm_days / 365` years, as `time_to_expiry_years` measures it.

## Regeneration

Generated 2026-10-14.

`cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m scripts.fixture_generators.bs_browser_parity`

The Frontend copy `Frontend/src/testing/bs-parity/grid.json` is `output.json` byte for byte.
