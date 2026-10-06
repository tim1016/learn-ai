# ADR 0076 — Option structures on the money path

**Status:** Accepted 2026-10-06. It gains a section as each part of #2913 it decides is built.
**Provenance:** Owner decisions in [#2913](https://github.com/tim1016/learn-ai/issues/2913), grilled on 2026-10-05 and revised on 2026-10-06 after a review against the code and Alpaca's documents.
**Related:** ADR 0012 (the Action Plan, and its 2026-10-06 amendment that defines an option structure), ADR 0022 (i) (the option expiry anchor), ADR 0059 (the live envelope and the budget, which cover US equities only until this ADR's later sections extend them), ADR 0068 (Python owns the math).

## Context

A bot may listen to one symbol's signal and trade option structures on any underlying. Nothing on the trading side could do that: an order was one stock leg, money was counted per share, and no rule said what an option position costs to hold or what the bot must have before it opens one.

Alpaca's order and account references (read 2026-10-06) describe no call that says, before an order, what that order will cost. So the bot works it out itself from Alpaca's published rule, and compares with what Alpaca took afterwards.

## Decision

### 1. A contract has one spelling

An option contract is its root symbol, its expiry, call or put, and its strike. The expiry is the date's 16:00 New York anchor. The strike is held in whole thousandths of a dollar, which is the unit the OCC symbol writes it in, so no float decides which contract an order names. The symbol Alpaca uses (`AAPL240119C00100000`) is built from those four and read back from them, and anything that is not such a symbol is refused.

A symbol cannot say that a contract delivers 100 shares or is American. Those are facts of the listing, to be checked where a contract is picked. The one thing the spelling shows is an adjusted contract, whose root carries a digit; it is refused.

### 2. Five money quantities, kept apart

All are whole cents. The fee provision is the one figure that rounds, up to the next cent, where it is worked out. Nothing else is rounded; an amount that does not come to whole cents is refused.

| Quantity | What it is | Sign |
|---|---|---|
| **Net entry premium** | What the entry pays or receives: bought legs' prices minus sold legs' prices, times ratio and multiplier | Positive is paid, negative is received. This is the sign Alpaca's multi-leg limit price carries |
| **Broker requirement** | Alpaca's maintenance requirement by its published rule. Premiums are left out | Zero or more |
| **Entry commitment** | What the bot sets aside when an entry is accepted: requirement plus premium plus the fee provision, for the structures requested | Above zero |
| **Loss at expiry, estimate** | The most the structure loses if every leg has the same expiry, is held to it, is never exercised or assigned early, and settles at its exercise value, before fees | Zero or more |
| **Free budget** | What the bot's budget has left for a new entry (ADR 0059) | Can go below zero |

- **The requirement and the commitment are different numbers by design.** A $5-wide call spread sold for $3.07 has a requirement of $500.00 and a commitment of $193.00 before fees. Comparing the two is never an error.
- **None of them is a cap.** The commitment decides whether an entry may start. A market exit, fees, an early assignment or a close that does not complete can each cost more than the estimate.

### 3. The requirement is the lowest point of the exercise payoff

The requirement sums every leg's exercise payoff as a function of the underlying's price and takes the lowest point. It is read at a price of zero, at every strike, and from the slope above the highest strike. The strikes alone are not enough: a put structure can be at its worst at zero, and a call structure can keep losing above its last strike.

- A loss that grows without limit above the highest strike is refused. That reason is given first, ahead of any about expiries.
- Alpaca's page says to find the price with the lowest payoff. Reading it at zero and from the slope above the top strike is this repo's way of finding it.
- A requirement plus premium of zero or less is refused. Such a result means the prices do not belong together.
- A structure priced under what it must be worth at expiry is admitted, with a loss estimate of zero. For one made of bought legs only, such as a 440 call and a 460 put bought for less than $20, those prices do not belong together either, and whether to refuse them is the owner's to say. It is not always a fault: a box of four legs is worth the same at every price and properly costs a little less, by the interest to expiry.
- A bounded payoff does not prove Alpaca will take the structure as one order. Cover is a separate check.

### 4. Mixed expiries

Alpaca's rule works a position with several expiries out expiry by expiry and takes the largest figure. Its documents do not say how that treats a sold leg covered by a bought leg that expires later.

- A structure with mixed expiries and a sold leg is refused: its commitment cannot be computed. This stands until the treatment is verified on the account.
- A structure with mixed expiries and bought legs only is admitted. Its requirement is zero under either reading.
- For any structure with mixed expiries the loss at expiry is not estimated, and is shown as "not estimated". A leg that outlives another is worth something no exercise payoff describes.

The owner chose on 2026-10-06 to keep mixed expiries on these terms. The recommendation had been one expiry per structure.

### 5. Round-trip cost

A per-leg limit on the bid-ask gap does not bound what a structure costs to trade. Two legs with mids of $5.00 and $4.90, each four cents wide, cost $0.14 to open against each other and return $0.06 closed at once.

- The **round-trip cost** is the sum over the legs of ask minus bid, times ratio and multiplier. It is always worked out.
- An entry is to be refused when it is more than the **structure cost limit**: a percent of the entry commitment, 10 by default, set at Deploy. The owner chose this on 2026-10-06. The comparison is built; the setting arrives with Deploy. The commitment is the measure because it is always above zero; a percent of the structure's mid value breaks down when that value is near zero.

### 6. The pricing model matches the contract

Stock and ETF options are American: the holder may exercise early. Everything this repo priced options with before is European Black-Scholes, which prices a different contract. Delta and implied volatility for a contract a bot may pick come from an American model.

- **The model is a Leisen-Reimer binomial tree,** with the exercise test at every step. It was chosen over a finite-difference solver because it can be proven: with no dividend it is the same construction as QuantLib 1.42.1's `BinomialLRVanillaEngine`, and the two agree to 1e-9 on 400 cases from thirty days to fifteen minutes to expiry (fixture `AM-001`). A finite-difference solver could only be shown to agree with another to within both grids.
- **Time to expiry is a fraction of a 365-day year, to the millisecond.** A date-based day count gives a contract expiring today no time at all.
- **Dividends are cash amounts on dates, given explicitly, and held in escrow:** the tree is built on the price less the present value of the dividends still to go ex, and a node is exercised at that plus the dividends not yet paid. There is no dividend yield, and no silent assumption of none. Against QuantLib's finite-difference engine under the same escrowed model the tree's delta is within 7e-5 at 2,001 steps and the gap falls as steps rise (fixture `AM-002`); that is two numerical schemes converging, and the tolerance says so.
- **The other cash-dividend model would move the answer more than the step count does.** QuantLib's "spot" model changes the same cases by up to 3e-3 in delta. Escrowed is the choice; it is the one the tree can carry.
- **The default is 401 steps.** Against a tree ten times finer that costs under a thousandth of delta. A delta rule's own tolerance is 0.05.
- **A solve with no answer says why and carries no number.** The reasons are kept apart: under a minute to expiry; a price under what exercising now pays; a price no higher than the model's at the lowest volatility tried, which includes an option priced at exactly its exercise value; a price above the model's at the highest; no convergence; an input the model has no meaning for. A vendor's implied volatility or Greeks are never put in a failure's place (ADR 0071 Decision 1, which this extends from the Polygon recorder to any quote a bot prices).
- **A strike dozens of standard deviations from the price is not priced.** The tree's branch sizes are rounding error there. It answers nothing, never a guess.

## Where it lives

`PythonDataService/app/options/`: `contract.py` (decision 1), `structure.py` (a resolved structure: one underlying, each contract in one leg, as one order carries it), `requirement.py` (decisions 2 to 5), `exact.py` (the one exact decimal conversion they share), `american.py` (decision 6). Golden fixture `OSM-001` pins the requirement, premium, commitment, loss estimate and round-trip cost against Alpaca's two documented examples and hand-derived cases, in exact cents. Fixtures `AM-001` to `AM-003` pin the American price, delta and implied volatility against QuantLib.

Nothing on the money path calls these yet.

## Alternatives considered

- **Ask Alpaca what an order costs.** Its references describe no such call. The account does report an option buying power, which Deploy is to read as a second check, but that is an account-wide figure and not the cost of one order.
- **Use the loss at expiry as the amount to set aside.** Rejected: Alpaca holds the requirement, not the loss, and the loss is not estimated at all when expiries are mixed. Keeping both lets the bot compare its own figure with Alpaca's.
- **Keep Black-Scholes and call it close enough.** Rejected: it gives a deep put less than exercising it pays, and it cannot see why a call is exercised the day before a dividend. The PRD allows an approximation only with its measured cost; the tree is the model itself, and its measured cost is the step count's.
- **Take delta from IBKR.** Rejected by the owner's rule that a number picking a contract must be one this repo can check. IBKR's delta is to be recorded beside ours, and blocks nothing.
- **Check the payoff only at the strikes.** Rejected: it reports zero for one bought 450 put against two sold 440 puts, whose payoff is above zero at both strikes and falls to minus $430 a share at a price of zero.
