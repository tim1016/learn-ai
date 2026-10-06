# AM-002 — American option price and delta, with cash dividends

## Source

QuantLib 1.42.1, `FdBlackScholesVanillaEngine` with `CashDividendModel.Escrowed`, the Douglas scheme, 2,000 time steps and 2,000 price steps, pricing an `AmericanExercise` `VanillaOption` with a `DividendVector` of cash dividends. Flat rate 0.045, flat volatility, no dividend yield, Actual/365 (Fixed). Generated 2026-10-06.

The escrowed model is the one in Hull, Options, Futures, and Other Derivatives, "Binomial model for a dividend-paying stock: known dollar dividend": the price less the present value of the dividends still to come follows the lognormal process, and an option is exercised against that plus the dividends not yet paid.

## What is in it

68 cases at a price of 100, calls and puts, volatilities 0.2 and 0.4:

- 30 days with $1.75 going ex on day 12, strikes 80 to 120;
- 7 days with $0.60 going ex on day 3, strikes 90 to 110;
- 90 days with $0.90 going ex on day 20 and again on day 80, strikes 80 to 120.

## Assumptions

- A dividend's time is the whole number of days to its ex-date, over 365.
- This is a second engine for the same model, not the same algorithm. QuantLib's binomial engines take no cash dividends, so the tree cannot be compared with a tree here.

## Independent numerical oracle

`reference_kind=external_reference`. The generator calls QuantLib only and never imports the canonical implementation.

## Tolerance

Price `atol=1e-3`, delta `atol=2e-4`, `rtol=0`, with the canonical tree run at 2,001 steps. This is looser than the strict default, and the reason is discretization: two different numerical schemes agree only as closely as both have converged. Largest gap over the 68 cases as the tree's steps rise:

| Tree steps | Price | Delta |
|---|---|---|
| 401 | 3.2e-3 | 3.8e-4 |
| 2,001 | 5.9e-4 | 7.2e-5 |
| 4,001 | 1.8e-4 | 3.9e-5 |

The gap falls roughly in proportion to the steps. It is not a difference of model: under QuantLib's other cash-dividend model (`Spot`) the same cases move by up to 2.5e-2 in price and 3e-3 in delta. A delta rule's own tolerance is 0.05, so the gap at the default 401 steps is under one percent of it.

The manifest's tolerance records the delta figure; the test file states both.

## Regeneration

`cd PythonDataService && .venv/bin/python -m scripts.fixture_generators.american_options`
