# Fixture attribution — activities

- **broker:** alpaca (paper)
- **endpoint_family:** activities
- **captured_at_ms:** 1784904166923
- **captured_at:** 2026-07-24T14:42:46.923000+00:00
- **source:** live Alpaca paper account (HITL gate — script `scripts/hitl_alpaca_capture.py`)
- **reference_kind:** `mixed_real_sanitized_capture_and_synthetic_scenarios`
- **sanitization:** UUIDs replaced with deterministic sentinel values (00000000-0000-0000-0000-{N:012d}); account numbers replaced with PA0SANITIZED00001.

## Synthetic supplemental records

- The FILL record with sentinel order ID ending in `000099` is synthetic; it
  retains deterministic trade-activity coverage when a current capture has no FILL.
  All other records are sanitized live paper-account captures.

## Status: `mixed-real-capture`

Replaced `pending-real-capture` synthetic fixtures on 2026-07-24 via HITL
gate #1178 / #1198. Adapter + schema-drift tests run against this payload.
