# Paper BTC quantity-fee evidence (#3311)

Captured read-only from Alpaca Paper on 2026-10-22 (America/Chicago).
Source: `GET /v2/account`, `GET /v2/positions`, and a newest-first walk of
`GET /v2/account/activities?page_size=100&direction=desc` continued by the
last activity ID until the final short page. All 27 pages (2,624 activities)
were read; the fixture retains every BTC fill and every CFEE row. No order
or account was changed. IDs are sanitized; numeric strings and vendor times
are unaltered. Raw vendor timestamps are converted by the normal adapter in
the regression test, never stored as internal application timestamps.

The complete 204 BTC fills and 204 CFEE records matched the Clerk's retained
activity identities and economics: none missing, changed or conflicting.
The 204 fills leave **0.000032112 BTC**. The 93 quantity-denominated CFEE rows
sum to **-0.000032112 BTC**, so their combined quantity is exactly zero.
The other 111 CFEE rows charge USD; they are not BTC quantity debits.
The current positions response is empty and cash equals equity; these facts
corroborate the activity evidence and are not used to infer a missing sale.

The first divergence is the first purchase: `1790559270045` ms UTC buys
0.000174984 BTC at 84034.69 USD. Its first quantity fee was created at
2026-09-28T01:34:36.129014Z and debits 0.000000438 BTC at the same price.
The Clerk retained that CFEE but its outside-money projection consumed only
FILL/PARTIAL_FILL. Decimal sums over the original broker strings establish
that the final difference is not float precision or missing fills.

Alpaca's [crypto fee specification](https://docs.alpaca.markets/docs/crypto-fees)
states that fees are charged in the received asset and shows a negative CFEE
quantity with zero USD net amount. A date-only fee cannot prove intraday FIFO
placement. This fixture proves complete quantity exhaustion, not partial-lot
cost allocation, a fee rate, or invented cash proceeds.

Regenerate input, the independent decimal output and manifest hashes from a
saved capture with the same shape, from `PythonDataService/`:

```sh
.venv/bin/python -m scripts.fixture_generators.outside_crypto_fees /path/to/capture.json
```

The checked-in sanitized `input.json` can also be supplied to reproduce the
oracle. It contains the full relevant population, capture time, pagination
exhaustion and broker account/position evidence. Re-capture requires the same
read-only exhausted walk on the Paper clerk using its effective saved profile.
Any later economic change requires a new fixture version; do not overwrite
this historical incident. Tolerance is exact decimal equality (`atol=0`,
`rtol=0`); the production reconciliation never uses a dust threshold.
