# OEV-001 — the shares and cash an exercise, an assignment and an expiry create

## Source

Two pages Alpaca publishes, both read on 2026-10-06. **These are
documentation examples, not recordings.** No account has been assigned, has
exercised or has let an option expire under this code, so the fixture shows
what Alpaca's pages print and not what its API sends.

- Alpaca, "Non-Trade Activities for Option Events",
  https://docs.alpaca.markets/docs/non-trade-activities-for-option-events
  (page dated 2025-09-24). A Trading API page, the product this repo trades
  through. The cases named `trading_api/…`: two calls exercised, two calls
  assigned, two calls expired.
- Alpaca, "Options Trading Overview",
  https://docs.alpaca.markets/docs/options-trading-overview (page dated
  2026-09-16), the sections Exercising, Assignment and Expiration. A page of
  the **Broker API, another product**. The cases named `broker_api/…`. They
  are kept because they are the only examples Alpaca publishes of an
  exercised put and an assigned put. They are not proof of what a Trading
  API account is sent.

Each case is named for the page and the heading its example sits under.

## What the files hold

`input.json` is each documented event, as the rule is given it:

- `activity_type`: Alpaca's code on the event's row (`OPEXC`, `OPASN`,
  `OPEXP`), and `kind`: the word this repo knows that code by.
- `contract`: the row's `symbol`, an OCC contract symbol.
- `position_change`: the row's `qty`, in contracts, signed as printed.
  Negative takes from a bought position, positive from a sold one.
- `event_date_ms`: the row's `date` at 00:00 New York time, `int64` ms UTC.
- `multiplier`: 100 shares a contract, for every case.

`output.json` is the share trade (`OPTRD`) the page prints beside that event:

- `share_trade`: the row's `symbol`, `qty`, `price` and `net_amount`, copied
  as text exactly as printed. `qty` is in shares, positive when bought.
  `net_amount` is dollars, negative when paid.
- `price_from_net_amount`: the size of `net_amount` over the size of `qty`,
  in exact decimal. It is what the row's own cash says a share cost.
- `price_in_sentence`: the dollars a share that the sentence under the
  example states ("at a per-share amount of $150 (strike price)"). `null`
  where the page states no figure: the Broker API page says only "at the
  strike price".
- For an expiry, `share_trade` is `null`. Each page prints one `OPEXP` row
  under its expiry heading and no `OPTRD` row, and the Trading API page says
  "When a contract expires OTM, the Alpaca system will flatten the position
  and no further action is taken."

## Where a page contradicts itself: one accepted divergence

`trading_api/option_exercise`. The page's share-trade row prints
`"price": "90"`. The contract is `AAPL230721C00150000`, struck at 150. The
sentence under the example says the 200 shares are "purchased at a per-share
amount of $150 (strike price)", and the row's own `net_amount` is `-30000`,
which over 200 shares is 150.

The fixture records all three: `share_trade.price` is `"90"` as printed,
`price_from_net_amount` is `"150"` and `price_in_sentence` is `"150"`.
Nothing is corrected in the file.

**What is taken:** the strike, 150. The rule is held to the row's shares, to
its net amount, and to the price the sentence and the net amount both give.
The printed 90 is taken to be a slip on the page. The test pins it by name
(`_PRICE_ALPACA_PRINTS_AGAINST_ITS_OWN_TEXT`) and pins that no other case
prints a price its own net amount disagrees with. Which of the two Alpaca's
API would send is not known; only a recording can settle it.

## Assumptions

- **100 shares a contract.** No page states it as a rule. Every documented
  pair has it, and the generator stops if one does not.
- **The put directions are documented for the Broker API only.** That an
  exercised put sells the shares and an assigned put buys them is printed on
  the Broker API page. It is also what a put means.
- **No lot is in the fixture.** The broker reports an event against the
  account and names no lot. The test offers the rule one lot that holds
  exactly the event's contracts, opened on the event's date, and checks that
  lot's shares and cash. Which lot is taken when several hold a contract is
  this repo's own rule (ADR 0076), is on no page of Alpaca's, and is pinned
  by hand-derived cases in `tests/options/test_event_allocation.py`.
- **The event itself moves no cash.** Every documented event row prints a
  `net_amount` of `"0"`. The fixture holds the share trade only.

## Independent numerical oracle

`reference_kind=external_reference`. Every expected figure is a value Alpaca
prints, cut from the page by the generator. `price_from_net_amount` is one
exact division of two printed values. The generator never imports
`app.options.event_allocation`, and nothing in either file is typed by hand.

Before it writes a file the generator checks what the page says about each
example against the example's own rows, and stops if any differs: one event
and at most one share trade under a heading, a share trade beside every
exercise and assignment and none beside an expiry, 100 shares a contract,
cash paid for shares bought and received for shares sold, a net amount that
divides exactly, and, where the page has a sentence, its contracts, its
shares and "purchased" or "sold". The printed price is the one thing it
records without checking, because that is where the page disagrees with
itself.

## Tolerance

`atol=0, rtol=0`. Shares and cents are whole numbers and prices are exact
decimals; each is compared exactly.

## What this does not show

- That an Alpaca account is sent these rows, or these amounts.
- A contract that delivers other than 100 shares.
- A share trade that is net of a fee.
- A sold option expiring: every documented expiry is signed negative.

## Regeneration

`cd PythonDataService && .venv/bin/python -m scripts.fixture_generators.alpaca_documented_option_activities`

The same run rewrites the documented activities in
`tests/fixtures/alpaca_documented/option_activities/`, from the same pages.
It fetches the pages as they stand that day and uses no Alpaca key or API.
With `--pages-dir <dir>` it reads saved copies instead. This version was
written on 2026-10-07 from the copies saved when the pages were read on
2026-10-06; those copies are not kept in the repo.
