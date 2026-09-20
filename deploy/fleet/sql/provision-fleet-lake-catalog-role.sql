-- Least-privilege lake-catalog role for the clerk lanes (#2166).
--
-- A lane-served read (bot panel deploy scopes, strategy-validation golden
-- dossiers, #2163) resolves lake evidence over asyncpg in the lane process
-- itself, so a clerk needs a database login — but never the superuser URL
-- compose.yaml hands the combined role. A clerk also carries Alpaca
-- execution credentials, so its database login is the blast radius of a
-- lane compromise: this role is read-only over exactly the tables those
-- reads touch, and can never write, never create, and never see the
-- account/order domain the .NET backend owns.
--
-- Idempotent; run as the database superuser (see
-- docs/runbooks/fleet-d-two-clerk-rollout.md):
--
--   podman compose -f compose.yaml -f compose.fleet.dev.yaml exec -T db \
--     psql -U postgres -v lane_password="<generated>" \
--     -f - < deploy/fleet/sql/provision-fleet-lake-catalog-role.sql
--
-- Then put the matching URL in each lane env file (deploy/fleet/env/
-- live.env, paper.env — untracked, chmod 600):
--
--   POSTGRES_URL=postgres://fleet_lake_catalog:<generated>@db:5432/postgres
--
-- Re-run this script after any migration that adds research_* or lake
-- catalog tables: tables created later are not readable by this role until
-- they are granted here.
--
-- Schema migrations are NOT applied through this role. It reads
-- research_schema_migrations only so a lane's ensure_schema() version check
-- passes on an already-migrated database; applying a new version is the
-- coordinator's (superuser's) job on first lake use. A lane connecting to a
-- not-yet-migrated database fails loudly — the runbook migrates first.

-- 1. The role. CREATE ROLE is not idempotent, so guard the creation; the
--    flag resets and grants below are safe to re-run.
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

-- 2. Reach the database and the (single, shared) public schema, and read
--    the migration ledger ensure_schema() checks first.
GRANT CONNECT ON DATABASE postgres TO fleet_lake_catalog;
GRANT USAGE ON SCHEMA public TO fleet_lake_catalog;
GRANT SELECT ON TABLE public.research_schema_migrations TO fleet_lake_catalog;

-- 3. Lane-served lake evidence, read-only:
--    - DataLakeArtifacts: the lake catalog the bot panel reads (#2163).
--    - golden runs / reviews / parity verdicts: the deploy-eligibility
--      dossiers panel_deploy lists on every lane-served panel read.
--    - backtest runs/trades + the Recency family: the dossier evidence
--      cross-references those repositories resolve.
GRANT SELECT ON TABLE
    public."DataLakeArtifacts",
    public.research_validation_golden_runs,
    public.research_golden_validation_reviews,
    public.research_parity_verdicts,
    public.research_backtest_runs,
    public.research_backtest_run_trades,
    public."RecencyLaunches",
    public."RecencyRuns",
    public."RecencyTrades",
    public."RecencyTradeMemberships"
TO fleet_lake_catalog;
