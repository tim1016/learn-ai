# Alpaca assets fixture — attribution

- **Endpoint:** `GET /v2/assets`
- **reference_kind:** `synthetic_representative`
- **Status:** `pending-real-capture`
- **Source:** hand-built from the alpaca-py `Asset` model field set (alpaca-py
  0.42.0) and Alpaca's public Trading API assets documentation. The payload
  uses Alpaca's raw `class` key (the SDK aliases it to `asset_class`). Two rows
  cover the `status` filter: an active tradable asset and an inactive one.
- **Sanitization:** `id` values are synthetic.
- **Regeneration:** replace with a real sanitized capture in HITL slice #1178,
  then remove the `pending-real-capture` marker.
