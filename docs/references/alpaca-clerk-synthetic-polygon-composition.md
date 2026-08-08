# Synthetic Polygon minute-to-5-second composition

## Scope

`PythonDataService/app/broker/alpaca/clerk/sqlite/qualification_polygon_replay.py::synthesize_polygon_5s_bars`
is a qualification-only fixture composer. It does not run in a production feed
and does not author trading state.

## Source capture

The source is the authenticated SPY Polygon minute capture in
`PythonDataService/tests/fixtures/polygon_capture/spy_minute_2025-01-13_2025-01-17/`.
Its attribution note pins `bars.json` at SHA-256
`36d3c9358f4bcbfa89b29ec12493f566fdbd71a61ee2ef9d6c7f3bdd6e90d961`.

## Reconstruction

Each captured minute is expanded into exactly twelve 5-second contributions.
The first contribution carries the minute open, the last carries the minute
close, and selected contributions carry the captured high and low. Integer
volume is divided by quotient and remainder so the twelve contributions sum to
the captured minute volume exactly. Timestamps are consecutive `int64` UTC
milliseconds at 5-second intervals.

The resulting bars traverse the production
`stream_minute_bars/aggregate_realtime_bar/IbkrMarketDataFeed` aggregation path.
The emitted minute must reproduce the captured Decimal OHLC and integer volume
without approximation.

## Tolerance

Parity is pinned at `atol=0, rtol=0`. The composer uses Decimal prices and
integer volume, and the reconstruction consists only of exact selection,
minimum/maximum, and integer summation. There is no floating-point operation
that would justify a nonzero tolerance.

## Validation

- `PythonDataService/tests/broker/alpaca/clerk/sqlite/test_qualification.py::test_synthetic_5s_composition_preserves_exact_minute_ohlcv`
- `PythonDataService/tests/broker/alpaca/clerk/sqlite/test_qualification.py::test_polygon_fixture_replays_through_live_feed_path`
