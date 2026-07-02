---
id: F-0038
severity: P2
status: fixed-verified
area: frontend-consumption
canonical_file: Frontend/src/app/components/broker/bot-control/reused/broker-activity-table/broker-activity-table.component.html
reference: docs/architecture/adrs/0016-bot-control-trader-authored-activity-and-deploy-packages.md
first_seen: 2026-06-26
last_seen: 2026-06-26
fixed_in: codex/fix-ibkr-activity-evidence
phase: ad-hoc-broker-interface
---

## What

The new normalized Activity event rows expose an evidence count but no row-level drawer or link to inspect the evidence refs. The older `BrokerActivityRow` table remains clickable and opens `<app-broker-activity-row-detail>`, but the newer `ActivityBrokerEventRow` table renders only flat cells. That means the app may capture IBKR request/callback evidence and include refs in the API response, while the Activity UI leaves the actual data unreachable from the row that claims it.

## Where

- `PythonDataService/app/schemas/live_runs.py:1848-1872` defines `ActivityBrokerEventRow.evidence`.
- `PythonDataService/app/routers/live_instances.py:2474-2490` creates folded broker-evidence event rows with evidence refs.
- `Frontend/src/app/components/broker/bot-control/reused/broker-activity-table/broker-activity-table.component.html:71-117` renders event rows and prints only `row.evidence.length`.
- `Frontend/src/app/components/broker/bot-control/reused/broker-activity-table/broker-activity-table.component.html:160-203` opens a detail drawer only for legacy `BrokerActivityRow` rows, not `ActivityBrokerEventRow` rows.

## Why this severity

P2. The data is not necessarily lost from the API contract, but the operator surface falls short of the stated broker-interface goal: "get all the data IBKR sends us back and display it." During an incident, an operator can see that evidence exists but cannot inspect which request/callback observations support a row without leaving the Activity workflow.

## Reproduction

Static-only. Compare the two table branches in `broker-activity-table.component.html`: event rows render no click handler, no `aria-expanded`, and no detail component, while legacy rows do.

## Resolution

Implemented in `codex/fix-ibkr-activity-evidence`.

- Activity event rows are now keyboard/click expandable.
- Expanded rows show the row-linked IBKR evidence refs: sequence, timestamp, request call, response callback, source, and captured identity (`order_ref`, `order_id`, `perm_id`, `exec_id`, `symbol`).
- The collapsed row stays trader-facing; raw request/callback details are visible only in the drill-down.
- Regression coverage: `Frontend/src/app/components/broker/bot-control/reused/broker-activity-table/broker-activity-table.component.spec.ts`.

Validation:

- `podman exec my-frontend npm test -- --watch=false --include src/app/components/broker/bot-control/reused/broker-activity-table/broker-activity-table.component.spec.ts --include src/app/components/broker/bot-control/tabs/configuration-tab.component.spec.ts` — passed, 24 tests.

## Suggested resolution (historical)

Add a drill-down for `ActivityBrokerEventRow` rows that shows the evidence refs and, ideally, deep-links or expands matching raw IBKR evidence events from `/api/broker/ibkr/evidence`. Keep trader language in the collapsed row, but make technical request/callback details reachable from the row.

## Provenance of the finding itself

Ad-hoc broker-interface auto-research tick after PR #690 merged at `2f86597d`. Scope: Activity table UI vs backend evidence projection.
