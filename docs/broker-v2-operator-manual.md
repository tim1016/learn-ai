# Broker V2 Operator Manual

This manual covers the broker-v2 control panel — the six-station order pipeline, every operator action, and the closed vocabulary the panel uses.

## Account authority selection

The service resolves the Alpaca account before constructing a Clerk. Only an account
with a valid account/generation/database-bound activation fence uses the SQLite Clerk
and its backend-authored recovery catalog. Database existence alone does not activate
SQLite. A missing, `OFF_DUTY`, malformed, conflicting, or failed activation installs no
broker-mutation capability. Legacy JSONL is not a selectable authority or fallback.

For activated SQLite accounts, the available actions are exactly `reconcile_now`,
`recover_exact_execution_evidence`, `resolve_execution_coverage`,
`cancel_verified_working_orders`, `prepare_safe_flatten`, `execute_safe_flatten`,
`stop_bot_decisions`, and `open_custody_timeline`. There is no generic Clear, blind
Retry, or unproven Flatten. Historical exact-execution recovery is paper-only and never
enables manual SQLite trading.

**Replacing a failed authority is not a panel action** (ADR 0047). Recovery refuses
while the Clerk holds its execution lease, and the reconciliation sweep — the only
thing that could produce the flat-and-order-free proof recovery requires — is the
lease holder. The panel therefore presents no authority-recovery button: during a
typed authority failure it raises a terminal blocker naming the offline ceremony.
Stop the data plane, run the recovery CLI (which validates lease expiry and
process-stop proof itself), then restart. See
`docs/references/alpaca-sqlite-clerk-recovery-language.md` for the wording matrix and
`docs/runbooks/alpaca-sqlite-clerk-recovery-and-cutover.md` for the offline subprocedure.

## Alpaca paper bot deployment

The Deploy page lists every catalog-visible strategy the operator has
marked validated in Strategy Validation. A validated strategy is never
silently absent: the catalog composes it from exactly two facts — the
canonical strategy registry and the validation flag-event log — and a
strategy that fails either the runtime check or proof re-verification stays
visible, shown blocked with a backend-authored reason, instead of being
dropped from the list.

A blocked row carries one of two distinct reasons, both surfaced through
the same `blocked_explanation` field with different wording. A
**stale-proof block** means the recorded acceptance evidence — the
settings file, the audit copy, or the manifest snapshot — no longer
re-verifies; this row stays Dry-Run-admissible, because Dry Run does not
depend on validation or behavioral evidence at all. A **no-runtime block**
means the strategy has no registered live-decision runtime yet — "not
built yet", distinct from "not validated" — and this row admits neither
Dry Run nor Paper, because Dry Run itself requires a registered runtime to
execute. Neither blocked reason counts toward the account's Deploy
eligibility, and neither is repaired by re-flagging the strategy validated;
a stale proof needs its evidence restored, a missing runtime needs an
engineer to register one.

Admission is tiered by execution mode (ADR 0034's mode-tiered-admission
amendment). A strategy with current `accepted_for_deploy` evidence follows
the normal path in every mode. A strategy whose behavioral verdict is
`evidence_only` is Paper-selectable on the human-validated flag alone — no
risk acknowledgement or operator reason is required, and the behavioral
verdict displays for information only; it does not gate Paper. Do not treat
a successful paper launch as numerical-equivalence evidence.

Dry Run is more permissive again: it admits any runtime-backed strategy
regardless of the human-validated flag or behavioral verdict, and needs only
a healthy market-data channel — it makes no broker contact and holds no
custody, so account posture, Clerk custody freeze, exposure hold, and intent
custody are all not applicable. Exposure carryover is forbidden in Dry Run.
Each strategy row carries a backend-authored `admissible_modes` set, and the
deploy form disables an execution mode the selected strategy cannot reach,
with the backend's own reason.

The evidence-only override contract (the **Dangerous human override**
acknowledgement, its typed reason, and the configuration-hash-bound record on
the launch receipt) is retained but currently unreachable: it is scoped to
Live, which remains unreachable and stubbed, not to Dry Run or Paper. A Paper
request that still carries the override is refused with a typed conflict.
When it does apply, it cannot make an unvalidated, invalidated, rejected, or
runtime-unsupported strategy deployable, and it does not relax account
posture, Clerk custody, channel health, intent custody, market-data, or
Start-admission gates.

SMA Crossover and RSI Mean Reversion are the initial runtime-supported
evidence-only choices. Both execute their canonical Python algorithms with
default registry parameters and emit asset-free ENTER/EXIT intents. The
selected symbol and sizing are bound by the Alpaca Action Plan; the Clerk
remains the only broker-effect authority.

A bot's strategy and override decision are immutable. To change either one,
stop the current bot if needed and deploy a new `strategy_instance_id`.
Resume creates a new run of the unchanged binding; it does not provide a
rebinding path.

## Signal Program build proof and legacy seal migration

A strategy backed by a registered Signal Program (EMA Crossover Signal, SMA
Crossover, RSI Mean Reversion, SPY Strategies A, B, and C, and Deployment
Validation) seals its exact resolved semantics — program version, golden trace
root, every resolved parameter with its unit and origin — once at deploy time.
Every Start and every Resume then re-hash the currently loaded program bytes
and require a golden-qualification receipt proving those exact bytes were
re-run against that program's own reference corpus for the sealed
`(program_version, golden_trace_root)`. Missing, stale, or mismatched evidence
refuses the run as `PROGRAM_BUILD_UNPROVEN` before any bar is evaluated or any
effect can occur; the panel's `explanation` and `next_step` are backend-
authored, so the recovery is always stated, never guessed. The usual recovery
is re-running the golden qualification job from `PythonDataService/` against
the currently deployed bytes —

```bash
.venv/bin/python -m scripts.run_signal_program_build_qualification
```

— which mints a fresh receipt only after that program's own golden-trace suite
passes; a code change that was never re-qualified after moving the program
version or trace root cannot produce an admissible receipt this way. If the
loaded bytes are simply the wrong build, the fix is deploying a newly sealed
instance instead. A strategy with no registered Signal Program at all (for
example EMA Crossover 2 bps or the legacy EMA Crossover compatibility key)
reports `NOT_APPLICABLE` rather than `PROGRAM_BUILD_UNPROVEN` and is not
gated by this proof; it runs on its existing, non-sealed execution path.

### The wiring half of the proof

The hashed bytes come in two halves, hashed separately so a mismatch says
which one moved. The **artifact** half is the program's decision math and its
import closure; drift there has always refused the run as
`PROGRAM_BUILD_UNPROVEN`, and still does. The **wiring** half — the module
under `app/engine/strategy/programs/` that binds a program's parameters to
that math, plus the shared parameter/decision-clock leaf — was not covered at
all until issue #1735: an edit to a program's factory moved no digest, so a
stale receipt still read `PROVEN`.

Wiring coverage is new, so it starts in a reporting posture. With
`SIGNAL_PROGRAM_WIRING_DIGEST_ENFORCED` off (the default), a program whose
wiring no longer matches its receipt still starts, and the build fact reports
`wiring: DRIFTED` with a `next_step` naming the re-qualification. Turning the
variable on makes that drift refuse the run like any other. Turn it on only
once every deployed program has been re-qualified against its current wiring —
otherwise every bot is blocked at once by a mismatch nobody has had the chance
to clear. The same `run_signal_program_build_qualification` command above
mints receipts covering both halves.

This toggle governs *only* the wiring half. Drift in the artifact half is the
admission control this proof was built around and keeps refusing runs in both
toggle positions.

A strategy instance deployed before this seal existed has no v2 seal on file.
Its first Resume after this feature attempts migration, and migration
succeeds only when the v1 configuration is *exactly* reconstructible — never
by guessing. Two things must hold: the persisted parameters still validate
against the currently registered strategy contract, and every parameter's
deploy-time origin is a recorded fact rather than an inference. A parameter
that was never supplied at deploy time is factually a registered default and
qualifies. A parameter that *was* explicitly supplied but whose origin was
never recorded does not: comparing its stored value against today's default
cannot prove it was never an override, because the registered default may have
drifted since. When both conditions hold, Resume appends an exact seal under
the same `strategy_instance_id` and proceeds — no operator action needed.

Otherwise Resume refuses with `PROGRAM_BUILD_UNPROVEN` and a `next_step`
naming the exact deterministically-derived clone instance id to deploy in its
place. The original instance's configuration and history remain permanently
inspectable, but it can never Resume again under its own id. Deploying the
named clone requires fresh parameters — it is a new deployment, not a rebind.
The clone's lineage back to the original is written before that instruction is
ever shown; if the lineage cannot be recorded, Resume fails rather than
promising a link it did not persist.

## The guarded Paper canary

Running a Signal Program against a real Alpaca paper account is deliberately
harder to reach than any other mode. Three things gate it, and none of them
can be satisfied by a code change alone.

**Shadow evaluation comes first.** Before a program is considered for the
canary, its canonical `EvaluationTrace` sequence is computed twice over the
same qualified bars — once through the Backtest engine, once through the same
session seam a running Paper or Dry Run bot uses — and the two are compared
field by field. The comparison stops at the *first* disagreement rather than
accumulating a diff, so what you are shown is the earliest point the two
authorities diverge, not a summary. Neither side can reach a broker: the
comparison never constructs a Clerk or a broker port, and the bars come from
memory.

**Admission requires an exact pairing.** The canary allowlist admits one
exact `(program, account)` tuple — never a program on any account, never an
account running any program. The source list **ships empty**, and a test fails
if it is ever non-empty. Operational admission instead lives in a gitignored,
append-only local ledger at
`PythonDataService/artifacts/canary_admission/events.json`. Nothing in CI,
migration, or startup writes it.

A sibling `events.json.checkpoint` anchors the latest event count and hash
outside the ledger. A missing or mismatched checkpoint makes admission fail
closed, including when the ledger is replaced with an earlier, internally
valid prefix. Treat the ledger and its checkpoint as one operational record;
never edit or restore either file by hand.

The Alpaca **Deploy** drawer is the primary approval workflow. Choose a
strategy, find **Paper access**, then select **Review & enable Paper**. The
first step only prepares a short-lived review bound to the current validation
proof, program build, strategy, and account. Check the displayed pairing and
select **Enable Paper access** to confirm it. This approval does not deploy a
bot or place an order; deployment remains a separate action. The same card is
available for every sealed Signal Program and shows **Enabled** only for the
currently selected account's exact pairing.

The command-line ceremony below remains available for operator recovery and
audit work outside the UI.

Use the broker-free command from `PythonDataService/` to prepare a short-lived
review plan. Planning re-hashes the current accepted validation snapshot, the
registered Signal Program bytes, and their golden qualification receipt; it
does not change admission:

```text
.venv/bin/python -m scripts.manage_canary_admission plan \
  --program ema_crossover_signal \
  --account-id YOUR_PAPER_ACCOUNT_ID \
  --reason "Reviewed EMA Crossover Signal paper canary" \
  --output /tmp/ema-canary-plan.json
```

The plan is written with owner-only (`0600`) permissions because it contains
the confirmation token. Read it and confirm its program, account, actor,
reason, validation event, artifact digest, qualification receipt, and expiry.
Then, before its two-minute default expiry, apply the reviewed plan:

```text
.venv/bin/python -m scripts.manage_canary_admission apply \
  --plan /tmp/ema-canary-plan.json
```

At an interactive terminal, paste the plan's exact `confirmation_token` into
the hidden prompt. For automation, provide the token on standard input from a
protected secret source. Never put it in a command argument or shell history.

`apply` re-proves the evidence and refuses if it changed, the plan expired,
or the ledger moved since planning. It appends an activation event only; it
does not start or resume a bot, contact Alpaca, or submit an order. Use
`status` to verify the active exact pairs. A run whose pairing is absent
refuses with `CANARY_PAIRING_NOT_ALLOWLISTED` before any bar is evaluated.

Revocation is also append-only:

```text
.venv/bin/python -m scripts.manage_canary_admission revoke \
  --program ema_crossover_signal \
  --account-id YOUR_PAPER_ACCOUNT_ID \
  --reason "EMA paper canary review complete"
```

Revocation blocks future Start and Resume admissions but does not terminate an
already-running bot. For rollback, Stop the bot first, wait for its
Clerk-proved safe-boundary verdict, and then revoke the pairing.

That gate is the *last* of seven proofs, not the only one. The seal and build
proof, the strategy validation disposition, the sealed provider identity, the
replay/boot-recovery readiness, and the Clerk's own custody state are all
re-proved on every Start and every Resume first. Being on the allowlist
exempts a run from nothing — it is the deciding factor only once everything
else would already have passed. Dry Run is never subject to it; it runs under
its own synthetic authority.

**Rollback stops at a proved boundary.** When a canary is stopped, the Clerk
proves the instance's custody state and the resulting stop outcome is
classified into a rollback verdict recorded alongside it. `STOPPED_FLAT` and
`STOPPED_WITH_APPROVED_ATTRIBUTED_EXPOSURE` are safe boundaries to roll back
at. `CANARY_ROLLBACK_REQUIRES_FLATTEN` means attributed exposure remains and
this instance's carryover policy does not approve carrying it through
rollback; `CANARY_ROLLBACK_BOUNDARY_UNPROVABLE` means the Clerk cannot
currently prove a safe boundary at all, usually because a freeze is active,
reconciliation is not clean, or working orders or unresolved intents remain.

A refusing verdict never prevents the Stop. Stopping a bot is a safety
action and always completes; the verdict is a record of whether the *canary*
may be considered rolled back, not a veto over terminating the process.
Resuming afterwards is not a resumption of the old run — it re-enters
admission, is re-gated by every proof above, and mints a new run. No running
process is ever hot-swapped, and no seal evidence is rewritten.

## Reading canary evidence

Every decision row names the authority it was read from. `authority_kind` is
`real_paper` for a genuine Alpaca paper account and `synthetic` for a Dry Run
`sim:` authority, and it is stamped from the account the evidence physically
came from rather than inferred at render time. A request that would mix the
two into one aggregate is rejected rather than silently blended.

Selecting an older transaction follows that transaction's own stored links.
Where a link does not exist, the panel says so explicitly; it never falls back
to attaching the newest unrelated decision, which would make an old
transaction appear to have caused a recent signal.

A decision that was staged but never captured before a crash is recorded as
`CANDIDATE_UNCAPTURED_AT_CRASH` on replay rather than being silently dropped
or replayed as if it had been decided. Evidence that is causally relevant —
effect-bearing, refused, crash, uncertainty, correction, validation, and seal
rows — is exempt from sequence-tail pruning. The bounded list the panel shows
is a display window, not the retention boundary: an older decision scrolled
out of that tail is still on file.

## Storage, backup, and retention

Every account authority — a real Alpaca paper account or a Dry Run's
synthetic `sim:<strategy_instance_id>` account — owns one directory under
the artifacts root: `accounts/alpaca/<account_id>/`. Two SQLite files in it
are the canonical recovery evidence named by the sealed-program PRD:
`clerk.db`, the account-scoped Clerk (dispositions, custody, effects, decision
receipts), and `source_bars.sqlite3`, the retained source-bar ledger (every
exact closed bar the bot evaluated, before any session filtering). Both run
in WAL mode with `synchronous=FULL`; the ledger auto-checkpoints every 1,000
pages and runs an explicit truncating checkpoint when its handle is closed,
even if a verifier or backup reader is still attached. If that reader still
pins the log after the five-second busy timeout, the close fails with
`SOURCE_BAR_CHECKPOINT_BUSY` and leaves the handle open rather than
reporting a folded WAL it did not fold; release the reader and close again.
Provider re-fetch is never accepted as recovery evidence; only these two
files are.

**Backup and restore.** `python -m scripts.manage_alpaca_sqlite_clerk
--artifacts-root <root> --account-id <id> backup` publishes a verified bundle
under `accounts/alpaca/<id>/verified-backups/`, cut online through SQLite's
backup API (WAL-safe, no stop required) and verified before it is published:
the Clerk database first, then the source-bar ledger, so a bundle can carry
bars that have no disposition yet — the crash window replay already handles —
but never a disposition whose bar is missing. The bundle manifest (schema
version 2) records both files' SHA-256 and the ledger's retained-row count.
`verify` checks the stopped authority database against its finalized mirror
head; a bundle itself is verified as the first step of `restore`. `restore
--bundle <path>` is refused while a live execution lease exists (and, if the
database is too damaged to read its lease, unless `--process-stop-evidence
<file>` carries a fresh process-stop proof), unless the bundle belongs to this
account, generation, and database identity, and unless both snapshots pass
integrity and hash checks — for the ledger, that includes every retained row
carrying this account's id, so a structurally sound ledger cut for another
account is refused even with a matching manifest. It then moves the previous
files under `recovery-preserved/` and swaps the snapshots in, ledger first.
Each swap is atomic; the pair is not: an in-process failure takes any
already-swapped snapshot back out and returns the previous files from
`recovery-preserved/`, while a hard crash between the two swaps leaves
the bars restored and the authority still missing — startup then fails
closed and the operator re-runs `restore`. That ordering is deliberate: the
opposite crash state (authority present, bars gone) would let crash replay
run against missing evidence. There is no restore into a running instance,
by construction. Bundles published before schema version 2 contain only
`clerk.db`; they still restore, and restoring one leaves the live ledger
untouched.

**Dry Run accounts are disposable.** A `sim:` directory is a clean-slate
simulation (ADR 0035): it is not part of any backup or restore procedure and
may be deleted when its instance is retired. Back up real paper accounts
only.

**Retention is a floor, not a window.** Nothing prunes the source-bar
ledger: a provider/symbol stream fails closed at 200,000 retained minute
bars (`SOURCE_BAR_RETENTION_LIMIT`, roughly two years of regular sessions)
and needs a reviewed rollover, never a silent deletion. Decision receipts
keep the most recent 1,000 ordinary rows per strategy instance — every
decision clock writes one, `no_action` included — while rows in a
`protected_*` retention class (effects, refusals, crash evidence, competing
exits) are exempt from pruning entirely. Crash replay reads that whole
1,000-row window, not the 500-row cap that bounds the panel's receipt reads.
A registry-parameterized test pins that both budgets exceed every sealed
program's declared warmup plus one full open session at its qualified
decision clock; the tightest margin today is Deployment
Validation, whose one-minute clock and one-day warmup consume 780 of the
1,000 ordinary receipts. A new program with a one-minute clock and a warmup
of two days or more would fail that test and needs the receipt budget raised
first.

## SQLite manual paper tickets

The Alpaca Account Desk is the only manual-order entry point when its selected
authority is SQLite. Manual trading is paper-only and remains unavailable until
the server enables `ALPACA_SQLITE_MANUAL_TRADING_ENABLED` after qualification.
The browser supplies stable ticket and leg UUIDs, but Python supplies the trusted
operator identity, validates the preview again at confirmation, and records the
SQLite intent before it contacts Alpaca. The former generic `/orders` mutation is
absent. Do not use the broker console or a bot action to work around a disabled
manual capability.

- A ticket may contain one to eight immutable market or limit legs with `DAY` or
  `GTC` time in force. Legs are serial, not atomic: the next leg requires its
  own durable confirmation.
- A broker-acknowledged leg may permit the next leg. An unknown result pauses the
  ticket; reconcile the exact order, refresh the backend preview, and explicitly
  choose **Continue remaining legs**. Never submit a replacement ticket for an
  unknown result.
- **Cancel ticket** only requests cancellation for verified working manual orders.
  It never targets bot or foreign orders, and it retires never-activated legs
  locally without broker contact.
- Account transaction history and FIFO reconciliation identify these rows as
  `manual` with the immutable manual custody subject. Bot catalog, panel, and
  strategy P&L remain strategy-scoped and therefore do not include manual
  attribution.

`Prepare safe flatten` refreshes the backend policy and displays a read-only,
versioned plan: each nonzero attributed position, the closing side and exact
quantity, its evidence time, and the authority/reconciliation identities that
make the plan current. Preparing the plan never submits an order. If custody
evidence changes, prepare again; a future reduction operation may not reuse the
old plan version. The backend only prepares one after a complete working-order
check and an account-wide reconciliation that is at least as new as every
included position.

### Manual paper qualification release gate

The feature flag remains disabled until both gates below are complete. A passing
automated report is deliberately not a production activation receipt.

1. Run the broker-free deterministic matrix from `PythonDataService/` and archive
   its JSON and Markdown outputs in the dated release audit:

   ```bash
   .venv/bin/python -m scripts.run_manual_order_qualification \
     --json-output /secured-audit/2026-08-13/manual-pre-live.json \
     --markdown-output /secured-audit/2026-08-13/manual-pre-live.md
   ```

   The report must say `PRE_LIVE_REHEARSAL_PASSED`, `live_environment_status`
   `NOT_RUN`, and `release_gate_status` `PENDING_DATED_PAPER_CEREMONY`.

2. On the selected paper authority, obtain a fresh process-stop proof and run the
   offline v8-to-v9 ceremony. Archive the upgrade receipt. For the supervised
   Account Desk sequence only, temporarily set
   `ALPACA_SQLITE_MANUAL_TRADING_ENABLED=true` on that selected paper deployment
   after verifying its Alpaca account mode is `paper`, its control-plane
   credential is present, and the operator has recorded the ceremony start time.
   Never perform this temporary enablement against a live account. Perform the
   one-share buy/fill, manual-owned sell/flatten, resting limit/cancel, duplicate
   confirmation/reload, accepted-before-ack restart, partial-fill restart,
   reconnect/reconciliation, coverage recovery, and bot-start admission after
   terminal reconciliation; then disable the flag again and archive the dated
   receipt. Each row must bind the Alpaca order ID, Clerk order reference and
   transition, mirror/hash head, position/FIFO/account-history observation, and
   start-admission result.

Only after that dated audit has every required receipt may a paper deployment
set `ALPACA_SQLITE_MANUAL_TRADING_ENABLED=true`. Do not enable it for a live
account, and do not replace a missing paper receipt with a test result.

---

## Six-Station Pipeline

Every order travels through exactly six stations in sequence. The panel shows the live state of each station for every bot on the account.

```
SIGNAL → INTENT → SUBMIT_GATE → BROKER_ACK → FILL → RECONCILED
```

Each station has a **station state** (what happened there) and may carry **evidence** (the structured receipt the system recorded).

---

## Station 1: SIGNAL {#station-1-signal}

**What it does.** The bot evaluates a closed bar and decides whether to act. A signal is recorded for every bar evaluation — including decisions to do nothing.

**What can block it.** The bot must be `ON_DUTY` and the market-data feed must be delivering bars. If the bot is `OFF_DUTY`, no signal is evaluated.

**What the operator sees.**
- Station state: `waiting` (bar not yet evaluated), `satisfied` (signal produced), `not_applicable` (bot paused or retired), or `blocked` (feed issue).
- The signal timestamp and ticker.

---

## Station 2: INTENT {#station-2-intent}

**What it does.** Before touching the broker, the bot writes a durable intent record to the journal. This is the "I am about to submit" checkpoint.

**What can block it.** Journal write failures or a bot crash between signal and intent.

**What the operator sees.**
- Station state: `waiting`, `satisfied`, or `blocked`.
- If `satisfied`: the intent ID, side (buy/sell), quantity, and ticker.
- If the bot crashed before writing intent, this station shows `blocked` and the duty reason explains the crash.

---

## Station 3: SUBMIT_GATE {#station-3-submit-gate}

**What it does.** Before submitting to the broker, the system checks two gating conditions:
1. **Stream health** — all market-data and execution channels must be `healthy`.
2. **Exposure holds** — no active hold on the account (`NO_HOLD` must be the hold state).

**What can block it.** Either condition failing holds all submissions account-wide.

**What the operator sees.**
- Station state: `waiting`, `satisfied`, or `blocked`.
- If `blocked`: which condition is blocking (`STREAM_HEALTH_HOLD` or `UNEXPLAINED_ORDER_HOLD`) and the exact evidence-backed recovery capability when one is safe.
- Channel health chip: `healthy`, `unhealthy`, or `unknown`.
- Hold state chip: `NO_HOLD` (submission allowed) or a hold code.

**Actions available here.** There is no direct clear. Use only the exact
backend-authored SQLite recovery capability; an unactivated account is unavailable.

---

## Station 4: BROKER_ACK {#station-4-broker-ack}

**What it does.** The order is submitted to the broker. The broker returns an acknowledgment (or rejection).

**What can block it.** Network errors, broker rejection, or a crash after submission but before the ack is recorded.

**What the operator sees.**
- Station state: `waiting`, `satisfied`, or `blocked`.
- If `satisfied`: the broker order ID, timestamp, and fill status.
- If `blocked`: the broker's rejection reason.

**Actions available here.**
- `cancel_order` — cancel one working order at the broker.

---

## Station 5: FILL {#station-5-fill}

**What it does.** The broker executes the order, in full or in part. Each partial fill is recorded.

**What can block it.** A market order in normal conditions fills nearly immediately. Limit orders may rest. A `FILL` station showing `waiting` for an extended period may indicate the order is resting at a limit.

**What the operator sees.**
- Station state: `waiting`, `satisfied`, or `blocked`.
- If `satisfied`: fill price, quantity, and timestamp.

---

## Station 6: RECONCILED {#station-6-reconciled}

**What it does.** A periodic reconciliation sweep compares the journal against the broker's live order state. When they agree, this station is `satisfied` with a `clean` verdict.

**What can block it.** Journal-broker disagreement produces `missing_intent` (intent with no broker order) or `unexplained_order` (broker order the journal cannot explain). The sweep runs every 15 seconds; if the broker is unreachable the verdict goes `stale`.

**What the operator sees.**
- Reconciliation verdict: `clean`, `missing_intent`, `unexplained_order`, or `stale`.
- Sweep timestamp (how recent the verdict is).

**Actions available here.**
- `reconcile_now` — run a reconciliation sweep against the broker immediately, without waiting for the next scheduled sweep.

---

## Bot Lifecycle {#bot-lifecycle}

Three separate vocabularies describe a bot, and confusing them is the most common
reading error:

- **Phase** — the bot's durable state: `OFF_DUTY`, `ON_DUTY`, `RETIRED`.
- **Desired state** — what the operator asked for: `RUNNING`, `PAUSED`, `STOPPED`.
  `PAUSED` holds one live run; **Continue** releases that same run without changing
  its run ID, which is what makes it different from **Resume**.
- **Duty outcome** — what actually happened on exit: `ON_DUTY` (has not exited),
  `STOPPED`, `CRASHED`, `EXITED_UNVERIFIED`.

Every code in all three is defined in the [Glossary](#glossary).

---

<!-- BEGIN GENERATED: button-reference -->

## Button Reference {#button-reference}

Every action the panel can present, with the label and explanation the backend
authors. This table is generated from `OPERATOR_COPY`, so it is exactly the
closed `ActionId` enum — no more, no less.

**Where did "when available" go?** It was dropped by decision (ADR 0041). The
backend does not author availability: enablement is gate logic evaluated per
request. The panel renders each action's live condition beside the action itself,
under **Active command gates**, with its own reason — *"No attributed exposure
requires a flatten plan."* A condition computed at the moment of asking cannot
rot; a prose condition can, and did. Read the panel for "can I use this now";
read this table for "what does it do". Losing the browsable conditions list is a
real cost, accepted knowingly.

**Surface** is the static broker scope declared in the action registry. It
answers "can this ever appear for me", not "is it enabled right now".

| Code | Button | What it does | Surface |
|---|---|---|---|
| `deploy` | Deploy | Create and start a new bot bound to this account. The bot begins evaluating bars immediately after creation. | Bots list page (`alpaca`) |
| `resume` | Resume | Create a new run of this unchanged strategy instance after backend admission. | Bot panel (`alpaca`) |
| `pause` | Pause | Hold bar evaluation while keeping the current process and run identity alive. | Bot panel (`alpaca`) |
| `continue` | Continue | Let this paused live run evaluate bars again without changing its run ID. | Bot panel (`alpaca`) |
| `stop` | Stop | Stop evaluating bars and cancel this bot's working entry orders. Exposure is left untouched. | Bot panel (`alpaca`) |
| `flatten_stop` | Flatten & stop | Cancel working orders, submit closing orders to flatten exposure, then stop. Use this to exit positions before stopping. | Bot panel (`alpaca`) |
| `retire` | Retire | Permanently decommission this bot. Its id is never reused. This is irreversible. | Bot panel (`alpaca`) |
| `cancel_order` | Cancel order | Cancel one working order at the broker. The broker may reject the request if the order has already filled. | **Nothing — no broker exposes this action.** |
| `reconcile_now` | Reconcile now | Run a reconciliation sweep against the broker immediately. Useful after a hold is cleared or after a manual order intervention. | Bot panel (`alpaca`) and SQLite Clerk recovery catalog |
| `recover_exact_execution_evidence` | Recover exact execution evidence | Read one retained Alpaca paper execution and prepare the Clerk's no-delta coverage proof. | SQLite Clerk recovery catalog |
| `resolve_execution_coverage` | Resolve execution coverage | Replace one matching cumulative recovery record with verified exact execution evidence. | SQLite Clerk recovery catalog |
| `cancel_verified_working_orders` | Cancel verified working orders | Cancel only working orders whose exact Clerk and broker identities are proven. | SQLite Clerk recovery catalog |
| `prepare_safe_flatten` | Prepare safe flatten | Prepare a fresh reduction plan without submitting an order. | SQLite Clerk recovery catalog |
| `execute_safe_flatten` | Execute safe flatten | Submit the prepared reduction as recovery EXIT custody with exact attributed quantities. | SQLite Clerk recovery catalog |
| `stop_bot_decisions` | Stop bot decisions | Stop new decisions while existing exposure remains under Clerk custody. | SQLite Clerk recovery catalog |
| `open_custody_timeline` | Open custody timeline | Inspect the immutable operation-first evidence timeline. | SQLite Clerk recovery catalog |

<!-- END GENERATED: button-reference -->

---

## Hold Actions {#hold-actions}

Holds are account-wide. When a hold is active, **no bot on the account** can submit new orders.

| Hold | Trigger | Clear with |
|---|---|---|
| `STREAM_HEALTH_HOLD` | A market-data or execution channel becomes `unhealthy`. | Wait for channel health, reconcile, and use only a backend-presented exact recovery capability. There is no direct clear. |
| `UNEXPLAINED_ORDER_HOLD` | Reconciliation finds a broker order the SQLite authority cannot explain. | Investigate the exact order and use the evidence-bound recovery catalog; never clear the hold blindly. |

---

## Reconcile Actions {#reconcile-actions}

| Verdict | Meaning | Next step |
|---|---|---|
| `clean` | Journal and broker agree. | None. |
| `missing_intent` | Broker inventory or an owned order does not match the durable SQLite exposure. | Resolve uncertain or working orders through the evidence-bound recovery catalog. No inventory-baseline adoption action exists. |
| `unexplained_order` | A broker order exists that the journal cannot explain. | Investigate the source of the unexplained order. This triggers an `UNEXPLAINED_ORDER_HOLD`. |
| `stale` | The last sweep could not reach the broker. | Wait for broker connectivity to restore; run `reconcile_now` when available. |

---

<!-- BEGIN GENERATED: glossary -->

## Glossary {#glossary}

The panel uses a closed vocabulary. Every code the system emits is defined below,
generated from the same backend copy map the panel itself renders.

### Phases

| Code | Label | Meaning |
|---|---|---|
| `OFF_DUTY` | Off duty | The bot is not running. It evaluates no bars and places no orders. |
| `ON_DUTY` | On duty | The bot is running and evaluating bars as they close. |
| `RETIRED` | Retired | The bot is permanently decommissioned. Its id is never reused. |

### Desired State

| Code | Label | Meaning |
|---|---|---|
| `RUNNING` | Running | The operator wants this bot evaluating bars. |
| `PAUSED` | Paused | The current run remains alive but bar evaluation is held until Continue. |
| `STOPPED` | Stopped | The operator wants this bot idle. Exposure is left untouched. |

### Duty Outcomes

| Code | Label | Meaning |
|---|---|---|
| `ON_DUTY` | On duty | The bot is running and evaluating bars as they close. |
| `STOPPED` | Stopped cleanly | The bot exited on an operator stop or a service shutdown. |
| `CRASHED` | Crashed | The bot exited on an unhandled runtime error. This terminal outcome is not a market-data health verdict. |
| `EXITED_UNVERIFIED` | Exited unverified | The bot's task ended without a clean stop. Its final state is not confirmed. |

### Hold States

| Code | Label | Meaning |
|---|---|---|
| `NO_HOLD` | No hold | No exposure hold is active. Order submission is allowed. |
| `UNEXPLAINED_ORDER_HOLD` | Unexplained-order hold | An order this account did not submit was seen in the journal. New submits are paused account-wide. |
| `STREAM_HEALTH_HOLD` | Stream-health hold | A market-data or execution channel is unhealthy. New submits are paused account-wide. |
| `UNKNOWN_HOLD` | Hold active; cause unrecognised | The Clerk holds this account against new entries under a cause this build cannot name. New submits are paused account-wide until it clears. Read the Clerk's own hold record for the cause. |

### Reconciliation Verdicts

| Code | Label | Meaning |
|---|---|---|
| `clean` | Clean | The last sweep found the journal and the broker in agreement. |
| `unexplained_order` | Unexplained order | The last sweep found a broker order the journal cannot explain. |
| `missing_intent` | Missing intent | The last sweep found broker inventory or an owned order that does not match the durable journal exposure. |
| `stale` | Stale | The last sweep could not reach the broker; the verdict is out of date. |

### Channel Health

| Code | Label | Meaning |
|---|---|---|
| `healthy` | Healthy | The channel is connected and current. |
| `unhealthy` | Unhealthy | The channel is down or lagging. Trading is gated until it recovers. |
| `unknown` | Unknown | The channel's health has not been observed yet. |

### Station IDs

| Code | Label | Meaning |
|---|---|---|
| `SIGNAL` | Signal | The bot evaluated a bar and produced (or withheld) a decision. |
| `INTENT` | Intent | The bot recorded an order intent before touching the broker. |
| `SUBMIT_GATE` | Submit gate | Holds and channel health were checked before submission. |
| `BROKER_ACK` | Broker ack | The broker acknowledged (or rejected) the submitted order. |
| `FILL` | Fill | The order executed, in full or in part, at the broker. |
| `RECONCILED` | Reconciled | A sweep confirmed the journal and the broker agree on this order. |

### Station States

| Code | Label | Meaning |
|---|---|---|
| `satisfied` | Satisfied | This station completed with recorded evidence. |
| `waiting` | Waiting | This station is expected to progress. Nothing is wrong. |
| `blocked` | Blocked | An identified condition is preventing this station from progressing. |
| `unknown_stale` | Unknown (stale) | Evidence for this station exists but is too old to trust. |
| `not_applicable` | Not applicable | This broker or mode has no such station. |

### Action IDs

The ninth closed vocabulary is `ActionId`. Every one of its codes is documented —
with the surface that can present it — in the
[Button Reference](#button-reference) above, and is not repeated here.

<!-- END GENERATED: glossary -->

---

## Terminal incident references

These runbook slugs remain the documentation contract for terminal notices that the
current broker-activity bridge can emit. They describe evidence handling only. The
retired IBKR launcher/evaluator is not an operator surface, and none of these entries
authorizes an IBKR order, cancellation, deploy, start, or stop action.

### `ibkr-order-rejection`

<!-- terminal-runbook-slug: ibkr-order-rejection -->

Preserve the broker rejection evidence and inspect the read-only IBKR account/order
evidence. Do not retry or alter an IBKR order. For an Alpaca bot, use only the
evidence-bound recovery capability presented by the Broker V2 panel.

### `submit-outcome-uncertain`

<!-- terminal-runbook-slug: submit-outcome-uncertain -->

Treat the submission as unknown until reconciliation supplies exact broker evidence.
Do not create a replacement order or clear a hold manually.

### `bot-halted`

<!-- terminal-runbook-slug: bot-halted -->

Read the backend-authored duty reason and the account custody timeline. Resume only
when the Broker V2 panel presents an enabled, evidence-backed action.

### `bot-launch-failed`

<!-- terminal-runbook-slug: bot-launch-failed -->

Keep the bot off duty, retain the failure evidence, and correct the selected Alpaca
deployment input before using the panel's next admitted action. Do not use a legacy
IBKR host launcher.

### `unmapped-terminal-diagnostic`

<!-- terminal-runbook-slug: unmapped-terminal-diagnostic -->

Preserve the opaque diagnostic and inspect the custody timeline. Escalate an
unclassified failure rather than mapping it to a generic retry or recovery action.
