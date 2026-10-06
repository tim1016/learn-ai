# OSM-001 — what an option structure costs to enter

## Source

Alpaca, "Options Level 3 Trading",
https://docs.alpaca.markets/docs/options-level-3-trading. The page showed
"Last Updated 2025-09-24" when it was read on 2026-10-06. It states the rule
this fixture pins:

- maintenance margin leaves premiums out and uses each option's exercise
  payoff as a piecewise linear function of the underlying's price; the payoffs
  of all legs are summed, and the requirement is the absolute value of the
  lowest point of that sum;
- the cost of an order is that requirement plus the net premium, a credit
  entering as a negative number.

Two of the cases are the page's own worked examples, numbers unchanged:

- `alpaca_documented_four_calls_offset_to_nothing`: long $100 call, short $110
  call, long $200 call, short $190 call. The page's "universal spread" total
  is $0.
- `alpaca_documented_call_credit_spread`: long $200 call paid $10, short $190
  call received $15. The page gives a maintenance margin of $1,000, a net
  credit of $500 and a total cost basis of $500.

Two more are the worked cases of PRD
https://github.com/tim1016/learn-ai/issues/2913: a call spread bought for
$1.94, and a $5-wide call spread sold for $3.07. Its third, two narrow legs
that hide a costly spread, pins the round-trip cost.

The loss-at-expiry estimate and the round-trip cost are this repo's own
definitions (ADR 0076), not Alpaca's. Alpaca's page is the source for the
requirement and for the sign of the premium only.

## Assumptions

- Every contract is standard: 100 shares a contract (`multiplier` in
  `input.json`).
- Strikes and prices are in dollars per share; every expected figure is whole
  cents for the units the case asks for (one unless `units` says otherwise).
- A case with `quotes` enters at the natural price: the ask for a bought leg,
  the bid for a sold one. A case with `leg_prices` states each leg's price.
- `commitment_before_fees_cents` is requirement plus premium. The fee
  provision is added on top and is not part of this fixture.
- Legs that share an expiry are worked together. Alpaca's page says a position
  with several expiries is worked expiry by expiry and the largest figure
  taken; it does not say how that treats a sold leg covered by a bought leg
  that expires later, so that case is pinned as a refusal, not a number.

## Independent numerical oracle

`reference_kind=hand_computed`. Every expected figure is written out by hand
in the generator. Before it writes the files, the generator checks each one
against a brute-force scan: the summed exercise payoff at every thousandth of
a dollar from zero to $2 past the highest strike, in exact fractions. The
canonical implementation instead reads the payoff at zero and at each strike.
The generator never imports it.

## Tolerance

`atol=0, rtol=0`. Every figure is an integer number of cents and is compared
exactly.

## Regeneration

`cd PythonDataService && .venv/bin/python -m scripts.fixture_generators.option_structure_money`
