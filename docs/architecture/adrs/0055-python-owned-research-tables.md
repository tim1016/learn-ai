# ADR 0055 — Research sweep tables are owned by the Python service, schema and writes alike

**Status:** Accepted 2026-09-05
**Provenance:** Design session for PRDs [#1925](https://github.com/tim1016/learn-ai/issues/1925) and [#1926](https://github.com/tim1016/learn-ai/issues/1926), 2026-09-04, and the adversarial review recorded on both issues (revision 6 / revision 3). The operator's architecture direction stated in that session: the .NET backend is being deprecated; no new feature adds a .NET-owned table.
**Decision drivers:** A parameter sweep writes hundreds to thousands of summary rows per launch. Routing each through the .NET `RecencyApi` pattern (a POST per cell, an EF Core entity, a migration that cannot be generated on this host because there is no `dotnet-ef` and the backend container mounts a shared checkout) would reintroduce the per-cell round-trip the sweep deliberately removes when it suppresses the study save, and would grow the surface being deprecated. Server-side sorting, paging, and filtered history are explicit requirements, so a JSON artifact tree is not an option either.
**Vocabulary:** `attempt` — the generation a search's writer claims atomically; `owner_kind` — who a sweep belongs to (`user` for Grid Search history, `walk_forward` for a study's per-fold sweeps). Both defined where they are read: `PythonDataService/app/research/grid_search/schema.py`.
**Related:** ADR 0049 (the data lake catalog is Postgres coordination over immutable files; its schema is EF-owned and `catalog_schema.py` follows it — direct asyncpg *access* was precedented there, *ownership* was not). ADR 0022 (every wire and stored temporal value is `int64 ms UTC`; the calendar is the session authority). Issue [#1927](https://github.com/tim1016/learn-ai/issues/1927) (moving the Recency Chart's tables onto this pattern) and [#1929](https://github.com/tim1016/learn-ai/issues/1929) (the `/api/studies` path) are sequenced after this decision, not folded into it.

## Context

Until this decision every Postgres table was declared by an EF Core migration in `Backend/Migrations`, and Python either called the .NET API to write (Recency Chart, Strategy Lab studies) or, for the data-lake catalog, read and wrote the EF-owned tables directly through asyncpg while a drift test asserted the Python expectation matched what EF had applied.

PRD #1926 needs cell rows that arrive from eight worker threads at once, are keyed idempotently so a resumed search overwrites its own cells, are fenced so a stale worker cannot overwrite a newer attempt, and are paged and sorted by the server. None of that wants an HTTP hop per row, and the schema will evolve with the research feature, not with the backend.

## Decision

1. **Python declares and applies the schema.** `app/research/grid_search/schema.py` holds idempotent DDL (`CREATE TABLE IF NOT EXISTS`) applied on first use under a transaction-scoped advisory lock, with each applied version recorded in `research_schema_migrations`. A schema change ships as a new numbered statement list, never an edit to an old one. EF Core does not know these tables exist; they are outside its model and its migrations cannot touch them.
2. **Python writes directly.** Cells and lifecycle transitions go through `app/research/grid_search/repository.py` over asyncpg. No .NET endpoint participates.
3. **The writer loop and pool owner is named.** Worker threads submit onto the one process-wide loop in `app/utils/background_loop.py`, on which `catalog_client.init_pool()` creates the pool once; FastAPI handlers use their own loop's pool. Eight cells may persist concurrently on one bounded pool; a FastAPI read never borrows a worker connection.
4. **Every write is fenced by an attempt generation.** `claim_attempt` increments `attempt` atomically; chunk writes, terminal transitions, and deletes check the current generation inside their transaction and raise `StaleAttemptError` otherwise. A worker that lost its Redis job record but stayed alive cannot overwrite what a later Finish produced.
5. **Ownership is a column, not a flag a caller sets.** Grid Search history lists `owner_kind = 'user'`; a walk-forward study's per-fold sweeps carry `owner_kind = 'walk_forward'`, its `owner_id`, `fold_index`, and `phase`, and never appear in that history while remaining fetchable by id.
6. **Table names are `snake_case` and unquoted**, distinguishing Python-owned tables from the quoted PascalCase EF-owned ones at a glance.

## Consequences

- No GraphQL layer comes for free; the Grid Search and Walk-Forward pages read from FastAPI, as the other research surfaces do.
- The data-lake schema drift test does not cover these tables; the DDL in `schema.py` is their authority and the repository tests run against a database attested ephemeral (`POSTGRES_URL_IS_EPHEMERAL=1`), as the catalog tests do.
- The Recency Chart tables followed this pattern in ADR 0057 (#1927), which also records the non-dropping handover of tables EF had created.
- Backups and operational tooling that enumerate EF-managed tables must be taught about `research_*` tables.
