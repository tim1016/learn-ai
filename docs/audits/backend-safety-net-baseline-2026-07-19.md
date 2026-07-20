# Backend safety-net baseline — 2026-07-19

**Issue:** #1124 — reconnect the backend safety net.

## Test baseline

| Point in time | Result | Notes |
| --- | --- | --- |
| Before this work | 226 passed | Existing `Backend.Tests` suite, built in Release against local PostgreSQL. |
| After this work | 229 passed | Includes migration discovery, startup-path, and fresh-PostgreSQL migration regression coverage. |

`Backend.Tests` currently contains 31 `*Tests.cs` files. The test suite does not call the Python service: every Python-facing service test uses a fake `HttpMessageHandler`. The CI gate therefore needs PostgreSQL, but not Python or Redis.

The build reports three inherited warnings that are not changed by this issue:

- `NU1904` for `HotChocolate.Language` 15.1.12;
- `MSB3277` because the test dependency graph resolves EF Core Relational 10.0.0 and 10.0.2; and
- `CS8625` in `MarketDataServiceTests`.

## Migration discovery baseline

The repository held 11 logical migrations before this work, but EF discovered only seven. The four migrations dated 2026-07-12 through 2026-07-17 had neither a generated designer file nor inline migration metadata.

Each now declares both `[DbContext(typeof(AppDbContext))]` and `[Migration("...")]`. The `AllConcreteMigrations_AreDiscoverableByEf` regression test compares every concrete `Migration` subtype with EF's `IMigrationsAssembly` inventory.

The migration chain now contains 12 migrations, including `20260720010000_RepairLegacySchemaDrift`.

## Development-schema audit and adoption

Before adoption, the development database contained 32 public tables but only one migration-history row:

```text
20260630023000_AddLifecycleProjectionReadModel
```

The pre-adoption custom-format backup is stored outside the repository at:

```text
/private/tmp/learn-ai-issue1124-dev-before-adoption-20260720.dump
```

A fresh shadow database was created by applying the repaired migration chain only. A schema-only dump found this meaningful drift in the existing development database:

- missing Data Lake check constraints;
- missing `source_rank` fields, checks, and complete timeline indexes on lifecycle tables;
- missing `ix_strategyexecution_datapolicy_symbol`; and
- stale `PortfolioSnapshots.NetDelta`, `NetGamma`, `NetTheta`, and `NetVega` columns.

The affected Data Lake tables and portfolio snapshot table had zero rows. `bot_lifecycle_events` had one row, which receives `source_rank = 0` through the repair migration's temporary default.

After the verified 11 historical IDs were backfilled, `20260720010000_RepairLegacySchemaDrift` was applied normally through `DatabaseInitializer.MigrateAsync`. The development migration history now has all 12 IDs.

An order-insensitive PostgreSQL catalog fingerprint of tables, columns, defaults, constraints, indexes, and sequences matches the fresh shadow database exactly. Physical column order differs between an `EnsureCreated` database and an incrementally migrated database, so it is intentionally excluded from this semantic comparison.

## Runtime and CI evidence

- `Backend/Program.cs` now calls `DatabaseInitializer.MigrateAsync` before mapping endpoints.
- The development backend logs `No migrations were applied. The database is already up to date.` followed by `Database migrations applied successfully.` and serves `/health` successfully.
- `.github/workflows/ci.yml` now defines `Backend Tests`, using a PostgreSQL 16 service and the full Release suite.

The repository administrator must add the `Backend Tests` check to the `master` branch-protection/ruleset requirements after the job first appears in GitHub Actions.
