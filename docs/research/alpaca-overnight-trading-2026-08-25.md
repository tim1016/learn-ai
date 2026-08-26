# Alpaca overnight / 24-by-5 trading — 2026-08-25

## Verdict

**Alpaca can support 24-by-5 US-equity trading, but learn-ai cannot get there by
only changing market orders to limit orders.** A direct Trading API order must
also carry `extended_hours=true`; the app must select an entitled overnight
quote feed, validate the symbol's current overnight attributes, respect the
overnight calendar, and handle overnight-specific halts and buying-power
rules. [Alpaca 24/5 Trading][trading-245] and [Create an Order][create-order]
are the controlling direct-Trading-API sources.

The current learn-ai Alpaca integration is deliberately paper-only and does not
send `extended_hours`, retain Alpaca's overnight asset attributes, or select an
overnight/BOATS market-data feed. Paper 24/5 trading is therefore feasible
after an implementation and paper qualification pass; live 24/5 trading is not
currently enabled by this repository.

## Direct Trading API contract

For a whole-share US-equity order intended to execute overnight, send:

```json
{
  "symbol": "SPY",
  "qty": "1",
  "side": "buy",
  "type": "limit",
  "limit_price": "640.00",
  "time_in_force": "day",
  "extended_hours": true
}
```

The required rules are:

- `type` must be `limit`, with an explicit `limit_price`.
- `time_in_force` must be `day` or `gtc`.
- `extended_hours` must be `true`; it defaults to `false`.
- A `day` order placed overnight remains eligible through that trade date's
  pre-market, regular, and after-hours sessions, then cancels at 8:00 p.m. ET
  if unfilled.
- A `gtc` order expires after 90 days and is canceled before a corporate action.

Sources: [24/5 Trading — order rules][trading-245], [Create an Order — request
schema][create-order], and [Orders at Alpaca — extended-hours orders][orders].

Changing the order to `limit` without setting `extended_hours=true` is not
sufficient: an order that is not designated for extended-hours execution is
queued for a later eligible session rather than participating overnight.

## Sessions and live symbol eligibility

Alpaca describes a continuous weekday sequence in Eastern Time:

| Session | Hours |
|---|---:|
| Overnight | 8:00 p.m.–4:00 a.m. |
| Pre-market | 4:00 a.m.–9:30 a.m. |
| Regular | 9:30 a.m.–4:00 p.m. |
| After-hours | 4:00 p.m.–8:00 p.m. |

The sequence begins Sunday at 8:00 p.m. and ends Friday at 8:00 p.m. The
overnight session follows the NYSE holiday calendar: it does not run on the
evening before a market holiday, but it runs the full eight hours on market
half-days. [Alpaca 24/5 Trading][trading-245]

Alpaca's current multi-market clock is `GET /v3/clock`; its market identifiers
include `BOATS` and `OCEA` for US overnight trading. A production session gate
can use that evidence alongside the holiday calendar rather than treating the
legacy regular-market clock as overnight authority. [Get Market Clock][market-clock]

The eligible universe is NMS securities. OTC securities and options are not
eligible overnight. "All NMS securities" is a universe statement, not a
guarantee for a particular symbol or session: compliance, risk, venue, halt,
and corporate-action controls may remove or pause a symbol. Before placing an
overnight order, retrieve the asset and require:

- active and ordinarily tradable US equity;
- the `overnight_tradable` asset attribute;
- no `overnight_halted` asset attribute; and
- for fractional orders, `fractionable` plus `fractional_eh_enabled`.

The [Get Assets API][assets] exposes those overnight and fractional
extended-hours attributes. Alpaca refreshes overnight eligibility from 7:05 to
7:45 p.m. ET and recommends a final sync between 7:45 and 8:00 p.m., ideally at
7:55 p.m. [Alpaca 24/5 Trading][trading-245]

An overnight-halted order may be accepted yet remain pending until trading
resumes. An accepted status is therefore not proof that the symbol can
currently execute.

## Overnight market-data matrix

Overnight data is available from 8:00 p.m. to 4:00 a.m. ET. Quote entitlement
is independent of order eligibility: getting a quote does not make the symbol
or account tradable.

| Direct Trading API plan | Feed | Latest quote | Latest trade | Historical overnight data |
|---|---|---|---|---|
| Algo Trader Plus | `boats` | Real-time BOATS | Real-time BOATS | `boats`, no 15-minute restriction stated on the 24/5 page |
| Free / Basic | `overnight` for latest endpoints | Real-time **indicative** quote | 15-minute delayed | `boats`, at least 15 minutes old; `overnight` is invalid for delayed historical requests |

Bars and snapshots are also available on the documented feeds. For live
streaming, Alpaca documents `v1beta1/boats` and `v1beta1/overnight`; attempting
to authenticate to a feed outside the account's subscription fails. Alpaca
recommends streaming instead of polling when freshness and performance matter.

Sources: [24/5 Trading — market data][trading-245], [Real-time Stock Data][stock-stream],
and [About Market Data API][market-data]. Basic is the default for both direct
Trading API paper and live accounts; Algo Trader Plus is a separate
subscription.

The free feed's bid/ask is explicitly **indicative**. A strategy should not
treat it as a guaranteed executable quote or infer fillability merely because
its limit crosses that bid/ask. The overnight risk disclosure also warns that
overnight venues may not publicly display prices and that an execution can be
worse than a price available elsewhere. [Extended Hours & Overnight Trading
Risk Disclosure][risk]

## Account and order-lifecycle caveats

- Direct Trading API accounts are enabled for 24/5 by default, but the account
  configuration can set `disable_overnight_trading=true`. Check it rather than
  assuming. [24/5 Trading][trading-245] and [Account Configurations][account-config]
- Alpaca can still reject an order for account authorization, restrictions, or
  insufficient tradable balance. [Create an Order][create-order]
- The current 24/5 page says overnight margin buying power is capped at 2x and
  that day-trading buying power does not apply. However, Alpaca removed the PDT
  and DTBP fields from the Trading API account schema on July 6, 2026. Do not
  hard-code removed `daytrading_buying_power`/PDT fields; consume the current
  account buying-power surface and preserve broker rejections. [24/5
  Trading][trading-245] and [July 2026 PDT/DTBP removal][pdt-removal]
- Overnight executions before midnight receive the next morning's trade date;
  settlement is T+1 from that assigned trade date. That also affects dividend
  eligibility around ex-dates. [24/5 Trading][trading-245]
- Overnight trading can be suspended without notice, and lower liquidity,
  wider spreads, partial fills, higher volatility, unlinked markets, and lack
  of Reg NMS price protection all need fail-safe handling. [Risk Disclosure][risk]

## Cross-broker quotes: IBKR data -> Alpaca execution

Using IBKR market data to price an Alpaca order is technically viable: the
quote/data provider does not need to be the execution broker. It is also the
direction that best fits learn-ai's current product boundary: IBKR is already a
read-only market-data source while Alpaca Broker V2 owns execution custody.
Reversing the direction (Alpaca data -> IBKR execution) would revive the
deprecated IBKR bot-control/order surface and is not the recommended product
path.

However, the current IBKR feed is not yet a safe overnight price source:

- `app/broker/ibkr/contracts.py::qualify_underlying` qualifies `SMART` stock
  contracts.
- `app/broker/ibkr/bars.py::stream_minute_bars` requests 5-second `TRADES`
  bars and emits closed one-minute OHLCV bars; it does not expose a live
  bid/ask quote.
- IBKR says overnight trading is an independent venue and that API market data
  should use the same `OVERNIGHT` exchange routing. Ordinary SMART-routed data
  can show different trade values. [IBKR API Overnight Trading][ibkr-api-overnight]
- IBKR's `OVERNIGHT` venue combines internal and external liquidity, including
  Blue Ocean, while Alpaca's overnight executions occur on BOATS. The sources
  overlap but the displayed book and executable liquidity are not guaranteed
  to be identical. [IBKR Overnight Trading][ibkr-overnight] and [Alpaca 24/5
  Trading][trading-245]

The conservative design is therefore: subscribe to venue-aligned IBKR
`OVERNIGHT` top-of-book bid/ask ticks, timestamp and freshness-check them, and
use them only to derive a spread- and slippage-capped Alpaca limit. Also compare
against Alpaca's own `boats` or indicative `overnight` quote when available.
Disagreement beyond a configured bound should block new exposure rather than
select one source silently. The Alpaca order must still independently pass its
own asset eligibility, halt, session, buying-power, and `extended_hours=true`
checks; an IBKR quote cannot prove any of those Alpaca facts.

## Direct Trading API versus Broker API

The two similarly named Alpaca 24/5 pages have different scopes:

| Scope | Enablement | TIF stated | Data entitlement |
|---|---|---|---|
| Direct Trading API (`/v2/orders`, learn-ai's integration) | Enabled by default unless disabled in account configuration | `day` and `gtc` | Free/Basic or Algo Trader Plus matrix above |
| Broker API partner platform | Contact Customer Success Manager to enable and price | `day` only; GTC described as future | Separately priced indicative feed / partner entitlement |

Sources: [direct Trading API 24/5][trading-245] and [Broker API 24/5][broker-245].
The Broker API page must not be used to override the direct Trading API request
contract. Conversely, a future learn-ai migration to Alpaca's Broker API would
need a new entitlement and contract review.

## Fractional-order ambiguity

Alpaca's current official pages do not form one internally consistent
fractional overnight contract:

- The direct 24/5 page says fractional trading is supported overnight and
  otherwise works like extended hours.
- The fractional page says market, limit, stop, and stop-limit fractional
  orders are supported with `day`, says fractional and notional limit orders
  work in extended hours, and requires an enabled fractional asset. The same
  page's final disclosure sentence nevertheless says fractional transactions
  can only use market orders during normal hours.
- The `POST /v2/orders` reference describes fractional `qty` and `notional` as
  market/`day` only, in conflict with the dedicated fractional page.
- Fractional short sales are prohibited, and Alpaca rejects certain overlapping
  same-symbol fractional sell sequences outside regular hours.

Sources: [Fractional Trading][fractional], [Create an Order][create-order], and
[Orders at Alpaca][orders].

The conservative implementation choice is to qualify whole-share overnight
orders first. If fractional overnight trading is added, require all three asset
eligibility signals, use quantity-based `day` limit orders initially, and pin
accepted/rejected paper payloads before claiming notional-limit or fractional
GTC support. This is a recommendation derived from the conflicting official
contracts, not an additional Alpaca rule.

## Current learn-ai gaps

Read-only inspection on 2026-08-25 found:

- `PythonDataService/app/broker/contract/models.py::BrokerOrderLeg` models
  market/limit and DAY/GTC but has no extended-hours instruction.
- `PythonDataService/app/broker/alpaca/adapter.py::to_alpaca_order_request`
  therefore never emits `extended_hours`.
- `PythonDataService/app/broker/contract/models.py::BrokerAsset` and
  `PythonDataService/app/broker/alpaca/adapter.py::from_alpaca_asset` discard the
  `overnight_tradable`, `overnight_halted`, and `fractional_eh_enabled`
  attributes needed for the pre-order gate.
- `PythonDataService/app/broker/alpaca/broker.py` advertises
  `supports_extended_hours=True` while declaring `data_feed="iex"`; no
  overnight/BOATS selector exists on that broker surface.
- The broker client/adapter still consume the legacy regular-market clock
  shape (`get_clock()` / `/v2/clock`); they do not request the current
  `/v3/clock` BOATS/OCEA phase evidence needed for an overnight session gate.
- `PythonDataService/app/broker/alpaca/config.py` rejects every mode except
  `paper`, so real-money overnight trading is intentionally unavailable.
- The manual-order asset gate in
  `PythonDataService/app/broker/alpaca/clerk/sqlite/manual_order_runtime.py`
  checks active/tradable US equity status but not overnight eligibility or halt
  state.

Accordingly, the existing `supports_extended_hours=True` capability is broader
than the current order and data plumbing can prove. It should not be interpreted
as evidence that learn-ai is already 24/5-ready.

[trading-245]: https://docs.alpaca.markets/us/docs/245-trading-for-trading-api
[create-order]: https://docs.alpaca.markets/us/reference/postorder
[orders]: https://docs.alpaca.markets/us/docs/orders-at-alpaca
[assets]: https://docs.alpaca.markets/us/reference/get-v2-assets-1
[stock-stream]: https://docs.alpaca.markets/us/docs/real-time-stock-pricing-data
[market-data]: https://docs.alpaca.markets/us/docs/about-market-data-api
[account-config]: https://docs.alpaca.markets/us/reference/patchaccountconfig-1
[risk]: https://files.alpaca.markets/disclosures/library/ExtHrsOvernightRisk.pdf
[broker-245]: https://docs.alpaca.markets/us/v1.1/docs/245-trading
[fractional]: https://docs.alpaca.markets/us/v1.1/docs/fractional-trading
[pdt-removal]: https://docs.alpaca.markets/us/changelog/2026-07-06-pdt-db49dba
[market-clock]: https://docs.alpaca.markets/us/reference/clock-1
[ibkr-api-overnight]: https://ibkrcampus.com/campus/ibkr-quant-news/api-overnight-trading/
[ibkr-overnight]: https://ibkrcampus.com/campus/trading-lessons/overnight-trading-in-tws/
