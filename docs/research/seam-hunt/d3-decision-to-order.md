# D3: from strategy decision to Alpaca order

Research ticket #2280, on the map #2276 (seam bug hunt, money path).
Question: where can one strategy decision become **zero** or **two** Alpaca orders?

- **Baseline:** `a14f1df1` (master, 2026-09-23). Every `file:line` below is at that SHA. Paths are relative to `PythonDataService/` unless they start with `docs/`.
- **Method:** a static trace of the code. The code is the authority; docstrings and ADRs were treated as claims and checked against it. One local probe ran against the vendored SDK in the host venv (G1 below). Nothing ran against a live service, a container, IB Gateway, Alpaca or Polygon.
- **Severity scale:** the map's P0–P3.

## Answer in one paragraph

The decision path itself is sound. Each decision has one durable identity. It is committed together with its order row and its client order id before any broker contact. A second submission is refused by fences that the Clerk owns, not by values the bot supplies. No broker call happens under the intake lock. A duplicate bar, a retry of the same decision, or a lifted quarantine does not reach the broker twice through this path.

The hazards sit **below** the Clerk and **beside** it:

1. alpaca-py silently re-sends `POST /v2/orders` when it gets an HTTP 504. The Clerk then folds the vendor's "duplicate client_order_id" answer as "the order never reached the broker". This is proven by reading plus a local SDK probe, and is filed as **#2304**.
2. The same SDK retry can outlive both of the Clerk's absence windows, because the worker thread is abandoned rather than stopped.
3. The stuck-EXIT watchdog reads its candidate entries outside the intake lock, then captures a recovery EXIT without re-checking for an active EXIT. A strategy EXIT captured in that gap gives two full-size reductions.

There are also two "zero orders" paths. In both, the strategy believes it holds a position the Clerk has voided (P2).

---

## (a) The trace across the boundary

### 1. Bot side: `bot_runner` to strategy to Clerk call

| Step | Where | What happens |
|---|---|---|
| Supervision | `app/services/bot_runner.py:1765-1790` | `_supervise` → `execute_bot_run` (`app/services/bot_runtime.py:119-141`) → `run_trade_bot` for `mode == "trade"`. |
| Run gate | `app/services/bot_runtime.py:67-84` | `PauseAwareFeed` stamps each bar with `DECIDE` or `OBSERVE_ONLY` at yield time. |
| Retained feed | `app/services/bot_trade_strategy.py:249-282` | Each bar passes `admit_on_delivery` (late recovered bars are refused, `app/services/feed_continuity_policy.py:64-101`), is appended to the source-bar ledger, then goes through the session filter. |
| Duplicate/out-of-order bar | `app/engine/strategy/signal_program.py:195-205` | `SignalSession.advance` quarantines an unsettled stage (`UNSETTLED_STAGE`) and any `end_ms <= last` (`NON_MONOTONIC_DECISION_CLOCK`). **A double bar never produces a second evaluation.** |
| Decision identity minted | `app/engine/strategy/signal_program.py:251-259` | `evaluation_id = sha256(program_key, program_version, settings, bar_close_ms)`. It includes neither `run_id`, `symbol` nor `strategy_instance_id`. |
| One intent per bar | `app/services/bot_trade_strategy.py:837-838` | More than one intent raises. |
| OBSERVE_ONLY | `app/services/bot_trade_strategy.py:864-873` | The candidate is DISCARDed and a `blocked` receipt is written. No Clerk call. |
| ENTER liveness pre-check | `app/services/bot_trade_strategy.py:880-911` | ENTER only. A block is DISCARDed with a receipt. EXIT is exempt by design. |
| Evidence | `app/services/bot_trade_strategy.py:1074-1115` | Builds `EffectDecisionEvidence`: `evaluation_id`, `bar_ref`, `observed_at_ms = now`, `trace_digest`. |
| **The call** | `app/services/bot_trade_strategy.py:925-936` | `clerk.execute_for_instance(decision_id=evaluation_id, …)`. |
| Disposition | `app/services/bot_trade_strategy.py:937-953` | `AdmissionBlockedError`: TRANSIENT is DISCARDed; TERMINAL re-raises and the bot crashes (`:1030-1064`). Receipt state `REJECTED` is DISCARDed. **Every other state, including `UNCERTAIN` and `ACCEPTED`, is COMMITted.** |

### 2. Clerk side: intake, acceptance, submission

| Step | Where | What happens |
|---|---|---|
| Task dedup | `app/broker/alpaca/clerk/sqlite/runtime.py:811-834` | One shielded task per `(strategy_instance_id, decision_id)`. The caller's cancellation cannot cancel an accepted effect. It checks `evidence.evaluation_id == decision_id` (`:813`), but both values come from the bot. |
| **Intake held** | `app/broker/alpaca/clerk/sqlite/runtime.py:907-1117` | Leg shaping (`shape_program_leg`), the stream-health refusal (`:964`), the live-clock liveness recheck plus the `before_submit` guard built for later (`:996-1014`), then `accept_enter` (`:1016`) or the EXIT branch (`:1043-1117`). |
| ENTER identity | `app/broker/alpaca/clerk/sqlite/enter.py:138-157` | `idempotency_key = "{sid}:{decision_id}"`, `command_id = "cmd:…"`, `effect_idempotency_key = "enter:…"`, and a payload hash over account, sid, decision and leg. |
| ENTER admission | `app/broker/alpaca/clerk/sqlite/enter.py:214-232` | Under the repository write lock: active-run fence, `require_admission`, arming (live only), envelope reservation. |
| **Client order id minted** | `app/broker/alpaca/clerk/sqlite/enter.py:234-235`, `app/engine/live/order_identity.py:96-110` | `order_ref = learn-ai/{sid}/v1:{uuid4-b64}`, minted by the **Clerk** inside `build_transition`. It becomes the Alpaca `client_order_id` (`app/broker/alpaca/adapter.py:304-330`). |
| Atomic commit | `app/broker/alpaca/clerk/sqlite/repository.py:973-1068` | Lookup by idempotency key plus append, under the write lock. **Same key, same hash** returns `CommandExistingSame` (no new effect). **Same key, different hash** (or diverging decision receipt, `app/broker/alpaca/clerk/sqlite/decision_receipts.py:209-254, 501-517`) returns `CommandExistingConflict`. Uniqueness is enforced by `ux_commands_idempotency` / `ux_effect_operations_idempotency` (`app/broker/alpaca/clerk/sqlite/schema.py:148-149, 195-196`). |
| Existing decision | `app/broker/alpaca/clerk/sqlite/enter.py:272-283, 352-353` | `created=False` returns **without broker contact**. |
| **Intake released**, then claim | `app/broker/alpaca/clerk/sqlite/enter.py:356`, `app/broker/alpaca/clerk/sqlite/repository.py:1086-1127` | CAS claim token, TTL 60 s (`app/broker/alpaca/clerk/sqlite/repository.py:105`). There is no `await` between the intake release and the claim. |
| Pre-contact guard | `app/broker/alpaca/clerk/sqlite/enter.py:365-377` | A liveness re-check (paper/shadow/live clock only) folds `ENTER_SUBMISSION_REFUSED`. |
| **Broker POST** | `app/broker/alpaca/clerk/sqlite/enter.py:379` → `app/broker/alpaca/clerk/sqlite/claimed_broker_io.py:49-66` → `app/broker/alpaca/clerk/sqlite/broker_port_guard.py:138-140` → `app/broker/alpaca/broker.py:228-238` → `app/broker/alpaca/client.py:471-497` | `client.post("/orders", body)` in a worker thread under `anyio.fail_after(15 s)` (`app/broker/alpaca/client.py:254-266`). |
| Response folds | `app/broker/alpaca/clerk/sqlite/enter.py:380-424` | `BrokerUnavailable` → `fold_uncertain`, then the exact-lookup resolver. **Any other `BrokerError`** → `fold_failed("ORDER_SUBMIT_FAILED", "The order did not reach the broker.")`, which is terminal. Success with a matching `client_order_id` → `fold_order_submission_response`. A mismatch → `fold_uncertain`. |
| Resolver | `app/broker/alpaca/clerk/sqlite/order_evidence.py:773-877` | Claim, then exact `get_order_by_client_order_id`. An absent order is voided only past the grace (`:880-905`). The grace is anchored at the order's **first transition** (`ENTER_ACCEPTED`, `:908-913`) and is 30 s long (`app/broker/alpaca/clerk/sqlite/uncertainty_policies.py:191`). |

The EXIT path runs through the same intake block:

1. It checks `active_exit_for_strategy`. A hit returns `EXIT_IN_PROGRESS` and captures the competing receipt (`app/broker/alpaca/clerk/sqlite/runtime.py:1044-1076`).
2. It picks `candidates[-1]` (`:1077-1099`).
3. `accept_exit` runs (`app/broker/alpaca/clerk/sqlite/exit.py:221-256`). Its key is `{sid}:{decision_id}` and its payload hash includes `entry_order_ref` (`:51-67`).
4. After intake is released, `resolve_accepted_exit` (`:363-411`) cancels the entries and proves them, then creates **one** reducing order per EXIT effect. The order is sized from `repo.position(sid, symbol)` (`app/broker/alpaca/clerk/sqlite/exit_resolution.py:241-291`). "One per EXIT" is enforced by `ux_operation_order_links_one_reducing`, which is per effect (`app/broker/alpaca/clerk/sqlite/schema.py:244-245`).

### 3. Writes before and after the broker call (ENTER)

| When | Durable write |
|---|---|
| Before contact (one txn) | Command, effect operation (`accepted`), order row with `client_order_id = order_ref`, the atomic decision receipt (`outcome = enter_intent`), and the envelope reservation (`app/broker/alpaca/clerk/sqlite/enter.py:236-269`). |
| Before contact | Claim token (`app/broker/alpaca/clerk/sqlite/repository.py:1102-1108`), then a renewal right before the POST (`app/broker/alpaca/clerk/sqlite/claimed_broker_io.py:50`). |
| After contact | Renewal (`app/broker/alpaca/clerk/sqlite/claimed_broker_io.py:65`). Then one of: `fold_order_submission_response`, `fold_uncertain` or `fold_failed`. Then the claim release (`app/broker/alpaca/clerk/sqlite/enter.py:425-428`). |

### 4. Crash between them, and boot recovery

- **Crash after accept, before or during the POST.** The effect stays `accepted`, the claim expires within 60 s, and the decision receipt says `enter_intent`.
  - At boot, `compose_repository_runtime` awaits `facade.recover()` (`app/broker/alpaca/clerk/active_runtime.py:402-405`).
  - `recover()` stops every active run under intake, then reconciles (`app/broker/alpaca/clerk/sqlite/runtime.py:1167-1183`). Reconciliation resolves the order by exact `client_order_id` lookup.
  - If the order is absent and more than 30 s have passed since `ENTER_ACCEPTED`, the order is voided as never reached.
  - **Nothing ever re-submits a captured `order_ref`.** The only `.submit(` call sites are the ENTER, reducing-EXIT and manual-order paths (`app/broker/alpaca/clerk/sqlite/enter.py:379`, `exit_resolution.py:1098`, `manual_orders.py:540`).
- **Crash after the POST, before the fold.** Same as above. The lookup finds the order and folds its evidence.
- **Strategy side after a crash.** Warmup replay re-applies each bucket's **receipt outcome**. `enter_intent` → COMMIT (`app/services/bot_trade_strategy_warmup.py:56-58, 157-162`), whatever the effect's terminal state. See G5.

### 5. Is there broker I/O under the intake lock?

**No, by construction.**

- Every broker port the facade holds is wrapped by `guard_broker_ports`. Any call made from a fenced dynamic scope raises `BrokerCallUnderIntakeError` (`app/broker/alpaca/clerk/sqlite/broker_port_guard.py:110-112, 172-174`; composition at `app/broker/alpaca/clerk/active_runtime.py:310-315` and `app/broker/alpaca/clerk/sqlite/runtime.py:271-272`).
- The ENTER and EXIT drive run after the `async with self._intake` block (`runtime.py:907` ends before `:1125` / `:1146`).
- Yields while the fence is held are detected and counted (`app/broker/alpaca/clerk/sqlite/intake_fence.py:222-250`).
- The stream-health refusal and the liveness re-check under intake read in-process facts, not Alpaca.

---

## (b) Invariants each side assumes of the other

| # | Assumed by | Assumption | Guaranteed? |
|---|---|---|---|
| I1 | Clerk | `decision_id` is stable for one decision and distinct across decisions. | **Mostly.** It is a hash of program, settings and `bar_close_ms` (`signal_program.py:251-259`), minted by the **subject** and checked only against the subject's own evidence (`runtime.py:813`). It excludes `run_id`. Safety does **not** rest on it for ENTER: the Clerk-owned fences `ENTER_IN_PROGRESS`, `ATTRIBUTED_EXPOSURE_EXISTS`, `EXIT_IN_PROGRESS` and `RECONCILIATION_IN_PROGRESS` refuse a second ENTER whatever id it carries (`app/broker/alpaca/clerk/sqlite/uncertainty.py:611-632, 689-716`). The owner's evidence rule holds in substance: the broker-facing identity (`order_ref`) is Clerk-minted (`enter.py:234-235`). |
| I2 | Bot | A receipt that is not `REJECTED` means the position was, or will be, taken. | **No.** `UNCERTAIN` and `ACCEPTED` are COMMITted (`bot_trade_strategy.py:942-953`) but can later void to `failed`. Nothing tells the strategy. See G4. |
| I3 | Bot | `REJECTED` means no order exists at the broker. | **No.** A duplicate-client-order-id reply after an SDK 504 retry is folded `failed` while the order lives at Alpaca. See G1 / #2304. |
| I4 | Clerk | A `BrokerError` other than `BrokerUnavailable` from `submit` means the POST did not land. | **No.** Same root cause as I3 (`app/broker/alpaca/errors.py:90-105` maps 409 and 422 to definitive rejects). |
| I5 | Clerk | When `fail_after(15 s)` returns, the submission attempt is over apart from Alpaca's index lag, which is covered by 30 s windows. | **No.** The worker thread is abandoned, not stopped (`client.py:254-266`), and the SDK loop inside it can keep re-POSTing for tens of seconds. See G2. |
| I6 | Clerk | At most one non-terminal EXIT effect exists per strategy and symbol. | **Enforced only at the strategy capture site** (`runtime.py:1044`, under intake). No schema constraint exists (`schema.py:244-245` is per effect). The watchdog capture does not re-check it inside its transaction. See G3. |
| I7 | Clerk | The bot never re-sends the same `decision_id` with different evidence. | **Yes in practice.** The runner calls once per evaluation and warmup never re-sends. If it did, the result would be `DurableConflictError`, which the runner does not catch, so the bot crashes and places zero orders (G6, P3). |
| I8 | Bot | A decision reaches the broker close to its bar close. | **Not bounded.** Only market liveness is re-checked. Realtime bars are exempt from the delivery allowance (`feed_continuity_policy.py:77`), and the intake queue time is unbounded. See G7. |

---

## (c) Named suspected gaps

Each gap is a falsifiable hypothesis with a provisional severity. "Paper" says whether a prototype needs the Alpaca paper account.

### G1: SDK 504 re-POST is folded as never sent (P0, **proven path; bug #2304**)

**Hypothesis.** A `POST /v2/orders` that Alpaca accepts but answers with 504 is re-sent by alpaca-py with the same `client_order_id`. The duplicate answer (422 or 409) maps to `BrokerRequestInvalid` or `BrokerOrderRejected`. `submit_accepted_enter` then folds `ORDER_SUBMIT_FAILED`, a terminal state, and the strategy DISCARDs. Result: a live order that the Clerk calls failed and the strategy does not hold.

**Evidence:**
- The client is built with no retry arguments (`app/broker/alpaca/client.py:217-222`), so the SDK defaults apply: `[429, 504]`, 3 attempts, 3 s apart (`alpaca/common/constants.py:11-13`, alpaca-py 0.42.0 pinned at `requirements-light.txt:44`).
- The SDK retry loop is at `alpaca/common/rest.py:127-135` and `:201-202`.
- The duplicate answer is mapped at `app/broker/alpaca/errors.py:90-105` and folded at `app/broker/alpaca/clerk/sqlite/enter.py:388-401`.
- The comment at `app/broker/alpaca/client.py:104-112` ("a write is never re-issued") is true only of the repo's own GET/HEAD retry.
- A local probe against the pinned SDK saw **two POSTs** with the same `client_order_id` for a 504 followed by 422, with the 422 raised to the caller.

**What remains unproven:** whether Alpaca ever answers 504 after accepting an order, and whether it answers a duplicate id with 409 or 422. Either status leads to the same fold.

**Prototype.** In process: a fake `requests.Session` answering 504 then a duplicate-id error, driven through `AlpacaBroker.submit` → `submit_accepted_enter` against a temp repository. Assert the effect is `failed` and the receipt is `REJECTED`, while the fake holds an accepted order. **Paper: optional**, only to learn Alpaca's duplicate-id status: two POSTs with one `client_order_id`, a far-from-market limit, then cancel.

### G2: an abandoned SDK worker posts after the void (P0)

**Hypothesis.** After `fail_after(15 s)` fires (`client.py:254-266`), the worker thread keeps running its SDK retry loop: up to 3 more POSTs, each with a 15 s read timeout (`client.py:88-102`) and a 3 s sleep between them. So a POST can land about 35–70 s after `ENTER_ACCEPTED`. By then two things have happened:

- The durable void grace (30 s from `ENTER_ACCEPTED`, `order_evidence.py:880-913`) has passed.
- The in-memory visibility window (30 s from the timeout, `client.py:68, 186-205`) has passed.

So the resolver voids the ENTER as never reached (`fold_submit_absence_void`), and then the order lands and fills.

**Prototype.** In process, with a fake session whose 504 responses are delayed, a fake clock, and a sweep pass run between them. Assert the void is recorded and a later POST is observed. **Paper: no.**

### G3: the watchdog re-drive races a strategy EXIT, giving a double reduction (P0)

**Hypothesis.** `redrive_or_escalate_stale_exits` reads candidate entries through `to_thread`, **outside intake** (`app/broker/alpaca/clerk/sqlite/exit_watchdog.py:231-239`, `reconcile.py:825-827`). It then captures in a separate `intake.off_loop` (`exit_watchdog.py:299-309`). `accept_recovery_exit` does not re-check for an active EXIT inside the capture (`exit.py:320-326`).

A strategy EXIT captured between the read and the capture (`runtime.py:1044-1100`) leaves two non-terminal EXIT effects on the same entry. Each sizes its reducing order from `repo.position()` (`exit_resolution.py:247-291`), with no allowance for the other's working reducer. The result is two full-size SELLs, which overshoot into a short.

**Reachability.** The watchdog needs a stale `EXIT_NOT_FLAT` episode. When an EXIT folds `failed`, its receipt is `REJECTED`. The strategy then DISCARDs (`bot_trade_strategy.py:942-945`), still believes it is long, and re-emits EXIT on its next bucket. So the two can coincide.

The same read-then-capture split also races a safe flatten (`safe_flatten_execution.py:124-150`, whose read and capture are atomic on their own side).

**Prototype.** In process: a temp repository, a fake trade port, and an `EXIT_NOT_FLAT` episode aged past the policy. Pause the watchdog after `_candidate_entries` and run `execute_for_instance(EXIT)`. Assert that two EXIT effects and two reducing orders exist. **Paper: no.** (Paper, RTH, would show only the broker's view of the overshoot.)

### G4: a COMMIT on UNCERTAIN, later voided, splits beliefs (P2)

**Hypothesis.** A `BrokerUnavailable` submission returns `UNCERTAIN` (`runtime.py` `_effect_receipt` maps `unknown` to `UNCERTAIN`), and the strategy COMMITs (`bot_trade_strategy.py:953`). The sweep later voids the ENTER. The strategy believes it is long while the Clerk is flat, so it suppresses ENTERs until its next EXIT folds attributed-flat. That is a missed trade, not a wrong order.

**Prototype.** In process: a fake port raising `BrokerUnavailable`, then a 404 lookup past the grace. Assert the strategy position is long and the Clerk position is flat. **Paper: no.**

### G5: warmup replays the receipt, not the outcome (P2)

**Hypothesis.** Suppose a crash between accept and POST, then a resume. Warmup COMMITs `enter_intent` (`bot_trade_strategy_warmup.py:56-58, 157-162`) although the effect voided at boot. This is the same split belief as G4, reached through restart.

**Prototype.** In process: an accepted ENTER, no POST, a clock advanced past 30 s, `recover()`, then warmup replay. **Paper: no.**

### G6: replaying a decision with drifted evidence crashes the bot (P3)

**Hypothesis.** A same-key replay whose `bar_ref` differs, because the retained bar was found once and missing once (`bot_trade_strategy.py:1094-1109`), returns `CommandExistingConflict`. That raises `DurableConflictError`, which the runner does not catch (`bot_trade_strategy.py:937`), so the bot crashes and places zero orders.

It is not reachable today: the runner never re-sends a decision (I7). It fails loud.

**Prototype.** A unit test at `commit_first_transition`. **Paper: no.**

### G7: late realtime decisions are submitted (P3)

**Hypothesis.** A realtime bar delivered late, or queued behind intake, still reaches `submit`. The delivery allowance skips `provenance == "realtime"` (`feed_continuity_policy.py:77`), and nothing bounds the time from decision to POST.

**Prototype.** In process: stall intake for N minutes, then assert the submit still goes out. **Paper: no.**

### G8: the idempotency key is subject-supplied and omits the run (P3)

**Hypothesis.** `evaluation_id` excludes `run_id` (`signal_program.py:251-259`). A new run of the same instance that re-decides a `bar_close_ms` an earlier run already decided resolves to the old command (`CommandExistingSame`), with no new order and the old state returned.

Warmup prevents re-deciding a captured bucket live, so this is imprecise, not wrong.

**Prototype.** In process: two runs with overlapping live bars. **Paper: no.**

## (d) Defects proven by reading alone

- **G1 / #2304 (P0).** The order POST is re-issued by the vendor SDK on 504, and a duplicate-client-order-id answer is folded as "the order did not reach the broker". The code path is proven by reading and by a local probe against the pinned SDK. Only the vendor's 504-after-accept behaviour is unverified.
- No other defect is proven by reading alone. G2 and G3 are strong static hypotheses that need prototypes.

## Cleared statically

- Retry of one decision: the key plus `CommandExistingSame` gives no second POST.
- Duplicate bar: quarantined before staging.
- Quarantine lift: record-only, and refused bars never stage (`app/services/bot_decision_quarantine.py:1-10`).
- Late recovered bar: refused on delivery.
- Broker I/O under intake: guarded and structurally absent.
- ENTER-vs-ENTER and ENTER-vs-EXIT: Clerk-owned admission fences.
- Strategy EXIT-vs-EXIT: `active_exit_for_strategy` under intake.
