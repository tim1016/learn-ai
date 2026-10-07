# Fixture attribution — option orders (documentation examples)

- **broker:** alpaca
- **reference_kind:** `documentation_example`. **Not a recording.** To be replaced by a recording from Alpaca Paper.
- **read on:** 2026-10-06
- **regenerate:** `python -m scripts.fixture_generators.alpaca_documented_option_examples` (from `PythonDataService/`)

## Sources

| File | Page | Page last updated |
|---|---|---|
| `documented_requests.json` → `single_leg` | Options Orders, https://docs.alpaca.markets/docs/options-orders | 2025-09-24 |
| `documented_requests.json` → `multi_leg` | Options Level 3 Trading, https://docs.alpaca.markets/docs/options-level-3-trading | 2025-09-24 |
| `documented_responses.json` | Create an Order, https://docs.alpaca.markets/reference/postorder (the OpenAPI examples `OptionOrderResponse` and `MultilegOptionsOrderResponse`) | 2026-05-27 |

Each request is keyed by the heading it sits under on its page. Nothing is
edited: a body is the JSON object inside the page's `curl` example.

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
| Legs on a lookup | The `Order` schema: `legs` is required for `mleg` | `nested`, the parameter that asks for them, is documented on `GET /v2/orders/{order_id}` and on the list, not on `GET /v2/orders:by_client_order_id` | The lookup by client order id sends no undocumented parameter; a parent that comes back without legs is read again by its broker id with `nested=true` |

## What these examples do not show

No recording exists for any of this. Until one does, it is unverified.

- **A credit.** Every documented multi-leg body has a positive `limit_price`.
  The negative sign is from the reference's sentence, with no example body
  and no example answer. How Alpaca writes a filled credit's
  `filled_avg_price` is not shown.
- **A multi-leg market order.** The reference lists the type; no page shows a
  body or an answer.
- **A closing order of one leg**, and **`position_intent` in a single-leg
  answer** (the example answer predates the field and omits it).
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
