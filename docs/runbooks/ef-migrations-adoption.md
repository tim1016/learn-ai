# EF Core migrations adoption runbook

Use this runbook only for a populated PostgreSQL environment that was previously created with `EnsureCreated()`. A new empty database must start from the normal migration chain; do not baseline its migration-history table.

## Preconditions

1. Check out the candidate commit and run the full backend suite, including the PostgreSQL migration test.
2. Stop the backend process before it can call `MigrateAsync` against the legacy database. In local compose, `Backend/` is bind-mounted into `dotnet watch`, so stop `my-backend` before editing or deploying `Program.cs`.
3. Take a restorable custom-format backup:

   ```bash
   pg_dump --format=custom --file=pre-migration-adoption.dump "$DATABASE_URL"
   ```

4. Record the existing `__EFMigrationsHistory` rows and the production schema fingerprint.

## Establish the canonical schema

1. Create a shadow PostgreSQL database at the candidate commit.
2. Run `DatabaseInitializer.MigrateAsync` against the empty shadow database. Never call `EnsureCreated` in this path.
3. Confirm that the shadow database has every migration reported by `AppDbContext.Database.GetMigrations()` and none reported by `GetPendingMigrationsAsync()`.
4. Compare the legacy database with the shadow database using a catalog fingerprint that sorts tables, columns, constraints, indexes, and sequences by name. Do not compare physical column order: it is not a schema contract and differs between `EnsureCreated` and incremental migrations.

The canonical contract is the EF model snapshot plus the intentional raw-SQL objects owned by migrations, including Data Lake checks/indexes and lifecycle projection tables. A model-snapshot-only comparison is insufficient because those raw objects are intentionally not represented by `AppDbContext`.

## Repair drift before history adoption

1. If the legacy database differs from the shadow schema, add a forward migration that converges it. Do not apply manual DDL as the repair.
2. Verify data safety before destructive operations. For example, the #1124 repair drops obsolete portfolio-Greek columns only after confirming they contain no development data.
3. Apply the new repair migration to the shadow database and compare the two schemas again.

For #1124, `20260720010000_RepairLegacySchemaDrift` recreates the canonical raw-SQL constraints and indexes (rather than trusting a matching object name), restores the six Data Lake partial indexes skipped by `EnsureCreated`, and removes the four stale `PortfolioSnapshots` Greek columns only when they contain no data. `20260720020000_ReconcileLegacySchemaRepairContract` repeats the non-destructive catalog reconciliation for databases that had already recorded the first repair before its contract was strengthened.

## Baseline the legacy history and migrate

Only after the schema comparison has a documented explanation for every difference:

1. In one SQL transaction, insert the migration IDs already represented by the legacy schema into `__EFMigrationsHistory`. Use the IDs and `ProductVersion` from the verified shadow database.
2. Do not insert the new repair migration's ID. It must remain pending so EF executes it normally.
3. Start a controlled `DatabaseInitializer.MigrateAsync` run against the legacy database.
4. Verify the resulting history exactly equals the shadow history and `GetPendingMigrationsAsync()` is empty.
5. Re-run the catalog fingerprint comparison and check the backend health endpoint only after migrations complete.

Never run an idempotent EF script against a legacy database with absent or partial history before this baseline step: EF will interpret prior migrations as pending and attempt to recreate existing tables.

## Recovery

If the comparison, history baseline, or migration run fails, stop the backend and restore the pre-adoption backup into a fresh database. Do not delete or rewrite history rows until the failure has been classified.

For production, prefer reviewed SQL scripts or a migration bundle over granting the application principal ongoing schema-modification permissions. Runtime `MigrateAsync` remains appropriate here for the controlled local development adoption and its regression coverage.
