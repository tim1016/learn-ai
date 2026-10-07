# AM-005: American option daily time decay from a second engine, with cash dividends

## Source

QuantLib 1.42.1, `FdBlackScholesVanillaEngine` with `CashDividendModel.Escrowed`, the Douglas scheme, 2,000 time steps and 2,000 price steps, pricing the same `AmericanExercise` `VanillaOption` twice: as it stands now, and 24 hours on. The decay is the second price less the first. Flat rate 0.045, flat volatility, no dividend yield, Actual/365 (Fixed). Generated 2026-10-07.

The engine and the dividend model are AM-002's. That fixture's attribution describes the escrowed model.

## What is in it

84 cases at a price of 100, calls and puts, volatilities 0.2 and 0.4:

- 30 days with no dividend, strikes 90 to 110, so the same engine also checks the decay where nothing is paid;
- 30 days with $1.75 going ex on day 12, strikes 90 to 110;
- 7 days with $0.60 going ex on day 3, strikes 95 to 105;
- 30 days with $1.75 going ex on day 1, which is inside the day the decay is taken over, strikes 90 to 110;
- 60 days with $0.90 going ex on day 1 and again on day 40, strikes 90, 100 and 110.

`input.json` holds each case's inputs and what QuantLib was given for the later price. `output.json` holds QuantLib's `NPV()` now, its `NPV()` a day later, the second less the first, and the cash that went ex inside the day.

## Assumptions

- The decay is a difference of two prices 24 hours apart, with the stock price, the volatility and the rate held and every dividend kept on its date. A day later each dividend still ahead is a day nearer.
- **A stock that goes ex inside the day is priced a day later at its price less that dividend, in full, with the dividend gone from the schedule.** This is the definition under test, not a fact about QuantLib: the generator gives QuantLib the lower stock price for the later price. Holding the quoted price across an ex-date would say the stock had risen by the dividend.
- A dividend going ex exactly a day from now has gone ex a day later: the generator takes it off the stock price and leaves it out of the later schedule. QuantLib is not asked what it does with a dividend on the valuation date.
- A dividend's time is the whole number of days to its ex-date, over 365. Every time here is a whole number of days, so nothing is restated.
- This is a second engine for the same model, not the same algorithm. QuantLib's binomial engines take no cash dividends, so the tree cannot be compared with a tree here.
- The step counts the canonical tree is run at, 401 and 2,003, are prime, where AM-002 uses 2,001. An ex-date that falls exactly on a tree step is counted before or after that step by the last bit of the two times, and the price moves by up to a step's error for it. At 2,001 steps, 69 to the day over 29 days, every whole day of a 29-day span is on a step. A prime count greater than the days to expiry has no whole day on a step, and the generator refuses a case that would put one there.

## Independent numerical oracle

`reference_kind=external_reference`. The generator calls QuantLib only and never imports the canonical implementation.

## Tolerance

**This is looser than the repository's strict default, and it is an accepted divergence, not rounding.** Decay `atol=2e-3`, `rtol=0`, with the canonical tree run at 2,003 steps. A second test holds the default 401 steps to `atol=1e-2`. Each is twice what AM-002 holds one price to, since the decay is a difference of two prices. The two prices the decay is worked from are held to the same figures.

The gap is the tree's own discretization error. The reference is good to about 2e-5 in the decay: at 4,000 by 4,000 its decay moves by under 1.7e-5 from the stored 2,000 by 2,000, and each price by under 2.9e-5. Largest gap in the decay as the tree's steps rise:

| Tree steps | All 84 cases | No dividend | Ex on day 12 | 7 days, ex on day 3 | Ex inside the day | 60 days, two dividends |
|---|---|---|---|---|---|---|
| 401 | 5.0e-3 | 1.3e-4 | 8.0e-4 | 6.7e-4 | 5.0e-3 | 1.3e-3 |
| 1,009 | 7.9e-4 | 2.3e-5 | 4.4e-4 | 7.1e-5 | 7.9e-4 | 2.4e-4 |
| 2,003 | 9.4e-4 | 2.0e-6 | 4.7e-4 | 4.5e-5 | 9.4e-4 | 2.6e-4 |
| 4,001 | 2.2e-4 | 1.7e-6 | 1.8e-4 | 1.8e-5 | 2.2e-4 | 1.5e-4 |

- The gap falls unevenly as the steps rise, as AM-002's price gap does. Why it is uneven was not isolated here either.
- The 5.0e-3 at 401 steps is one case: the call at strike 90, at volatility 0.4, whose stock goes ex $1.75 a day out. Its price now is 4.997e-3 under the reference, which is at the edge of the 5e-3 AM-002 holds the price to at 401 steps. AM-002's own cases reach 3.2e-3; it has no dividend a day out. The cause was not isolated. The tree can exercise only on a step, and the last one before the ex-date falls up to a step early, which is the likely one.
- Over the other 83 cases the largest gap at 401 steps is 1.3e-3. Half of all the cases are within 1e-5 at 401 steps and within 2e-6 at 2,003.
- The decays here run from 1.1e-3 to 1.32 a share in size. An absolute tolerance of 2e-3 barely tests the smallest.
- With no dividend the two engines' decays agree to 2.0e-6 at 2,003 steps. AM-004 holds that path to 1e-9 against the same tree.

**What the tolerance tells apart, and what it does not.**

- Holding the quoted price across the ex-date moves the 32 cases that go ex inside the day by 6.4e-2 to 1.75. That is 6 to 175 times the looser tolerance.
- Leaving the dividends still ahead where they were, not a day nearer, moves 25 of the 44 cases that have one by over 2e-3, and by up to 6.2e-2.
- Taking off the dividend's present value in place of the cash in full moves the later price by at most 2.2e-4. This fixture does not tell those two apart. `tests/options/test_american_daily_decay.py` pins the choice directly, against the pricer's own price at the lower stock price.

The manifest holds one tolerance figure for a fixture. Here it is the looser of the two, the default 401 steps (`atol=1e-2`), so it is never tighter than a test; the test file states each.

## Regeneration

`cd PythonDataService && .venv/bin/python -m scripts.fixture_generators.american_options` (writes AM-001 to AM-005). It needs QuantLib 1.42.1 (`.venv/bin/pip install QuantLib==1.42.1`): the service's own requirement allows newer releases, and the generator refuses to write these fixtures from any other.
