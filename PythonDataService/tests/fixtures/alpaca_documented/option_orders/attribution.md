# Fixture attribution — option orders (documentation examples)

- **broker:** alpaca
- **reference_kind:** `documentation_example`. **Not a recording.** To be joined by a recording from Alpaca Paper, beside it: this example stays.
- **read on:** 2026-10-06
- **regenerate:** `python -m scripts.fixture_generators.alpaca_documented_option_examples` (from `PythonDataService/`)

## Sources

| File | Page | Page last updated |
|---|---|---|
| `documented_requests.json` → `single_leg` | Options Orders, https://docs.alpaca.markets/docs/options-orders | 2025-09-24 |
| `documented_requests.json` → `multi_leg` | Options Level 3 Trading, https://docs.alpaca.markets/docs/options-level-3-trading | 2025-09-24 |
| `documented_requests.json` → `order_reference` | Create an Order, https://docs.alpaca.markets/reference/postorder (the OpenAPI request examples `Options` and `MultilegOptions`) | 2026-05-27 |
| `documented_responses.json` | Create an Order, https://docs.alpaca.markets/reference/postorder (the OpenAPI examples `OptionOrderResponse` and `MultilegOptionsOrderResponse`) | 2026-05-27 |

Each request from a guide page is keyed by the heading it sits under on its
page; nothing is edited: a body is the JSON object inside the page's `curl`
example. The two from the order reference are keyed by the names the
reference gives them, and are its only option requests: its other request
examples are a stock, a crypto and an IPO order.

The reference's `Options` request (2 contracts of `AAPL250620C00100000` at a
limit of 10) and its `OptionOrderResponse` answer agree with each other. Its
multi-leg pair does not: see the table below.

## What the pages state, and where it is used

From the order reference (`CreateOrderRequest`, `MLegOrderLeg`, and the
`OrderType`, `OrderClass`, `TimeInForce`, `PositionIntent` lists):

- A multi-leg order has `order_class` `mleg`, up to four `legs`, and each leg
  a `symbol`, a `ratio_qty`, a `side` and a `position_intent`
  (`buy_to_open`, `buy_to_close`, `sell_to_open`, `sell_to_close`).
- `symbol` and `side` are required for every order class but `mleg`. `qty` is
  required for `mleg` and counts units of the strategy.
- **The sign of a multi-leg `limit_price`:** "A positive value indicates a
  debit ... A negative value signifies a credit".
- A multi-leg order's type is `market` or `limit`. An option order's time in
  force is `day` or `gtc`.
- `client_order_id` is at most 128 characters, in the request and in an
  answer. A longer one is refused here before anything is sent. The pages
  say nothing of a space before or after one; this repo refuses that too,
  as its own rule, because the id Alpaca kept might not be the one sent.

From the same reference's answer schemas (`Order`, `OrderLeg`, `OrderSide`),
read again on 2026-10-07 for the rules an answer is held to:

- An order's `id`, and a leg's, is a string of format `uuid`. An answer
  whose id, or a leg's, is not a UUID is refused by Alpaca's reader: nothing
  could be read or cancelled by it.
- `symbol` is "required for all order classes except for `mleg`", and
  `OrderSide` is "Required for all order classes except for mleg". The one
  published multi-leg parent writes both as the empty string and has no
  `position_intent`. So a parent is read when each of the three is the
  empty string, null or left out, and refused when it states any of them.
- The published legs' `filled_avg_price` are unsigned: three bought at 0.43
  and one sold at 0.01 net to the parent's 1.28 only when the sold leg's
  price is taken as positive and its side applied. So a leg's fill price,
  and a single order's two prices, are never below zero; only a parent's
  net price is signed.

From Options Trading (https://docs.alpaca.markets/docs/options-trading, last
updated 2026-09-16): an option order's `qty` is a whole number, `notional` is
not set, and `extended_hours` is false or absent.

From Options Level 3 Trading: leg ratios must have a greatest common divisor
of one, a multi-leg order may not hold a stock leg, and every leg must be
covered inside the same order.

## Where two pages disagree, and which was taken

| Subject | One page | The other | Taken |
|---|---|---|---|
| **The sign of a received premium** | The order reference: a negative `limit_price` is a credit. Level 3's cost-basis section agrees: a $5 credit "becomes -$5" | Level 3's iron condor example (bought outer strikes, sold inner ones, a structure that ordinarily receives premium) carries `"limit_price": "1.80"`, positive. So do its two roll examples | **Negative is a credit**, the order reference. The three examples are matched as written and prove the body's shape, not the sign. The first received order on Paper must confirm it |
| The ratio's field name | Level 3's examples and the order reference: `ratio_qty` | Level 3's own prose on the divisor rule: `leg_ratio` | `ratio_qty`, the order reference |
| A stock leg in a multi-leg order | Level 3's opening paragraph: legs may be "calls, puts, or even shares" | Level 3, further down: an equity leg is not supported | Not supported. The contract sends option legs only |
| `position_intent` on a single-leg order | The order reference lists it on every order | Options Orders shows four single-leg bodies without it | It is sent. The four bodies are otherwise matched exactly |
| A leg's fields in an answer | The example answer's legs carry `ratio_qty` and `expires_at` | The `OrderLeg` schema lists neither | Read `ratio_qty` when present; `expires_at` is not read |
| **A leg's `qty`, and whether the reference's multi-leg request and answer are one exchange** | The order reference's request example `MultilegOptions`: `qty` "3", legs with `ratio_qty` "3" and "1", `limit_price` "10" | The answer example the same reference gives, `MultilegOptionsOrderResponse`, on the same two contracts, ratios, sides and limit price: parent `qty` "1" and `filled_qty` "1"; legs' `qty` "3" and "1" | **Neither is taken as a rule.** Had the answer answered that request, its parent would say 3; it says 1. Its legs' 3 and 1 are the ratios times one structure, and equally the ratios alone, so this one answer cannot say what a leg's `qty` is when more than one structure is ordered. A leg's quantity is read as written and held to nothing but "no more filled than ordered". Open until an order for two or more structures is recorded on Paper |
| Legs on a lookup | The `Order` schema: `legs` is required for `mleg` | `nested`, the parameter that asks for them, is documented on `GET /v2/orders/{order_id}` and on the list, not on `GET /v2/orders:by_client_order_id` | The lookup by client order id sends no undocumented parameter; a parent that comes back without legs is read again by its broker id with `nested=true` |

## What these examples do not show

No recording exists for any of this. Until one does, it is unverified.

- **A credit.** Every documented multi-leg body has a positive `limit_price`.
  The negative sign is from the reference's sentence, with no example body
  and no example answer. How Alpaca writes a filled credit's
  `filled_avg_price` is not shown.
- **A multi-leg market order.** The reference lists the type; no page shows a
  body or an answer.
- **What a real multi-leg parent writes for its own `symbol`, `side` and
  `position_intent`.** One example shows two empty strings and no third
  field. A real parent that states any of them would not be read.
- **A leg's fill price in a received structure.** If Alpaca wrote a sold
  leg's price below zero, the answer would not be read.
- **A closing order of one leg**, and **`position_intent` in a single-leg
  answer** (the example answer predates the field and omits it).
- **A leg's `qty` and `filled_qty` for an order of more than one
  structure.** The only answer shown has a parent quantity of 1 (see the
  table above), so whether a leg reports its ratio or its ratio times the
  parent's quantity is not shown.
- **A partial fill, a cancel, a rejection or an expiry** of a multi-leg
  order: the one example answer is fully filled. Whether more than one unit
  can fill in part, and what the parent and legs then report, is not stated.
- **The price step** for an option or for a multi-leg net price. The
  "sub-penny" rule on Placing Orders is written for stock limit orders.
- **What a closing intent beyond the held quantity does.** The documents do
  not say it is refused.
- **Whether cancelling the parent cancels every leg**, and whether a leg can
  be cancelled or looked up by its own id.
- **The error bodies** for an account not approved for the order, for
  ratios with a common divisor, and for an uncovered leg.
- **The `trade_updates` stream** for an option order: no page read shows a
  multi-leg frame.
