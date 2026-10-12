# options-backtest-spy-2025-06-10 — the vendor's real answers for one SPY session, and the trade a person works out from them

Receipt for #3030 (the Options Backtest). Every other test of the feature uses data its author made up. This fixture is
the vendor's real answers for SPY on Tuesday 2025-06-10, kept byte for byte, so that one test can follow them through the
lake's fetchers, the lake, the lake reader and the run core to a trade worked by hand.

## Source

Polygon (now Massive) REST API, `https://api.polygon.io`, five `GET` requests, recorded on **2026-10-12** with the
owner's Options Starter key. The key rode in an `Authorization: Bearer` header and is in no URL, so it is in no recorded
request. `manifest.json` lists each request (method, path, query) with the HTTP status, the byte size and the sha256 of the
body it answered with.

| File | Request | Bytes |
|---|---|---|
| `spy_minute_2025-06-10.json` | `/v2/aggs/ticker/SPY/range/1/minute/2025-06-10/2025-06-10?adjusted=false&sort=asc&limit=50000` | 79 683 |
| `spy_contracts_as_of_2025-06-10_expiring_2025-06-13.json` | `/v3/reference/options/contracts?underlying_ticker=SPY&as_of=2025-06-10&expiration_date=2025-06-13&limit=1000` | 71 965 |
| `O_SPY250613C00600000_minute_2025-06-10.json` | `/v2/aggs/ticker/O:SPY250613C00600000/range/1/minute/2025-06-10/2025-06-10?adjusted=false&sort=asc&limit=50000` | 31 406 |
| `O_SPY250613P00600000_minute_2025-06-10.json` | `/v2/aggs/ticker/O:SPY250613P00600000/range/1/minute/2025-06-10/2025-06-10?adjusted=false&sort=asc&limit=50000` | 32 370 |
| `O_SPY250613C00680000_minute_2025-06-10.json` | `/v2/aggs/ticker/O:SPY250613C00680000/range/1/minute/2025-06-10/2025-06-10?adjusted=false&sort=asc&limit=50000` | 144 |

Each body is exactly what the vendor sent: never re-saved through a JSON library, never reformatted. `manifest.json` here is
the recording's own manifest (the request list, the sha256 of each body, and a few `facts` the recording script found).
`expected.json` is the answer key: the figures of each traded scene in the test, worked by hand from these bodies (see below).
`regenerate.py` is a tidied version of the script that made `manifest.json` and the five bodies, and **it has not been run in
this form**. The recording itself was made on 2026-10-12 by the original script, which sent the same requests (the same paths
and queries, the key in the same header) and wrote the same manifest fields. What ties these files to that recording is the sha256
of each body in `manifest.json`, which the golden registry holds the files to.

## Regeneration

```
POLYGON_API_KEY=<key> python tests/fixtures/golden/options-backtest/options-backtest-spy-2025-06-10/v1/regenerate.py <an empty folder>
```

Read-only (`GET` only), key from the environment (stripped of surrounding whitespace), sent in a header, never printed or
written, and in no error text it raises. It is fixture paperwork: no test and no CI job runs it, and it has not been run in this
tidied form (above), so run it into an empty folder and read the result before trusting it.

Regenerating makes a **new** recording, not these bytes again: the vendor's `request_id` differs on every call, and its data
for a past day can be revised. The hand-worked figures in `tests/options_backtest/test_golden_spy_2025_06_10.py` would have to
be worked again from the new bytes, and the change made as a new fixture version. The options plan reaches back two years from
the day it is asked, and that floor moves forward every day: after about 2027-06-10 this session can no longer be recorded at all, and
these files are then the only copy.

## Parameters and assumptions

- **Unadjusted.** Every aggregates request asks `adjusted=false` (each answer says `"adjusted":false`), and the lake stores raw
  prices. Nothing here reads an adjusted price.
- **A bar's `t` is its START**, in epoch milliseconds UTC. The bar stamped `t = 1749562440000` (09:34:00 New York) holds the trades
  of 09:34:00 up to but not including 09:35:00, and so closes at 09:35. This is the vendor's convention as probed on 2026-10-12 and
  as the recording shows: the first regular-session bar of SPY is stamped 13:30:00 UTC (09:30 New York).
- **Trade bars only**: open, high, low, close, volume, vwap and trade count of the *trades*. The plan carries no bid and no ask,
  so nothing here says what a quote was.
- **A minute in which nothing traded has no bar.** `O:SPY250613C00600000` has no bar stamped 11:28 or 11:29, and no bar
  stamped 12:16, 13:34 and others; the put has none stamped 10:09 and others. Nothing fills them.
- **Extended hours are in the answers**: SPY has 830 bars from 04:00 to 19:59 New York, 390 of them in the regular session
  (09:30 to 15:59, one for every minute); the options print until 16:15 (the call's last bar is stamped 16:13, the put's 16:14).
- **The listing was recorded with `expiration_date=2025-06-13` added**, to keep it to one page of 316 contracts. The lake's own
  fetcher asks for the whole listing as of the date, with no expiry filter. So this fixture proves the reading of the vendor's
  *answer* (316 rows, 158 calls and 158 puts, every one American and 100 shares, all expiring 2025-06-13), and **not the paging**
  of a whole listing.
- **`O:SPY250613C00680000` was listed and did not trade that day.** The vendor's answer for it is `status: "OK"`, the ticker,
  `resultsCount: 0`, `queryCount: 0` and no `results` key. The vendor answers exactly so for a contract that never existed too, so
  the lake stores it as an empty day only because the stored listing for the date holds the contract.
- **The underlying closed 600.375 in the bar that ends at 09:35 New York** (stamped 09:34, `t = 1749562440000`; open 600.71, high
  600.76, low 600.29). The listed call and put strikes near it are one dollar apart (590, 591, ... 610), so the strike nearest
  the money is 600.

## Reference kind

By the README's taxonomy this is **`hand_computed`**, with observed market input. The *inputs* are observed from the vendor
(layer 1, `market_input.source = polygon`). The *expected trade* is not the vendor's output and not our engine's: it is worked
by hand, with arithmetic on the recorded prices, from those same bytes (layer 3, independent of the code it checks). The rules it
applies (a rule is read on a bar's close, an order decided there fills at the next bar's open, a bar is named by its close) are
the owner's, stated in #3030 (layer 2).

## The expected output, worked by hand

The test asserts each figure below as a literal; none is computed by the code under test. Exact `Decimal` arithmetic on recorded
prices, so the tolerance is zero.

One leg: buy one at-the-money call expiring this week; entry 09:35 New York, exit by the clock at 10:00; no stop, no momentum,
no re-entry, no slippage, no fee; 10 000.00 of cash.

1. The bar closing at 09:35 is the one stamped 09:34: SPY closes at 600.375. The listed strikes around it are 600 and 601, 0.375 and
   0.625 away, so the money is 600. The listing's one expiry, Friday 2025-06-13, is the last session of its week. The contract is
   `O:SPY250613C00600000`. The order is decided at 1749562500000.
2. It fills at the **open** of the next bar, the one closing at 09:36 (1749562560000), stamped 09:35: `o 4.05`. Bought at 4.05,
   costing 4.05 x 100 x 1 = 405.00.
3. The exit time 10:00 (1749564000000) decides the sale. It fills at the open of the bar closing at 10:01 (1749564060000), stamped
   10:00: `o 4.09`. Sold at 4.09, bringing in 409.00.
4. Profit = (4.09 − 4.05) x 100 x 1 = **4.00**. Cash 10 000.00 − 405.00 + 409.00 = **10 004.00**.

More scenes are worked the same way in the test, and their figures are in `expected.json`: the same trade with three contracts
(1215.00 in, 1227.00 out, profit 12.00); the at-the-money put at the same minutes (3.37 in, 3.15 out, −22.00); an exit decided at
11:28, whose next two minutes have no call bar, selling at the open of the first minute that has one (4.40, profit 35.00); an entry
one strike below the money at 09:37 (4.19 in, profit −10.00); and an entry at 09:58, when the underlying closes exactly between two
strikes, which takes the lower (4.07 in, profit 2.00). One more is asserted in the test only: an entry decided at 11:28 into a minute
the call did not trade, which is dropped with no money moved.

## What this fixture does not prove

- **The paging of a whole listing** (above): one page only.
- **Rounding to a price step.** No recorded price is finer than one cent (the options) or four decimals (the underlying), so
  rounding a price to the option price step, or to the lake's grid of ten-thousandths of a dollar, is not exercised by this
  fixture, and a break in either would not show here.
- **The stock pipeline's reading of every kind of vendor answer.** It reads this one session; fractional volume (since
  2026-02-23), an adjusted request, a split day and a day with a halt are not here.
- **The lake against a real Postgres catalog.** The catalog in the test is the suite's in-memory stand-in.
- **Any rule beyond the plainest on real data**: stops, momentum, re-entries, a half day, a leg left open at the close, estimated
  fees (no rate is published before 2026-07-01), and an option that expires on the day.
- **That the vendor will answer the same way tomorrow.** It records what it said once.
