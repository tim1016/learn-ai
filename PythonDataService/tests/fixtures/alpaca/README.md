# Alpaca golden fixtures (Broker System v2)

Real sanitized Alpaca REST payloads captured from a live paper account via the
HITL gate (script `scripts/hitl_alpaca_capture.py`, run 2026-07-24).
One subdirectory per endpoint family, each with the raw payload(s) and an
`attribution.md`.

These are **real sanitized captures** (`reference_kind: real_sanitized_capture`):
UUID fields and account numbers have been replaced with deterministic sentinel
values; all other field values (numerics, timestamps, status strings, symbols)
are verbatim from the wire. Each `attribution.md` documents the sanitization
applied.

These fixtures are outside the numerical golden manifest
(`tests/fixtures/golden/manifest.json`) — that system governs
tolerance-pinned math equivalence, which does not apply to broker payload shape.

## Status: `real-capture`

Replaced `pending-real-capture` synthetic fixtures on 2026-07-24.
Adapter + schema-drift tests pass against these payloads.

## Regeneration

Run `python scripts/hitl_alpaca_capture.py` from `PythonDataService/` with
paper credentials in `.env`. The script calls all read endpoints, optionally
submits a test order, and regenerates every fixture + attribution file.
