-- Least-privilege lake-catalog role for the clerk lanes (#2166).
--
-- A lane-served read resolves lake evidence over asyncpg in the lane
-- process itself (#2163), so a clerk needs a database login — but never
-- the superuser URL compose.yaml hands the combined role. A clerk also
-- carries Alpaca execution credentials, so its database login is the
-- blast radius of a lane compromise: this role is read-only over exactly
-- the tables those reads touch, and can never write, never create, and
-- never see the account/order domain the .NET backend owns.
--
-- Idempotent AND convergent; run as the database superuser (see
-- docs/runbooks/fleet-dev-two-lane-posture.md):
--
--   podman compose -f compose.yaml -f compose.fleet.dev.yaml exec -T db \
--     psql -U postgres -v ON_ERROR_STOP=1 --single-transaction \
--     -v lane_password="<generated>" \
--     -f - < deploy/fleet/sql/provision-fleet-lake-catalog-role.sql
--
-- Then put the matching URL in each lane env file (deploy/fleet/env/
-- live.env, paper.env — untracked, chmod 600):
--
--   POSTGRES_URL=postgres://fleet_lake_catalog:<generated>@db:5432/postgres
--
-- Re-run this script after any migration that adds tables the lane-served
-- reads touch: tables created later are not readable by this role until
-- they are granted here, and the revoke-then-grant below means a narrowing
-- of this list takes effect on re-run rather than accumulating.
--
-- Schema migrations are NOT applied through this role. It reads
-- research_schema_migrations only so a lane's ensure_schema() version check
-- passes on an already-migrated database; applying a new version is the
-- coordinator's (superuser's) job on first lake use. A lane connecting to a
-- not-yet-migrated database fails loudly — the runbook migrates first.

-- 1. The role. CREATE ROLE is not idempotent, so guard the creation; the
--    flag resets and the privilege converge below are safe to re-run.
DO $role$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fleet_lake_catalog') THEN
        CREATE ROLE fleet_lake_catalog LOGIN;
    END IF;
END
$role$;
ALTER ROLE fleet_lake_catalog
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
ALTER ROLE fleet_lake_catalog PASSWORD :'lane_password';

-- 2. Reach the database and the (single, shared) public schema. The
--    migration ledger itself is granted with the allowlist in step 5 —
--    granting anything before the converging revoke in step 4 would just
--    be stripped again.
GRANT CONNECT ON DATABASE postgres TO fleet_lake_catalog;
GRANT USAGE ON SCHEMA public TO fleet_lake_catalog;

-- 3. Refuse the inherited temp-table write path. PostgreSQL grants
--    TEMPORARY on every database to PUBLIC, and a role cannot selectively
--    refuse an inherited PUBLIC privilege — so the wildcard is closed at
--    the PUBLIC boundary. Every real consumer of this database is a
--    superuser login today, so nothing legitimate loses anything; if a
--    future non-superuser application role needs TEMP, grant it back to
--    that role explicitly rather than re-opening PUBLIC.
REVOKE TEMPORARY ON DATABASE postgres FROM PUBLIC;

-- 4. Converge, not accumulate: strip every table privilege the role
--    currently holds before applying the allowlist, so a narrowing of the
--    list below (or an operator's temporary widening elsewhere) actually
--    takes effect on re-run instead of persisting forever.
REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM fleet_lake_catalog;
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM fleet_lake_catalog;

-- 5. Lane-served lake evidence, read-only. This is the exact set the
--    lane-served deploy path resolves — panel_deploy's golden-validation
--    scopes (app/services/broker_v2_panel/panel_deploy.py ->
--    app/research/golden_validation/service.py's
--    list_latest_accepted_dossiers, whose repository reads these tables
--    and nothing else) plus the migration ledger above. Adding or removing
--    a table is a deliberate edit to both this list and the contract that
--    pins it (test_fleet_lake_catalog_role_grants.py).
GRANT SELECT ON TABLE
    public.research_validation_golden_runs,
    public.research_golden_validation_reviews,
    public.research_parity_verdicts,
    public.research_backtest_runs,
    public.research_schema_migrations
TO fleet_lake_catalog;
