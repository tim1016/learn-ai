# Fixture attribution — orders

- **broker:** alpaca (paper)
- **endpoint_family:** orders
- **captured_at_ms:** 1784904168837
- **captured_at:** 2026-07-24T14:42:48.837000+00:00
- **source:** live Alpaca paper account (HITL gate — script `scripts/hitl_alpaca_capture.py`)
- **reference_kind:** `real_sanitized_capture`
- **sanitization:** UUIDs replaced with sentinel values; client_order_id sanitized (contains real operator token).

## order_ref length cap proof

- `order_ref` sent:   `manual/hitl-gate/v1:0VwdO-qVS3er7KrPtwRGQw` (42 chars)
- `DEFAULT_ORDER_REF_MAX_LENGTH`: 60
- Alpaca echoed `client_order_id` back UNTRUNCATED (exact match).
- Margin: 18 chars spare.
- Cap is proven safe for the current namespace format.

## Status: `real-capture`

Replaced `pending-real-capture` synthetic fixtures on 2026-07-24 via HITL
gate #1178 / #1198. Adapter + schema-drift tests run against this payload.
