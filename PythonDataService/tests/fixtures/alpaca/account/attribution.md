# Alpaca account fixture — attribution

- **Endpoint:** `GET /v2/account`
- **reference_kind:** `synthetic_representative`
- **Status:** `pending-real-capture`
- **Source:** hand-built from the alpaca-py `TradeAccount` model field set
  (alpaca-py 0.42.0) and Alpaca's public Trading API account documentation. No
  live account was contacted; values are representative and internally
  consistent (`equity == portfolio_value`, `cash == non_marginable_buying_power`).
- **Sanitization:** `id` and `account_number` are synthetic placeholders.
- **Regeneration:** replace with a real sanitized capture in HITL slice #1178
  (verbatim `raw_body` from `var/broker_captures/alpaca/account/<day>.jsonl`,
  account identifiers scrubbed), then remove the `pending-real-capture` marker.
