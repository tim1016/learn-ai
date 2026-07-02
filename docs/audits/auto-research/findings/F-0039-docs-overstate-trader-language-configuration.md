---
id: F-0039
severity: P2
status: fixed-verified
area: documentation
canonical_file: docs/architecture/adrs/0016-bot-control-trader-authored-activity-and-deploy-packages.md
reference: Frontend/src/app/components/broker/bot-control/tabs/configuration-tab.component.html
first_seen: 2026-06-26
last_seen: 2026-06-26
fixed_in: codex/fix-ibkr-activity-evidence
phase: ad-hoc-broker-interface
---

## What

The documentation now says Bot Control should hide implementation vocabulary and present configuration/runtime facts in trader language, but the implemented Configuration tab still exposes raw field names and JSON as primary content: `strategy_key`, `spec_path`, `schema_version`, `max_orders_per_day`, `hydrate_policy`, `instrument_surface`, raw sizing policy JSON, `governed_by`, `parent_run_id`, `redeploy_reason`, and `redeployed_at_ms`. The docs are directionally right, but they read more complete than the implementation actually is.

## Where

- `docs/architecture/adrs/0016-bot-control-trader-authored-activity-and-deploy-packages.md:45-57` requires backend-authored trader-facing explanations and technical details only in expansion.
- `docs/architecture/adrs/0016-bot-control-trader-authored-activity-and-deploy-packages.md:105-112` says runtime configuration should be grouped as human-readable fields and exact JSON may remain in technical details.
- `Frontend/src/app/components/broker/bot-control/tabs/configuration-tab.component.html:22-48` renders raw deployment keys as labels.
- `Frontend/src/app/components/broker/bot-control/tabs/configuration-tab.component.html:51-72` renders raw sizing policy JSON and implementation labels.
- `Frontend/src/app/components/broker/bot-control/tabs/configuration-tab.component.html:83-90` renders action-plan enum names plus raw JSON.
- `Frontend/src/app/components/broker/bot-control/tabs/configuration-tab.component.html:96-106` renders raw redeploy lineage keys and integer timestamp.

## Why this severity

P2. This is not an order-execution failure, but it directly affects the operator manualization goal. If docs claim the bot control page is trader-language while the visible app still displays implementation keys, the eventual trader manual will either be inaccurate or forced to explain internal field names.

## Reproduction

Static-only. Compare ADR 0016's Configuration boundary and trader-language decision against the Configuration tab template.

## Resolution

Implemented in `codex/fix-ibkr-activity-evidence`.

- The Configuration tab now uses trader-facing primary labels for deployment, sizing, action plan, and lineage.
- Raw paths, hashes, policy JSON, and action-plan JSON are still available under technical detail disclosures.
- Redeploy lineage timestamps render as formatted NY timestamps instead of raw epoch integers.
- Regression coverage: `Frontend/src/app/components/broker/bot-control/tabs/configuration-tab.component.spec.ts`.

Validation:

- `podman exec my-frontend npm test -- --watch=false --include src/app/components/broker/bot-control/reused/broker-activity-table/broker-activity-table.component.spec.ts --include src/app/components/broker/bot-control/tabs/configuration-tab.component.spec.ts` — passed, 24 tests.

## Suggested resolution (historical)

Either narrow ADR/runbook language to explicitly mark this as partially implemented, or update the Configuration tab to present trader labels first and move raw paths, hashes, JSON, and lineage keys into technical detail disclosures. The documentation hardening pass should record the exact current state before converting the runbook/manual into trader language.

## Provenance of the finding itself

Ad-hoc broker-interface auto-research tick after PR #690 merged at `2f86597d`. Scope: documentation consistency with merged Bot Control implementation.
