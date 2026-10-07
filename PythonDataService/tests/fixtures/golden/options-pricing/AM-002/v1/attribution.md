# AM-002 — American option price and delta from a second engine, with cash dividends

## Source

QuantLib 1.42.1, `FdBlackScholesVanillaEngine` with `CashDividendModel.Escrowed`, the Douglas scheme, 2,000 time steps and 2,000 price steps, pricing an `AmericanExercise` `VanillaOption` with a `DividendVector` of cash dividends. Flat rate 0.045, flat volatility, no dividend yield, Actual/365 (Fixed). Generated 2026-10-06.

The escrowed model is the one in Hull, Options, Futures, and Other Derivatives, "Binomial model for a dividend-paying stock: known dollar dividend": the price less the present value of the dividends still to come follows the lognormal process, and an option is exercised against that plus the dividends not yet paid.

## What is in it

96 cases at a price of 100, calls and puts, volatilities 0.2 and 0.4:

- 30 days with no dividend, strikes 80 to 120, so the same engine also checks the tree where nothing is paid;
- 30 days with $1.75 going ex on day 12, strikes 80 to 120;
- 7 days with $0.60 going ex on day 3, strikes 90 to 110;
- 90 days with $0.90 going ex on day 20 and again on day 80, strikes 80 to 120.

## Assumptions

- A dividend's time is the whole number of days to its ex-date, over 365.
- This is a second engine for the same model, not the same algorithm. QuantLib's binomial engines take no cash dividends, so the tree cannot be compared with a tree here.
- QuantLib counts a dividend on the expiry day as going ex before expiry. So does the canonical tree. None of these cases has one; `tests/options/test_american.py` pins it for every whole-day expiry in a year.

## Independent numerical oracle

`reference_kind=external_reference`. The generator calls QuantLib only and never imports the canonical implementation.

## Tolerance

**This is looser than the repository's strict default, and it is an accepted divergence, not rounding.** Price `atol=1e-3`, delta `atol=2e-4`, `rtol=0`, with the canonical tree run at 2,001 steps. A second test holds the default 401 steps to price `atol=5e-3`, delta `atol=1e-3`.

The gap is the tree's own discretization error. The reference is good to about 5e-5: at 4,000 by 4,000 its answers move by under 3e-5 in price and 3.4e-6 in delta from the stored 2,000 by 2,000, and on its 28 cases with no dividend it settles about 4e-5 from the tree. Largest gap over the 96 cases as the tree's steps rise:

| Tree steps | Price | Delta |
|---|---|---|
| 401 | 3.2e-3 | 4.2e-4 |
| 1,001 | 1.4e-3 | 1.5e-4 |
| 2,001 | 5.9e-4 | 7.2e-5 |
| 4,001 | 1.8e-4 | 3.9e-5 |
| 8,001 | 2.0e-4 | 2.0e-5 |
| 16,001 | 5.8e-5 | 1.2e-5 |

- The delta gap falls steadily. The price gap falls unevenly: 1.3e-4 at 6,001 steps, back up to 2.0e-4 at 8,001, 1.0e-4 at 12,001. Why it is uneven was not isolated. At 16,001 it has reached what the reference itself is good to.
- It is not a difference of model. Under QuantLib's other cash-dividend model (`Spot`) the dividend cases move by up to 8e-2 in price and 3e-3 in delta.
- Five cases are worth under 1e-3, four of them with a delta under 2e-4 (the far out-of-the-money ones at volatility 0.2). An absolute tolerance barely tests them.
- A delta rule's own tolerance is 0.05, so the gap at the default 401 steps is under one percent of it.

**Implied volatility.** A third test gives the solver each price here and asks for the volatility back, for the 52 cases within one standard deviation of the money: volatility `atol=5e-4`, delta `atol=1e-3`, at the default steps. Largest gaps measured: 3.0e-4 and 5.0e-4. The volatility gap is the price gap divided by vega, so farther from the money, where vega is small, it reaches 0.1 and those cases are left to the price test. Solving the dividend cases as if the stock paid nothing misses by 2e-2 or more.

The manifest's tolerance records the delta figure at 2,001 steps; the test file states all of them.

## Regeneration

`cd PythonDataService && .venv/bin/python -m scripts.fixture_generators.american_options`
