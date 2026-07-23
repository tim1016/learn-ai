# Alpaca positions fixture — attribution

- **Endpoint:** `GET /v2/positions`
- **reference_kind:** `synthetic_representative`
- **Status:** `pending-real-capture`
- **Source:** hand-built from the alpaca-py `Position` model field set
  (alpaca-py 0.42.0) and Alpaca's public Trading API positions documentation.
  Two positions cover the sign cases: a long (`AAPL`, `qty "10"`) and a short
  (`TSLA`, `qty "-3"`, `side "short"`, negative `market_value`).
- **Sanitization:** `asset_id` values are synthetic placeholders.
- **Regeneration:** replace with a real sanitized capture in HITL slice #1178,
  then remove the `pending-real-capture` marker.
