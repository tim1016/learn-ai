# ADR 0035: Alpaca Account Clerk — event-sourced SQLite authority (append-only log + folded state), no Postgres in scope

- **Date:** 2026-08-04
- **Status:** Accepted for the existing Alpaca-paper authority on 2026-08-10;
  generation 1 is active. Acceptance is supported by the deterministic/adversarial
  qualification suite, verified online backup and recovery evidence, no-fallback
  authority guards, and the UI-driven one-share SPY ENTER/EXIT/Stop/reconcile
  ceremony in
  [`alpaca-sqlite-clerk-paper-soak-2026-08-07.md`](../../audits/alpaca-sqlite-clerk-paper-soak-2026-08-07.md).
  The execution-ledger sole-authority expansion below starts a **fresh schema-v7
  authority generation**. That generation is planned, not yet initialized or
  activated; it requires the clean-slate, human-supervised paper cutover in
  [the execution PRD](../../prds/2026-08-10-sqlite-sole-authority-alpaca-execution.md).
  The earlier multi-session fault matrix remains historical governance and
  post-acceptance hardening; it is not represented as having been run. Live-money
  trading remains disabled and is out of scope (this ADR neither gates nor enables
  live-money).
- **Context:** Alpaca Account Clerk control-plane; the SQLite control-plane PRD
  (`docs/prds/alpaca-account-clerk-sqlite-control-plane.md`); an architecture
  grilling session on 2026-08-04.
- **Supersedes (on acceptance, for the Alpaca clerk only):**
  - **ADR 0001** — the JSON/Parquet control-plane *substrate* choice, as
    instantiated by the Alpaca clerk's two JSONL files (`order_inbox.jsonl`,
    `order_journal.jsonl`). For the Alpaca clerk the substrate becomes the
    SQLite append-only command/transition log plus folded current-state
    projections. ADR 0001's substrate choice is unchanged for IBKR and every
    other control-plane consumer.
  - **ADR 0008** — the run-scoped WAL / durable-submit-record *mechanics*
    (the JSONL-backed uncertain-ack bookkeeping and order-identity-recovery
    implementation) are replaced by the SQLite command log's own
    durable-submit and idempotency machinery for the Alpaca clerk. ADR 0008's
    ownership-ladder and uncertain-ack *semantics* are inherited, not
    overturned — only their JSONL-file implementation is replaced.
  - **ADR 0030** — specifically decision 7 ("journal-canonical ledger"): for
    the Alpaca clerk, "canonical" moves from the JSONL journal to the SQLite
    hash-chained transition log with folded projections. Decision 2
    (account-rooted authority) and decision 8 (identity-scoped
    fencing/retirement) are preserved unchanged — the SQLite authority is
    still exactly one per account and still identity-fenced; only the
    JSONL-as-canonical-ledger mechanic is replaced.
  - **ADR 0033** — only its JSONL-specific plumbing (the A0–A3 custody
    clocks computed by folding the JSONL journal) is superseded, replaced by
    SQLite-native transition folds. ADR 0033's governing *principle* — one
    append-only authority, custody is not an economic claim, the A0/A1/A2/A3
    clock model itself — is **not** overturned; ADR 0035 is explicitly
    designed to honor it (see "Context" and "Decision" below). This is an
    implementation-substrate supersession, not a philosophy reversal.

  IBKR is unchanged; all four ADRs remain in force everywhere else in the
  repository. The acceptance status above activates exactly these Alpaca-clerk
  supersessions and no broader ones.

## Context

> **Cutover note — 2026-08-06:** issue #1395 completed the activation-gated
> runtime, operator recovery/cutover tooling, adversarial campaign, and 1M-row
> qualification artifacts. Alpaca paper account generation 1 was then activated
> under #1383; the phase 1 receipt is recorded in
> `docs/audits/alpaca-sqlite-clerk-phase-1-cutover-2026-08-06.md`. This activation
> preceded acceptance. On 2026-08-10 the evidence-driven amendment in
> `docs/prds/alpaca-sqlite-ui-paper-acceptance-and-ibkr-control-retirement.md`
> accepted the ADR for Alpaca paper after a complete UI-driven paper round trip
> and terminal reconstruction. Accounts without a valid activation fence continue
> to use the legacy JSONL Clerk; an invalid activated authority fails closed and
> never falls back.

At proposal time, the Alpaca Account Clerk
(`PythonDataService/app/broker/alpaca/clerk/`, ~6,745 lines) was an in-process
async service whose durable authority was two
append-only JSONL files per account (`order_inbox.jsonl`, `order_journal.jsonl`),
each fsync'd before broker contact. Every current-state question was answered by
folding the **entire** journal from byte 0 (O(n) per submit/cancel/reconcile).
Around that sat: an in-process `asyncio.Lock` plus a documented
single-uvicorn-worker correctness requirement (two workers silently corrupt
state); a hand-rolled file idempotency ledger (`CustodyResolutionStore`); a
best-effort JSONL→Postgres projection (cursor-tail reader + inode/prefix-hash
guard + `pg_advisory_xact_lock` + rebuild machinery); and optimistic-concurrency
tokens computed as a hash of derived state — the direct cause of the Resume-409
and Stop-409 churn fixed twice in the week before this ADR.

Prior decisions kept files canonical (ADR 0001), the journal canonical (ADRs
0030, 0033), and gated any SQL **substrate** behind three named triggers (ADR
0008). ADR 0033 specifically rejected "a new account-safety ledger … because it
would compete with the Clerk journal just as custody needs one authority."

Named pain has now fired: correctness bugs from deriving concurrency tokens over
folded state, and continuously growing bespoke concurrency/recovery machinery.
The operator's stated priorities for the next milestone are **correctness and
robustness first, machinery-removal over time, and speed as a later factor.**
Deployment remains trusted-local, single-operator, Alpaca paper only.

The controlling question is not "SQLite yes/no." It is *where canonical custody
truth lives and whether derived state can silently diverge from it.* This ADR
answers that in a way that **honors** ADR 0033's "one append-only authority"
principle while changing the storage medium.

## Decision

For the Alpaca Account Clerk, adopt an **event-sourced SQLite authority** — one
account-scoped database (`accounts/alpaca/<account_id>/clerk.db`) — with the
following load-bearing decisions.

1. **One append-only log is the single authority.** A hash-chained,
   append-only `custody_transitions` table is the sole canonical custody record.
   This *moves the append-only log's storage medium* from a JSONL file into a
   SQLite table; it does **not** abandon the "one append-only authority"
   philosophy of ADR 0033 — it preserves it.

2. **Current-state is a materialized fold of the log, never authored directly.**
   Every mutable current-state table (commands, effects, orders, positions,
   holds, uncertainties, …) is updated by applying a transition to the affected
   rows, **committed in the same transaction as the log append**. Current-state
   is therefore fully rebuildable from the log by replay; the rebuild path is a
   first-class, tested capability (used at boot and in disaster recovery). The
   dual-write drift risk of "mutable tables + a side timeline" is structurally
   eliminated.

3. **Idempotency is content-addressed and enforced by `UNIQUE` constraints.**
   Strategy decisions keep the proven natural key `(strategy_instance_id,
   decision_id)`. Operator lifecycle commands get a natural key
   `(account, strategy_instance_id, lifecycle_run_id, action,
   intended_end_state)`. Start/Resume reserves its proposed run identity before
   committing the key; Stop binds the active run. A transport retry of the same
   command returns the existing result with no new effect and no error; a genuine
   re-request returns a typed reason (e.g. "a stop has already been requested");
   the UI renders the control disabled with a backend-authored tooltip. A random
   nonce is used only where re-issue is genuinely meaningful. A run-2 lifecycle
   command therefore cannot collide with the equivalent run-1 command.

4. **Capture-before-contact is absolute.** `PRAGMA synchronous = FULL`; the
   intent-transition commit fsyncs **before** any broker call. Write latency is
   tracked as an observable, not traded away.

5. **The database serializes mutations; a durable lease owns execution.**
   `BEGIN IMMEDIATE` + a bounded `busy_timeout` make a second database writer
   serialize or error rather than corrupt. They do not prove one FastAPI process,
   stream consumer, or reconciler. The supported one-worker topology is validated
   at startup, and every broker contact additionally requires a database-durable
   per-account execution lease plus a transactionally claimed operation work item.
   An accidental second process therefore cannot race broker recovery or stream
   handling merely because its database writes serialize.

6. **No PostgreSQL in scope.** The best-effort projection and its outbox,
   projector, cursor-tail reader, advisory lock, and rebuild runbook are
   removed. SQLite serves current-state *and* operator history/analytics for a
   single-operator local tool. Postgres re-enters only when a named ADR-0001
   trigger fires (concurrent multi-consumer load, hot cross-run analytics,
   authenticated multi-operator audit).

7. **Disaster recovery via a write-only append mirror.** A SQLite transaction
   and a plain file cannot commit atomically, so each transition uses a two-phase
   fsync fence: write a mirror prepare record, commit the matching SQLite log and
   folds, then write a mirror finalize record. The command is neither returned as
   accepted nor broker-eligible until finalization. On recovery, only a contiguous
   hash-verified sequence of finalized records can rebuild a corrupt database;
   prepare-only records are excluded because the fence guarantees they had no
   broker effect. This restores graceful degradation and a rebuild source without
   introducing a second *authority* or any hot-path read cost.

8. **The log is hash-chained for tamper-evidence.** Each `custody_transitions`
   row carries the prior row's hash. Corruption and tampering are detectable,
   and a mirror-rebuild can **verify** integrity rather than trust it —
   preserving ADR 0001's hash-sealed, reproducible posture.

9. **Broker-neutral core, Alpaca-first.** The log / fold / idempotency engine is
   broker-agnostic; only the adapter is broker-specific. Alpaca is implemented
   and qualified first and to a high bar. IBKR convergence (and eventual
   deletion of its JSONL + lease + RPC machinery) is an aspiration, not a
   constraint on this work.

10. **Clean-slate cutover; no long-lived dual authority.** Flatten the account,
    obtain fresh broker proof of flat/no-open-orders, retire (do not import) the
    legacy JSONL, initialize a new Clerk authority generation, and redeploy
    desired instances. There is no dual-authority mode.

11. **Two uncertainty scopes, extensible causes.** Blast radius is `BOT` or
    `ACCOUNT_CLERK`; symbol/venue/order and future attributes ride as
    extensible `reason_code` + `facts_json`, not new top-level scopes.
    Unrecognized reasons default to `ACCOUNT_CLERK` and block new exposure
    (fail-closed).

12. **The frontend derives no safety.** The backend authors capability —
    including which action is primary, why each is disabled, and freshness/
    staleness. Angular renders and never re-derives. The Alpaca roster moves
    onto the existing versioned-SSE infrastructure (`SurfaceHub` +
    `versioned-snapshot-stream`), retiring its 3-endpoint polling; that
    infrastructure is reused, not rebuilt.

13. **The fresh schema-v7 generation owns the complete internal execution
    record and its product projections.** Its SQLite folds are the sole internal
    authority for execution slices, effective fills, attributed positions and
    FIFO P&L, bot attribution, account history, external-order observation,
    immutable bot configuration, and decision receipts. The panel, catalog,
    chart, account history, and consolidated desk read those folds only. JSONL,
    PostgreSQL, the process registry, and broker-direct reads may remain for
    capture, reconciliation, diagnostics, or task liveness, but never supply a
    product fallback while this authority generation is active. This scope is
    delivered by the fresh-generation program; it does not retroactively claim
    that the accepted generation-1 projections already supply every field.

**Crown-jewel invariants preserved unchanged** (they are application logic,
orthogonal to storage, and must be re-proven under SQLite with tests):
capture-before-contact; never fabricate a terminal outcome from a single lost
response (30s grace + `by_client_order_id` resolution); namespace-attributed
exposure that never nets from the raw account position; cancel-first /
prove-terminal EXIT; live-idempotent websocket dedup.

## Fresh-generation execution vocabulary

This vocabulary governs the fresh schema-v7 authority generation described in
decision 13. All persisted times are `int64 ms UTC`; session boundaries come
from the canonical NYSE calendar, including half-days.

- An **execution slice** is one broker execution fact, identified on the primary
  capture path by the raw Alpaca `execution_id`, with its own side, quantity,
  price, and broker-occurrence time. It is not an order's cumulative filled
  quantity or a lifecycle update.
- An **effective execution slice** is the currently applicable version of an
  execution fact. **Fill count**, including `fills_today`, is the number of
  effective execution slices, never the number of filled orders, lifecycle
  updates, or FIFO closed lots. `fills_today` restricts that count to slices
  whose broker-occurrence time is in the current NYSE session window
  `[session_open_ms, session_close_ms)`.
- A **correction** is an append-only effective replacement for one prior
  execution slice. Its superseding transition identifies the corrected slice,
  leaves that prior slice auditable, and applies the quantity/price/fee delta to
  the effective position and FIFO projection. A quantity regression without a
  matching superseded slice is an exposure-blocking uncertainty, not a silently
  accepted fill.
- **`realized_pnl_today`** is computed by running FIFO over the complete
  effective fill history and summing only closed lots whose
  `closed_at_ms ∈ [session_open_ms, session_close_ms)`. The result is therefore
  based on the canonical NYSE session window (half-day aware), and a prior-session
  buy closed today is counted today. `fills_today` and `realized_pnl_today` have
  deliberately different units: execution slices and closed-lot P&L,
  respectively.

## Considered options

- **Keep JSONL canonical + an offset-tailing warm read model** (the remediation
  research plan's D2 hypothesis). *Rejected:* it keeps the fold, the file
  idempotency ledger, and the JSONL write discipline — it does not achieve the
  machinery-removal goal; it only patches read latency.

- **SQLite canonical with directly-authored mutable tables + a side custody
  timeline** (the 2026-08-04 deep-research draft). *Rejected:* the mutable
  tables and the timeline are a dual-write and will drift — the exact
  "derived state disagrees with truth" bug class this work exists to remove.

- **SQLite canonical, event-sourced (append-only log = authority, current-state
  = fold).** *Chosen.* Single transactional store, no drift, idempotency by
  constraint, and — critically — honors ADR 0033's one-authority principle by
  keeping an append-only log as the authority.

- **Keep Postgres as an async projection.** *Rejected for this scope:* pure
  machinery for a single-operator local tool once a fast local queryable store
  exists; re-introduce only on a named ADR-0001 trigger.

- **Append-only log canonical + SQLite as a disposable view** (two stores).
  *Rejected* in favor of a single store, but its central robustness property
  (a rebuild source that degrades gracefully) is recovered by the write-only
  append mirror (decision 7).

## Consequences

**Positive**
- One transactional store; a crash yields the old or new committed state, never
  a partially-published set of control records.
- Current-state cannot drift from custody truth (it is a fold of the one log).
- Idempotency is a constraint, not derived-state comparison — the Resume/Stop
  409-churn class is retired at the source.
- The entire Postgres projection subsystem is deleted.
- Corruption is survivable from a contiguous finalized mirror sequence and
  detectable (hash chain + `integrity_check`); the account is reproducible from a
  WAL-safe SQLite online-backup snapshot, not a raw database-file copy.
- The one-worker deployment is defended by startup validation, execution leases,
  and transactional work claims rather than an honor-system configuration.

**Negative / costs**
- Capture-before-contact and never-fabricate-terminal must be **re-proven** under
  SQLite with adversarial tests; they do not come "for free" as they did from the
  hand-rolled JSONL discipline.
- Whole-file corruption is a new failure mode (mitigated by mirror + backups +
  startup `integrity_check`, and fail-closed on detection).
- The external mirror adds a prepare/finalize recovery fence; it cannot be
  described as one atomic SQLite-and-file transaction and must be fault-tested.
- A follow-up requires this ADR to supersede parts of four accepted ADRs for the
  Alpaca clerk; the boundary must be stated precisely (Alpaca only).
- Order replacement (PATCH), including Alpaca's larger-of-old-and-new
  buying-power behavior, is **net-new** work — it does not exist today and is not
  a rewrite of existing behavior.

## Pinned implementation contracts

The concrete schema DDL, PRAGMA set, transaction matrix, hash-chain row
format, write-only mirror line format, and fail-closed startup checks this
ADR requires are pinned in
[`docs/architecture/alpaca-clerk-sqlite-pinned-contracts.md`](../alpaca-clerk-sqlite-pinned-contracts.md)
(PRD Phase 0 / issue #1374). That document is binding on Slice 2 onward; this
ADR's Status is unchanged by its existence.

## Qualification gate

Implementation evidence for both gates is published in
`docs/audits/alpaca-sqlite-clerk-qualification-{smoke,full}.{json,md}`: (a) the
adversarial correctness matrix (atomicity, idempotency, broker races, custody/
uncertainty, database failure incl. mirror-rebuild and hash-chain verification,
UI delivery), and (b) the performance budgets at the 1/10/100-bot and
10k/100k/1M-row fixtures. The 2026-08-10 acceptance ceremony added live
Alpaca-paper proof of exactly one one-share ENTER and one strategy-owned EXIT,
capture-before-contact identity continuity, SQLite-attributed exposure, broker
fills, terminal flatness, Stop, reconciliation, reload reconstruction, and
side-effect-free evidence inspection. The closure record is the soak report and
the execution PRD linked above. Remaining injected-fault and multi-session rows
are tracked in [issue #1440](https://github.com/tim1016/learn-ai/issues/1440) as
post-acceptance hardening and are not claims of completed live execution.
Live-money trading stays disabled throughout.
