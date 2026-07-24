# Fixture attribution — assets

- **broker:** alpaca (paper)
- **endpoint_family:** assets
- **captured_at_ms:** 1784904168376
- **captured_at:** 2026-07-24T14:42:48.376000+00:00
- **source:** live Alpaca paper account (HITL gate — script `scripts/hitl_alpaca_capture.py`)
- **reference_kind:** `real_sanitized_capture`
- **sanitization:** UUIDs replaced with deterministic sentinel values (00000000-0000-0000-0000-{N:012d}); account numbers replaced with PA0SANITIZED00001.



## Status: `real-capture`

Replaced `pending-real-capture` synthetic fixtures on 2026-07-24 via HITL
gate #1178 / #1198. Adapter + schema-drift tests run against this payload.
