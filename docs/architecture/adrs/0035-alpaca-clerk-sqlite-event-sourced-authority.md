# ADR 0035: Alpaca Account Clerk — event-sourced SQLite authority (append-only log + folded state), no Postgres in scope

- **Date:** 2026-08-04
- **Status:** Proposed (requires acceptance + qualification before implementation authority)
- **Context:** Alpaca Account Clerk control-plane; the SQLite control-plane PRD
  (`docs/prds/alpaca-account-clerk-sqlite-control-plane.md`); an architecture
  grilling session on 2026-08-04.
- **Supersedes (on acceptance, for the Alpaca clerk only):** the
  JSONL-as-canonical-authority portions of ADRs 0001, 0008, 0030, and 0033.
  IBKR is unchanged; those ADRs remain in force everywhere else.

## Context

The Alpaca Account Clerk today (`PythonDataService/app/broker/alpaca/clerk/`,
~6,745 lines) is an in-process async service whose durable authority is two
append-only JSONL files per account (`order_inbox.jsonl`, `order_journal.jsonl`),
each fsync'd before broker contact. Every current-state question is answered by
folding the **entire** journal from byte 0 (O(n) per submit/cancel/reconcile).
Around that sit: an in-process `asyncio.Lock` plus a documented
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
   `(account, strategy_instance_id, action, intended_end_state)`. A transport
   retry of the same command returns the existing result with no new effect and
   no error; a genuine re-request returns a typed reason (e.g. "a stop has
   already been requested"); the UI renders the control disabled with a
   backend-authored tooltip. A random nonce is used only where re-issue is
   genuinely meaningful.

4. **Capture-before-contact is absolute.** `PRAGMA synchronous = FULL`; the
   intent-transition commit fsyncs **before** any broker call. Write latency is
   tracked as an observable, not traded away.

5. **The database enforces the single writer.** `BEGIN IMMEDIATE` + a bounded
   `busy_timeout` make a second writer serialize or error rather than corrupt —
   retiring the honor-system single-uvicorn-worker footgun (a robustness gain
   over today, where two workers corrupt silently).

6. **No PostgreSQL in scope.** The best-effort projection and its outbox,
   projector, cursor-tail reader, advisory lock, and rebuild runbook are
   removed. SQLite serves current-state *and* operator history/analytics for a
   single-operator local tool. Postgres re-enters only when a named ADR-0001
   trigger fires (concurrent multi-consumer load, hot cross-run analytics,
   authenticated multi-operator audit).

7. **Disaster recovery via a write-only append mirror.** Every committed
   transition is *also* appended to a plain fsync'd file that is **never read
   except to rebuild a corrupt database**. This restores graceful degradation
   and a rebuild source (the property "append-only log canonical" would have
   given) **without** introducing a second *authority* and without any hot-path
   read cost.

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

**Crown-jewel invariants preserved unchanged** (they are application logic,
orthogonal to storage, and must be re-proven under SQLite with tests):
capture-before-contact; never fabricate a terminal outcome from a single lost
response (30s grace + `by_client_order_id` resolution); namespace-attributed
exposure that never nets from the raw account position; cancel-first /
prove-terminal EXIT; live-idempotent websocket dedup.

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
- Corruption is survivable (append mirror rebuild) and detectable (hash chain +
  `integrity_check`); the account is reproducible from a single-file backup.
- The multi-worker footgun becomes a structural impossibility.

**Negative / costs**
- Capture-before-contact and never-fabricate-terminal must be **re-proven** under
  SQLite with adversarial tests; they do not come "for free" as they did from the
  hand-rolled JSONL discipline.
- Whole-file corruption is a new failure mode (mitigated by mirror + backups +
  startup `integrity_check`, and fail-closed on detection).
- A follow-up requires this ADR to supersede parts of four accepted ADRs for the
  Alpaca clerk; the boundary must be stated precisely (Alpaca only).
- Order replacement (PATCH), including Alpaca's larger-of-old-and-new
  buying-power behavior, is **net-new** work — it does not exist today and is not
  a rewrite of existing behavior.

## Qualification gate

Both gate before this ADR moves to Accepted-for-implementation: (a) the
adversarial correctness matrix (atomicity, idempotency, broker races, custody/
uncertainty, database failure incl. mirror-rebuild and hash-chain verification,
UI delivery), and (b) the performance budgets at the 1/10/100-bot and
10k/100k/1M-row fixtures. Live-money trading stays disabled throughout.
