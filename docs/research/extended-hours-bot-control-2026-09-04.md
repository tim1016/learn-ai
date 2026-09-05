# IBKR extended-hours market data and orders — 2026-09-04

## Scope and verdict

This note records current **Interactive Brokers API behavior only**. It does not
inspect or recommend restoring learn-ai's deprecated IBKR bot-control product.

IBKR can supply data and accept orders outside regular trading hours, but there
are two distinct cases:

1. **Ordinary extended hours** (for a typical eligible US stock, pre-market and
   after-hours within the instrument's total `tradingHours`): use the ordinary
   contract/routing, include outside-RTH data, and submit an eligible order with
   `outsideRth = true`.
2. **The separate US overnight session**: this is a distinct venue. Use an
   `OVERNIGHT`-routed contract for overnight-only data/orders, or the documented
   SMART plus `includeOvernight = true` combination for Overnight+DAY behavior.

For US equities, IBKR gives 09:30–16:00 ET as regular hours and 04:00–20:00 ET
as a typical instrument's total available hours. Product-, exchange-, holiday-,
and account-specific eligibility still governs the actual session. [IBKR
Stock Trading notes](https://www.interactivebrokers.com/en/trading/products-stocks.php)

The safe baseline order for extended hours is an explicit **limit** order. A
limit order guarantees only that any fill is at the limit price or better; it
does not guarantee a fill. [IBKR Limit Order](https://ibkrcampus.com/docs/general/order-types/basic-orders/limit-orders/limit-order)

## 1. Market data outside RTH

### Do not equate `liquidHours` with market-data availability

`ContractDetails` exposes both:

- `tradingHours`: the contract's total trading hours on the specified exchange;
- `liquidHours`: the contract's liquid/regular trading hours; and
- `timeZoneId`: the time zone in which those schedules are expressed.

It also exposes `orderTypes`, `validExchanges`, `minTick`, and `marketRuleIds`,
which are useful for per-contract order validation. [IBKR `ContractDetails`
reference](https://ibkrcampus.com/docs/tws-api/ref/contract-details)

Therefore, an application that calls the market "closed" whenever the current
time is outside `liquidHours` will incorrectly suppress valid extended-hours
data. `liquidHours` answers whether the regular/liquid session is active;
`tradingHours` answers whether the selected contract/exchange has a trading
session at all. This is an implementation inference from the two documented
fields, not a new IBKR status code.

### Request bars that include extended hours

For TWS API historical bars, `reqHistoricalData(..., useRTH=0, ...)` requests
data generated outside RTH as well as RTH; `useRTH=1` filters to RTH only.
[IBKR Requesting Historical Bars](https://ibkrcampus.com/docs/tws-api/doc/market-data-historical/historical-bars/requesting-historical-bars)

For TWS API five-second real-time bars,
`reqRealTimeBars(..., useRTH=0, ...)` includes data generated outside RTH;
`useRTH=1` restricts the subscription to RTH. The API supports `TRADES`,
`MIDPOINT`, `BID`, and `ASK` for this request. [IBKR Request Real Time
Bars](https://ibkrcampus.com/docs/tws-api/doc/market-data-live/5-second-bars/request-real-time-bars)

For top-of-book `reqMktData`, there is no RTH filter in the request signature;
the contract identifies the instrument and routing context. The callback stream
may be real-time or delayed depending on entitlement. [IBKR Request Watchlist
Data](https://ibkrcampus.com/docs/tws-api/doc/market-data-live/top-of-book-l-1/request-watchlist-data)

`reqMarketDataType` is also **not** a trading-session switch. Its values select
live, frozen, delayed, or delayed-frozen data; the word "regular" in that API's
documentation means live/non-frozen data, not Regular Trading Hours. [IBKR
Market Data Types](https://ibkrcampus.com/docs/tws-api/doc/market-data-delayed/introduction),
[IBKR Market Data Type Behavior](https://ibkrcampus.com/docs/tws-api/doc/market-data-delayed/market-data-type-behavior)

IBKR also supports a historical `whatToShow="SCHEDULE"` request for one-day
schedule bars, returned as session start/end/reference-date data. This can be
useful as broker-supplied schedule evidence, although `ContractDetails` remains
the direct source for the distinction between total and liquid hours. [IBKR
SCHEDULE data](https://ibkrcampus.com/docs/tws-api/doc/market-data-historical/historical-bar-what-to-show/schedule),
[IBKR `HistoricalSession`](https://ibkrcampus.com/docs/tws-api/ref/historical-session)

### Entitlement and freshness remain separate gates

Most securities require a Level 1 market-data subscription for API use; IBKR
notes that data visible for free in TWS may not be licensed for off-platform API
use. [IBKR Market Data Subscriptions](https://ibkrcampus.com/docs/general/market-data-subscriptions/introduction),
[IBKR TWS Data vs API Data](https://ibkrcampus.com/docs/general/market-data-subscriptions/tws-data-vs-api-data)

A missing field is not itself evidence that the market is closed. IBKR documents
that a price tick of `-1` or `0` followed by size `0` means no data is currently
available for that field. The application still has to distinguish session
state from entitlement, route, tick-type, and freshness failures. [IBKR Receive
Live Data](https://ibkrcampus.com/docs/tws-api/doc/market-data-live/top-of-book-l-1/receive-live-data)

Consequently, "the instrument is inside `tradingHours`" is not proof that a
fresh usable quote exists. A robust liveness model should report, separately:

- transport/session connectivity;
- the contract's current session (`regular`, `extended`, `overnight`, or
  `closed`);
- market-data entitlement/type;
- quote/bar freshness; and
- execution eligibility.

This separation is an application-design implication of IBKR's independent
schedule, subscription, and streaming interfaces.

## 2. Ordinary pre-market and after-hours orders

The TWS API `Order` object has the required building blocks:

- `orderType = "LMT"`;
- `lmtPrice = <explicit price>`;
- `tif = "DAY"`, `"GTC"`, or another supported value chosen deliberately; and
- `outsideRth = true`, which allows an eligible order to trigger or fill outside
  RTH.

[IBKR `Order` reference](https://ibkrcampus.com/docs/tws-api/ref/order), [IBKR
Order and Contract Objects](https://ibkrcampus.com/docs/tws-api/doc/orders/the-order-and-contract-objects)

Illustrative TWS API shape:

```python
order = Order()
order.action = "BUY"
order.totalQuantity = quantity
order.orderType = "LMT"
order.lmtPrice = limit_price
order.tif = "DAY"
order.outsideRth = True
```

The `outsideRth` attribute expands when an order may trigger/fill; it does not
create a trade signal or override a strategy's own entry/exit schedule. That
means extended-hours permission can be implemented as an execution-policy
choice while leaving strategies with explicit entry/exit times unchanged.

### Time in force is a separate choice

IBKR defines `DAY` as valid for the day and `GTC` as remaining active until
execution/cancellation, subject to IBKR's documented automatic-cancellation
rules. `GTD` can be paired with `goodTillDate`. [IBKR `Order`
reference](https://ibkrcampus.com/docs/tws-api/ref/order)

The order ticket guide stresses that valid TIF choices depend on product,
order type, and destination. It also says Outside RTH is unavailable for some
combinations, including IOC, OPG/MOO/LOO, FOK, MOC, and LOC. [IBKR Classic TWS
Order Ticket](https://www.ibkrguides.com/traderworkstation/classic-order-ticket.htm)

For a bot, "extended hours" should therefore not be a blind global toggle. The
order policy should validate the exact contract/order-type/TIF/destination
combination and show the effective expiry/cancel behavior before transmission.

### Why limit orders should be the default

IBKR defines a market order as providing no price protection. It also states
that market orders placed before regular US-equity hours are treated as
MarketOnOpen orders, so a `MKT` order is not a reliable way to request immediate
pre-market execution. [IBKR Market Order](https://www.interactivebrokers.com/en/trading/ordertypes.php),
[IBKR Stock Trading notes](https://www.interactivebrokers.com/en/trading/products-stocks.php)

An explicit `LMT` order is therefore the clean execution contract for an
extended-hours-capable bot: the bot supplies a price, the broker may fill at
that price or better, and an unmarketable order may remain unfilled. [IBKR Limit
Order](https://ibkrcampus.com/docs/general/order-types/basic-orders/limit-orders/limit-order)

The limit price must comply with the contract's price increment. IBKR exposes
`minTick` and `marketRuleIds` in `ContractDetails`, and documents
`reqMarketRule` for price-dependent increments. [IBKR `ContractDetails`
reference](https://ibkrcampus.com/docs/tws-api/ref/contract-details), [IBKR
Minimum Price Increment](https://ibkrcampus.com/docs/tws-api/doc/orders/minimum-price-increment/introduction)

## 3. The separate overnight venue

IBKR's US overnight session is not merely another part of SMART extended hours.
IBKR says overnight trading is a separate venue and that the **same OVERNIGHT
routing must be used for market data and orders**, because OVERNIGHT prices can
differ from ordinary SMART-routed data. [IBKR API Overnight
Trading](https://ibkrcampus.com/campus/ibkr-quant-news/api-overnight-trading/)

Current TWS API documentation uses `Order.includeOvernight`:

- overnight only: `contract.exchange = "OVERNIGHT"` and
  `order.includeOvernight = True`;
- Overnight+DAY: `contract.exchange = "SMART"` and
  `order.includeOvernight = True`.

[IBKR Trading the Overnight Session](https://ibkrcampus.com/docs/tws-api/doc/orders/place-order/trading-the-overnight-session)

IBKR currently describes the US overnight window as 20:00–03:50 ET, Sunday
night through Friday morning, for eligible US stocks and ETFs. It describes
overnight-only orders as single-session `DAY` orders that expire if unfilled.
IBKR's current training material says Limit and Adaptive are the supported
overnight order types; GTC is not supported for that session. [IBKR Overnight
Trading Using Limit Order](https://ibkrcampus.com/campus/trading-lessons/overnight-trading-using-limit-order/)

IBKR's overnight product page says overnight market data is provided without a
separate market-data subscription for eligible US stocks and ETFs. That specific
overnight entitlement should be checked independently from ordinary off-platform
API data subscriptions. [IBKR US Overnight
Trading](https://www.interactivebrokers.com/en/trading/us-overnight-trading.php?menu=B)

Do not silently treat `outsideRth = true` as permission for the separate
overnight venue. Ordinary extended-hours and overnight participation should be
distinct execution-policy values, because their contract routing, quote source,
supported order types, and expiry behavior differ.

## 4. Submission, acknowledgement, and fill risks

IBKR recommends monitoring all of `EWrapper.error`, `orderStatus`, `openOrder`,
and `execDetails`. An order can be rejected or cancelled, and a submitted or
acknowledged order is not proof of a fill. [IBKR Order Placement
Considerations](https://ibkrcampus.com/docs/tws-api/doc/orders/place-order/order-placement-considerations)

Relevant documented cases include:

- a large-size rejection through error 201;
- a price-check cancellation through error 202 when a limit is too far from the
  current reference price;
- broker price capping, reported through warnings and `mktCapPrice`; and
- TWS precautions that may require acknowledgement or stop automatic
  transmission.

[IBKR Order Placement Considerations](https://ibkrcampus.com/docs/tws-api/doc/orders/place-order/order-placement-considerations),
[IBKR Understanding Order Precautions](https://ibkrcampus.com/docs/tws-api/doc/orders/place-order/understanding-order-precautions)

Outside RTH commonly has lower liquidity, wider spreads, and greater volatility.
IBKR specifically warns that market orders can execute unfavorably in that
environment. [IBKR Outside RTH](https://ibkrcampus.com/campus/glossary-terms/outside-rth/)

Operationally, a bot must preserve the distinction between:

- `accepted`/`submitted`;
- `working` but unfilled;
- `partially filled`;
- `filled`;
- `cancelled`/`expired`; and
- `rejected` or held by a precaution.

It should also have an explicit stale-order policy (continue, reprice within a
bounded rule, cancel at strategy deadline, or cancel at session end). Blindly
converting a strategy signal into a highly marketable limit order would weaken
the very price protection the limit order is meant to provide.

## 5. Mapping the IBKR facts to learn-ai's broker boundary

Per learn-ai's current broker-authority decision, IBKR is a **read/evidence**
source while Alpaca Broker V2 is the sole bot-control and order-execution
authority. The IBKR facts above should therefore be used to repair the IBKR
market-data/capability evidence supplied to liveness, not to restore IBKR order
actuation. [learn-ai IBKR integration
authority](../ibkr-integration-authority.md)

The local audit accompanying this research found that:

| Current seam | What already works | What blocks extended-hours bots |
|---|---|---|
| [Alpaca V2 deploy service](../../PythonDataService/app/services/broker_v2_panel/panel_deploy.py) | The runner and durable binding already accept `use_rth`. | Both deploy and preview hard-code `use_rth=True`; the deploy request and Angular ticket expose no session choice. |
| [IBKR market-data feed](../../PythonDataService/app/marketdata/ibkr_feed.py) and [IBKR bars](../../PythonDataService/app/broker/ibkr/bars.py) | Historical warm-up and real-time bars propagate `use_rth`, so `False` can retain extended bars. | The current stock contract is SMART-routed; separate overnight support needs venue-aligned OVERNIGHT data rather than assuming SMART prices are the same. |
| [Market liveness](../../PythonDataService/app/services/market_liveness.py) and [Market Pulse](../../PythonDataService/app/services/broker_v2_panel/market_pulse.py) | A `use_rth=False` bot can already exempt an RTH-only `CLOSED` clock when fresh, symbol- and IBKR-account-scoped capability proves PRE/POST/OVERNIGHT. HALTED and UNKNOWN still fail closed. | The panel cannot create such a bot today, and one combined market badge still makes session state easy to confuse with feed availability. |
| [Immutable bot binding](../../PythonDataService/app/services/bot_binding_repository.py) and [bar filter](../../PythonDataService/app/services/bot_trade_strategy.py) | `use_rth` is per bot, defaults to `True`, and controls only which retained bars enter the strategy. Strategy parameters are separately sealed. | The boolean cannot express the safer product distinction between ordinary pre/post and the separate overnight venue. |
| [Bot-to-Clerk order construction](../../PythonDataService/app/broker/alpaca/clerk/sqlite/runtime.py) | Entry and exit intents pass through the single-writer Clerk. | The bot constructs a bare order leg, whose defaults are market/DAY. There is no autonomous limit-price policy. |
| [Broker order contract](../../PythonDataService/app/broker/contract/models.py) | Manual orders already support market/limit, positive `limit_price`, and DAY/GTC validation. | There is no `extended_hours` execution instruction or price-provenance/freshness contract. |
| [Alpaca adapter](../../PythonDataService/app/broker/alpaca/adapter.py) | It serializes limit price and TIF to `POST /v2/orders`. | It does not serialize Alpaca's required `extended_hours=true`, and asset mapping omits overnight eligibility/halt attributes. |

Alpaca's current Trading API rules line up with the missing contract: an
extended-hours order must set `extended_hours=true`, must be a limit order with
an explicit price, and must use DAY or GTC. [Alpaca Placing
Orders](https://docs.alpaca.markets/us/docs/orders-at-alpaca) The active learn-ai
path uses Alpaca's direct Trading API, so these are the execution rules the
implementation must satisfy even though IBKR supplies the market-data evidence.

Those findings imply that enabling extended hours is a cross-boundary contract
change: deployment must preserve a per-bot session policy; IBKR must keep
supplying fresh extended-session evidence; order intent must carry a limit-price
policy and TIF; and the **Alpaca** adapter must express the corresponding
extended-hours order instruction. IBKR's `outsideRth` order field is useful for
understanding the broker concept, but it is not the field learn-ai should add to
its execution path.

## 6. Implications to combine with learn-ai repository findings

These are implementation implications, not claims about the current code:

1. **Fix liveness at the right semantic layer.** A regular-session calendar must
   not decide whether IBKR data is available. Model total trading session,
   quote freshness, entitlement, and broker connectivity independently.
2. **Make session permission per bot/deployment, not universal.** Suggested
   values are `regular_only`, `extended` (pre/post), and, only if intentionally
   supported, `overnight`. A strategy with its own entry/exit clock remains the
   final source of trade intent.
3. **Make order policy explicit.** Extended-hours participation should require a
   supported limit-price policy, TIF, maximum quote age, maximum spread, price
   rounding by market rule, and unfilled/partial-fill handling.
4. **Show effective behavior in the control panel.** Display the selected
   session policy, order type, TIF/expiry, limit-price rule, data freshness, and
   last broker acknowledgement/rejection. Do not collapse them into a single
   "market open/closed" badge.
5. **Treat overnight as a later, separate capability unless explicitly needed.**
   It needs OVERNIGHT-specific quotes/routing and different lifecycle rules;
   the ordinary `outsideRth` change is sufficient only for eligible pre/post
   sessions.
6. **Qualify in paper before live use.** Test transitions at 04:00, 09:30,
   16:00, 20:00, and 03:50 ET; holidays and early closes; delayed or missing
   quotes; partial fills; rejected/capped prices; reconnect/restart; and
   outstanding-order recovery.

## 7. Recommended implementation slices

### Slice 1 — truthful display and dry-run data

- Add an immutable, per-deployment session policy. Keep existing bots and the
  default at `regular_only`; offer `extended` for PRE+RTH+POST. Do not expose
  overnight in the first slice.
- Thread that choice through deploy preview, deploy, the binding, warm-up, and
  live bar subscriptions. Preview and execution must call the same admission
  decision.
- Split the panel presentation into **Session**, **Data**, and **Orders**. For
  example: `PRE`, `LIVE (bar age 8 s)`, `BLOCKED — limit mechanism unavailable`.
  Never translate an RTH-only clock's `CLOSED` into “market data unavailable.”
- Release this slice for dry run only. It proves that strategies receive the
  expected bars without authorizing a broker order.

### Slice 2 — protected extended-hours paper orders

- Add an execution instruction to the broker-neutral order contract and Alpaca
  adapter. In extended sessions the valid serialized shape is `type=limit`, an
  explicit `limit_price`, `time_in_force=day|gtc`, and
  `extended_hours=true`. Any impossible combination must be rejected before
  broker contact.
- Compute autonomous limit prices in Python from a fresh top-of-book quote, not
  in Angular or .NET and not from the close of a one-minute trade bar. Record
  quote timestamp/source, bid, ask, spread, limit rule, and rounded price in the
  decision receipt.
- Use a bounded policy: maximum quote age, maximum spread, maximum price offset,
  tick-size rounding, a cancel/reprice deadline, maximum attempts, and a total
  slippage cap. A stale/missing/wide quote blocks submission; it must never
  silently fall back to a market order.
- Apply the mechanism to both entries and strategy-requested exits because
  extended-hours market orders are not eligible. Preserve the existing rule
  that risk-reducing exits are not blocked by an entry-only liveness gate, while
  still requiring a valid limit price and broker session.
- Treat accepted, working, partial, filled, cancelled, expired, and rejected as
  distinct states. Recovery and idempotency must prevent a restart or reprice
  from creating a duplicate order.

### Slice 3 — overnight only after venue alignment

- Add a distinct `overnight` policy only after the data contract can request
  venue-aligned overnight quotes and the execution asset can prove overnight
  eligibility and non-halted state.
- If IBKR provides the reference quote while Alpaca executes, treat it as
  cross-venue evidence. Compare it with an Alpaca/BOATS execution-side quote
  where available and fail closed on stale, wide, or materially divergent
  books.

The effective permission at submission should be the intersection of four
independent facts:

```text
strategy emits ENTER/EXIT
AND bot session policy allows the current phase
AND fresh market-data/capability evidence proves the phase
AND the broker order mechanism is eligible for that phase
```

This keeps strategy clocks intact. Enabling extended hours only gives a bot
eligible bars and an eligible execution mechanism; it does not manufacture a
signal or change a strategy's configured entry/exit time.

## 8. Minimum regression and acceptance coverage

- A PRE/POST bar is retained but reaches only a bot whose policy includes that
  phase; an RTH-only bot remains closed.
- A timed strategy emits no action outside its own window even when its bot is
  extended-enabled; an otherwise-identical untimed test strategy may act.
- Fresh matching IBKR capability can reconcile the RTH-only closed clock;
  missing, stale, wrong-symbol, or wrong-account evidence cannot.
- HALTED and UNKNOWN still block new exposure.
- Every extended order observed at the Alpaca adapter is LIMIT + price + DAY/GTC
  + `extended_hours=true`; the invalid market-order combination never contacts
  the broker.
- Stale quotes, excessive spreads, invalid price increments, and crossed or
  inconsistent cross-venue books fail closed with operator-readable receipts.
- Partial fill, cancel/replace timeout, restart, and recovery tests prove no
  duplicate exposure and preserve the remaining quantity.
- Deploy preview, panel readiness, Clerk submit-time recheck, and the emitted
  broker request agree on the same session decision.

## Simple answers

- **Can IBKR provide data outside regular hours?** Yes, when the instrument,
  exchange/routing, account subscription, and request mode support it. For bar
  APIs, use `useRTH=0`; do not use `liquidHours` as the total availability gate.
- **Can an IBKR API client trade pre-market/after-hours?** Yes. It can use an
  explicit limit order with `outsideRth=true`, after validating the contract,
  TIF, destination, quote freshness, and price increment. In learn-ai, however,
  IBKR remains data/evidence only and the equivalent execution capability must
  be implemented through Alpaca Broker V2.
- **Does this have to change every strategy's timing?** No. Session eligibility
  belongs in execution policy; a strategy's explicit entry/exit times should
  continue to decide when it emits an action.
- **Is overnight the same as extended hours?** No. IBKR documents it as a
  separate venue with separate routing/data and order-lifecycle rules.
- **Does a limit order guarantee execution?** No. It protects price if filled;
  it may remain partially filled or not fill at all.

## Primary sources

All external sources cited in this note are official Interactive Brokers API,
product, Campus, or TWS user-guide pages. The current IBKR Campus pages supersede
the older `interactivebrokers.github.io/tws-api` documentation where the two
differ.
