# Broker V2 Operator Manual

This manual covers the broker-v2 control panel — the six-station order pipeline, every operator action, and the closed vocabulary the panel uses.

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
- `clear_hold` — lift the account hold once the root condition is healthy and freshly observed.

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

A bot has three durable states:

| State | Meaning |
|---|---|
| `OFF_DUTY` | Not running. Evaluates no bars, places no orders. |
| `ON_DUTY` | Running. Evaluates bars as they close. |
| `RETIRED` | Permanently decommissioned. The bot ID is never reused. |

Desired state (`RUNNING` or `STOPPED`) is separate from duty outcome (`ON_DUTY`, `CRASHED`, `EXITED_UNVERIFIED`, `STOPPED_OUTCOME`, `RETIRED`).

---

## Button Reference {#button-reference}

### `deploy` {#action-deploy}
**When available:** On the account desk, to add a new bot.
**What it does:** Creates and starts a new bot bound to this account. The bot begins evaluating bars immediately after creation.

### `start` {#action-start}
**When available:** When the bot's desired state is `STOPPED` and it is `OFF_DUTY`.
**What it does:** Signals the evaluator to begin evaluating bars for this bot. The bot transitions to `ON_DUTY`.

### `stop` {#action-stop}
**When available:** When the bot is `ON_DUTY`.
**What it does:** Stops bar evaluation and cancels any working entry orders. Existing exposure (open positions) is left untouched.

### `flatten_stop` {#action-flatten-stop}
**When available:** When the bot is `ON_DUTY` and holds open positions.
**What it does:** Cancels working orders, submits closing orders to flatten all exposure, then stops. Use when you need to exit positions before stopping.

### `retire` {#action-retire}
**When available:** When the bot is `OFF_DUTY` and not `RETIRED`.
**What it does:** Permanently decommissions the bot. The bot ID is never reused. This is irreversible.

### `cancel_order` {#action-cancel-order}
**When available:** When a working order exists at the broker.
**What it does:** Sends a cancellation request for one working order. The broker may reject if the order has already filled.

### `clear_hold` {#action-clear-hold}
**When available:** When an exposure hold (`STREAM_HEALTH_HOLD` or `UNEXPLAINED_ORDER_HOLD`) is active and its root condition has been freshly observed as healthy.
**What it does:** Lifts the account-wide hold and allows new order submissions to proceed.

### `record_inventory_baseline` {#action-record-inventory-baseline}
**When available:** When the latest verdict is `missing_intent`, or when a stopped bot retains stale attributed exposure while the reconciled account is flat; no unresolved intents or working orders may exist.
**What it does:** After typed `BASELINE` confirmation, records the freshly observed broker positions as an account accounting cutover and immediately reconciles. It does not delete prior trades. It retires all pre-cutover bot attribution as current custody and leaves current broker positions unassigned.

### `reconcile_now` {#action-reconcile-now}
**When available:** Always, when the reconciliation station is visible.
**What it does:** Runs an immediate reconciliation sweep against the broker. Useful after a hold is cleared or after a manual order intervention.

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

## Glossary {#glossary}

The panel uses a closed vocabulary. Every code the system emits is defined below.

### Station IDs (pipeline stages)

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
| `waiting` | Waiting | This station is expected to progress. Nothing is wrong. |
| `satisfied` | Satisfied | This station completed with recorded evidence. |
| `blocked` | Blocked | An identified condition is preventing this station from progressing. |
| `not_applicable` | Not applicable | This broker or mode has no such station. |
| `unknown_stale` | Unknown (stale) | Evidence for this station exists but is too old to trust. |

### Desired State

| Code | Label | Meaning |
|---|---|---|
| `RUNNING` | Running | The operator wants this bot evaluating bars. |
| `STOPPED` | Stopped | The operator wants this bot idle. Exposure is left untouched. |

### Duty Outcomes (what actually happened)

| Code | Label | Meaning |
|---|---|---|
| `ON_DUTY` | On duty | The bot is running and evaluating bars as they close. |
| `OFF_DUTY` | Off duty | The bot is not running. It evaluates no bars and places no orders. |
| `STOPPED_OUTCOME` | Stopped cleanly | The bot exited on an operator stop or a service shutdown. |
| `CRASHED` | Crashed | The bot exited on an unhandled error. Check the duty reason. |
| `EXITED_UNVERIFIED` | Exited unverified | The bot's task ended without a clean stop. Its final state is not confirmed. |
| `RETIRED` | Retired | The bot is permanently decommissioned. Its id is never reused. |

### Hold States

| Code | Label | Meaning |
|---|---|---|
| `NO_HOLD` | No hold | No exposure hold is active. Order submission is allowed. |
| `STREAM_HEALTH_HOLD` | Stream-health hold | A market-data or execution channel is unhealthy. New submits are paused account-wide. |
| `UNEXPLAINED_ORDER_HOLD` | Unexplained-order hold | An order this account did not submit was seen in the journal. New submits are paused account-wide. |

### Reconciliation Verdicts

| Code | Label | Meaning |
|---|---|---|
| `clean` | Clean | The last sweep found the journal and the broker in agreement. |
| `missing_intent` | Missing intent | The last sweep found a recorded intent with no matching broker order. |
| `unexplained_order` | Unexplained order | The last sweep found a broker order the journal cannot explain. |
| `stale` | Stale | The last sweep could not reach the broker; the verdict is out of date. |

### Channel Health

| Code | Label | Meaning |
|---|---|---|
| `healthy` | Healthy | The channel is connected and current. |
| `unhealthy` | Unhealthy | The channel is down or lagging. Trading is gated until it recovers. |
| `unknown` | Unknown | The channel's health has not been observed yet. |

### Action IDs

| Code | Label | Meaning |
|---|---|---|
| `deploy` | Deploy | Create and start a new bot bound to this account. |
| `start` | Start | Begin evaluating bars for this off-duty bot. |
| `stop` | Stop | Stop evaluating bars and cancel this bot's working entry orders. Exposure is left untouched. |
| `flatten_stop` | Flatten & stop | Cancel working orders, submit closing orders to flatten exposure, then stop. |
| `retire` | Retire | Permanently decommission this bot. Its id is never reused. |
| `cancel_order` | Cancel order | Cancel one working order at the broker. |
| `clear_hold` | Clear hold | Lift the account exposure hold once its root condition is healthy and freshly observed. |
| `reconcile_now` | Reconcile now | Run a reconciliation sweep against the broker immediately. |
