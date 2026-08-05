# PRD — Alpaca Bot Control Safety, Run History, and Live Updates

- **Date:** 2026-08-02
- **Status:** Published for implementation planning
- **GitHub:** https://github.com/tim1016/learn-ai/issues/1344
- **Product surfaces:** Alpaca Bots roster, Trader bot panel, Operator bot panel, lifecycle actions, Dry Run
- **Source:** `docs/architecture/alpaca-bot-control-remediation-research-plan-2026-08-02.md`
- **Builds on:** the Alpaca Account Clerk, broker-v2 panel, existing versioned SSE utilities, the Clerk-governed bot-control PRD, and the fleet/control/deploy redesign PRD
- **Authority:** the process registry owns process liveness; the Account Clerk owns reconciled broker custody; Python authors admission and receipt meaning; Angular renders those answers.

> **Storage-decision follow-up (2026-08-04):**
> [`alpaca-account-clerk-sqlite-control-plane.md`](alpaca-account-clerk-sqlite-control-plane.md)
> proposes replacing this PRD's JSONL-authority and cold-replay requirements
> with an account-scoped SQLite authority and operation-first custody timeline.
> JSONL remains the accepted implementation authority until a follow-up ADR
> supersedes ADRs 0001, 0030, and 0033 and the SQLite cutover qualifies.

---

## 1. Executive summary

The Alpaca Bot Control panel must become a stable control room rather than a
screen that repeatedly reconstructs itself from files.

Today, recurring requests can reread and parse the account journal, while the
chart can replace its complete candle set every five seconds. This creates slow
refreshes and visible UI churn. The control path also needs a clearer authority
boundary: bot/process evidence answers whether a run is alive, while the Account
Clerk must be the only source for reconciled positions, working orders,
unresolved effects, and custody state.

This PRD delivers five connected outcomes:

1. Start and Resume decisions use one backend-owned run-admission function with
   two typed inputs: bot facts and Clerk truth.
2. Strategy instances remain immutable while every execution is an append-only
   run that can be inspected later.
3. Commands remain safe through browser refreshes and network retries because
   the backend owns a durable idempotency ledger.
4. The panel loads one snapshot, then receives versioned SSE updates without
   resetting the screen or chart.
5. Market-data freshness becomes a prominent header element called Market
   Pulse, because no data means the strategy cannot make a current decision.

When the system is uncertain, it does not needlessly destroy the bot process.
It may keep the process alive to cancel, reduce exposure, or reconcile, but it
must block new exposure and show the uncertainty clearly.

## 2. Problem

### 2.1 Refreshing is expensive and visually disruptive

- Recurring catalog, panel, and evidence reads can scan historical journal
  content instead of consuming an already-built current projection.
- The V2 panel polls on a timer even when no meaningful state changed.
- The chart calls full `setData` during routine live refreshes rather than
  updating only the current or newly appended candle.
- Repeated object replacement can disturb expansion, selection, scroll, and
  historical-run context.

### 2.2 Custody and account meaning can be assembled in multiple places

The current Start service reads process information and several separate
account-level guards. That allows the presented capability and the executed
policy to drift, and it exposes account interpretation outside the Clerk even
though the Clerk already reconciles account truth.

The intended boundary is:

```python
decision = evaluate_run_admission(bot, clerk)
```

The account still exists, but its positions, orders, reconciliation, holds, and
freshness are represented to callers through the Clerk's typed custody snapshot.
There is no third independent `account` policy input.

### 2.3 Instance, run, pause, and Resume language is overloaded

- A strategy instance is durable configuration and custody identity.
- A run is one process lifetime.
- Continuing a paused live process and creating a new run have both been called
  Resume in parts of the existing system.
- Current and previous runs are not presented as a coherent, lazy history in
  both Trader and Operator views.

### 2.4 A browser retry is not yet sufficient proof of command safety

The browser can lose a response after the backend started a command. A second
POST must not create a second Stop, flatten, Start, or broker effect. Browser
storage can remember a pending request, but only a backend-persisted command
ledger can make the retry authoritative.

### 2.5 Terminal words can outrun terminal proof

`Stopped`, `flat`, `cleared`, and `success` are safety claims. They must not be
displayed merely because a request returned, a timeout expired, or the UI thinks
the expected state probably occurred.

## 3. Goals

1. Make the Clerk the sole caller-facing authority for account custody.
2. Make process liveness explicit, typed, and independent from custody.
3. Ensure the exact same admission decision authors the UI and permits the
   command.
4. Preserve immutable strategy instances and append-only run history.
5. Add a correct new-run Resume and a clearly separate same-run Continue.
6. Add a first-class Dry Run that cannot submit broker orders.
7. Make every command retry-safe across response loss, page refresh, and backend
   restart.
8. Remove historical journal replay from warm request paths.
9. Deliver stable, incremental panel and chart updates.
10. Make market-data state and timing impossible to miss.
11. Retire the superseded Interactive Brokers Bot Control surface after its
    useful SSE behavior is migrated and proven.

## 4. Non-goals

- Live-money trading support.
- A new external event broker, Redis dependency, WebSocket platform, or
  distributed control plane.
- Moving numerical trading logic into .NET or Angular.
- Letting the UI infer safety, custody, fill, order, or terminal meaning.
- Editing a strategy instance's immutable configuration through Resume.
- Loading all historical runs or the full journal into the initial panel.
- Storing broker credentials, control secrets, or authentication tokens in
  browser storage.
- Treating SSE delivery as the source of truth. SSE carries authoritative
  snapshots; it does not author their meaning.

## 5. Users and primary questions

### Trader

The trader needs short answers:

- Is this bot running?
- Is its market data current?
- May it create new exposure?
- What position and working orders does the Clerk attribute to it?
- What happened during the current run?
- Can I Continue, Stop, Resume, or start a Dry Run safely?
- What happened in a previous run?

### Operator

The operator additionally needs:

- Which process identity and run are currently bound?
- Which Clerk generation, journal sequence, and reconciliation prove custody?
- Is a command pending, unknown, or terminal?
- Which exact receipt supports `Stopped`, `flat`, or `cleared`?
- Did SSE reconnect cleanly, backfill from its cursor, or recover through a fresh
  snapshot?

## 6. Canonical product language

### Strategy instance

One immutable configured bot, identified by `strategy_instance_id`. Changing
strategy, symbol, quantity/action plan, or submission mode creates a new strategy
instance.

### Run

One process lifetime of a strategy instance, identified by `run_id`. Run records
are append-only and remain available as historical evidence.

### Continue

Allow an existing paused and authoritatively live run to proceed. Continue keeps
the same `run_id`.

### Resume

Create and bind a new run of the same immutable strategy instance after the
prior run stopped. Every successful Resume creates a new `run_id`.

### Dry Run

A non-submitting instance/run that consumes real market data and produces
clearly labelled simulated decisions and fills. It must have a tested
zero-broker-write guarantee.

### Bot process fact

The process registry's typed observation of the process bound to a run. It
contains instance ID, run ID, process identity, lifecycle state, registry
generation, and observation time. A PID or artifact alone is not liveness proof.

### Clerk custody snapshot

The Clerk's typed answer about account identity, instance-attributed exposure,
working/open orders, unresolved effects, holds, reconciliation, freshness, and
evidence references. Unknown or stale facts remain explicit.

### Command ID

The durable identity of one operator intent across retries. The same ID plus the
same request returns the same backend record. The same ID plus a different
request is a conflict.

### Market Pulse

The persistent header display of market session, feed state, latest market-data
time in ET, and data age.

## 7. Product and safety principles

1. **Uncertainty blocks new exposure.** Cancellation, reduction, exit, and
   reconciliation remain available when they can make the account safer.
2. **Process liveness is not custody.** A running process does not prove an
   order, position, fill, or flat account.
3. **Clerk custody outranks local state.** The bot never declares broker truth.
4. **One decision function.** Projection and execution consume the same typed
   admission logic.
5. **Terminal words require terminal evidence.** No optimistic success copy.
6. **Retries preserve command identity.** Transport failure does not create new
   intent.
7. **Cold replay, warm projection.** Historical scans happen at startup or
   recovery, never during routine panel refresh.
8. **Stable screen.** Background updates preserve the user's visual and
   interaction context.

## 8. Functional requirements

### R1 — Typed bot process fact

The process registry must return:

```text
strategy_instance_id
run_id
process_identity
state: STARTING | RUNNING | STOPPING | EXITED | UNKNOWN
registry_generation
observed_at_ms
```

- Missing, stale, or contradictory evidence produces `UNKNOWN`.
- `RUNNING` is unavailable until the process registry proves it.
- Desired state remains separate: `RUNNING | PAUSED | STOPPED` describes
  operator intent, not process liveness.

### R2 — Clerk-owned custody snapshot

The Clerk must provide one typed snapshot containing at least:

- broker account identity and Clerk generation;
- snapshot/reconciliation time and journal sequence;
- reconciliation state and freshness;
- instance-attributed positions;
- working, pending, and terminal order counts/states;
- unresolved or uncertain effects;
- account and instance holds/freezes;
- stable reason codes, evidence references, and operator next steps.

The snapshot must distinguish `zero`, `non-zero`, and `unknown`. Missing data may
never be converted to zero.

### R3 — One Start/Resume run-admission function

Start and Resume capability projection and execution must call the same pure
typed function:

```python
decision = evaluate_run_admission(bot, clerk)
```

- `bot` is a typed `StartRunFacts | ResumeRunFacts` input. Both variants supply
  immutable instance facts, proposed/current run facts, process-registry
  evidence, required market-data freshness, and lifecycle intent. Resume also
  supplies the stopped `previous_run_id`, proposed new `run_id`, and immutable
  configuration hash.
- `clerk` supplies all custody and reconciled account meaning.
- The function returns a typed `RunAdmissionDecision` containing the operation,
  `allowed`, stable reason code, explanation, next step, evaluated time, fact
  ages, and evidence references.
- Execution reruns that exact function under the Clerk admission lock
  immediately before mutation. Resume may bind the proposed new run only while
  that lock is held and only after the decision admits the same immutable
  instance configuration and fresh Clerk custody snapshot.
- Angular renders the result without rebuilding the rule.

### R4 — Honest terminal state

- `Stopped` requires durable Stop intent, revoked entry admission for the run,
  and process-owner evidence that the process exited or is otherwise terminal.
- `Flat` requires Clerk evidence that attributed exposure is zero and relevant
  working entry/exit orders are terminal.
- `Cleared` requires the registered proof for the specific hold reason.
- `Success` requires the owning backend component's terminal receipt.
- Pending work displays `accepted` or `in_progress`.
- Unprovable work displays `unknown` and continues reconciliation.
- A stopped instance with approved carryover is labelled `Stopped with
  carryover`, never `flat`.

### R5 — Run identity, Resume, Continue, and history

- Instance configuration is create-once and hash-verifiable.
- Run records are append-only.
- Continue keeps the current live `run_id`.
- Resume creates a new run and current-run binding for the same instance only
  after fresh Clerk custody proof passes.
- A Resume request containing changed immutable settings is rejected with a
  clear instruction to create a new strategy instance.
- Until new-run Resume exists end to end, the V2 panel does not show a Resume
  button.
- Trader and Operator lenses show Current Run first and fetch one historical run
  at a time by date.
- Selecting historical evidence cannot retarget any lifecycle command.

### R6 — First-class Dry Run

- The existing `shadow`/no-submit engine path is the preferred implementation
  candidate and must be validated before the UI promise is enabled.
- A Dry Run consumes the configured market-data source.
- It never submits, cancels, or modifies a real broker order.
- Simulated orders/fills are visually and structurally labelled.
- A live-paper instance cannot change into dry mode. The UI creates a new
  dry-run strategy instance with a new ID.
- If an instance is already configured for dry mode, a new start creates a new
  run of that dry instance.

### R7 — Durable command idempotency

Angular generates `command_id` with `crypto.randomUUID()` before POST. The
backend must persist the command before executing an effect.

Minimum backend record:

```text
command_id
target
action
request_hash
state
receipt
created_at_ms
updated_at_ms
```

Required behavior:

- same ID and same canonical request hash: return the existing record/receipt;
- same ID and different target, action, or payload: return conflict;
- execution underway: return `in_progress`;
- outcome cannot be proved: return `unknown` and continue reconciliation;
- proven completion: return the original terminal receipt without repeating the
  effect.

Provide `GET /commands/{command_id}` for refresh and reconnect recovery.

The browser keeps the active command in memory and stores only small, non-secret
pending-command metadata in IndexedDB. A narrowly scoped local-storage fallback
is acceptable if IndexedDB adds unjustified complexity. The pending browser
record is removed only after a terminal backend receipt.

### R8 — Account-scoped live projection

Create one account-scoped projection owner that:

1. replays the Clerk journal once during startup/recovery;
2. tails only newly appended events using sequence/offset;
3. maintains current bot, Clerk, command, and bounded evidence projections;
4. publishes immutable snapshots with stream epoch and monotonic revision;
5. serves warm REST requests without reopening historical journal content;
6. marks output stale/unknown and blocks new exposure on sequence gaps,
   corruption, or loss of Clerk authority.

JSONL remains the durable authority. A rebuildable local index is allowed only
if measured evidence paging cannot meet the budget through offsets alone.

### R9 — Initial snapshot plus versioned SSE

The panel delivery sequence is:

1. Fetch one versioned REST snapshot.
2. Open a versioned SSE stream for meaningful later snapshots/revisions.
3. Resume from a durable cursor when possible.
4. Fetch a fresh snapshot after epoch change, cursor gap, or invalid replay.
5. Use bounded revision/ETag polling only as fallback while SSE reconnects.

SSE transport state does not prove Clerk, broker, process, or market-data health.

### R10 — Reuse and prune the old IBKR implementation

Reuse only broker-neutral behavior from:

- `authenticated-sse-connection.ts`;
- `versioned-snapshot-stream.ts`;
- their reconnect, malformed-event, epoch, and latest-version-wins tests.

Create a thin V2 adapter for the new panel contract. Do not copy the legacy
`LiveInstanceStatus` view model or its old action/safety meanings.

Delete the original Interactive Brokers Bot Control routes, components, stores,
and presentation contracts only after:

- V2 passes the full SSE/REST fallback parity matrix;
- route, template, and import searches find no consumers;
- bookmarks have an approved redirect or removal decision;
- generic SSE utilities are retained only if still used.

### R11 — Stable Angular updates

- Retain the last same-session snapshot while reconnecting and mark it stale.
- Do not display a full-page loading state after initial load.
- Do not replace the current signal/object when an incoming revision is not
  newer.
- Merge rows by stable identity.
- Preserve selected lens, selected run, expanded rows, scroll, and chart range.
- Update the current/new candle incrementally.
- Use full chart `setData` only on first load, symbol/range change, or explicit
  gap recovery.
- Show refresh/reconnect state as a small status treatment, not a page flash.

### R12 — Market Pulse header

The panel header contains a persistent Market Pulse with:

- market session: pre-market, open, after-hours, or closed;
- feed state: `LIVE`, `STALE`, or `MISSING`;
- latest usable bar/event time in ET;
- age such as `4s ago`;
- expected cadence/source when useful;
- a backend-authored reason and next step when degraded.

Example healthy presentation:

```text
MARKET DATA  ● LIVE
Last bar 10:35:00 ET · 4s ago
```

When the market is closed, the header says that no live bar is expected instead
of raising a false alarm. Required stale or missing data blocks data-dependent
Start/Continue admission through the backend decision.

## 9. UX structure

### Persistent header

- Bot/strategy identity and paper mode.
- Current process/run state.
- Market Pulse.
- Clerk custody summary.
- Last panel snapshot time and stream/reconnect state.

### Run navigation

- `Current Run` is pinned first.
- `Previous Runs` opens a date-labelled list with bounded pagination.
- Only one previous run is fetched and rendered at a time.
- Historical mode displays a clear `Viewing history — controls apply to the
  current run` notice or hides controls entirely.

### Trader lens

- Plain-language readiness and next action.
- Market Pulse and current run timing.
- Attributed exposure and working-order summary from the Clerk.
- Stable chart and recent activity.
- Dry Run is permanently distinguished from paper submission.

### Operator lens

- Process fact, Clerk generation, reconciliation age, and journal sequence.
- Command ledger state and receipt recovery.
- Exact open/resolved order evidence and attributed positions.
- Current or selected historical run evidence.
- SSE/replay diagnostics in expandable technical details.

## 10. Performance and reliability budgets

Validate on 1, 10, and 100 bots with up to 1,000,000 journal entries:

- warm catalog server p95 below 100 ms;
- warm panel server p95 below 75 ms;
- zero historical journal bytes parsed during an unchanged warm refresh;
- meaningful new state visible within five seconds under polling fallback and
  sooner when SSE is healthy;
- one cold replay per process start, journal rotation, or explicit recovery;
- no duplicate command effect after response loss or page refresh;
- no loss of chart zoom, scroll, expansion, lens, or selected run during routine
  updates.

Use structural counters in addition to wall-clock measurements: bytes read,
lines parsed, objects validated, snapshots emitted, chart `setData` calls, and
duplicate-effect count.

## 11. Delivery slices

### Slice 1 — Authority and truthful lifecycle

- Typed bot process fact.
- Clerk custody snapshot.
- `evaluate_run_admission(bot, clerk)` shared by Start/Resume projection and execution.
- Run-generation fence and honest Stop/unknown outcomes.
- Reason-specific hold clearance.

### Slice 2 — Instance/run model and safe actions

- Immutable instance repository and migration.
- Append-only runs and current-run binding.
- Lazy current/history APIs and UI.
- Same-run Continue and new-run Resume.
- Verified Dry Run.

### Slice 3 — Durable command recovery

- Backend command ledger and canonical request hash.
- `GET /commands/{command_id}`.
- Angular command ID generation and pending IndexedDB record.
- Transport-loss and backend-restart tests.

### Slice 4 — Projection, SSE, and stable rendering

- Account-scoped tailing projection owner.
- Initial REST snapshot, versioned SSE, and bounded fallback polling.
- Market Pulse.
- Incremental chart/row updates and interaction preservation.
- Measured 100-bot/1M-entry budgets.

### Slice 5 — Legacy retirement

- Migrate the broker-neutral IBKR SSE tests/utilities.
- Prove V2 parity and redirects.
- Delete the superseded IBKR Bot Control surface and unused contracts.
- Run the strict frontend maintainability review on the final surface.

Slices are ordered. Later slices must not hide or compensate for an unresolved
authority or lifecycle defect in Slice 1.

## 12. Required acceptance tests

### Authority

- Projection and mutation return the same Start decision for the same bot/Clerk
  facts.
- Direct account interpretation outside the Clerk cannot authorize Start.
- Stale or missing Clerk truth returns unknown/blocked, never empty/flat.
- A running process plus unknown custody cannot create new exposure.

### Lifecycle and runs

- Stop racing a late ENTER rejects the new exposure at the Clerk boundary.
- Stop cannot become terminal while its process remains authoritatively alive.
- Resume twice produces one immutable instance and three append-only runs.
- Continue retains the current run ID.
- Changed configuration during Resume is rejected.
- Viewing a previous run never retargets a command.
- Dry Run produces simulated activity and zero broker-write calls.

### Commands

- Same command ID and payload executes once and returns the original receipt.
- Same command ID with changed payload returns conflict.
- Lost HTTP response recovers through `GET /commands/{command_id}`.
- Unknown remains recoverable and resolves only from later authority evidence.
- Browser reload restores pending status without producing a second effect.
- Corrupt or deleted browser storage cannot weaken backend idempotency.

### Projection and UI

- Startup performs one complete replay; warm unchanged requests parse zero
  historical lines.
- SSE reconnect resumes from the last cursor without duplicate visible rows.
- Epoch change or gap forces a conservative fresh snapshot.
- An unchanged revision causes no meaningful DOM or chart mutation.
- A new candle uses incremental update rather than full `setData`.
- Background updates preserve zoom, scroll, selection, expansion, lens, and run.
- Stale/Missing Market Pulse states are prominent and block the correct actions.
- Market-closed state does not raise a false missing-data alarm.

### Legacy deletion

- No route, import, template, or navigation consumer references the deleted
  IBKR Bot Control surface.
- All migrated SSE contract and reconnect tests pass against V2.
- No legacy frontend safety rule survives as a second authority.

## 13. Observability and recovery

Record metrics/logs for:

- projection replay duration, tail offset/sequence, and lag;
- journal bytes and lines read per request;
- stream epoch, revision, active subscribers, reconnects, and cursor gaps;
- panel snapshot age and Market Pulse age;
- command state duration and unknown-to-terminal reconciliation;
- Start refusal reason codes;
- chart full replacements versus incremental updates.

On projection corruption or a sequence gap:

1. mark the panel snapshot stale/unknown;
2. block new exposure;
3. keep safe reduction/reconciliation actions available where proven;
4. rebuild from the durable journal;
5. begin a new stream epoch and force clients to adopt a fresh snapshot.

## 14. Rollout and migration

1. Add schemas and shadow-read comparisons without changing enforcement.
2. Prove the Clerk snapshot is no weaker than existing account guards.
3. Switch Start projection and execution together to the shared decision.
4. Migrate existing instance/run artifacts deterministically.
5. Enable the durable command ledger before browser persistence.
6. Introduce the projection owner and benchmark it under polling.
7. Enable V2 SSE for one route with REST fallback.
8. Enable stable chart/row updates and Market Pulse.
9. Enable Resume and Dry Run only after their complete acceptance tests pass.
10. Delete the old IBKR surface after the parity and consumer-removal gates.

Every enforcement cutover must fail closed and have a rollback that restores the
previous code path without rewriting custody history or command identity.

## 15. Definition of done

- Start has exactly one backend decision function with `bot + Clerk` inputs.
- The Clerk is the only caller-facing source for account custody.
- Process liveness has an explicit typed identity/state contract.
- Unknown state blocks new exposure without preventing proven safety work.
- Instance configuration is immutable and run history is append-only.
- Resume creates a new run; Continue keeps the same live run.
- Trader and Operator users can inspect current and previous runs one at a time.
- Dry Run has a proven zero-broker-write contract.
- Backend command idempotency survives response loss and restart.
- Browser refresh recovers pending commands without duplicating them.
- Warm panel delivery performs no historical journal replay.
- V2 receives versioned SSE updates with conservative fallback recovery.
- Market Pulse is prominent and backend-authored.
- Routine updates do not reset the page or chart.
- Terminal language is backed by terminal evidence.
- Superseded IBKR Bot Control code is deleted only after V2 parity is proven.

## 16. Dependencies and references

- `docs/audits/alpaca-bot-control-panel-architecture-audit-2026-08-02.md`
- `docs/architecture/alpaca-bot-control-remediation-research-plan-2026-08-02.md`
- `docs/prds/alpaca-clerk-governed-bot-control.md`
- `docs/prds/alpaca-bot-fleet-control-deploy-redesign.md`
- `docs/architecture/adrs/0030-account-clerk-account-rooted-journal.md`
- `docs/architecture/adrs/0033-account-custody-clocks-and-safety-contract.md`
- `CONTEXT.md`

This platform is for research and education. Live trading requires separately
validated infrastructure.
