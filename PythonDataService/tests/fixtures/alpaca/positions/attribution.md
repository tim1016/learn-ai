# Fixture attribution — positions

- **broker:** alpaca (paper)
- **endpoint_family:** positions
- **captured_at_ms:** unavailable for this legacy fixture; the exact value was not retained.
- **captured_at:** 2026-07-24 (after HITL test order filled; exact timestamp unavailable)
- **source:** live Alpaca paper account (HITL gate — script `scripts/hitl_alpaca_capture.py`)
- **reference_kind:** `mixed_real_sanitized_capture_and_synthetic_scenarios`
- **sanitization:** UUIDs replaced with sentinel values (00000000-0000-0000-0000-{N:012d}). All numeric values, timestamps, status strings, and symbols are verbatim from the wire.

## Synthetic supplemental records

- The TSLA short position with sentinel asset ID ending in `000099` is synthetic;
  it retains signed-short mapping coverage. All other records are sanitized live
  paper-account captures.

## Capture-time limitation

This committed legacy fixture did not retain its exact capture timestamp. The
capture script now writes each endpoint's own `captured_at_ms` from the current
run journal on regeneration; this file does not borrow a timestamp from orders.

## Status: `mixed-real-capture`

Replaced `pending-real-capture` synthetic fixtures on 2026-07-24 via HITL
gate #1178. Contains 1 real position (SPY long, 1 share) created by the HITL
order-gate test. Adapter + schema-drift tests run against this payload.
