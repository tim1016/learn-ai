# Alpaca `trade_updates` frame fixtures (Broker System v2, phase 2, S4)

Representative `trade_updates` websocket event frames used to exercise the
adapter mapping (`from_alpaca_trade_update`) and the live-lifecycle consumer
(`app/broker/alpaca/trade_updates.py`).

- **reference_kind:** `synthetic_representative` (not a numerical oracle — a
  wire-shape fixture; intentionally outside `tests/fixtures/golden/manifest.json`).
- **source:** Alpaca WebSocket Streaming docs
  (<https://docs.alpaca.markets/us/docs/websocket-streaming>) and the alpaca-py
  `TradingStream` protocol (v0.42.0, the schema-drift authority), which sends
  `{"action":"authenticate",…}` / `{"action":"listen",…}` and decodes each
  frame as JSON. The trading `/stream` endpoint defaults to JSON encoding.
- **date generated:** 2026-07-23.
- **encoding:** JSON (verified against alpaca-py `TradingStream`).

## Status: `pending-real-capture`

These frames are hand-built from the documented event shape so the AFK slices
can build and test parsing/idempotency/attribution without live credentials.
The HITL closeout (S7, #1178 family) replaces them with a **real sanitized
capture** from a live paper account's websocket (account IDs / order IDs
scrubbed, `client_order_id` values kept in the `manual/{operator}/v1:{intent}`
shape so ownership attribution still exercises), then re-runs the adapter +
consumer tests against reality.

## Frames

One JSON array of `{"stream":"trade_updates","data":{…}}` frames, covering the
five representative lifecycle events the adapter maps: `new`, `partial_fill`,
`fill`, `canceled`, `rejected`. Fills carry the per-execution `execution_id` +
top-level `price`/`qty` (the slice that filled) distinct from the order's
cumulative `filled_avg_price`/`filled_qty`. All `client_order_id` values use the
`manual/inkant/v1:{intent}` namespace so the consumer attributes them as OWNED.
