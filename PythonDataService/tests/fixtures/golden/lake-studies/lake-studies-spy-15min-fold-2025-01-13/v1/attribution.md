# lake-studies-spy-15min-fold-2025-01-13 — Polygon's own 15-minute SPY aggregates against our fold of its minute bars

Receipt for #3522 (the Signal study reads the lake). The study folds the lake's one-minute bars to the requested length
with `fold_minute_bars`; the provider path it replaced got the vendor's own N-minute aggregates. Until this recording that
equivalence was an assumption, asserted only against a stand-in we wrote ourselves. This is the vendor's real answer for one
symbol and one week, kept byte for byte, so that one test can hold the fold to it.

## Source

Polygon (now Massive) REST API, `https://api.polygon.io`, one `GET`, recorded on **2026-10-24** with the owner's key, after the
owner approved the call on #3522. The key rode in an `Authorization: Bearer` header and is in no URL, so it is in no recorded
request; the saved body holds no key (the recording script refuses a body that does) and no `next_url`. The body's
`request_id` is the vendor's own opaque id for the request, the same field the bodies already recorded under `golden/options-backtest/` carry.
`recording.json` lists the request (method, path, query), the HTTP status, the date, the sha256 and the size of the body.

| File | Request | Bytes |
|---|---|---|
| `spy_15min_2025-01-13_2025-01-17.json` | `/v2/aggs/ticker/SPY/range/15/minute/2025-01-13/2025-01-17?adjusted=false&sort=asc&limit=50000` | 31 673 |

The body is exactly what the vendor sent: never re-saved through a JSON library, never reformatted.

## What it is compared with

The repo's own SPY minute capture, `tests/fixtures/polygon_capture/spy_minute_2025-01-13_2025-01-17/`: 4,062 one-minute bars
for the same five sessions, `adjusted=false`, extended hours included, captured on 2026-05-20 (its own `attribution.md`
describes it). `recording.json` pins the sha256 of that capture's `bars.json` under `compared_with`, and the test asserts it, so
the comparison cannot go on holding a capture that has been replaced.

## What was measured

The capture's minute bars, served at the vendor's HTTP seam and taken through the real lake writer, committed-byte admission and
reader, then folded by `fold_minute_bars(…, 15)`, equal the vendor's 15-minute aggregates **bar for bar: 320 of 320, open, high, low,
close and volume, with no bar on one side only**. That holds for the whole day (64 bars a session, 04:00 to 19:45 New York) and for
regular hours alone (130 bars, 09:30 to 15:45). 135 of the 320 buckets, all of them extended hours, hold fewer than 15 of their
minutes (738 minutes missing in all, where nothing printed); the vendor's volume still equals the sum of the minutes the capture
holds, so a minute that traded without a minute bar of its own is not what separates the two here.

It does **not** cover: any other symbol (an illiquid name is where a minute without a bar and a trade in the 15-minute volume
would be likeliest), a half-day or a week with a clock change, split-adjusted aggregates, or any other bar length. The fold's
alignment for the other lengths that divide the hour is proven against an independent stand-in, not against the vendor.

## Parameters and assumptions

- **Unadjusted.** The request asks `adjusted=false` (the answer says `"adjusted":false`), and the lake is read in its `raw` tree.
- **A bar's `t` is its START**, in epoch milliseconds UTC. The first bar is stamped 09:00 UTC on 2025-01-13, which is 04:00 New York.
- **A bucket with no minute has no bar.** Nothing fills it, on either side.
- **A date-string window.** The request names only dates. The last bar is the one starting at 19:45 New York on 2025-01-17, so the
  end date arrived as the whole New York day. One winter week does not settle how the vendor reads a date string in general.

## Reference kind

By the README's taxonomy this is **`vendor_observed`**, so it is not externally certified: the vendor's aggregation is opaque,
and what is proven is an observed equality on one symbol and one week, not a rule. The input is the repo's own capture (layer 1,
`market_input.source = polygon`); the oracle is the vendor's aggregate (layer 3); the rule applied (a bucket is the minutes whose
start falls in a New York wall-clock interval of N minutes, open the first, close the last, volume the sum) is the fold's own,
documented on `fold_minute_bars`.

## Tolerance

None: `atol=0, rtol=0`. Prices are the vendor's decimals (two places or fewer here) and volumes are whole shares; the fold takes the
first, the largest, the smallest, the last and the sum of the same decimal numbers, so equality is exact. A tolerance would only hide
a missing minute or a misplaced bucket.

## Regeneration

```
POLYGON_API_KEY=<key> python tests/fixtures/golden/lake-studies/regenerate.py \
  tests/fixtures/golden/lake-studies/lake-studies-spy-15min-fold-2025-01-13/v1 <an empty folder>
```

Read-only (`GET` only), key from the environment, sent in a header, never printed or written. It is fixture paperwork: no test and
no CI job runs it, and it has **not been run** in this form; the recording itself was made on 2026-10-24 by a one-off script that
sent the same request. Regenerating makes a **new** recording, not these bytes again (the `request_id` differs on every call and the
vendor can revise a past day), and the figures in `recording.json` and the literals in
`tests/research/test_signal_study_lake_vs_provider.py` would have to be measured again, as a new fixture version.
