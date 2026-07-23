# Alpaca golden fixtures (Broker System v2)

Representative Alpaca REST payloads used to exercise adapter mapping behavior
and feed the SDK schema-compatibility guard. One subdirectory per
endpoint family (`account`, `positions`, `orders`, `activities`, `assets`,
`clock`), each with the raw payload(s) and an `attribution.md`.

These are **synthetic representative** fixtures
(`reference_kind: synthetic_representative`), not
numerical oracles, so they are intentionally **outside** the numerical golden
manifest (`tests/fixtures/golden/manifest.json`) — that system governs
tolerance-pinned math equivalence, which does not apply to a broker payload
shape.

## Status: `pending-real-capture`

The committed payloads are **representative** — hand-built from the alpaca-py
model field sets (v0.42.0) and Alpaca's public API documentation — so the AFK
slices can build and test the mapping without live credentials. The HITL
closeout slice (#1178) replaces each with a **real sanitized capture** from a
live paper account (account IDs / order IDs scrubbed), removes the
`pending-real-capture` marker, and re-runs the adapter + schema-drift tests
against reality.

## Regeneration (HITL slice #1178)

With paper credentials in `.env`, run the read paths against the live paper
account, take the verbatim payloads from the capture journal
(`var/broker_captures/alpaca/<family>/<day>.jsonl` → `raw_body`), sanitize
linkable identifiers, and replace the representative files here. Record source,
date, and sanitization notes in each family's `attribution.md`.
