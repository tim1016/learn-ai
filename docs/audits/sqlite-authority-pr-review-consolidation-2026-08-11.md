# SQLite authority PR review consolidation — 2026-08-11

## Scope and disposition

GitHub issues #1441 through #1447 were checked for reviewer conversation. They
contain no comments. The actionable feedback is instead on the associated
SQLite-authority pull-request stack.

PRs #1448 through #1454 formed one linear branch chain. PR #1454 is now the
single canonical PR, retargeted to `master`; #1448, #1449, #1450, #1451, and
#1453 are closed as superseded. PR #1452 is a separate Strategy Lab change and
is deliberately out of scope for this consolidation.

This ledger preserves the 53 unresolved review threads that existed when the
stack was flattened. All are resolved by the implementation and regression
coverage summarized below; their original threads are resolved after the
canonical PR is updated and its required checks complete.

## S0: contracts and acceptance fixtures (#1448)

1. Enforce the tolerance declared in each fixture rather than silently using a
   module-level value.
2. Assert execution-slice evidence only for fill events, while still checking
   non-fill lifecycle events correctly.
3. Require a non-empty external-order premise and prove that every broker order
   in that scenario is external.
4. Reject boolean values where a fill count must be an integer.
5. Make acceptance coverage prove the complete execution-authority projection,
   including the required evidence and coverage invariants.
6. Partition strict expected failures by fixture family so a newly passing
   family does not make unrelated work fail as an XPASS.
7. Repair the malformed PRD reference in the downward-correction attribution.
8. Represent every fixture event time as `int64` epoch milliseconds UTC and
   include correction event time.
9. Prevent an accepted, active generation from being migrated in place to
   schema v7.
10. Assert omitted fill and closed-lot collections are empty rather than
    skipping their comparison.
11. Reject `fill` and `partial_fill` broker frames without a non-blank
    execution ID.
12. Correct the workflow documentation's default-branch assumption.
13. Preserve canonical FIFO lot closure granularity for the round-trip fixture.
14. Rebuild FIFO state after execution corrections instead of appending through
    stale incremental state.
15. Define `fills_today` from execution slices, not closed lots.
16. Regenerate the committed OpenAPI contract for the broker-event schema.
17. Implement activity recovery from execution slices, not only cumulative
    order snapshots.
18. Define a total FIFO order when execution timestamps collide.
19. Treat a changed redelivery of an execution ID as a correction conflict,
    distinct from an identical replay.
20. Keep the migration, recovery, correction, and evidence guarantees aligned
    between the PRD and executable acceptance tests.

## S1: authoritative execution ledger (#1449)

1. Recover REST-only fills rather than merely acknowledging their absence from
   websocket evidence.
2. Allow a zero-fill terminal reducing order to complete an EXIT outcome.
3. Send changed redeliveries with a reused execution ID through correction
   handling.
4. Provide an operator recovery path for execution-coverage conflicts.
5. Resolve display names for executable compatibility strategies that are not
   catalog-visible.
6. Enforce bounded retention for SQLite decision receipts while writing.
7. Reject occupied v6 databases in read-only verification; only empty legacy
   databases may be upgraded.
8. Record the execution-ledger ownership move in both authority registries.

## S2: SQLite economic projections (#1450)

1. Treat unexplained execution corrections as incomplete coverage.
2. Preserve original/root time through superseded correction chains.
3. Convert every malformed cursor encoding into the documented domain error.
4. Retain durable ledger order for same-millisecond FIFO fills.
5. Register the new economic engine path in both authority registries.
6. Reject cursors after a revision changes the effective fill set.
7. Include decision receipts in catalog last-activity calculation.
8. Avoid re-materializing the account-wide effective ledger for every catalog
   bot.

## S3: product consumers (#1451)

1. Keep log-only and dry-run charts outside trade-only SQLite custody.
2. Write SQLite decision receipts before routing panel decision reads to SQLite.
3. Derive a bot's last-bar time from decision evidence, not fill activity.

## S4: account history and sole desk (#1453)

1. Surface acknowledgement of held external orders in the sole transaction
   desk.
2. Use an immutable cursor key for external-order pagination.
3. Restrict EXIT economics to the reducing order, not its linked entry orders.
4. Keep strategy-order instruction fields visible in the replacement history.
5. Summarize only rows included by the requested origin filter.
6. Keep requested limit/stop prices distinct from execution-average price.
7. Preserve the custody journal sequence belonging to each execution.
8. Queue a refresh received while a transaction-history request is active.
9. Hydrate/revalidate all merged transaction rows inside one identity-verified
   SQLite snapshot.
10. Strip and reject blank acknowledgement operators at the API boundary.

## S5: secondary-authority retirement (#1454)

1. Make one decision receipt per closed bar idempotent and reject conflicting
   replays.
2. Do not construct the legacy transaction store unless SQLite explicitly
   declines the request.
3. Treat failed SQLite startup as selected-but-unavailable, never as permission
   to fall back to legacy projections.
4. Persist a blocked decision outcome and refusal reason when a gate rejects an
   ENTER submission.

## Resolution map and completion evidence

### S0

- `test_execution_authority_golden.py` now validates fixture-declared numeric
  tolerances, exact integer (not boolean) counts, projection key sets,
  authority generation, omitted empty collections, every external order, and
  only actual fill/partial-fill execution frames.
- Golden execution frames and correction evidence now use canonical
  `timestamp_ms` / `source_event_at_ms` `int64` values. The Alpaca adapter
  accepts those already-normalized timestamps while retaining the one raw
  RFC-3339 ingestion conversion boundary.
- `schema.py`, `database_verification.py`, `test_schema_parity.py`, and
  `test_corrective_foundation.py` fail closed on occupied legacy v6 authority
  and pin the v8 migration/DDL. The golden, adapter, fold, and FIFO tests cover
  execution IDs, replay-vs-conflict behavior, activity recovery, correction
  rebuilds, same-millisecond order, fill-based daily counts, and OpenAPI
  contract regeneration.

### S1

- `trade_updates.py`, `trade_evidence.py`, and `order_evidence.py` distinguish
  immutable websocket execution slices from REST aggregate recovery. A closed
  REST order is folded as `cumulative_recovery` without fabricating an
  execution ID; a changed replay of an existing execution ID raises a durable,
  idempotent coverage uncertainty rather than losing economics.
- Exit, coverage-reconciliation, compatibility display-name, receipt-retention,
  read-only v6 verification, and authority-registry findings are covered by
  the SQLite fold, trade-evidence, decision-receipt, corrective-foundation,
  roster, and authority-map tests and documentation.

### S2

- `economic_projection.py` preserves root correction time and durable custody
  sequence, rejects malformed/stale cursors, makes incomplete coverage
  explicit, and includes decision evidence in activity time.
- Catalog rollup now materializes the effective execution lineage once for the
  requested bot set, then partitions rows by strategy. The regression test
  asserts the single scoped lineage query for a two-bot catalog.
- `docs/math-sources-of-truth.md`, `engine-authority-map.md`, and the migration
  plan record the canonical economic authority and validating tests.

### S3

- SQLite panel/data/chart adapters retain log-only and dry-run sources outside
  trade custody, bridge durable decision writes before SQLite reads, and derive
  last evaluated bar exclusively from decision evidence. Panel regression tests
  prove a later fill cannot relabel the evaluated-bar clock.

### S4

- The Account Desk exposes external-order acknowledgement, validates a
  non-blank operator, preserves requested order type/limit/stop separately
  from fill average, and keeps each execution's custody sequence.
- External pagination is anchored to immutable first-observation sequence.
  Exit presentation uses reducing-order economics only; origin filtering
  summarizes visible rows only; refresh queuing is covered by the history
  store tests.
- Merged history hydration revalidates account identity and control revision
  after typed-fold hydration, rejecting a mixed-revision page for retry.

### S5

- Decision receipts are one-per-closed-bar, idempotent for an identical replay,
  bounded on write, and updated in place to record a final gate refusal.
- The router constructs the legacy store only after SQLite declines and treats
  a selected-but-unavailable SQLite authority as unavailable rather than a
  fallback. Authority-isolation and bot-runner tests cover both conditions.

### Terminal-tier review

The complete PR #1454 diff against `master` was audited for transition complexity,
duplicate control paths, and file-boundary drift. The actionable duplicate
execution-transition validation found in the changed-replay path was extracted
to `ClerkSqliteRepository._validated_execution_slice_transition`; targeted
tests and lint pass after the refactor. The remaining large SQLite modules are
existing authority coordinators with read/external-order/model seams already
split into focused modules; this consolidation adds no new cross-domain
service or fallback path to them.

### Validation run locally

- Python lint: `ruff check app tests` passed.
- The generated Python OpenAPI contract was regenerated and its `--check`
  verification passed.
- Frontend OpenAPI client generation passed after the regenerated contract was
  committed.
- SQLite/Alpaca/panel/router/retirement scope: 793 passed.
- SQLite synthetic custody drills: 10 passed, including the disabled fault-seam
  regression that must remain a typed partial result without a websocket slice.
- Frontend guard checks passed; 47 PR-scoped Angular tests passed.
- The broad Python suite's first failure is an untouched deprecated IBKR
  router expectation (current `403`, historical expected `503`); that retired
  surface is excluded by the repository policy. A later broad run stalled in
  an idle process after its active work, so the scoped command above is the
  deterministic completion record.
- Backend tests could not be run because the local .NET runtime is absent.
