# Alpaca golden fixtures (Broker System v2)

Sanitized Alpaca REST payloads captured from a live paper account via the HITL
gate (script `scripts/hitl_alpaca_capture.py`, run 2026-07-24).
One subdirectory per endpoint family, each with the raw payload(s) and an
`attribution.md`.

These are **sanitized raw Alpaca wire fixtures**. UUIDs, account numbers, and
client-order identifiers are replaced with deterministic sentinel values. The
remaining values, including RFC3339 timestamps, retain the vendor wire shape so
the adapter ingestion boundary is tested. The adapter immediately converts those
raw vendor timestamps to canonical `int64 ms UTC`; the fixtures are not internal
storage or contract payloads.

Most records are real paper-account captures. Deterministic synthetic
supplemental records retain edge cases that a live recapture cannot guarantee:
the FILL activity, inactive `DELISTED` asset, TSLA short position, open limit
order, and `partial_fill`/`canceled`/`rejected` trade-update frames. Each
`attribution.md` identifies its mixed provenance.

These fixtures are outside the numerical golden manifest
(`tests/fixtures/golden/manifest.json`) — that system governs
tolerance-pinned math equivalence, which does not apply to broker payload shape.

## Status: `mixed-real-capture`

Replaced `pending-real-capture` synthetic fixtures on 2026-07-24.
Adapter + schema-drift tests pass against these raw wire payloads.

## Regeneration

Run `python scripts/hitl_alpaca_capture.py` from `PythonDataService/` with
paper credentials in `.env`. The script calls all read endpoints, submits the
documented paper test order, waits for terminal websocket evidence, captures
post-order state, and regenerates every fixture + attribution file. It fails
without changing fixtures if the order lifecycle cannot be proven.
