# Open pull-request review — 2026-08-05

## Scope and outcome

Reviewed all three open pull requests authored by `tim1016` in
[`tim1016/learn-ai`](https://github.com/tim1016/learn-ai/pulls), including every
unresolved inline review thread:

| PR | Reviewed commit | Scope | Open threads | Decision |
| --- | --- | --- | ---: | --- |
| [#1385](https://github.com/tim1016/learn-ai/pull/1385) | `23b6c5b1` | Pinned SQLite contracts | 10 | **Block** |
| [#1386](https://github.com/tim1016/learn-ai/pull/1386) | `04cdad0a` | SQLite repository spine | 9 | **Block** |
| [#1387](https://github.com/tim1016/learn-ai/pull/1387) | `517d4ba4` | Local Start/Stop command lifecycle | 7 | **Block** |

No GitHub comments were posted and no review threads were resolved. The
“address” text below is the proposed author response and implementation plan
for each existing reviewer comment.

The review used the thermonuclear maintainability standard: this is not an
approval bar based only on passing focused tests. It tests whether the stack
has one coherent source of truth, preserves fail-closed recovery, and avoids
locking in unnecessary control-flow complexity.

The checkout contains unrelated uncommitted work, so I reviewed the immutable
PR commits above rather than treating a test run on the current working tree as
evidence for any PR.

## Cross-stack blocker: the source-of-truth contract contradicts the PRD

This is the first repair. The pinned document describes a reservation as a
standalone `commands` insert with no transition or mirror record; #1387 then
implements it as a separately committed `reserve_command()` call. That is in
direct conflict with the higher-authority PRD:

- The PRD requires command reservation, effect creation, custody transition,
  projection fold, and revision advancement in **one SQLite transaction**
  ([PRD §4, goal 3](../prds/alpaca-account-clerk-sqlite-control-plane.md#L184)).
- It also says every business-state row, including `commands`, is a fold of the
  canonical `custody_transitions` log in the append transaction
  ([PRD §9.3](../prds/alpaca-account-clerk-sqlite-control-plane.md#L372)).
- The pinned document states the inverse: a reservation-only `commands` write
  has no transition or mirror fence ([#1385 contract §4](../architecture/alpaca-clerk-sqlite-pinned-contracts.md#L426)).

The pinned document itself ranks the PRD above the pin, so the PRD must win.
This is not a choice to make silently. Amend #1385 first, then rebase the two
implementation PRs on the corrected contract.

### The code-judo repair

Do not add a retry branch that tries to “resume” a raw `reserved` row. Remove
the split write for the local command slice instead:

1. Define a versioned transition facts envelope that contains every input needed
   to reconstruct the command, effect, order, receipt, and other affected
   projections. A finalized mirror line must be sufficient to recreate the
   exact durable command resource and its idempotency identity.
2. For local Start and Stop, decide admission and append **one terminal
   transition** whose fold creates the command, creates/stops the run, and links
   the receipt in the same transaction. There is no useful intermediate
   reservation to expose for a command that has no broker work.
3. For future broker-facing commands, use a first canonical command/effect
   transition that includes the immutable command snapshot, operation, and
   captured broker identity; later transitions only advance that state machine.
4. Keep the process write lock private to the repository operation. The public,
   re-entrant `serialized()` escape hatch currently makes every future command
   author responsible for reproducing a persistence protocol correctly. The
   single-transition model deletes that coordination layer for the local slice.

This one reframing fixes the stranded-reservation, rebuild, Start `run_id`, and
file-growth concerns together. It also brings the implementation back into
agreement with the PRD instead of adding compensating branches.

## Required repair order

1. **Correct and re-review #1385.** Pin the event/replay envelope, generation
   model, database invariants, and full mirror-reconciliation rules. Regenerate
   the schema parity fixture from that contract.
2. **Repair #1386 against the corrected contract.** Make the R9 fence, lease,
   path confinement, and durability mechanics fail closed. Move any generic
   spine correction currently hidden in #1387 down to #1386.
3. **Rebuild #1387 on the repaired spine.** Collapse local command admission to
   its canonical event transaction; then add typed boundary errors and
   non-blocking HTTP dispatch.
4. Only then re-run the full Python suite from the corrected base, plus the
   fault-injection and recovery cases listed at the end of this report.

## #1385 — pinned SQLite implementation contracts

**Decision: block.** This is a 627-line contract document, not an ordinary
docs-only change: its schema and transaction statements are copied literally
into the implementation. Its unresolved issues are therefore implementation
blockers for every downstream slice.

### Existing reviewer comments and proposed responses

| Priority | Thread | Disposition and address |
| --- | --- | --- |
| P1 | [Mirror lacks projection inputs](https://github.com/tim1016/learn-ai/pull/1385#discussion_r3718130324) | **Agree.** `payload_canonical` contains only `custody_transitions` columns; it cannot rebuild `commands.idempotency_key`, `payload_hash`, action, or operation/order metadata. Pin the versioned replay envelope described above and require projection-equivalence recovery tests for accepted, rejected, Start, Stop, and broker-effect commands. |
| P1 | [Reset sequencing cannot satisfy the schema](https://github.com/tim1016/learn-ai/pull/1385#discussion_r3718130326) | **Agree.** An immutable, global `sequence` primary key cannot restart at 1 in the same database. Pin reset as a freshly initialized database and mirror generation, retaining the prior database/mirror read-only for audit. Do not mutate the old `control_meta` in place. Test a reset after non-empty history, then verify the new generation begins at 1 and the old generation remains auditable. |
| P1 | [Registry does not validate the active generation](https://github.com/tim1016/learn-ai/pull/1385#discussion_r3718130338) | **Agree.** Startup must compare the database’s **generation and token** with the registry’s latest entry, not merely compare a token if present. Reject a restored older database after reset; test that exact rollback case. |
| P1 | [Operation claim is promised but absent](https://github.com/tim1016/learn-ai/pull/1385#discussion_r3718130339) | **Agree.** Add a durable owner, unique claim/fencing token, expiry, and compare-and-swap claim protocol for `effect_operations`. Pair it with lease renewal and captured broker idempotency identity so an expired worker cannot duplicate broker contact. Exercise competing workers and a crash after claim before contact. |
| P2 | [Terminal state can regress](https://github.com/tim1016/learn-ai/pull/1385#discussion_r3718130328) | **Agree.** Pin the allowed state-transition matrix and enforce it at the sole fold boundary, with a SQLite backstop rejecting a transition away from a terminal state. Include `rejected` in the explicit terminal-decision discussion rather than leaving its semantics implicit. |
| P2 | [Hold/uncertainty scope is not coupled to bot identity](https://github.com/tim1016/learn-ai/pull/1385#discussion_r3718130331) | **Agree.** Add the same `CHECK` to both tables: `BOT` requires a non-null strategy instance and `ACCOUNT_CLERK` requires null. Add four direct DDL tests for valid and invalid combinations. |
| P2 | [Genesis predecessor is contradictory](https://github.com/tim1016/learn-ai/pull/1385#discussion_r3718130335) | **Agree.** The schema says null while the hash rule says `GENESIS`. Store the `GENESIS` sentinel consistently, make the database column non-null, and test the literal first-row value and hash. |
| P2 | [Terminal transactions omit receipts](https://github.com/tim1016/learn-ai/pull/1385#discussion_r3718130337) | **Partly agree; clarify the product rule.** Successful/failed local terminal and reconciliation transactions must create and link the required receipt atomically. For rejection, the contract must explicitly choose either an immutable rejection receipt or state that the canonical rejection transition is its proof; #1387 currently chooses the latter without the contract saying so. Do not leave this as an accidental schema limitation. |
| P2 | [Only the mirror tail is checked](https://github.com/tim1016/learn-ai/pull/1385#discussion_r3718130342) | **Agree.** Check every committed sequence against an exact PREPARE/FINALIZE pair before accepting more work. A tail-only check leaves an earlier unrecoverable gap invisible. |
| P2 | [Commands/effects may point at another bot’s run](https://github.com/tim1016/learn-ai/pull/1385#discussion_r3718130346) | **Agree.** Add a unique `(strategy_instance_id, run_id)` parent key and composite foreign keys from commands and effects. Test cross-bot combinations fail while pre-run null remains valid. |

### Additional structural direction

The contract currently calls `custody_transitions` “the sole canonical
authority” while allowing a command to exist only in `commands`. The source
model must be made internally consistent before more tables or DDL triggers are
added. Otherwise every later slice will need a bespoke recovery exception and
the event-sourced design will become a collection of parallel ledgers.

## #1386 — event-sourced SQLite repository spine

**Decision: block.** The spine should be the smallest, most reliable part of
the system. It currently allows an expired owner to continue writing and can
continue after an unresolved mirror-finalization failure. Those are fail-open
behaviors in the custody authority.

### Existing reviewer comments and proposed responses

| Priority | Thread | Disposition and address |
| --- | --- | --- |
| P1 | [Lease is never renewed](https://github.com/tim1016/learn-ai/pull/1386#discussion_r3718287605) | **Agree.** A 30-second lease acquired only at open is not a live-process fence. Use a per-process UUID owner, renew before every write/claim under the writer coordinator, and reject writes if ownership was lost or the lease expired. An owner must fail closed rather than “renew” after another process could have acquired it. |
| P1 | [Repository accepts later appends after finalize fails](https://github.com/tim1016/learn-ai/pull/1386#discussion_r3718287607) | **Agree.** Once the database commit has happened, a failed/unconfirmed finalize poisons the handle. Prevent all further appends and broker eligibility until exact pair reconciliation succeeds; startup must reconcile all committed sequences, not only the tail. Fault-inject a failure after commit and prove a later write is rejected. |
| P2 | [First mirror creation lacks parent-directory fsync](https://github.com/tim1016/learn-ai/pull/1386#discussion_r3718287610) | **Agree.** After the first successful mirror-file write, fsync its parent directory (and propagate failure). Test the creation path through a controllable filesystem seam. |
| P2 | [`clerk.db` is not itself confined](https://github.com/tim1016/learn-ai/pull/1386#discussion_r3718287616) | **Agree.** Resolve and confine the final database path before existence checks and before `sqlite3.connect`; account-directory confinement does not protect a child symlink. Apply the same rule to initialize and rebuild. |
| P2 | [Rollback leaves a poisoned prepared sequence](https://github.com/tim1016/learn-ai/pull/1386#discussion_r3718287619) | **Addressed only downstream, not in this PR.** #1387 changes rebuild to select a PREPARE by the finalization hash, which is the right direction for abandoned preps. Move that generic mirror correction into #1386 and add the commit/fold-failure regression test there. It must not be delivered as an incidental command-lifecycle change. |
| P2 | [First registry creation lacks parent-directory fsync](https://github.com/tim1016/learn-ai/pull/1386#discussion_r3718287624) | **Agree.** Fsync `accounts/alpaca` when the registry file is first created, just as for the mirror. Otherwise the anti-silent-recreation evidence is not durable. |
| P2 | [FINALIZE before PREPARE is not validated](https://github.com/tim1016/learn-ai/pull/1386#discussion_r3718287627) | **Addressed only downstream, not in this PR.** #1387 retains the FINALIZE hash and matches it to a PREPARE during rebuild. Move this to #1386, and also require authority-generation equality in the pair. |
| P2 | [Startup accepts any same-sequence FINALIZE](https://github.com/tim1016/learn-ai/pull/1386#discussion_r3718287630) | **Agree; still unfixed in #1387.** `has_finalize(sequence)` checks only the sequence. Replace it with a full pair verifier covering sequence, generation, row hash, predecessor, and canonical payload; use the same verifier for startup and rebuild to avoid two subtly different integrity policies. |
| P2 | [Mirror file is not itself confined](https://github.com/tim1016/learn-ai/pull/1386#discussion_r3718287633) | **Agree.** Resolve/constrain the final mirror path before every open/read/append. A symlink inside a safe account directory must fail startup rather than redirect recovery evidence outside the artifact root. |

### Structural direction

The R9 behavior should be one small state machine, not a collection of
independent `prepare`, transaction, `finalize`, and tail-check helpers. Give
the repository a single “fence healthy” invariant. A failure after commit moves
it to unavailable; a full exact reconciliation repairs it; only then may any
new transition or broker claim proceed. This deletes the need for callers to
guess whether it is safe to continue.

## #1387 — content-addressed local Start/Stop lifecycle

**Decision: block.** This PR has good local intentions—domain code is kept out
of the generic repository and competing Starts are serialized—but it preserves
the split reservation architecture that the PRD forbids. It adds 210 lines to
`repository.py`, taking it from 763 to 973 lines, while introducing a public
locking protocol future features must remember to use. Do not let the next
slice push this file over 1,000 lines; the contract correction above should
delete, rather than elaborate, the new reservation/serialization machinery.

### Existing reviewer comments and proposed responses

| Priority | Thread | Disposition and address |
| --- | --- | --- |
| P1 | [Reservation can be stranded](https://github.com/tim1016/learn-ai/pull/1387#discussion_r3720854907) | **Agree.** A crash between `reserve_command()` and `append_transition()` leaves a forever-`reserved` command; same-key retries return it without admission. Fix with the single canonical transaction, not a compensating “resume reserved” branch. Add crash injection at each former boundary and assert a retry either returns a complete terminal resource or safely completes the one canonical event. |
| P1 | [Recovery cannot rebuild command rows](https://github.com/tim1016/learn-ai/pull/1387#discussion_r3720854922) | **Agree.** Rebuild begins with an empty `commands` table, so receipt inserts hit foreign keys and updates no-op. The transition facts must reconstruct the command projection before any terminal fold; the contract-level replay envelope is the required fix. |
| P1 | [Cached repositories allow the lease to expire](https://github.com/tim1016/learn-ai/pull/1387#discussion_r3720854928) | **Agree.** The cache makes the #1386 renewal defect inevitable in normal operation. Renew and verify the lease at write/claim time; never return a cached handle that can write after losing ownership. |
| P2 | [Stop retry loses the active-run identity](https://github.com/tim1016/learn-ai/pull/1387#discussion_r3720854916) | **Agree.** After a successful Stop with a lost response, there is no active run from which to derive the old key. Require a caller-stable Stop identity (for example the lifecycle run id or a client command token), look up that command before active-run admission, and return it on retry. A new request with no matching identity and no active run remains a typed 404. |
| P2 | [Synchronous SQLite/fsync blocks the FastAPI event loop](https://github.com/tim1016/learn-ai/pull/1387#discussion_r3720854934) | **Agree.** The router calls synchronous database and file I/O directly from `async def`. Dispatch repository open, command submission, and reads through `asyncio.to_thread` (or adopt async primitives). The repository comment claiming handlers already use `to_thread` is currently false; make the code and comment agree. |
| P2 | [Unknown bot ID becomes raw SQLite 500](https://github.com/tim1016/learn-ai/pull/1387#discussion_r3720854944) | **Agree.** Validate registered strategy-instance identity inside the same atomic admission operation and raise a typed domain error translated to 404/declined-command response. Cover unknown Start and Stop routes; do not rely on a raw foreign-key exception as API validation. |
| P2 | [Successful Start command keeps `run_id = null`](https://github.com/tim1016/learn-ai/pull/1387#discussion_r3720854948) | **Agree.** The current terminal fold updates state/receipt but does not link the created run. The single-event Start fold should create the command with its run identity, eliminating the delayed backfill; otherwise update `commands.run_id` in the same transition transaction and test `GET /commands/{id}`. |

### Focused design observations

- `serialized()` is a thin synchronization abstraction that leaks an internal
  invariant into domain modules. It makes correctness depend on every future
  caller wrapping precisely the right reads and writes. Keep the locking and
  transaction boundary behind one repository operation instead.
- The focused tests cover happy-path idempotency and concurrent Starts, but not
  the failure boundaries that define this PR: crash after reservation, lost
  response after Stop, recovery/rebuild of command resources, lease expiry while
  cached, unknown bot ID, or event-loop responsiveness under slow fsync.

## Required regression suite

The repaired stack should add tests that fail on the reviewed commits and pass
only after the source-model change:

1. **Atomic command admission:** Start accepted, Start rejected, and Stop each
   leave either no command or a fully projected command/run/receipt/log state;
   inject failure before prepare, after prepare, during the SQLite transaction,
   and after commit before finalize.
2. **Retry semantics:** lost response after Start and Stop returns the same
   resource; same identity/different semantic payload yields 409; a fresh Stop
   without an active run yields the typed no-active-run outcome.
3. **Mirror recovery:** remove/corrupt the database after each lifecycle result,
   rebuild, and compare command, run, receipt, idempotency lookup, and timeline
   state with the pre-loss snapshot. Test every committed sequence, not just
   the tail, for missing/tampered/mismatched PREPARE/FINALIZE data.
4. **Lease and operation claims:** use a controllable clock and two independent
   repository instances. A live owner renews; an expired/dead owner can be taken
   over; an owner that has lost the lease cannot append or claim broker work.
5. **Filesystem durability and confinement:** first mirror/registry file
   creation fsyncs its directory; database and mirror symlink escapes fail
   closed on initialize, open, and rebuild.
6. **Schema invariants:** exercise terminal-state regression, scope coupling,
   cross-bot run links, genesis storage, active-generation rollback, and the
   claim compare-and-swap directly against SQLite.
7. **HTTP isolation:** a deliberately blocked repository operation does not
   stall an unrelated request, and typed unknown-bot errors remain 4xx rather
   than raw SQLite 500s.

## Approval condition

Re-review after the root contract correction, spine repair, and lifecycle
rebuild described above. Until then, the stack has no defensible proof that a
durable command can survive a crash, a lost response, or a database rebuild
without either becoming stranded or losing its identity.
