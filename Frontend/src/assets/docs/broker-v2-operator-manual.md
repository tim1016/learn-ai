# Broker V2 Operator Manual

This manual covers the broker-v2 control panel — the six-station order pipeline, every operator action, and the closed vocabulary the panel uses.

## Account authority selection

The service resolves the Alpaca account before constructing a Clerk. An account with no
activation fence uses the legacy JSONL authority described by the legacy actions below.
An account with a valid account/generation/database-bound activation fence uses the
SQLite Clerk and its backend-authored recovery catalog. Database existence alone does
not activate SQLite. A malformed fence or failed activated startup installs no
broker-mutation capability and never falls back to JSONL.

For activated SQLite accounts, the available actions are exactly `reconcile_now`,
`recover_exact_execution_evidence`, `resolve_execution_coverage`,
`cancel_verified_working_orders`, `prepare_safe_flatten`, `stop_bot_decisions`,
`open_custody_timeline`, and—only during typed authority failure—
`rebuild_from_mirror` or `reset_authority`. There is no generic Clear, blind Retry, or
unproven Flatten. Historical exact-execution recovery is paper-only and never enables
manual SQLite trading. See
`docs/references/alpaca-sqlite-clerk-recovery-language.md` for the wording matrix and
`docs/runbooks/alpaca-sqlite-clerk-recovery-and-cutover.md` for the offline subprocedure.

## SQLite manual paper tickets

The Alpaca Account Desk is the only manual-order entry point when its selected
authority is SQLite. Manual trading is paper-only and remains unavailable until
the server enables `ALPACA_SQLITE_MANUAL_TRADING_ENABLED` after qualification.
The browser supplies stable ticket and leg UUIDs, but Python supplies the trusted
operator identity, validates the preview again at confirmation, and records the
SQLite intent before it contacts Alpaca. Do not use the generic `/orders` route,
the broker console, or a bot action to work around a disabled manual capability.

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
- If `blocked`: which condition is blocking (`STREAM_HEALTH_HOLD` or `UNEXPLAINED_ORDER_HOLD`) and the `clear_hold` action when conditions are met.
- Channel health chip: `healthy`, `unhealthy`, or `unknown`.
- Hold state chip: `NO_HOLD` (submission allowed) or a hold code.

**Actions available here.**

- Legacy/unactivated account: `clear_hold` may lift a legacy account hold after the root condition is healthy and freshly observed.
- Activated SQLite account: no generic clear is presented. Use the exact backend-authored evidence-backed capability.

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
- `record_inventory_baseline` — recover a verified `missing_intent` mismatch or retire stale bot attribution on a reconciled flat account, then reconcile the accounting cutover.

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
| `retire` | Retire | Permanently decommission this bot. Its id is never reused. This is irreversible. | **Nothing — no broker exposes this action.** |
| `cancel_order` | Cancel order | Cancel one working order at the broker. The broker may reject the request if the order has already filled. | **Nothing — no broker exposes this action.** |
| `clear_hold` | Clear hold | Lift the account exposure hold once its root condition is healthy and freshly observed. | Bot panel (`alpaca`) |
| `record_inventory_baseline` | Recover inventory baseline | Record the freshly observed broker positions as the account accounting cutover, retiring prior bot attribution without deleting history or assigning current positions to a bot. | Bot panel (`alpaca`) |
| `reconcile_now` | Reconcile now | Run a reconciliation sweep against the broker immediately. Useful after a hold is cleared or after a manual order intervention. | Bot panel (`alpaca`) and SQLite Clerk recovery catalog |
| `recover_exact_execution_evidence` | Recover exact execution evidence | Read one retained Alpaca paper execution and prepare the Clerk's no-delta coverage proof. | SQLite Clerk recovery catalog |
| `resolve_execution_coverage` | Resolve execution coverage | Replace one matching cumulative recovery record with verified exact execution evidence. | SQLite Clerk recovery catalog |
| `cancel_verified_working_orders` | Cancel verified working orders | Cancel only working orders whose exact Clerk and broker identities are proven. | SQLite Clerk recovery catalog |
| `prepare_safe_flatten` | Prepare safe flatten | Prepare a fresh reduction plan without submitting an order. | SQLite Clerk recovery catalog |
| `stop_bot_decisions` | Stop bot decisions | Stop new decisions while existing exposure remains under Clerk custody. | SQLite Clerk recovery catalog |
| `open_custody_timeline` | Open custody timeline | Inspect the immutable operation-first evidence timeline. | SQLite Clerk recovery catalog |
| `rebuild_from_mirror` | Rebuild from mirror | Rebuild a failed authority only from a contiguous verified mirror. | SQLite Clerk recovery catalog |
| `reset_authority` | Reset authority | Create a new authority generation only after fresh flat and order-free proof. | SQLite Clerk recovery catalog |

<!-- END GENERATED: button-reference -->

---

## Hold Actions {#hold-actions}

Holds are account-wide. When a hold is active, **no bot on the account** can submit new orders.

| Hold | Trigger | Clear with |
|---|---|---|
| `STREAM_HEALTH_HOLD` | A market-data or execution channel becomes `unhealthy`. | Wait for channel to recover to `healthy`, then `clear_hold`. |
| `UNEXPLAINED_ORDER_HOLD` | The reconciliation sweep finds a broker order the journal cannot explain. | Investigate the unexplained order, resolve it out-of-band, then `clear_hold`. |

---

## Reconcile Actions {#reconcile-actions}

| Verdict | Meaning | Next step |
|---|---|---|
| `clean` | Journal and broker agree. | None. |
| `missing_intent` | Broker inventory or an owned order does not match the durable journal exposure. | Resolve uncertain/working orders; if the mismatch is verified pre-journal inventory, use `record_inventory_baseline`. |
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
| `CRASHED` | Crashed | The bot exited on an unhandled error. Check the duty reason. |
| `EXITED_UNVERIFIED` | Exited unverified | The bot's task ended without a clean stop. Its final state is not confirmed. |

### Hold States

| Code | Label | Meaning |
|---|---|---|
| `NO_HOLD` | No hold | No exposure hold is active. Order submission is allowed. |
| `UNEXPLAINED_ORDER_HOLD` | Unexplained-order hold | An order this account did not submit was seen in the journal. New submits are paused account-wide. |
| `STREAM_HEALTH_HOLD` | Stream-health hold | A market-data or execution channel is unhealthy. New submits are paused account-wide. |

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
