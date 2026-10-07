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

### 7. A structure is checked after its contracts are picked

A plan's legs are rules, and independent rules can land on a shape nobody meant. A bounded loss does not prove the result is the structure the owner named. So the picked contracts are checked before an order is built from them, and wherever a plan is previewed.

- **Every structure.** One contract per leg of the plan, of the leg's kind, on the plan's underlying. No contract in two legs. Every sold leg has a bought leg of its kind, contract for contract, that does not expire before it. A resolved structure takes each leg's side and ratio from the plan, so it cannot differ from what was declared.
- **A named structure, also its own shape.** One expiry, and:

  | Structure | Must hold |
  |---|---|
  | Bull call spread | bought strike below sold strike; premium paid |
  | Bear put spread | bought strike above sold strike; premium paid |
  | Bull put spread | bought strike below sold strike; premium received, and less than the width |
  | Bear call spread | bought strike above sold strike; premium received, and less than the width |
  | Long straddle | one strike |
  | Long strangle | put strike below call strike |
  | Iron condor | bought put, sold put, sold call, bought call, strikes rising; premium received |
  | Iron butterfly | the sold put and sold call share a strike, with the bought put below it and the bought call above; premium received |
  | Call or put butterfly | lower wing, body, upper wing, strikes rising; wings the same width |

- **The name is part of the plan** (ADR 0012's amendment). A plan with no name is Custom and gets the first check only.
- **A failure is a typed refusal** that names the rule and the legs. At an entry it skips the entry; in a preview it is the verdict. Its reasons are spelled as the money's are (`OPTION_STRUCTURE_…`), so one screen and one stored entry read both.
- **A share-class ticker is not handled yet.** A contract is on the plan's underlying when its root is the plan's ticker letter for letter, and `BRK.B` is written `BRKB` in a contract symbol. Such a plan's picks are refused until the broker's contract reference says which roots are whose.
- **Not asked here:** whether the loss is bounded, which the requirement answers (decision 3), and whether Alpaca lists each contract as standard and tradable, which waits for its contract reference.
- **Where a named structure starts** is a separate list: the legs and rules the builder fills in. Most hang on one leg picked by delta with the others a dollar distance from it; the strangle and the iron condor pick two legs by delta, a put and a call out of the money. Every value is a starting point the owner changes, and a deployed plan carries its own. **The values themselves are provisional.** Only the bull call spread's (delta 0.50, $5 above, the nearest weekly expiry) come from the builder's design (#2915); the 0.30 delta for a leg out of the money and the other eleven starts await the owner's word.

## Where it lives

`PythonDataService/app/options/`: `contract.py` (decision 1), `structure.py` (a resolved structure: one underlying, each contract in one leg, as one order carries it), `requirement.py` (decisions 2 to 5), `exact.py` (the one exact decimal conversion they share), `named_structures.py`, `structure_check.py` and `structure_catalogue.py` (decision 7). Golden fixture `OSM-001` pins the requirement, premium, commitment, loss estimate and round-trip cost against Alpaca's two documented examples and hand-derived cases, in exact cents.

Nothing on the money path calls these yet.

## Alternatives considered

- **Ask Alpaca what an order costs.** Its references describe no such call. The account does report an option buying power, which Deploy is to read as a second check, but that is an account-wide figure and not the cost of one order.
- **Use the loss at expiry as the amount to set aside.** Rejected: Alpaca holds the requirement, not the loss, and the loss is not estimated at all when expiries are mixed. Keeping both lets the bot compare its own figure with Alpaca's.
- **Check the payoff only at the strikes.** Rejected: it reports zero for one bought 450 put against two sold 440 puts, whose payoff is above zero at both strikes and falls to minus $430 a share at a price of zero.
