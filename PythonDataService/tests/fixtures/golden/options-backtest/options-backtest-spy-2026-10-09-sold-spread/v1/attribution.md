# options-backtest-spy-2026-10-09-sold-spread — the vendor's real answers for one SPY session, and the call credit spread a person works out from them

Receipt for #3110 (part C of #3095, covered sold legs in the Options Backtest). The first golden fixture of the feature,
`options-backtest-spy-2025-06-10`, proves bought legs on real data. This one proves a **sold leg and its cover**: the
vendor's real answers for SPY on Friday 2026-10-09, kept byte for byte, so that one test can follow them through the lake's
fetchers, the lake, the lake reader and the run core to the fills, fees, cash and collateral of a sold spread worked by hand.

## Source

Polygon (now Massive) REST API, `https://api.polygon.io`, four `GET` requests, recorded on **2026-10-13** with the owner's
key. The key rode in an `Authorization: Bearer` header and is in no URL, so it is in no recorded request. `manifest.json`
lists each request (method, path, query) with the HTTP status, the byte size and the sha256 of the body it answered with.

| File | Request | Bytes |
|---|---|---|
| `spy_minute_2026-10-09.json` | `/v2/aggs/ticker/SPY/range/1/minute/2026-10-09/2026-10-09?adjusted=false&sort=asc&limit=50000` | 86 188 |
| `spy_contracts_as_of_2026-10-09_expiring_2026-10-09.json` | `/v3/reference/options/contracts?underlying_ticker=SPY&as_of=2026-10-09&expiration_date=2026-10-09&limit=1000` | 83 340 |
| `O_SPY261009C00777000_minute_2026-10-09.json` | `/v2/aggs/ticker/O:SPY261009C00777000/range/1/minute/2026-10-09/2026-10-09?adjusted=false&sort=asc&limit=50000` | 33 499 |
| `O_SPY261009C00779000_minute_2026-10-09.json` | `/v2/aggs/ticker/O:SPY261009C00779000/range/1/minute/2026-10-09/2026-10-09?adjusted=false&sort=asc&limit=50000` | 33 843 |

Each body is exactly what the vendor sent: never re-saved through a JSON library, never reformatted. `manifest.json` here
is the recording's own manifest (the request list, the sha256 of each body, and the `facts` the recording script found).
`expected.json` is the answer key: the figures of each scene in the test, worked by hand from these bodies (see below).
`regenerate.py` is the script that made `manifest.json` and the four bodies on 2026-10-13; it was run through `ruff format`
afterwards and changed in nothing else. What ties these files to that recording is the sha256 of each body in
`manifest.json`, which the golden registry holds the files to.

## How the session and the two contracts were chosen

* **The session** is the latest one the tests can capture. The suite's lake fixtures fix the clock at 2026-10-12, 14:00 New
  York (`tests/options_backtest/_lake.py`), and the lake captures a session only after its New York date has passed
  (ADR 0078 decision 14); 2026-10-10 and 2026-10-11 are a weekend. The latest is **Friday 2026-10-09**, a full regular
  session (390 one-minute bars of SPY, one for every minute). It was chosen for being the newest, because the options plan
  reaches back two years from the day it is asked and that floor moves forward every day: this session stays recordable
  until about 2028-10-09, and after that these files are the only copy. It was chosen before it was looked at, and no other
  session was tried.
* **The expiry** is the session's own: SPY lists an expiry every weekday, and the setup's same-day rule picks it. The
  listing is recorded for that one expiry (366 contracts, one page).
* **The two contracts** are chosen by `regenerate.py` from the listing and the underlying's price, and by nothing else:
  the call at the money at the entry decision, and the call two listed strikes above it. The decision is on the bar that
  closes at 09:35 New York: SPY closed 776.55 there, which is 0.55 from 776 and 0.45 from 777, so the money is 777, and the
  two calls are `O:SPY261009C00777000` (sold) and `O:SPY261009C00779000` (bought, its cover): a call credit spread two
  dollars wide. Two strikes and not one, so that the setup's strike rule counts along the ladder more than one place.
* **Both contracts traded in the minutes the scenes need.** The script refuses to write a fixture unless each call has a bar
  stamped 09:35 (the minute the entry fills in) and 15:30 (the minute the exit fills in). The minutes of the stop were not
  known to the script: they come from the recorded closes, and the stop's percentage was chosen after reading the sold
  call's closes so that the stop is hit once, well after the entry and well before the exit (20 percent; it is no
  recommendation, and no other percentage was run against the code). A scratch run also asked the vendor for four other
  contracts of the same session to see how they traded. None of those answers is kept or used.

## Regeneration

```
POLYGON_API_KEY=<key> python tests/fixtures/golden/options-backtest/options-backtest-spy-2026-10-09-sold-spread/v1/regenerate.py <an empty folder>
```

Read-only (`GET` only), key from the environment (stripped of surrounding whitespace), sent in a header, never printed or
written, and in no error text it raises. It is fixture paperwork: no test and no CI job runs it against the vendor. The test
file runs it offline, with the vendor played by this recording, and holds it to writing these same files and this same
manifest, and to sending the key in a header only.

Regenerating makes a **new** recording, not these bytes again: the vendor's `request_id` differs on every call, and its data
for a past day can be revised. The hand-worked figures in `expected.json` and in
`tests/options_backtest/test_golden_spy_2026_10_09_sold_spread.py` would have to be worked again from the new bytes, and the
change made as a new fixture version. A regenerated recording that does not agree with the hand-worked figures is a finding
to be classified (a different bar, a different rule), never a reason to change a figure to make a test pass.

## Parameters and assumptions

- **Unadjusted.** Every aggregates request asks `adjusted=false` (each answer says `"adjusted":false`), and the lake stores
  raw prices. Nothing here reads an adjusted price.
- **A bar's `t` is its START**, in epoch milliseconds UTC. The bar stamped `t = 1791552840000` (09:34:00 New York) holds
  the trades of 09:34:00 up to but not including 09:35:00, and so closes at 09:35. The first regular-session bar of SPY is
  stamped 13:30:00 UTC (09:30 New York).
- **Trade bars only**: open, high, low, close, volume, vwap and trade count of the *trades*. The plan carries no bid and no
  ask, so nothing here says what a quote was.
- **A minute in which nothing traded has no bar.** Neither call has a bar stamped 09:32, 09:36, 09:39, 09:43 or 09:46
  (the same five minutes for both). Nothing fills them.
- **Extended hours are in the answers**: SPY has 844 bars from 04:00 to 19:59 New York, 390 of them in the regular session;
  each call has 400 bars, 385 of them in the regular session (the last stamped 16:14).
- **SPY's volume is fractional in the early hours** (the first bar's `v` is `5967.495636`, as the vendor has sent since
  2026-02-23). The stock pipeline takes the answer as it is, and the test reads the 390 regular-session bars back price
  for price.
- **The listing was recorded with `expiration_date=2026-10-09` added**, to keep it to one page of 366 contracts. The lake's
  own fetcher asks for the whole listing as of the date, with no expiry filter. So this fixture proves the reading of the
  vendor's *answer* (366 rows, 183 calls and 183 puts, every one American and 100 shares, all expiring 2026-10-09), and **not
  the paging** of a whole listing.
- **Whole-dollar strikes near the money**: the listed calls from 770 to 784 are one dollar apart, so "two strikes above the
  money" is two dollars.

## Reference kind

By the README's taxonomy this is **`hand_computed`**, with observed market input. The *inputs* are observed from the vendor
(layer 1, `market_input.source = polygon`). The *expected figures* are not the vendor's output and not our engine's: they are
worked by hand, with arithmetic on the recorded prices, from those same bytes (layer 3, independent of the code they check).
The rules they apply are the owner's, stated in #3095 and ADR 0078 decision 4 (layer 2): a rule is read on a bar's close, an
order decided there fills at the next bar's open, a sold leg opens at the sale price and closes at the buy price, a sold
leg's stop is a rise above what it was sold for, and the collateral is the lowest point of the summed exercise payoff with
the premiums left out (ADR 0076 decision 3).

## The setup and the answer key, worked by hand

The setup: sell **2** at-the-money calls and buy **2** calls two listed strikes above them, for today's expiry; entry at
09:35 (5 minutes after the open); exit by the clock at 15:30 (30 minutes before the close); a partial square-off; slippage
**0.3 percent** (the page's default) and the price step 0.01; a fee of **0.65 a contract** on every fill; 10 000.00 of cash.
The key keeps three scenes, `entry`, `stop_on_the_sold_leg` and `exit`, from two runs: the spread entered and left by the
clock (`entry` and `exit`), and the same spread with a stop of 20 percent on the sold leg (`stop_on_the_sold_leg`). Every
figure is exact `Decimal` arithmetic on recorded prices, so the tolerance is zero.

**The bars used** (each named by its START; open, high, low, close):

| Contract | Stamp | Open | High | Low | Close |
|---|---|---|---|---|---|
| SPY | 09:34 | 776.57 | 776.77 | 776.54 | 776.55 |
| 777 call | 09:35 | 1.00 | 1.07 | 0.94 | 0.98 |
| 779 call | 09:35 | 0.35 | 0.38 | 0.33 | 0.33 |
| 777 call | 11:18 | 1.10 | 1.26 | 1.09 | 1.20 |
| 777 call | 11:19 | 1.19 | 1.28 | 1.14 | 1.15 |
| 777 call | 15:30 | 1.90 | 1.92 | 1.84 | 1.88 |
| 779 call | 15:30 | 0.32 | 0.32 | 0.28 | 0.31 |

**The entry** (scene `entry`). The order is decided at 09:35 (1791552900000), on the bar stamped 09:34, and fills at the open
of the bar stamped 09:35 (it closes at 09:36, 1791552960000).

1. The sale of the 777 call: 1.00 x (1 - 0.3/100) = 0.997, rounded **down** to the step: **0.99**. It brings in
   0.99 x 100 x 2 = 198.00, less the fee 0.65 x 2 = 1.30: **+196.70**.
2. The purchase of the 779 call: 0.35 x (1 + 0.3/100) = 0.35105, rounded **up**: **0.36**. It costs 0.36 x 100 x 2 = 72.00
   and the fee 1.30: **-73.30**.
3. Cash 10 000.00 + 196.70 - 73.30 = **10 123.40**. Fees **2.60**.
4. Collateral. Held: 2 sold 777 calls and 2 bought 779 calls. Above 779 the pair pays 2 x (p - 779) - 2 x (p - 777) = -4 per
   share, and that is the worst it gets, so the loss is bounded at 4 x 100 = **400.00**, the width of the spread. 10 123.40
   covers it, so the group is taken.

**The exit by the clock** (scene `exit`). 15:30 is 30 minutes before the 16:00 close (1791574200000). The orders are decided
there and fill at the open of the bar stamped 15:30 (it closes at 15:31, 1791574260000). The sold leg is bought back first.

1. The buy-back of the 777 call: 1.90 x 1.003 = 1.9057, rounded up: **1.91**. 1.91 x 100 x 2 = 382.00 and the fee 1.30 go
   out: **-383.30**.
2. The sale of the 779 call: 0.32 x 0.997 = 0.31904, rounded down: **0.31**. 0.31 x 100 x 2 = 62.00 comes in, less the fee
   1.30: **+60.70**. It may leave because no sold call is left to cover.
3. Cash 10 123.40 - 383.30 + 60.70 = **9 800.80**. Collateral **0.00**. Fees **2.60**.
4. Profit of the sold leg: (0.99 - 1.91) x 100 x 2 - 1.30 - 1.30 = **-186.60**. Of the bought leg: (0.31 - 0.36) x 100 x 2
   - 1.30 - 1.30 = **-12.60**. Together **-199.20**, and 10 000.00 - 199.20 = 9 800.80.

**The stop on the sold leg** (scene `stop_on_the_sold_leg`). The entry is the one above.

1. The stop level is what the leg was **sold for** (the price filled, 0.99, not the 1.00 seen) moved up by the rule:
   0.99 x (1 + 20/100) = **1.188**. It is hit by a minute that **closes** at or above it; a high inside a minute never does.
2. From the bar stamped 09:35 on, the 777 call's closes stay under 1.188 up to the bar stamped 11:17 (the highest is 1.15,
   the bar stamped 10:58). The bar stamped 11:18 closes at **1.20**, so the stop is hit at its close, 11:19
   (1791559140000), and decides the sold leg's close.
3. The buy-back fills at the open of the bar stamped 11:19 (it closes at 11:20, 1791559200000): 1.19 x 1.003 = 1.19357,
   rounded up: **1.20** (the level was 1.188 and the close that hit it 1.20; the fill is the next open). 1.20 x 100 x 2 =
   240.00 and the fee 1.30 go out: **-241.30**. Cash 10 123.40 - 241.30 = **9 882.10**.
4. The stop closes only the leg that carries it. The 779 call stays held and a bought call has no requirement, so the
   collateral falls from 400.00 to **0.00**. Profit of the sold leg: (0.99 - 1.20) x 100 x 2 - 1.30 - 1.30 = **-44.60**.
5. At 15:30 the 779 call is sold as above, at 0.31, for +60.70: cash 9 882.10 + 60.70 = **9 942.80**. Its profit is -12.60.
   Together -44.60 - 12.60 = **-57.20**, and 10 000.00 - 57.20 = 9 942.80.

## What this fixture does not prove

- **That a broker would have taken these orders, or that Alpaca requires 400.00.** The replay fills each leg from its own
  trade prints, one minute bar at a time, and treats the two legs of a minute as one simulated group; minute bars cannot show
  which printed first, so this is a modelling choice and not observed coverage. The collateral is today's published rule
  worked by this system. No option order from this system has reached Alpaca.
- **Quotes.** The plan has no bid and no ask: every price is a trade's.
- **A stop on the bought leg** (a sold leg closed with its protection), momentum, re-entry, a refusal for cash, a leg
  marked at the close, a spread of puts, an iron condor, two expiries, a half day and estimated fees (a fee per contract
  is charged here). None is reached by this fixture; the replay's own tests work those it has rules for on markets their
  authors made up.
- **The expiry itself.** The contracts expire on the day at 16:00 and the replay leaves at 15:30, so no exercise,
  assignment or expiry value is reached.
- **The paging of a whole listing** (above): one page only.
- **The lake against a real Postgres catalog.** The catalog in the test is the suite's in-memory stand-in.
- **The stock pipeline's reading of every kind of vendor answer.** It reads this one session.
- **That the vendor will answer the same way tomorrow.** It records what it said once.
