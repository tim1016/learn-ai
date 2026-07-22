# Alpaca orders fixture — attribution

- **Endpoint:** `GET /v2/orders`
- **reference_kind:** `vendor_observed`
- **Status:** `pending-real-capture`
- **Source:** hand-built from the alpaca-py `Order` model field set (alpaca-py
  0.42.0) and Alpaca's public Trading API orders documentation. Two orders
  cover both event cases: a filled market buy (`AAPL`, non-null `filled_at` +
  `filled_avg_price` → one synthesized `fill` event) and an open limit order
  (`TSLA`, `status "new"`, `filled_qty "0"` → no events).
- **Sanitization:** `id` / `client_order_id` / `asset_id` are synthetic.
- **Regeneration:** replace with a real sanitized capture in HITL slice #1178,
  then remove the `pending-real-capture` marker.
