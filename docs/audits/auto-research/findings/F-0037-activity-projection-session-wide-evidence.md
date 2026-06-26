---
id: F-0037
severity: P1
status: fixed-verified
area: wire
canonical_file: PythonDataService/app/routers/live_instances.py
reference: docs/architecture/adrs/0016-bot-cockpit-trader-authored-activity-and-deploy-packages.md
first_seen: 2026-06-26
last_seen: 2026-06-26
fixed_in: codex/fix-ibkr-activity-evidence
phase: ad-hoc-broker-interface
---

## What

The Activity projection attaches session-wide IBKR API evidence to each fill/order row by filtering only on request type, not on the specific order, execution, perm id, order ref, symbol, or row identity. In a multi-order session, a single fill row can therefore display evidence refs for unrelated `placeOrder`, `reqExecutionsAsync`, or `reqAllOpenOrders` observations. This weakens the Bot Cockpit's broker traceability: the UI can imply that a row is backed by request/response evidence that actually belongs to another order.

## Where

- `PythonDataService/app/routers/live_instances.py:2174-2202` creates `ActivityEvidenceRef` with only source, seq, timestamp, request call, and response callback.
- `PythonDataService/app/routers/live_instances.py:2312` attaches every session ref whose request is `placeOrder`, `reqExecutionsAsync`, or `reqAllOpenOrders` to each fill marker.
- `PythonDataService/app/routers/live_instances.py:2392` attaches every session `placeOrder` / `reqAllOpenOrders` ref to each Orders Today row.
- `PythonDataService/app/routers/live_instances.py:2450` and `PythonDataService/app/routers/live_instances.py:2471` attach request-type-only refs to order intent / terminal-state rows.

## Why this severity

P1. This does not directly submit an order, but it can corrupt the operator's forensic view of broker interactions. For broker integration, evidence provenance is safety-critical: if an order is duplicated, partially filled, rejected, or stale, the operator needs row-level request/response linkage. Session-wide evidence attribution can hide the real causal chain and make an unrelated IBKR callback look like supporting evidence.

## Reproduction

Static-only. Read the projection builder and note that the evidence refs do not carry row-level identity, while the row builders attach refs by request-call set only.

## Resolution

Implemented in `codex/fix-ibkr-activity-evidence`.

- `ActivityEvidenceRef` now carries observed broker identity (`order_ref`, `order_id`, `perm_id`, `exec_id`, `symbol`).
- `PythonDataService/app/services/activity_evidence_matching.py` extracts identity from raw request/response payloads and matches Activity row evidence only on concrete row identity.
- Fill markers, Orders Today rows, order intents, and terminal-state rows now call the row matcher instead of filtering session-wide evidence by request type alone.
- Regression coverage: `PythonDataService/tests/routers/test_live_instances.py::test_activity_projection_matches_evidence_to_specific_order`.

Validation:

- `PythonDataService/.venv/bin/pytest PythonDataService/tests/routers/test_live_instances.py -k activity_projection_matches_evidence_to_specific_order` — passed, 1 selected.

## Suggested resolution (historical)

Extend `ActivityEvidenceRef` or the projection join path with row-correlating facts already captured in the raw evidence payloads: `order_ref`, `perm_id`, `order_id`, `exec_id`, `symbol`, and/or callback-specific identifiers. Attach evidence to a fill/order row only when the identity matches. Keep unmatched session evidence as separate folded `broker_evidence` rows.

## Provenance of the finding itself

Ad-hoc broker-interface auto-research tick after PR #690 merged at `2f86597d`. Scope: Activity projection and broker request/response display coverage.
