# lake-studies-wmt-split-adjusted-grid-2024-02-20 — a real split-adjusted tape against the lake's price grid

Receipt for #3522 (the Signal study reads the lake). The lake stores prices on LEAN's 1/10,000 grid (`to_deci_cent` rounds half-up);
the provider path the study replaced kept the vendor's floats as they came. The first comparison tape was synthetic, with five and
six decimals, and showed large movements (the best threshold flipping at $12, the best Sharpe moving 0.49 at $2). Whether a real
tape ever carries a price finer than the grid was the open question; this is the vendor's real answer for a split-adjusted name,
kept byte for byte, so that one test can answer it.

## Source

Polygon (now Massive) REST API, `https://api.polygon.io`, one `GET`, recorded on **2026-10-24** with the owner's key, after the
owner approved the call on #3522. The key rode in an `Authorization: Bearer` header and is in no URL, so it is in no recorded
request; the saved body holds no key (the recording script refuses a body that does) and no `next_url`. The body's `request_id` is
the vendor's own opaque id for the request, the same field the bodies already recorded under `golden/options-backtest/` carry. `recording.json`
lists the request (method, path, query), the HTTP status, the date, the sha256 and the size of the body.

| File | Request | Bytes |
|---|---|---|
| `wmt_minute_adjusted_2024-02-20_2024-02-23.json` | `/v2/aggs/ticker/WMT/range/1/minute/2024-02-20/2024-02-23?adjusted=true&sort=asc&limit=50000` | 212 753 |

The body is exactly what the vendor sent: never re-saved through a JSON library, never reformatted.

## Why this tape

Walmart split 3-for-1 with an execution date of 2024-02-26 (stated by the owner; no request was made for it). The four sessions
2024-02-20 to 2024-02-23 are the week before, so the vendor's `adjusted=true` answer divides every price by three: a price that
ended on a cent no longer does, which is where a grid effect would show. NVDA's 10:1 split would stay on the grid and hide it, so
it was not used.

## What was measured

- **2,226 minute bars, 8,904 open, high, low and close values, between $56.0186 and $60.45: none finer than 1/10,000.** The vendor
  writes adjusted prices to at most four decimals (5,975 of the values use all four), so `to_deci_cent` changes none of them.
- **Through the real lake writer, committed-byte admission and reader, every output of the Signal study is identical on the provider
  path and the lake path** for `rsi_14` and `momentum_5m` on one-minute bars: the same bars, prices and volumes, the same best
  threshold and cost, Sharpe, active share, effective sample and grade; the strict-float comparison differs by 0.0.
- **A positive control, in the test:** the same bars with every price multiplied by 1.0000337 and rounded to six decimals (off the
  grid) do move the outputs, so the comparison can see a grid effect when there is one.

It does **not** cover: other split ratios (10:1, a reverse split), other tickers, prices below $1, or a window long enough for
the walk-forward folds (four sessions give none: the report's `walk_forward` holds no window). At five and fifteen minutes this
window gives the engine nothing to answer (it refuses a series whose valid targets fall under 70% at five minutes, and one under
500 bars at fifteen, the same on both paths), so only the bars themselves are compared there.

## Parameters and assumptions

- **Split-adjusted.** The request asks `adjusted=true` (the answer says `"adjusted":true`), and the lake is read in its
  `polygon_split_adjusted` tree. The test serves the vendor's one answer for both the raw and the adjusted request, and serves the
  corporate-action feed with the split above; neither was requested.
- **A bar's `t` is its START**, in epoch milliseconds UTC. The first bar is 04:00 New York on 2024-02-20.
- **A minute in which nothing traded has no bar.** Nothing fills it, on either side.

## Reference kind

By the README's taxonomy this is **`vendor_observed`**, so it is not externally certified. The tape is the vendor's (layer 1,
`market_input.source = polygon`); the claim is a fact about it (no price finer than the grid) and an equality between two paths of
our own code on it, not a number a second implementation produced.

## Tolerance

`atol=1e-9, rtol=0` on the study's outputs, the repo's strict-float default. Prices and volumes are compared exactly. The measured
difference is 0.0 on every output, because both paths run the same arithmetic on identical bars.

## Regeneration

```
POLYGON_API_KEY=<key> python tests/fixtures/golden/lake-studies/regenerate.py \
  tests/fixtures/golden/lake-studies/lake-studies-wmt-split-adjusted-grid-2024-02-20/v1 <an empty folder>
```

Read-only (`GET` only), key from the environment, sent in a header, never printed or written. It is fixture paperwork: no test and
no CI job runs it, and it has **not been run** in this form; the recording itself was made on 2026-10-24 by a one-off script that
sent the same request. Regenerating makes a **new** recording, not these bytes again (the `request_id` differs on every call, and
a vendor re-adjusts a past day if it is revised), and the figures in `recording.json` and the literals in
`tests/research/test_signal_study_lake_vs_provider.py` would have to be measured again, as a new fixture version.
