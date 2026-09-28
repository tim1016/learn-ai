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
`observed_at_ms`, #2441), whose `cash` reflects the fill. Delays are measured
from the fill event's stream receipt — the instant the Clerk could have
recorded the fill — because `FILL_VISIBILITY_GRACE_MS`
(`app/broker/alpaca/clerk/live_envelope.py`) is exactly the margin between
that instant and a later read whose cash is trusted to include the fill.

## Result

- 45 fill events: 44 `visible_before_receipt`, 1 censored.
- Every resolvable fill's cash effect was already readable **before** the
  stream event arrived (cash led by 35–425 ms, median 222 ms). The measured
  post-receipt visibility delay is therefore 0 ms for all 44; a read issued
  at the receipt instant already reflects the fill.
- The one censored fill (a buy whose cash level had already dropped before
  its pre-submission baseline read) is an attribution artifact of the
  round-trip fill source, not evidence of cash lag; see
  `docs/references/alpaca-live-envelope.md` § "The fill-visibility grace".
- Read cadence honesty: the poller was asked for 150 ms but achieved p50
  291 ms / p95 438 ms / max 489 ms per read (`read_cadence`). The report's
  full `read_series` (196 reads) makes every delay recomputable from the
  fill records alone.
- Earlier rehearsal runs the same day (43, 33, and 34 fills) agree: worst
  post-receipt delay observed across all runs was 401 ms, and no fill ever
  showed cash lagging its stream event.

## Provenance and sanitization

Account number and all UUIDs (order, execution, event, client-order ids) are
replaced with deterministic `fixture-*` tokens by first appearance. All
quantities, prices, cash values, and int64 ms UTC timestamps are the real
wire data. Nothing in a `trade_updates` event is a secret.

The fixture is outside the numerical golden manifest — like the other
fixtures in this directory it is broker evidence, not tolerance-pinned math —
but `tests/broker/alpaca/clerk/test_live_envelope.py` loads it to pin the
grace's safety floor (measured maximum + documented cushion).

## Regeneration

Read-only on any Alpaca account, order-placing on paper only::

    ALPACA_API_KEY_ID=... ALPACA_API_SECRET_KEY=... \
    python3 scripts/measure_fill_to_cash_visibility.py roundtrips \
        --count 16 --symbol 'BTC/USD' --notional 15 --poll-ms 150 --out report.json

On a trading day, `observe` measures passively alongside normal account
activity (equity fills included) without placing anything.
