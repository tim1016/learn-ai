# Fixture attribution — trade_updates

- **broker:** alpaca (paper)
- **endpoint_family:** trade_updates
- **captured_at_ms:** 1784904168837
- **captured_at:** 2026-07-24T14:42:48.837000+00:00
- **source:** live Alpaca paper account (HITL gate — script `scripts/hitl_alpaca_capture.py`)
- **reference_kind:** `real_sanitized_capture`
- **sanitization:** Auth frame replaced with structural placeholder (no key material). Order UUIDs replaced with sentinel values. client_order_id in lifecycle frames sanitized.

## Frames captured

- `auth_ack`
- `subscribe_ack`
- `lifecycle/pending_new`
- `lifecycle/new`
- `lifecycle/fill`

## Status: `real-capture`

Replaced `pending-real-capture` synthetic fixtures on 2026-07-24 via HITL
gate #1178 / #1198. Adapter + schema-drift tests run against this payload.
