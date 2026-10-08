# OSM-002 — held option portfolio collateral

Alpaca, [Options Level 3 Trading](https://docs.alpaca.markets/docs/options-level-3-trading),
maintenance-margin steps 1–5, read 2026-10-08. Premiums are excluded, open
positions' exercise payoffs are combined, and each expiry is evaluated
separately. The largest expiry requirement is retained per underlying;
independent underlyings' requirements add. Contract multipliers are recorded
inputs. An unbounded expiry is refused; a later long call cannot cover it.
Submitted orders retain ADR 0076's separate shape and mixed-expiry restrictions.

Generated 2026-10-08 with:

`cd PythonDataService && .venv/bin/python -m scripts.fixture_generators.option_portfolio_money`

`reference_kind=hand_computed`. The generator writes the hand-derived outputs
only after checking them with OSM-001's independent exhaustive Fraction scan
at every thousandth of a dollar from zero through $2 beyond the largest
strike. It never imports the canonical implementation. Strikes and premiums
are dollars per share; ratios are actual whole held contracts, with one
portfolio unit. Figures are whole cents and comparison is bit-exact:
`atol=0, rtol=0`.

Worked expectations:

- Expiries with $5 × 100 and $10 × 2 × 100 losses reserve $2,000, their maximum.
- Three $5-wide credit spreads sharing an expiry reserve $1,500 across six calls.
- Bought spreads can offset a sold spread at the same expiry; they cannot
  offset it at a different expiry or on another underlying.
- Two independent $5-wide spreads on SPY and QQQ reserve $1,000 in total.
- An uncovered short call, including an uneven residual, has unlimited loss.
- Two sold $4 puts reserve $800 at an underlying price of zero.
- A $5-wide spread with multiplier 10 reserves $50; empty holdings reserve zero.
