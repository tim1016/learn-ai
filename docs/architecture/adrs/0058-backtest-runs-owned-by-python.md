# ADR 0058 — Backtest runs are persisted, read and compared by the Python service; the .NET study surface is deleted

**Status:** Accepted 2026-09-06
**Provenance:** PRD [#1929](https://github.com/tim1016/learn-ai/issues/1929) and its four slices [#1963](https://github.com/tim1016/learn-ai/issues/1963), [#1964](https://github.com/tim1016/learn-ai/issues/1964), [#1965](https://github.com/tim1016/learn-ai/issues/1965), [#1966](https://github.com/tim1016/learn-ai/issues/1966). Surfaced by the adversarial review of PRDs #1925–#1927, which found that "Recency is the only research feature persisted through .NET" was false: every engine backtest, every LEAN sidecar run and every parity verdict went through it.
**Decision drivers:** With .NET being deprecated, a researcher could not run a backtest, see it in run history, open its report or see a parity verdict without .NET being up and correct. Three Python code paths POSTed runs to .NET, not one; the reads ran through Apollo GraphQL while the REST read endpoints had no consumer; a study row could not be written without .NET resolving a `Ticker` row owned by another domain; and `ParityVerdicts`, which records how a Python run compared against its LEAN companion, was written by Python over HTTP into a .NET-owned table that foreign-keyed back into the same study rows.
**Vocabulary:** the tables are named for **backtest runs**, not studies — "study" already names a different concept with a different id type in the walk-forward tables (ADR 0056). No operator-facing term changes.
**Related:** ADR 0055 (Python-owned research tables: the versioned ledger, the writer loop, the connection helper), ADR 0057 (the Recency Chart's tables adopted by Python; its study hard-delete guard lived in .NET and reached into a Python table by name — that reach-across is gone), ADR 0022 (every stored temporal value is `int64 ms UTC`).

## Decision

1. **Ownership moves whole, in one change.** Fresh tables mean the .NET resolvers cannot see new rows, so any smaller split leaves a broken intermediate: move the writers first and run history freezes at pre-cutover runs; move the readers first and they read an empty table. Writers, reads, parity verdicts, the Recency guard, the Frontend transport and the .NET deletion land together.
2. **Fresh tables, not adopted ones.** `research/persistence/schema.py` version 5 declares `research_backtest_runs`, `research_backtest_run_trades` and `research_parity_verdicts`. Existing rows are not migrated — the repo owner ruled them expendable. Consequences of a fresh schema: the symbol is stored on the run row (no `Ticker` foreign key); execution time and the run's start and end are `int64 ms UTC` (the start and end are date-anchored values stored at ET midnight of the date — the anchor the Recency window already uses, chosen because a run may start on a non-trading day for which the calendar's session open is undefined — and served on the wire as that anchor, which the client renders in `date-et` mode); the headline columns nobody read (CAGR, PSR, alpha, beta, information ratio, tracking error, Treynor, VaR, annual standard deviation, drawdown recovery days), the per-trade cumulative P&L the engine wrote as a literal zero, and the per-trade type nothing consumed are dropped. The `lean_run_id` stays the idempotency key for LEAN runs (unique partial index; a redelivery returns the existing row and refuses a different `requested_engine`); an engine run has no external key and every persist is a new row.
3. **One converter, one write.** The three producers — the engine backtest (`app/research/backtest_runs/engine_payload.py`, a pure function of the engine response that keeps the fee-policy and realized-equity rules directly testable), the LEAN sidecar (`lean_sidecar_persistence.build_persist_payload`) and the spec-strategy runner (`engine_persistence.build_engine_persist_payload`) — hand the same snake_case persist payload to `records.record_from_payload`, which enforces the rules the .NET writers enforced (source, `lean_run_id` presence, `requested_engine` agreement, positive trade timestamps, the synthesized legacy data policy, the engine's `algorithm_default` brokerage) and to a single repository `insert_run`. The documentation context the .NET reader used to *infer* for rows that carried none is now *recorded* by the converter from the same producer catalogue, so the read is a plain pass-through.
4. **Persistence stays best-effort, exactly.** A failure logs, yields a null run id, and never fails the backtest that produced it. The failure modes moved from HTTP errors to database errors; the semantics did not, and the service test pins them. Writes from worker threads go through the shared writer loop (ADR 0055); a coroutine on any other loop reaches the same pool through a thread rather than creating a pool of its own.
5. **Parity verdicts move with the runs they reference.** The run-time disposition (`pending` / `unavailable`) and the companion-failure transition (`run_failed` / `persist_failed`) are direct writes; the frozen verdict is computed by `app/research/backtest_runs/parity.py`, a port of the .NET `ParityVerdictService` that calls the trade reconciler in-process instead of over HTTP and verifies the same three receipts (LEAN-native metric reproduction, readiness signature, compatibility inputs). First terminal state wins. The companion launch still goes through the .NET jobs surface, which is a separate concern with its own consumers.
6. **Reads are two REST endpoints, not a Relay connection.** `GET /api/research/backtest-runs?engine=&limit=` and `GET /api/research/backtest-runs/{id}` (plus `PATCH …/notes` and `DELETE …`) replace the GraphQL queries and the study REST surface. Top-level field names are the ones the GraphQL layer emitted (camelCase, `totalPnL` and `pnL` included) because the wire contract is unchanged in this slice; the `python.md` snake_case convention is deliberately not applied. The heavy envelopes — equity curve, validation analytics, data policy, metric documentation — are served in the producer's stored shape rather than re-typed, which deletes the .NET re-typing and the Frontend alias layer that undid it. The five-hundred-trade truncation flag is preserved.
7. **The Recency hard-delete guard moves to the delete path**, where both tables are Python-owned, as a join. Schema version 5 also nulls every `RecencyRuns.StudyId`: those ids named rows of the old table, and under a fresh identity sequence they would resolve to unrelated runs; the UI already renders no link for a null reference. The Recency trade focus link itself now targets the Strategy Lab run report, which is the surface that renders a run id.
8. **The Frontend talks to Python behind a service.** `BacktestRunsService` fronts the endpoints; run history is a `resource` whose engine filter is a params change and whose job-completion refresh is a `reload()` (a params change discards the previous value and would blank the table); the run report reads once and re-reads on a fixed cadence only while a parity verdict is pending. Component specs mock the service at the injection level.
9. **The .NET study surface is deleted, not proxied.** The study REST endpoints, the LEAN persist endpoint, the parity endpoints and service, the persistence service, the three backtest-run GraphQL files and their types, and the entities leave with a tool-generated migration that emits real drops. #1964 first deleted the dead trade-attribution feature whose foreign keys blocked the drop.

## Consequences

- `contracts/openapi` gains the `/api/research/backtest-runs/*` reads and verbs; the GraphQL schema snapshot shrinks by the run queries, the notes mutation and their types.
- `tests/research/backtest_runs/` pins the converter, the repository round trip and guards, the parity port (ported from the .NET tests) and the best-effort semantics; `tests/routers/test_backtest_runs_endpoints.py` pins the HTTP contract against the ephemeral database. Twelve .NET test files leave with their subjects.
- A future change to these tables is a new numbered statement list in `schema.py`, never an EF migration.
- `docs/engine-persistence-authority.md` describes the new path; the LEAN backfill CLI writes through the same repository.

## Amendment 2026-09-07 — "first terminal state wins" becomes "the companion that landed wins"

**Provenance:** [#1977](https://github.com/tim1016/learn-ai/issues/1977), from a read-only audit of the parity port.

Decision 5 above said *first terminal state wins*, which read the four verdict
states as peers. They are not. `run_failed` and `persist_failed` are written by
the **dispatch** path — the job worker and the companion dispatcher — before any
companion row exists, so each is a claim about the future: *no comparable
companion is coming*. `agree` and `diverged` are **computed** from two rows in
hand, and `unavailable` records that no companion was ever dispatched.

Treating the dispatch claims as terminal left three wrong outcomes. A companion
dispatch whose HTTP read timed out after the job had started marked the group
`run_failed`, and the real LEAN row that landed minutes later was then refused —
a wrong verdict on two comparable runs. A slow insert that outran the caller's
60 s budget marked the group `persist_failed` while the row was still
committing. And a companion that exited 0 without a parseable result persisted a
row that carried no `parity_group_id` at all, so nothing ever settled the group:
the verdict stayed `pending` and the run report polled it every 5 s for ever.

So:

1. **A landed companion row supersedes a dispatch claim.** `freeze_parity_verdict`
   overwrites `pending`, `run_failed` and `persist_failed`; a computed verdict and
   an `unavailable` disposition are still never overwritten.
2. **A companion that produced no comparable result settles its group at
   `run_failed`.** Its failed row now carries `parity_group_id` *and*
   `parity_failure_detail`; the persist path reads the second and marks the group
   instead of comparing. Carrying the group without the detail would compare a
   zero-trade row against a real Python run and report a divergence that never
   happened.
3. **The settle is chained onto the insert on the writer loop**, not made a
   second hop from the calling thread, so it still runs when the caller has
   stopped waiting — `run_sync` does not cancel on timeout. A timeout is
   therefore reported as *outcome unknown*, distinct from *not persisted*.

Because every dispatch mark is now provisional, the ordering between a timed-out
caller's mark and the writer loop's settle no longer matters: either order
converges on the verdict computed from the rows.
