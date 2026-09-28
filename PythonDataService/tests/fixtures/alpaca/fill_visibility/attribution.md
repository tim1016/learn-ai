# Fill-visibility measurement fixture

`paper-btcusd-2026-09-28.json` is the verbatim report of
`scripts/measure_fill_to_cash_visibility.py roundtrips` (#2487), run on the
owner's Alpaca **paper** account (`PA-FIXTURE-PAPER` below; the real account
number is sanitized per this directory's convention) on 2026-09-28
01:52–02:04 UTC (2026-09-27 local), a Sunday, using BTC/USD round trips of
$15 notional because equities were closed.

## What it measures

For each `fill`/`partial_fill` event received on the account's
`trade_updates` stream: the first `GET /v2/account` read, **dated when its
request was issued** (the same discipline as `LiveEnvelopeSync.observe`'s
`observed_at_ms`, #2441), whose `cash` reflects the fill. Bounds are measured
from the fill event's stream receipt — the instant the Clerk could have
recorded the fill — because `FILL_VISIBILITY_GRACE_MS`
(`app/broker/alpaca/clerk/live_envelope.py`) is exactly the margin between
that instant and a later read whose cash is trusted to include the fill.

What a reflecting read *proves* depends on where its answer landed relative
to the receipt (schema v2 classification, per the #2549 review):

- `led_stream` — the read answered at/before the receipt: the cash was
  provably visible before the Clerk could have recorded the fill. Bound 0.
- `resolved` — the read was issued at/after the receipt: bound =
  issue time − receipt (the envelope's issue-time dating; the true transition
  is somewhere in the preceding read gap, so this too is an upper bound).
- `interval_censored_at_receipt` — the read was issued before the receipt
  but answered after it: the cash was visible no later than the answer and
  possibly before the receipt. Bound = answer time − receipt. A read whose
  answer postdates the receipt proves nothing about the receipt instant, so
  these are **not** zero-delay observations.

## Result

- 45 fill events: 2 `led_stream`, 42 `interval_censored_at_receipt`,
  1 `not_visible_within_window`.
- The two provable leads answered 24 ms before their events. The 42
  interval-censored fills' first reflecting reads answered 4–329 ms after
  their events (p50 97 ms): visibility at the receipt instant is unknown for
  them, bounded above by 329 ms. Distribution over the 44 measured bounds:
  p50 80 ms, p95 244 ms, max 329 ms.
- No fill in this run — or in the same day's three rehearsal runs (43, 33,
  and 34 fills, issued-time dating, worst 401 ms) — showed cash lagging its
  stream event by more than a read's own round trip. The bounds are dominated
  by read cadence: reads were requested at 150 ms but achieved p50 291 ms /
  p95 438 ms / max 489 ms apart (`read_cadence`).
- The one censored fill (a buy whose cash level had already dropped before
  its pre-submission baseline read) is an attribution artifact of the
  round-trip fill source, not evidence of cash lag; see
  `docs/references/alpaca-live-envelope.md` § "The fill-visibility grace".
- The report's full `read_series` (196 reads) makes every bound recomputable
  from the fill records alone.

## Provenance and sanitization

Account number and all UUIDs (order, execution, event, client-order ids) are
replaced with deterministic `fixture-*` tokens by first appearance. All
quantities, prices, cash values, and timestamps (int64 ms UTC; the raw
events' ISO-8601 temporal fields are stored as parsed `*_ms` integers) are
the real wire data. Nothing in a `trade_updates` event is a secret.

The fixture is outside the numerical golden manifest — like the other
fixtures in this directory it is broker evidence, not tolerance-pinned math —
but `tests/broker/alpaca/clerk/test_live_envelope.py` loads it to pin the
grace's safety floor (measured maximum × documented cushion factor).

## Regeneration

Read-only on any Alpaca account, order-placing on paper only::

    ALPACA_API_KEY_ID=... ALPACA_API_SECRET_KEY=... \
    python3 scripts/measure_fill_to_cash_visibility.py roundtrips \
        --count 16 --symbol 'BTC/USD' --notional 15 --poll-ms 150 --out report.json

On a trading day, `observe` measures passively alongside normal account
activity (equity fills included) without placing anything; fills whose
attribution windows overlap another fill's are censored
(`overlapping_fills`) rather than measured, because the reflection band
attributes cash steps to exactly one fill.
