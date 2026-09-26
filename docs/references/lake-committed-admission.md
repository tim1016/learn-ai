# Committed lake-file admission (#2456)

## Failure and authority

At baseline `eb13d31a678e618ee2da270737a940099a8b6631`, publication promotes
staged files while holding the artifact row lock, then commits its completion
receipt. A crash or failed commit leaves a canonical file and a non-complete
catalog row. Filesystem-only readers previously accepted it.

This implements ADR 0049's catalog-verified integrity contract; it introduces
no mathematical formula, engine, vendor port, or tolerance change.

## Admission boundary

`app/data_lake/admission.py::read_committed_bytes` reads the payload once and
admits those bytes only when `catalog_client.has_committed_file_receipt` finds
a committed `complete` row matching root UUID, adjustment mode, relative path,
SHA-256, and byte count. The root UUID comes from the existing immutable root
marker. Managed `lake/<mode>` paths with missing or invalid markers refuse;
independent LEAN reference trees retain their existing file contract.

The check runs at each file read. No positive admission result is cached:
refresh can revoke a row without changing filesystem cache keys. Parsed daily
history already admitted into a reader's memory remains that reader's input.
The database migration adds a partial index on the physical-file identity for
complete rows so each read does not scan the lake catalog.

Covered boundaries are Python minute/daily readers, coverage probes, sweep
snapshot capture and manifest-bound readers, chart history, return studies,
factor-file consumption, quote/daily derivation, and LEAN preflight. LEAN
preflight checks trade, quote, daily, required metadata, and present optional
factor/map/interest-rate files. It runs off the request event loop.

Charts fall back to the provider for uncommitted history, including revocation
between planning and reading. Catalog outages stop range probes after one
failure: charts use provider fallback, and study/coverage endpoints report
service unavailability. Study probes treat uncommitted files as missing and use the
existing capture path. Execution readers refuse an uncommitted physical file;
they never silently filter it out of a running backtest's requested window.

LEAN still reads its mounted paths after preflight. Pinning a mounted file's
generation for the entire external run, and reconciling manifest hashing with
later filesystem replacements, belongs to #2455; this change establishes the
committed admission check at preflight.

## Recovery

A vanished writer cannot release its lease. Once that lease expires, the next
capture uses the existing fenced retry path to replace the orphan and commit
its receipt. The orphan remains inadmissible until that commit. No unfenced
cleanup deletes a path that a concurrent publisher might now own. A lost
catalog still follows ADR 0049's wipe-and-re-backfill recovery policy.

## Evidence

- `tests/engine/test_lake_admission.py` includes the original red regression:
  the real publication transaction promotes a valid zip and then fails at
  commit. On baseline the reader accepted it (`DID NOT RAISE`); after the fix
  it refuses. The suite also covers all preflight inputs, root/mode isolation,
  replacement bytes, catalog outages, cached coverage, snapshots, chart
  fallback, study probes, and derived input reads.
- `tests/unit/data_lake/test_catalog_write_ops.py::test_publication_commit_failure_is_invisible_until_reclaimed_and_committed`
  uses a real deferred PostgreSQL constraint trigger to fail **at commit**,
  checks refusal, reclaims the expired lease, publishes again, and checks
  admission plus all five receipt dimensions.
- `tests/unit/data_lake/test_run_materialization.py::test_next_capture_reconciles_a_file_promoted_before_failed_commit`
  drives the real capture pipeline through an interrupted publication and a
  subsequent successful capture, with provider I/O and catalog storage faked.
- `Backend.Tests/Data/SchemaMigrationTests.cs` exercises discovery and migration
  of the new committed-file index against isolated PostgreSQL databases.

Fixture catalogs record receipts when files are seeded, never when they are
read. Replacing a fixture after seeding therefore still fails real admission.
