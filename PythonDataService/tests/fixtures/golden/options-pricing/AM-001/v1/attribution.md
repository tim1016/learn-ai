# AM-001 — American option price and delta, no dividend

## Source

QuantLib 1.42.1, `BinomialLRVanillaEngine` (a Leisen-Reimer tree; Leisen & Reimer, "Binomial models for option valuation - examining and improving convergence", Applied Mathematical Finance 3(4), 1996), pricing an `AmericanExercise` `VanillaOption` on a `BlackScholesMertonProcess` with a flat rate, a flat volatility and no dividend yield. Day count Actual/365 (Fixed). Generated 2026-10-06.

## What is in it

400 cases at a price of 100: calls and puts; strikes 4 and 1 standard deviations of the price at expiry below it, at it, and 1 and 4 above; 30 days, 7 days, 1 day, 2 hours and 15 minutes to expiry; volatilities 0.15 and 0.45; rates 0.045 and 0.01; trees of 101 and 401 steps. `input.json` holds each case's inputs and what QuantLib was given; `output.json` holds QuantLib's `NPV()` and `delta()` at full precision.

## Assumptions

- QuantLib counts time in whole days. The 2-hour and 15-minute cases are the same problem restated over one day: with `f` the fraction of the day, QuantLib is given the rate `r·f` and the volatility `σ·√f`. That leaves `r·T` and `σ²·T` unchanged, and with them the price and the delta. `quantlib.days` and `quantlib.fraction_of_span` record it per case.
- A year is 365 days.
- Delta is QuantLib's: read from the tree's first step, not from a bump of the price.
- QuantLib 1.42.1's binomial engines lose the payoff at expiry when the step, multiplied back by the number of steps, rounds to less than the time to expiry: at 45 days and 51 steps its American price is under its European one. None of the spans and step counts here does that, and the generator refuses one that would.

## Independent numerical oracle

`reference_kind=external_reference`. The generator calls QuantLib only and never imports the canonical implementation.

## Tolerance

`atol=1e-9, rtol=0`: the repository's strict default. The canonical tree is the same construction, so the two differ by rounding only; the largest gap measured on generation was 4.4e-10 in price and 1.3e-11 in delta.

The cases a day or less from expiry are the sensitive ones. The tree takes `1 - exp(-x)` of a very small `x` there, as QuantLib does, so the last bit of `exp` moves their price by up to 2e-9 at one day, 8e-9 at two hours and 2e-8 at fifteen minutes. If only those cases fail on another platform, that is the cause. `expm1` is not the cure: it would stop matching QuantLib's own rounding.

## Regeneration

`cd PythonDataService && .venv/bin/python -m scripts.fixture_generators.american_options` (writes AM-001, AM-002 and AM-003). It needs QuantLib 1.42.1 (`.venv/bin/pip install QuantLib==1.42.1`): the service's own requirement allows newer releases, and the generator refuses to write these fixtures from any other.
