# AM-005: American option daily time decay from a second engine, with cash dividends

## Source

QuantLib 1.42.1, `FdBlackScholesVanillaEngine` with `CashDividendModel.Escrowed`, the Douglas scheme, 2,000 time steps and 2,000 price steps, pricing the same `AmericanExercise` `VanillaOption` twice: as it stands now, and 24 hours on. The decay is the second price less the first. Flat rate 0.045, flat volatility, no dividend yield, Actual/365 (Fixed). Generated 2026-10-07.

The engine and the dividend model are AM-002's. That fixture's attribution describes the escrowed model.

## What is in it

100 cases at a price of 100, calls and puts, volatilities 0.2 and 0.4, in six groups:

- 30 days with no dividend, strikes 90 to 110, so the same engine also checks the decay where nothing is paid;
- 30 days with $1.75 going ex on day 12, strikes 90 to 110;
- 7 days with $0.60 going ex on day 3, strikes 95 to 105;
- 30 days with $1.75 going ex on day 1, which is inside the day the decay is taken over, strikes 90 to 110;
- 60 days with $0.90 going ex on day 1 and again on day 40, strikes 90, 100 and 110;
- 60 days with $1.75 going ex on day 2, strikes 85 to 100. A day later that ex-date is one day ahead, which is where the canonical tree is coarsest at its default steps. The group is there so the fixture holds its own worst kind of case.

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

**This is looser than the repository's strict default, and it is an accepted divergence, not rounding.** The gap is the canonical tree's own discretization error against another method's answer. ADR 0076 decision 6 grants this tree that divergence with dividends. Every figure in this section is a gap measured on this fixture's 100 cases. None is a bound on the tree: see "Measured outside the fixture" below.

Two tests hold the fixture, each on the decay and on the two prices it is worked from, `rtol=0`:

- With the canonical tree at 2,003 steps, one figure for every case: `atol=2e-3`. That is twice the largest gap measured there (9.5e-4) and twice what AM-002 holds one price to at 2,001 steps.
- With the canonical tree at the default 401 steps, each group is held to about twice its own largest gap:

| Group | Largest gap in the decay | In the price now | In the price a day later | Held to (`atol`) |
|---|---|---|---|---|
| 30 days, no dividend | 1.3e-4 | 2.5e-4 | 2.4e-4 | 5e-4 |
| 30 days, ex on day 12 | 8.0e-4 | 8.1e-4 | 8.5e-4 | 2e-3 |
| 7 days, ex on day 3 | 6.7e-4 | 8.9e-4 | 3.8e-4 | 2e-3 |
| 60 days, ex on day 1 and day 40 | 1.3e-3 | 1.5e-3 | 1.3e-3 | 3e-3 |
| 30 days, ex on day 1 | 5.0e-3 | 5.0e-3 | 2.6e-4 | 1e-2 |
| 60 days, ex on day 2 | 8.1e-3 | 7.2e-4 | 8.0e-3 | 1e-2 |

The last two groups keep the looser figure because one of their two prices is a day before an ex-date: the price now when the stock goes ex on day 1, the price a day later when it goes ex on day 2. This is not a figure loosened to make a test pass. At 2,003 steps the same two groups are within 1e-3, so the gap is the tree's step error at 401 steps. The day-2 group is held to 1.2 times its measured gap, not twice: `1e-2` is the most this fixture has been held to, and adding its worst kind of case did not raise it.

The reference is good to about 2e-5 in the decay: at 4,000 by 4,000 its decay moves by under 1.7e-5 from the stored 2,000 by 2,000, and each price by under 3e-5. Largest gap in the decay as the tree's steps rise:

| Tree steps | All 100 cases | No dividend | Ex on day 12 | 7 days, ex on day 3 | 30 days, ex on day 1 | 60 days, ex on day 1 and day 40 | 60 days, ex on day 2 |
|---|---|---|---|---|---|---|---|
| 401 | 8.1e-3 | 1.3e-4 | 8.0e-4 | 6.7e-4 | 5.0e-3 | 1.3e-3 | 8.1e-3 |
| 1,009 | 8.9e-4 | 2.3e-5 | 4.4e-4 | 7.1e-5 | 7.9e-4 | 2.4e-4 | 8.9e-4 |
| 2,003 | 9.4e-4 | 2.0e-6 | 4.7e-4 | 4.5e-5 | 9.4e-4 | 2.6e-4 | 3.2e-4 |
| 4,001 | 2.2e-4 | 1.7e-6 | 1.8e-4 | 1.8e-5 | 2.2e-4 | 1.5e-4 | 1.8e-4 |

- The gap falls unevenly as the steps rise, as AM-002's price gap does. Why it is uneven was not isolated here either.
- The two largest gaps at 401 steps are calls in the money at volatility 0.4 with one price a day before the ex-date. The call at strike 85 whose stock goes ex $1.75 on day 2 has a later price 8.0e-3 under the reference. The call at strike 90 whose stock goes ex $1.75 on day 1 has a price now 4.997e-3 under it, at the edge of the 5e-3 AM-002 holds the price to at 401 steps. AM-002's own cases reach 3.2e-3; it has no dividend a day out. The cause was not isolated. The tree can exercise only on a step, and the last one before the ex-date falls up to a step early, which is the likely one.
- Only four cases are over 1e-3 at 401 steps, all in the three groups with an ex-date on day 1 or day 2. Half of all the cases are within 1e-5 at 401 steps and within 2e-6 at 2,003.
- The decays here run from 1.1e-3 to 1.32 a share in size. Four of the 100 are smaller than the figure their group is held to at 401 steps, and two are smaller than 2e-3, so those cases would pass with a decay of zero.
- With no dividend the two engines' decays agree to 2.0e-6 at 2,003 steps. AM-004 holds that path to 1e-9 against the same tree.

**Measured outside the fixture.** A call struck at 80 on a stock at 100, 60 days out, at a volatility of 0.6 and a rate of 0.045, with $3.00 going ex on day 2. At the default 401 steps the tree's decay is -0.14078 and this engine's is -0.12105: a gap of 2.0e-2, 16% of the decay. The tree's later price, one day before the ex-date, is 1.9e-2 under the engine's. At 2,003 steps the gap is 1e-4. A second call, struck at 85, 80 days out, at volatility 0.4 with $3.00 going ex on day 2, is 1.2e-2 out at 401 steps, 13% of its decay. So for a call in the money with a large dividend going ex a day or two out, the decay at the default steps is an estimate. The day-2 group above is that kind of case at a smaller dividend.

**What the tolerance tells apart, and what it does not.**

- Holding the quoted price across the ex-date moves the 32 cases that go ex inside the day by 6.4e-2 to 1.75. That is 6 to 175 times the loosest figure here.
- Leaving the dividends still ahead where they were, not a day nearer, moves 29 of the 60 cases that have one by over 2e-3, and by up to 6.2e-2.
- Taking off the dividend's present value in place of the cash in full moves the later price by at most 2.2e-4. This fixture does not tell those two apart. `tests/options/test_american_daily_decay.py` pins the choice directly, against the pricer's own price at the lower stock price.

The manifest holds one tolerance figure for a fixture. Here it is the loosest a test of this fixture accepts, `atol=1e-2`, so it is never tighter than a test; the test file states each.

## Regeneration

`cd PythonDataService && .venv/bin/python -m scripts.fixture_generators.american_options` (writes AM-001 to AM-005). It needs QuantLib 1.42.1 (`.venv/bin/pip install QuantLib==1.42.1`): the service's own requirement allows newer releases, and the generator refuses to write these fixtures from any other.
