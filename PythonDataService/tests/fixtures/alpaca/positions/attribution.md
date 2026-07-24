# Fixture attribution — positions

- **broker:** alpaca (paper)
- **endpoint_family:** positions
- **captured_at_ms:** (see orders attribution — same session)
- **captured_at:** 2026-07-24 (after HITL test order filled)
- **source:** live Alpaca paper account (HITL gate — script `scripts/hitl_alpaca_capture.py`)
- **reference_kind:** `real_sanitized_capture`
- **sanitization:** UUIDs replaced with sentinel values (00000000-0000-0000-0000-{N:012d}). All numeric values, timestamps, status strings, and symbols are verbatim from the wire.

## Status: `real-capture`

Replaced `pending-real-capture` synthetic fixtures on 2026-07-24 via HITL
gate #1178. Contains 1 real position (SPY long, 1 share) created by the HITL
order-gate test. Adapter + schema-drift tests run against this payload.
