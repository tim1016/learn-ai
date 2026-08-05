# Alpaca SQLite Clerk corrective foundation slice

**Status:** Proposed corrective slice, ready to implement

**Position in the sequence:** Insert after issue #1376 / PR #1387 and before
issue #1377 (broker-facing ENTER).

**Inputs:**

- `docs/prds/alpaca-account-clerk-sqlite-control-plane.md`
- `docs/architecture/alpaca-clerk-sqlite-pinned-contracts.md`
- `docs/architecture/adrs/0035-alpaca-clerk-sqlite-event-sourced-authority.md`
- `docs/audits/open-pr-review-2026-08-05.md`
- `/tmp/learn-ai-sqlite-clerk-handoff-2026-08-05.md`

## Outcome

Produce one corrected, internally consistent SQLite Clerk foundation containing
the work from PRs #1385, #1386, and #1387, plus every required correction from
their 26 unresolved review threads. The corrected foundation becomes the only
base for issue #1377 and later slices.

The slice does not add new product behavior or contact Alpaca. It repairs the
event model, durability fence, schema invariants, command lifecycle, and HTTP
boundary already introduced by the three open PRs.

## Decision

Accept the audit's central finding: the standalone `commands` reservation is
incompatible with the PRD's canonical event/fold transaction. Remove the split
`reserve_command()` → `append_transition()` design rather than teaching callers
how to recover stranded reservations.

The corrected invariant is:

> A command first becomes durable as part of a canonical custody transition.
> That transition and every projection it creates or advances commit in one
> SQLite transaction. The exact transition is mirror-finalized before the
> command is returned as accepted or any broker work becomes eligible.

For local Start/Stop, the first command event is also terminal. For future
broker work, the first command event atomically creates the command, effect,
and captured order identity in `accepted` state; later evidence events advance
the state.

## Integration and merge topology

Use an unreleased integration branch so the three existing PRs retain their
review history without exposing an incorrect intermediate state on `master`.

```text
master
└── codex/alpaca-clerk-sqlite-foundation       (never deploy while incomplete)
    ├── PR #1385 — pinned contracts
    ├── PR #1386 — repository spine
    ├── PR #1387 — local command lifecycle
    └── corrective foundation PR               (this slice; mandatory gate)
        └── new #1377 ENTER branch              (rebuilt after the gate)
```

Merge protocol:

1. Create `codex/alpaca-clerk-sqlite-foundation` from the current `master`.
2. Retarget and merge #1385 into that integration branch.
3. After #1385 is present, retarget and merge #1386 into the integration branch.
4. After #1386 is present, retarget and merge #1387 into the integration branch.
5. Create `codex/alpaca-clerk-sqlite-corrective-foundation` from that aggregate
   head and implement this slice as one focused PR back to the integration
   branch.
6. Do not merge the integration branch to `master`, deploy it, or use it for
   paper trading until the corrective PR passes every gate in this document.
7. After the corrective PR lands, reply to and resolve the 26 original review
   threads with links to the corrective commits/tests.
8. Open one foundation integration PR from the corrected integration branch to
   `master`. The description must state that it contains #1385–#1387 plus the
   corrective slice and that ADR 0035 remains Proposed; no live cutover occurs.
9. Recreate/rebase issue #1377 from the corrected foundation head. Do not use
   `517d4ba4` directly as the base for new broker-facing work.

This protocol intentionally permits the integration branch to be broken between
steps 2 and 5. The branch is an assembly area, not a release candidate. The
corrective PR is the indivisible merge gate for the aggregate.

## Protect the staged #1377 work first

The current index contains 1,287 staged lines for the ENTER slice. Preserve it
before moving branches, but do not merge it into the corrective slice:

- Save the exact staged state on a local safety branch such as
  `codex/wip-1377-enter-pre-correction` with an explicitly non-release WIP
  commit.
- Do not push or open that WIP as the #1377 PR.
- Transplant only the items marked "retain" in the salvage table below after
  the corrected foundation exists.

No cleanup operation may discard the current staged changes without first
verifying that safety commit contains all eight staged paths.

## Scope A — reconcile the contract and schema

### A1. One canonical source of business state

Update the PRD clarification and pinned contract together:

- `commands`, `runs`, `effect_operations`, `orders`, `receipts`, positions,
  holds, uncertainties, and reconciliations are projections of
  `custody_transitions`.
- No repository method may insert a business-state row outside a canonical
  transition transaction.
- Remove the transaction-matrix rows that permit bare command reservations.
- Local Start success, Start rejection, and Stop success each use one event and
  one projection transaction.
- Broker acceptance uses one event whose fold creates the command, effect, and
  captured order identity atomically before broker contact.

### A2. Typed replay facts

Pin a versioned facts type for every registered transition kind. Avoid an
untyped generic snapshot bag. Each fold type must contain the immutable inputs
that are not already present in the outer transition row.

Minimum facts:

| Transition | Required facts beyond the outer transition row |
| --- | --- |
| `RUN_STARTED` | command idempotency key/hash/kind/action/end state; lifecycle run id; optional operator reason |
| `COMMAND_REJECTED` | command idempotency key/hash/kind/action/end state; stable rejection reason; optional operator reason |
| `RUN_STOPPED` | command idempotency key/hash/kind/action/end state; lifecycle run id; optional operator reason |
| `ENTER_ACCEPTED` | command idempotency key/hash/kind/action; decision id; effect idempotency key/kind; complete immutable broker leg/captured order fields |

The fold must be able to rebuild an identical command/effect/order/receipt
resource from a finalized mirror without consulting the lost database or a
caller closure. `facts_schema_version` selects the typed parser.

### A3. Schema corrections

Update the pinned DDL and executable schema byte-for-byte in the same commit:

- bump `SCHEMA_VERSION` because old local databases must fail closed rather
  than open under changed constraints;
- store `GENESIS` in a non-null `custody_transitions.prev_hash`;
- couple `BOT`/`ACCOUNT_CLERK` scope to nullable strategy identity for both
  `holds` and `uncertainties`;
- enforce command/effect run ownership with composite foreign keys;
- backstop terminal command/effect state against regression;
- define `rejected` as a terminal command decision and create a durable
  rejection receipt; effects still cannot be rejected after broker custody;
- add durable operation-claim owner, unique fencing token, claimed-at, and
  expiry fields with a compare-and-swap claim rule;
- validate both authority generation and database identity token against the
  latest established-generation registry record;
- pin reset as a fresh database/mirror generation with the old generation
  retained read-only, not an in-place sequence restart.

No reset endpoint or generation-rotation implementation is part of this slice;
only the contract and startup invariant required by the already-introduced
schema are in scope.

## Scope B — replace the split command write

### B1. Repository-owned command commit

Introduce one explicit repository operation for first-command events. Its API
may use a small typed planner, but it must not expose a lock, cursor, connection,
or arbitrary SQL callback.

Conceptual inputs and outputs:

```text
CommandIdentity
├── command_id
├── authority_generation (repository supplied)
├── idempotency_key
└── payload_hash

CommandPlan
├── typed transition kind + facts
├── command projection fields
└── any run/effect/order/receipt projection fields

CommandCommitOutcome
├── Created(resource)
├── ExistingSame(resource)
└── ExistingConflict(resource)
```

The operation runs, under the repository's private write coordinator:

1. assert the repository/fence is healthy;
2. renew and verify the execution lease;
3. look up the content-addressed command identity;
4. return existing-same or existing-conflict without a new event;
5. validate the strategy instance and command-specific admission using a
   read-only repository view;
6. build the typed transition plan;
7. fsync PREPARE;
8. `BEGIN IMMEDIATE`;
9. insert the custody transition, apply its fold (including command creation),
   advance revision, and insert the mirror-fence row;
10. commit SQLite;
11. fsync the exact FINALIZE;
12. return the created durable resource.

Delete `reserve_command()` and the public `serialized()` context manager after
all Start/Stop callers use this operation. A direct `INSERT INTO commands`
outside a registered fold is forbidden by tests.

### B2. Local Start/Stop behavior

- Start success creates the command, active run, terminal receipt, and command
  `run_id` in the same fold.
- Start while already active creates a terminal rejected command and rejection
  receipt in one fold; it creates no second run.
- Stop request carries a stable `lifecycle_run_id`. Existing-same lookup happens
  before active-run admission, so a lost HTTP response can return the completed
  Stop command after the run is already stopped.
- A new Stop identity with no matching active run remains a typed 404.
- An unknown strategy instance is a typed domain not-found result, never a raw
  SQLite foreign-key 500.
- Same identity plus a different payload remains a durable 409 with no effect.

Update the Pydantic request/response models and committed OpenAPI snapshot for
the Stop identity change.

## Scope C — make the R9 fence one fail-closed state machine

### C1. Exact pair verification

Use one mirror verifier for startup and rebuild. A valid finalized sequence
requires:

- the same sequence;
- the same authority generation;
- the same row hash;
- a PREPARE whose predecessor and canonical payload recompute that hash;
- one non-conflicting FINALIZE for that identity.

Multiple abandoned PREPARE records for a sequence are permitted. Only the
PREPARE matching the finalized identity is selected. Conflicting FINALIZE
records, a missing matching PREPARE, a finalized sequence gap, generation
mismatch, or hash-chain mismatch fails closed.

Startup reconciles **every** committed SQLite transition against the mirror,
not only the highest sequence. A committed row without FINALIZE may be
finalized from the exact committed payload. An unrepairable mismatch rejects
the open.

### C2. Poison after uncertain finalization

If SQLite commits but FINALIZE fails or is uncertain:

- mark the live repository handle unavailable immediately;
- reject subsequent transition commits and all broker claims;
- permit only exact fence reconciliation or close;
- return a typed authority-unavailable error to HTTP callers;
- allow a reopened repository only after the full startup reconciliation
  succeeds.

### C3. Filesystem durability and confinement

- Resolve and confine the final `clerk.db` and mirror paths—not merely their
  account directory—before initialize, open, append, read, or rebuild.
- Reject existing or dangling symlink escapes.
- Fsync the parent directory after first creation of the mirror and established
  generations registry; propagate directory-fsync failures.
- Apply identical path rules to initialize, open, and rebuild.

## Scope D — leases and operation claims

- Use a unique process/boot UUID for the execution-lease owner; a PID alone is
  reusable and insufficient.
- Verify and renew the lease before every mutation and broker work claim.
- If the lease has expired, or its owner/token no longer matches, the old handle
  loses write authority and cannot reacquire silently.
- Claim an `effect_operations` row with a unique fencing token in a guarded
  SQLite update before broker contact.
- Later operation updates must present that fencing token.
- Expired claims may be taken over only through the pinned recovery path; the
  new worker still relies on the previously captured client order identity to
  reconcile before deciding whether broker submission is needed.

The claim primitive is implemented and tested here, but no actual Alpaca call
is added. Issue #1377 consumes it.

## Scope E — HTTP and module structure

- Move repository open, Start/Stop submission, and reads off the FastAPI event
  loop with `asyncio.to_thread` (or an equivalent explicit thread boundary).
- Keep routers limited to request parsing, facade calls, response shaping, and
  typed error translation.
- Retain the staged `reads.py` extraction, adapted to the corrected resource
  types.
- Retain the staged `idempotency.py` extraction, adapted to the new commit
  outcomes.
- Extract resource/plan dataclasses from `repository.py` if necessary; end the
  slice with `repository.py` below 900 physical lines so issue #1377 has
  structural headroom. No file may cross 1,000 lines.
- Do not introduce a new dependency.

## Staged #1377 salvage map

| Staged item | Disposition after correction |
| --- | --- |
| `sqlite/idempotency.py` | **Retain and adapt.** Shared typed errors are useful; remove dependencies on `ReservedNew`/`ReservedExisting`. |
| `sqlite/reads.py` | **Retain and adapt.** This is a legitimate decomposition; move resource types out of the giant repository if needed. |
| `sqlite/commands.py` extraction edits | **Retain selectively.** Keep shared imports, then rewrite Start/Stop around the canonical command commit. |
| `sqlite/enter.py` | **Do not cherry-pick wholesale.** Keep the broker identity and reconciliation ideas, but rebuild `accept_enter()` around the canonical command/effect/order transition and operation claim. |
| ENTER additions in `folds.py` | **Rework.** `ENTER_ACCEPTED` must create the command/effect/order from typed facts and leave the command/effect `accepted`, not fabricate success before broker evidence. Preserve the idempotent evidence/fill logic where its tests support it. |
| `test_enter.py` | **Retain as behavioral specification.** Rewrite fixtures/internal assertions; keep capture-before-contact, duplicate suppression, lost-response, attribution, and no-regression cases. Add database-rebuild parity and lease/claim fencing. |
| repository read delegates | **Retain.** Do not retain new split-reservation or public-lock usage. |
| `__init__.py` exports | **Re-evaluate after the new module boundary.** Export only stable domain types/functions. |

## Required tests

### Contract and schema

- schema DDL remains byte-for-byte equal to the pinned SQL block;
- version mismatch rejects an old database;
- `GENESIS` is stored literally and non-null;
- invalid scope/strategy combinations fail;
- cross-bot command/effect run links fail;
- terminal state regression fails;
- every terminal command, including rejection, has its specified proof;
- stale generation/token rollback fails startup;
- operation claim compare-and-swap and stale-token updates fail correctly.

### Command lifecycle

- Start success is one transition transaction and returns a linked run;
- Start rejection is one transition transaction;
- Stop success survives a lost response and returns the same resource by stable
  lifecycle identity;
- unknown bot is typed 4xx;
- same identity/same hash creates one event under concurrency;
- same identity/different hash produces 409 and no event/effect;
- injected failures cannot leave a raw `reserved` command because that state is
  no longer separately committed.

### Mirror and recovery

- abandoned PREPARE followed by a finalized reuse rebuilds correctly;
- FINALIZE-before-PREPARE pairs only when generation and hash match;
- a lone/wrong-hash/wrong-generation FINALIZE fails startup;
- an earlier missing FINALIZE is repaired or fails closed even when the tail is
  valid;
- finalize failure poisons the live handle and blocks a later append;
- database deletion followed by rebuild reproduces command, run, receipt,
  idempotency lookup, and transition timeline exactly;
- first mirror/registry creation includes directory fsync;
- database and mirror symlink escapes fail initialize/open/rebuild.

### Lease and HTTP

- a live handle renews before expiry;
- a second process cannot acquire while renewal is healthy;
- an old handle cannot write after another owner takes over;
- an effect claim token fences stale workers;
- deliberately blocked SQLite/fsync work does not block an unrelated async
  request;
- typed repository/fence/lease errors map to stable 4xx/503 responses.

## Verification gates

The corrective PR is complete only when all of these pass from the aggregate
feature branch:

1. focused SQLite Clerk schema, repository, command, router, and fault tests;
2. full `PythonDataService/tests/broker/alpaca/` suite;
3. full project-scope Python test suite, with any inherited failure proven
   against the integration-branch baseline and documented;
4. `ruff check PythonDataService/app/ PythonDataService/tests/`;
5. OpenAPI snapshot regeneration/check;
6. `git diff --check`;
7. a fresh thermonuclear code-quality review of the complete aggregate diff;
8. all 26 findings in `docs/audits/open-pr-review-2026-08-05.md` mapped to a
   correcting commit and regression test, or to an explicit contract decision.

The previously launched `pytest tests/ -k "not slow"` process is still running
and has no confirmed result as of this plan. It is not evidence for this gate.

## Definition of done

- The integration branch contains the exact work from #1385, #1386, and #1387
  plus this corrective slice.
- No direct business-state write exists outside a registered transition fold.
- `reserve_command()` and public `serialized()` no longer exist.
- Start/Stop idempotency survives concurrency, restart, lost response, and full
  mirror rebuild.
- The repository cannot write after losing its lease or after an unconfirmed
  mirror finalization.
- The mirror can rebuild every projected resource introduced so far.
- The aggregate branch passes all verification gates and is the only permitted
  base for the rebuilt #1377 ENTER slice.
- No live or paper-trading cutover is enabled; ADR 0035 remains Proposed until
  its separate human acceptance and qualification gates are satisfied.
