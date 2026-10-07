# AM-004: American option daily time decay, no dividend

## Source

QuantLib 1.42.1, `BinomialLRVanillaEngine` (a Leisen-Reimer tree; Leisen & Reimer, "Binomial models for option valuation - examining and improving convergence", Applied Mathematical Finance 3(4), 1996), pricing the same `AmericanExercise` `VanillaOption` twice: with the time to expiry it has now, and with 24 hours less. The decay is the second price less the first. `BlackScholesMertonProcess` with a flat rate, a flat volatility and no dividend yield. Day count Actual/365 (Fixed). Generated 2026-10-07.

## What is in it

400 cases at a price of 100: calls and puts; 30 days, 7 days, 2 days, a day and 2 hours, and a day and 15 minutes to expiry; volatilities 0.15 and 0.45; rates 0.045 and 0.01; trees of 101 and 401 steps; strikes 4 and 1 standard deviations below the price, at it, and 1 and 4 above. The standard deviation is that of the price at the later price's expiry, the nearer of the two, so a strike is no more than four away at either time.

`input.json` holds each case's inputs and what QuantLib was given for each of the two prices. `output.json` holds QuantLib's `NPV()` now, its `NPV()` a day later, and the second less the first, at full precision.

## Assumptions

- The decay is a difference of two prices 24 hours apart, with the stock price, the volatility and the rate held. It is negative for a contract that loses value with time.
- It is not QuantLib's `theta()`. This engine works its theta out from the price, the delta and the gamma through the Black-Scholes equation, as a yearly rate. Over 365 that is not what a day takes: for a put at the money with two days left, at a rate of 0.045 and a volatility of 0.2, QuantLib's theta over 365 is -0.142 and its price a day later is 0.168 lower.
- QuantLib counts time in whole days. A span that is not a whole number of days is the same problem restated over the next whole number up: with `f` the fraction of that span the contract really has, QuantLib is given the rate `r·f` and the volatility `σ·√f`. That leaves `r·T` and `σ²·T` unchanged, and with them the price. A day and 2 hours, and a day and 15 minutes, are restated over two days for the price now and over one day for the price a day later. `quantlib.now` and `quantlib.a_day_later` record the days and the fraction per case.
- A year is 365 days and a day is 86,400,000 milliseconds. `years_to_expiry` is the span in milliseconds over the milliseconds in a year.
- The canonical function takes a day off `years_to_expiry` as a float. QuantLib is given the later span itself. The two later times can differ in their last bits; the price moves by under 4e-12 for it.
- QuantLib 1.42.1's binomial engines lose the payoff at expiry when the step, multiplied back by the number of steps, rounds to less than the time to expiry. None of the spans and step counts here does that, and the generator refuses one that would.

## What is not in it

- A contract with less than a day and a minute left. Its later price is what exercising pays, which no engine is asked for. `tests/options/test_american_daily_decay.py` covers it.
- A dividend. AM-005 holds the decay with cash dividends, from a second engine and at a looser tolerance.
- A later price under fifteen minutes from expiry. Measured on generation and not stored: with a day and one minute left the largest gap in the decay is 1.7e-9, over the strict default. That is the rounding described under Tolerance, which grows as the time left shrinks. AM-001 stops at fifteen minutes for the same reason.

## Independent numerical oracle

`reference_kind=external_reference`. The generator calls QuantLib only and never imports the canonical implementation.

## Tolerance

`atol=1e-9, rtol=0`: the repository's strict default, on the decay and on each of the two prices. Each price is the same construction as QuantLib's, so each differs from it by rounding only. A difference of two such prices could be out by the sum of the two gaps. Measured, it is not over the default. Largest gap on generation:

| Time to expiry now | Decay | Price now | Price a day later |
|---|---|---|---|
| 30 days | 8.9e-11 | 6.2e-11 | 4.5e-11 |
| 7 days | 3.1e-11 | 2.7e-11 | 2.2e-11 |
| 2 days | 2.2e-11 | 2.2e-11 | 1.9e-11 |
| A day and 2 hours | 7.6e-11 | 4.5e-11 | 1.1e-10 |
| A day and 15 minutes | 4.7e-10 | 3.0e-11 | 4.4e-10 |

The cases whose later price is a day or less from expiry are the sensitive ones. The tree takes `1 - exp(-x)` of a very small `x` there, as QuantLib does, so the last bit of `exp` moves the price by up to 2e-9 at one day, 8e-9 at two hours and 2e-8 at fifteen minutes. If only those cases fail on another platform, that is the cause. `expm1` is not the cure: it would stop matching QuantLib's own rounding.

## Regeneration

`cd PythonDataService && .venv/bin/python -m scripts.fixture_generators.american_options` (writes AM-001 to AM-005). It needs QuantLib 1.42.1 (`.venv/bin/pip install QuantLib==1.42.1`): the service's own requirement allows newer releases, and the generator refuses to write these fixtures from any other.
