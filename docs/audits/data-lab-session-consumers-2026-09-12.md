# DataLabSession consumer inventory — 2026-09-12

Audit backing PRD `docs/prds/2026-09-12-data-lab-workspace-redesign.md` §13
("Saved-session migration gate"). Produced before the additive int64-ms-UTC
timestamp migration (PRD §13 steps 1–4). Search terms: `DataLabSession`,
`dataLabSession`, `DataLabSessionInput` across `Backend/`, `Backend.Tests/`,
`Frontend/`, `PythonDataService/`, `contracts/`, and `docs/`.

## Consumer matrix

| Consumer | Location | Fields read | Fields written | Timestamp expectation | Compatibility strategy | Contract test |
|---|---|---|---|---|---|---|
| EF entity | `Backend/Models/DataLab/DataLabSession.cs` | all (persistence) | all | `CreatedAt`/`UpdatedAt` = `DateTime` UTC; `FromDate`/`ToDate` = 10-char date strings | Additive nullable `long?` columns (`WindowStartMsUtc`, `WindowEndMsUtc`, `CreatedMsUtc`, `UpdatedMsUtc`); legacy columns untouched | `Backend.Tests/Data/SchemaMigrationTests.cs` (migration inventory); new `DataLabSessionTimestampTests` |
| EF configuration | `Backend/Data/AppDbContext.cs` `ConfigureDataLabModels` (~line 203) | — | indexes on `UpdatedAt`, `Ticker`; column constraints | `UpdatedAt` indexed for "most recent first" ordering | Keep legacy index; do not move time-based index until PRD §13 step 7 | Schema snapshot in migration |
| EF migration history | `Backend/Migrations/` | — | — | — | New additive migration; snapshot updated | `SchemaMigrationTests` |
| GraphQL query | `Backend/GraphQL/DataLabQuery.cs` (`dataLabSessions`, `dataLabSession`) | projects entity fields | — | consumers order by `updatedAt` (DateTime) | Entity gains nullable `long?` fields exposed as `Long`; dual-read keeps legacy fields populated | Python contract test below; new xUnit read tests |
| GraphQL mutation | `Backend/GraphQL/DataLabMutation.cs` (save/update/chartSnapshot/rename/delete; input/result types ~line 238) | `DataLabSessionInput` (name, ticker, fromDate, toDate, session, forwardFill, adjusted, entriesJson, chartSnapshotJson) | all entity fields via EF | writes `DateTime.UtcNow` on every mutation | Mutations additionally populate numeric ms fields; input gains optional numeric fields; legacy DateTime fields still written | New xUnit mutation tests |
| GraphQL schema snapshot | `contracts/graphql/backend.schema.graphql` (`DataLabSession` line 112, `DataLabSessionInput`, `DataLabSessionResult`) | — | — | `createdAt`/`updatedAt: DateTime!` | Add nullable `Long` fields; keep `DateTime!` fields | `dotnet run --project Backend -- schema export` diff (per `docs/audits/contract-surface-drift-2026-08-18.md`) |
| Frontend service | `Frontend/src/app/services/data-lab-session.service.ts` (queries lines 66–149, `RawSession` ~line 155, mapping ~line 196) | id, name, createdAt, updatedAt, ticker, fromDate, toDate, session, forwardFill, adjusted, entriesJson, chartSnapshotJson | `DataLabSessionInput` via `toInput` | reads `createdAt`/`updatedAt` as ISO strings, passes through to component unchanged | No change required (additive fields not selected in queries); later cutover selects numeric fields and formats locally | `Frontend/src/app/services/data-lab-session.service.spec.ts` |
| Frontend component | `Frontend/src/app/components/data-lab/data-lab.component.ts` | session summaries incl. `createdAt`/`updatedAt` strings; `fromDate`/`toDate` parsed locally (`new Date(\`${from}T00:00:00Z\`)`, lines 92–99, 543–544) | — | ISO-string timestamps; date-only window parsed as UTC midnight client-side | Backend interim window derivation mirrors this UTC-midnight convention; component unchanged in this PR | `data-lab.component.spec.ts` |
| Frontend component spec | `Frontend/src/app/components/data-lab/data-lab.component.spec.ts` (lines 22–23) | fixture `createdAt`/`updatedAt` ISO strings | — | fixed ISO strings | untouched | Vitest suite |
| Frontend service spec | `Frontend/src/app/services/data-lab-session.service.spec.ts` | GraphQL wire shapes incl. `createdAt`/`updatedAt` | — | ISO strings | untouched | Vitest suite |
| Python contract test | `PythonDataService/tests/contracts/test_frontend_graphql_input_types.py` | `DataLabSessionInput` presence in schema + Frontend variable types | — | none (type names only) | Input keeps its name; additive fields only | the test itself |
| Backend tests | none existed for DataLab | — | — | — | new `Backend.Tests/Unit/GraphQL/DataLabSessionTimestampTests.cs` | new tests |
| Docs | `docs/prds/2026-09-12-data-lab-workspace-redesign.md`, `docs/audits/structural-integrity-2026-04-22.md` | descriptive | — | — | descriptive only | — |

## Findings

- No generated GraphQL client exists in `Frontend/` — the service uses
  hand-written query strings and a local `RawSession` interface, so additive
  nullable schema fields cannot break compilation.
- `DataLabSessionResult` carries only `success`/`id`/`message`; numeric
  timestamp verification is via the query resolvers after save/update, not the
  mutation payload.
- Numeric authority rule for this migration: on read, numeric fields are
  authoritative when non-null; legacy `DateTime`/date-string fields remain
  populated from legacy columns.
- Interim window derivation (until Python calendar-accurate resolution):
  `FromDate`/`ToDate` parsed as date-only UTC midnight → epoch ms. This
  matches the Frontend's existing `T00:00:00Z` parsing convention.
