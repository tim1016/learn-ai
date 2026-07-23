# Alpaca activities fixture — attribution

- **Endpoint:** `GET /v2/account/activities`
- **reference_kind:** `synthetic_representative`
- **Status:** `pending-real-capture`
- **Source:** hand-built from the alpaca-py `TradeActivity` / `NonTradeActivity`
  model field sets (alpaca-py 0.42.0) and Alpaca's public Account Activities
  documentation. Two rows cover both categories: a trade `FILL` (carries
  `transaction_time` → `trade_activity`, occurred-at from the timestamp) and a
  non-trade `DIV` (carries `date` → `non_trade_activity`, occurred-at anchored
  at 00:00 ET of that date).
- **Sanitization:** `id` / `order_id` are synthetic.
- **Regeneration:** replace with a real sanitized capture in HITL slice #1178,
  then remove the `pending-real-capture` marker.
