# Alpaca SQLite sole-authority retirement receipt

**Date:** 2026-08-19
**Decision:** ADR 0037
**Issues:** #1618, #1656, #1657, #1658, #1659, #1660
**Scope status:** selectable-code retirement implemented; deployment inventory
qualification remains a per-environment operator prerequisite

## Retirement result

SQLite is the only selectable Alpaca custody authority. Missing, `OFF_DUTY`,
malformed, conflicting, or failed activation yields no writer and no broker
mutation capability. The legacy JSONL Clerk, factory/fallback, order effects,
reconciliation and sweep, activity recovery, direct hold clear, unactivated
reset path, rollup/product projections, and generic mutation routes are absent.
The temporary `engine/live/run_ledger.py` parser is also absent.

The retained behavior is deliberately narrower:

- activated SQLite Start/Resume/Stop/retire and boot recovery;
- SQLite bot decisions, manual tickets, cancellation, reconciliation, typed
  recovery actions, activation/cutover, and qualification;
- read-only broker account/order/activity/history evidence;
- IBKR Account Clerk history; and
- a strict read-only historical IBKR strategy-instance reader used to deny SID
  collisions. It rejects symlinks, broken links, malformed/non-object JSON, and
  missing/non-string/blank IDs and has no writer or builder.

Structural tests pin retired module/import/route/component absence, generated
OpenAPI absence, surviving SQLite routes, retained IBKR history, and the narrow
historical reader. Behavioral tests retain the SQLite HTTP surface, trade-update
capture/deduplication/health/reconnect/gap recovery/fault injection, V2
profile/catalog/panel/SSE/actions/charts, activation selection, and boot recovery.

## ADR 0037 consequence 5: inventory qualification receipt

The repository now provides an all-or-nothing landing/deployment gate:

```text
cd PythonDataService
python -m scripts.qualify_alpaca_activation_inventory \
  --artifacts-root /secured/alpaca-clerk \
  --inventory /secured/operator-export/in-use-alpaca-accounts.json \
  --max-inventory-age-ms 300000 \
  --output /secured/audit/alpaca-activation-inventory-receipt.json
```

The input is a strict regular-file JSON object:

```json
{
  "schema_version": 1,
  "source": "operator account inventory export",
  "captured_at_ms": 1787115600000,
  "account_ids": ["PA3KWXU1C4C3"]
}
```

An empty, duplicate, unsafe, stale, future-dated, malformed, or symbolic-link
inventory is refused. For every named account the gate verifies the SQLite
integrity/hash-chain head, account ID, authority generation, database identity,
content-addressed activation record, broker proof, and legacy-quarantine
manifest. It publishes one content-addressed `status: qualified` receipt only
after the entire supplied inventory passes. A single missing or unqualified
account prevents any receipt.

Nine focused tests exercise a two-account success receipt, missing activation,
tampered cutover proof, empty/duplicate/unsafe scope, unknown fields, symbolic
links, stale/future capture, and durable CLI publication.

## Evidence boundary

Committed ADR-0035 and cutover evidence establishes paper account
`PA3KWXU1C4C3`, generation 2, as the known qualified activation example. The
2026-08-18 live operator-surface inventory observed that account. Neither source
is a fresh assertion that it is the complete set of accounts in use in every
deployment, and this receipt does not make that claim.

Before deployment, an operator must export the complete current account
inventory and archive a successful receipt from the command above against the
deployment's real authority artifacts. An account omitted from the export is not
qualified by that receipt. An account included but absent, unactivated, corrupt,
or conflicting is refused by the gate and by runtime selection. External
inventory completeness is therefore an explicit operational prerequisite, not
an assumption embedded in this code change.
