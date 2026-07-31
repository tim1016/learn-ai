# Offline replay field-validation capture — SPY and TSLA, 2026-07-31

## Purpose

This fixture pins real SPY and TSLA market input for the offline replay
coordinator. It is an integration-regression fixture, not an independent oracle
for strategy profitability or a claim about execution quality.

The validation test deliberately sets the service wall clock to 10:00 ET during
the next NYSE session while replaying this already-completed session. That proves
the offline engine does not depend on the exchange currently being closed. The
selected replay session must remain complete; replaying a still-open session is
rejected by design.

## Capture

- Source: Polygon/Massive adjusted US-equity one-minute aggregates, materialized
  by the canonical `OfflineReplayDataService` cache path.
- Symbols: SPY and TSLA.
- Session: 2026-07-31, regular-session open `1785504600000` ms UTC and close
  `1785528000000` ms UTC.
- Capture request began at `1785530166754` ms UTC (2026-07-31 20:36:06.754Z),
  immediately after the completed session became available to the operator.
- Polygon SDK: 1.12.5.
- Data policy: adjusted bars; full source-session archives are retained, while
  replay consumes the first 225 regular-session warm-up minutes plus 60 visible
  playback minutes.
- Runtime source: `master` at `6dd78aa7cf54d76d749a9e6b3d21d6c402431377`.

## Field result

The production-shaped HTTP run completed with session ID
`ab4ce894140e4299a33626cc0dc5eb4a`. Both bots processed 285 bars, emitted four
decisions, submitted no orders, observed no fills, held no final position, and
finished at the unchanged $100,000 starting equity with no failure code.

The host-mounted artifact path made the run I/O-bound: it took 840,151 ms from
engine start to completion even at the 60x clock setting. The same captured
input and engine path completed in under two seconds in the isolated regression
test. This timing is operational evidence, not part of the deterministic pass
gate; the pinned outputs and artifact row counts are.

`metadata.json` pins both archive-file SHA-256 values and the replay service's
canonical 285-bar digest for each symbol. The test also pins the terminal bot
summary and persisted row counts.

## Regeneration

Regenerate only if Polygon amends the historical bars or the offline replay
contract intentionally changes. State that reason in the regenerating commit.

1. Start the Python data service with `POLYGON_API_KEY` configured.
2. Launch the production-shaped capture:

   ```bash
   curl -X POST http://localhost:8000/api/offline-replay/sessions \
     -H 'Content-Type: application/json' \
     --data '{"session_date_ms":1785504600000,"symbols":["SPY","TSLA"],"playback_minutes":60,"speed":60,"initial_cash_usd":"100000","auto_fetch":true}'
   ```

3. Copy the generated policy-cache archives for `20260731` into the paths named
   by `metadata.json`.
4. Re-run
   `tests/integration/test_offline_replay_market_capture.py` and update metadata
   only when the documented regeneration reason explains the changed bytes or
   result contract.

## Fidelity boundary

The capture validates timestamp alignment, deterministic strategy callbacks,
and artifact persistence. The captured 60-minute playback produced no entry,
so it deliberately does not claim to validate an order or fill on real market
data; deterministic `ReplaySimBroker` order/fill behavior is covered by
`tests/engine/live/test_replay_layer.py`. It does not model quotes, spread,
partial fills, latency, halts, market impact, or live brokerage connectivity.
Nothing in this fixture is financial advice.
