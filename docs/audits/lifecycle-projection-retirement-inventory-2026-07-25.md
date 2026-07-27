# Legacy lifecycle-projection retirement inventory

**Issue:** #1224
**Cutover commit base:** `bc1175fce163a97c6d004db8e6ef6a2248136fa3`
**Canonical replacement:** Clerk JSONL evidence plus the bounded Clerk transaction
projection (`clerk_transaction_projection.py`).

This is a runtime inventory, not a claim that historical references disappeared.
`rg -n -i 'lifecycle[_-]?projection|LifecycleProjection'` was classified before
deletion; archived reports, applied migrations, and opaque audit template IDs are
forensic references, not active consumers.

| Surface | Pre-retirement active inventory | Disposition | Post-retirement active count |
| --- | --- | --- | ---: |
| Python runtime | `routers/lifecycle_projection.py`; `schemas/lifecycle_projection.py`; `services/lifecycle_projection_{store,replay,tailer,schema}.py`; `main.py` router registration | Deleted. The canonical `bot_lifecycle_projection.py` evidence fold remains separately owned. | 0 |
| Routes | `GET /api/lifecycle-projection/timeline`; `GET /api/lifecycle-projection/safety-triage` | Deleted; both Angular wrappers had zero component callers, so there is no transaction-history compatibility route. | 0 |
| Configuration and workers | `LIFECYCLE_PROJECTION_ENABLED`; tailer implementation | Flag and tailer deleted. `rg` found zero tailer callers or scheduled-worker registrations before deletion. | 0 |
| Python tests and schema checks | Router, store, replay, tailer, integration-store suites; lifecycle schema-drift registration | Deleted; replaced by retirement-contract and Clerk transaction-projection checks. | 0 |
| Angular client and UI | `lifecycle-projection.types.ts`; two `LiveRunsService` methods and specs; unused bot-control unit/E2E fixtures | Deleted. The active bot lifecycle chart types moved to `bot-lifecycle-chart.types.ts`; this is evaluator-owned UI evidence, not the retired Postgres projection. | 0 |
| Generated contract | OpenAPI paths/schemas and `broker.types.ts` operations/types | Removed mechanically from the checked-in contract and generated client. | 0 |
| Migrations and schema history | Applied `20260630023000_AddLifecycleProjectionReadModel`, `20260720010000_RepairLegacySchemaDrift`, and `20260720020000_ReconcileLegacySchemaRepairContract`; their raw-SQL schema-drift test seams | Retained unchanged as applied forensic history. New forward-only `20260725050000_DropLegacyLifecycleProjectionReadModel` retires the derived tables; no active application consumer remains. | 0 |
| Database | Five derived tables: `bot_lifecycle_events`, `account_lifecycle_events`, `operator_gate_snapshots`, `lifecycle_node_receipts`, `account_owner_status_snapshots` | Dropped by forward migration `20260725050000_DropLegacyLifecycleProjectionReadModel`. | 0 |

### Exact active-import and caller inventory before deletion

- `app/main.py` imported and registered `routers/lifecycle_projection.py`.
- `routers/lifecycle_projection.py` imported the legacy schema and store.
- `services/lifecycle_projection_store.py` and
  `services/lifecycle_projection_replay.py` imported the legacy schema; replay
  also used the separately owned Python lifecycle fold.
- `services/lifecycle_projection_tailer.py` imported replay; no runtime caller
  or scheduler invoked the tailer.
- `tests/integration/data_lake/test_schema_drift.py` imported the legacy schema
  table set. The five dedicated runtime test modules imported their matching
  retired seams.
- `LiveRunsService` was the only Angular production import/caller and exposed
  the two route methods. `rg` found zero component callers. The bot-control
  harness, unit fixture, E2E fixture, and service spec were test-only callers.

The post-retirement import search is intentionally limited to the retired
module/route/config names. It excludes separately owned
`bot_lifecycle_projection.py` and historical template IDs so an audit token or
canonical lifecycle fold cannot be misreported as a resurrected compatibility
runtime.

## Retained forensic or separately owned items

- Historical applied migrations `20260630023000_AddLifecycleProjectionReadModel`,
  `20260720010000_RepairLegacySchemaDrift`, and
  `20260720020000_ReconcileLegacySchemaRepairContract` are preserved unchanged.
- `docs/archive/**` and the 2026-07-19 schema baseline retain historical evidence.
- `bot_lifecycle_projection.py`, `bot_lifecycle_chart.py`, and
  `BotDailyLifecycleProjection` remain the Python-authored lifecycle-evidence
  surface; their legacy-looking template IDs are opaque historical audit tokens.
- Clerk transaction projection tables, router, bounded replay/rebuild protocol,
  and opaque-keyset history remain active. They never read broker history, Account
  Truth, a full journal, or the retired lifecycle projection.

## Regression evidence

`PythonDataService/tests/contracts/test_legacy_lifecycle_projection_retirement.py`
asserts that the six retired runtime files, router registration, configuration,
OpenAPI routes, and generated legacy schema are absent. `SchemaMigrationTests`
applies the full EF chain and asserts all five retired tables are absent.
