# Alpaca clock fixture — attribution

- **Endpoint:** `GET /v2/clock`
- **reference_kind:** `vendor_observed`
- **Status:** `pending-real-capture`
- **Source:** hand-built from the alpaca-py `Clock` model field set (alpaca-py
  0.42.0) and Alpaca's public Trading API clock documentation.
- **Authority note:** captured and surfaced strictly as **vendor evidence**
  (`BrokerClockEvidence`). The canonical calendar module remains the sole
  authority for scheduled session structure — nothing reads these values as
  authoritative (see the broker-contract-v2 ADR).
- **Regeneration:** replace with a real sanitized capture in HITL slice #1178,
  then remove the `pending-real-capture` marker.
