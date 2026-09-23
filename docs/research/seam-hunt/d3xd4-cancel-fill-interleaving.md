# D3×D4: a cancel crossing a fill while intake is still writing

Research ticket #2356, on the seam bug-hunt map #2276. It builds on the D3 trace (#2280, branch `research/d3-decision-to-order`) and the D4 trace (#2281, branch `research/d4-trade-updates`), and on prototypes #2310–#2312, #2315–#2317 and #2331.

- **Baseline:** `a14f1df1` (master, 2026-09-23). Every `file:line` is at that SHA, relative to `PythonDataService/app/` unless it says otherwise.
- **Method:** a static trace, plus one throwaway in-process probe (below) using the real `SqliteAlpacaClerkFacade`, a temp SQLite repository and the parked fake broker from `tests/broker/alpaca/clerk/sqlite/test_runtime.py`. The probe was not committed. Nothing ran against Alpaca, IB Gateway, Polygon or a container.
- **Already charted, linked, not re-filed:** #2304, #2305, #2306, #2342, #2343, #2346, #2347, #2348.

## Answer in one paragraph

The folds themselves survive the interleavings. The order row exists before any frame can arrive, every fold is one atomic transition, cumulative fills are computed as a delta inside the fold transaction, and acknowledgements never move a terminal order backwards. So a fill frame that lands between the intake write and the submit response, or a REST reconcile and a websocket frame folding the same order during a cancel, cannot double-count or regress custody on their own. The break is in **who a cancel thinks it has to cancel**. An ENTER leaves intake with `broker_state = NULL` and state `accepted`, and stays that way until its POST response is folded. The operator's Stop computes its cancel set from `broker_state` only, and its custody proof counts only `unknown` effects as unresolved. So a Stop that lands while the ENTER's POST is in flight **cancels nothing and reports the instance `STOPPED_FLAT`**, and the order then lands at Alpaca for a stopped run. This is proven by reading plus the probe, rated P0, and filed as a bug. The other interleavings are either covered by the charted bugs or fail closed (P2/P3).

---

## (a) The trace across the boundary

### 1. The write points on one ENTER, in order

| # | Write | Lock held | Where |
|---|---|---|---|
| W1 | `ENTER_ACCEPTED`: command, effect (`accepted`), order row with `client_order_id = order_ref`, **`broker_order_id = NULL`, `broker_state = NULL`** | intake + repo write lock | `broker/alpaca/clerk/sqlite/folds.py:441-468`, called from `broker/alpaca/clerk/sqlite/runtime.py:1016` inside `async with self._intake` (`runtime.py:907`) |
| — | intake released | — | end of the block, `runtime.py:1116` (the drive starts at `:1117`) |
| W2 | claim token on the ENTER effect (TTL 60 s) | repo write lock | `broker/alpaca/clerk/sqlite/enter.py:356`, `repository.py:1090-1127` |
| — | `before_submit` liveness guard (sync) | none | `enter.py:365-377` |
| — | **POST in a worker thread** (the first real `await` after intake) | claim only | `enter.py:379` → `claimed_broker_io.py:49-66` |
| W3 | submit response fold: `ORDER_SUBMIT_ACKED` (success), `ORDER_SUBMIT_UNCERTAIN` (timeout/5xx) or `ORDER_SUBMIT_FAILED` (other `BrokerError`) | claim; **not** intake | `enter.py:380-424` |
| W4 | claim released | repo write lock | `enter.py:425-428` |

Frames from `trade_updates` fold under **intake** (`broker/alpaca/clerk/trade_evidence.py:146`), not under the effect's claim. So W3 and a websocket fold for the same order interleave at transition granularity. Every transition is one atomic `append_transition` under the repository write lock.

### 2. What a frame does if it lands between W1 and W3

- The order row exists (W1 precedes the POST), so the sink finds it and does not raise an unexplained-order hold (`trade_evidence.py:147-181`; D4 invariant I3).
- A fill slice folds position under the owner `active_exit_for_order(...) or ENTER` (`trade_evidence.py:183-218`). The slice fold does not look at effect state (`folds.py:936-981`).
- The ack advances `orders.broker_state` monotonically and moves the effect to `in_progress` unless it is terminal (`folds.py:624-663`).
- W3 then arrives:
  - **Success:** `fold_order_submission_acknowledgement` appends a stale ack (`append_stale_ack=True`, `order_evidence.py:505-537`). `_ack_advances_order` refuses to regress a terminal or newer state (`folds.py:583-621`). Harmless.
  - **Timeout / `BrokerUnavailable`:** `fold_uncertain` moves an `in_progress` effect back to `unknown` and opens an unknown-outcome episode, even though an ack and possibly a fill are already recorded (`enter.py:381-387`, `folds.py:847-880`). The resolver's exact lookup then folds the evidence and resolves it (`order_evidence.py:773-877`). Self-correcting (G3).
  - **Any other `BrokerError`:** `fold_failed` terminalizes the ENTER without checking the recorded ack or fills (`order_evidence.py:609-650`, `folds.py:666-697, 744-755`). This is #2304 (and #2342, #2348 downstream). Note for the #2304 fix: the contradicting evidence can already be durable at the moment of the fold, and `order_never_reached_broker` (`order_evidence.py:659-683`) already encodes the check `fold_failed` skips.

### 3. The cancel paths and what they target

| Cancel | Target set | Claim taken | Where |
|---|---|---|---|
| Operator Stop / lane Stop-all | ENTRY orders whose `broker_state` is in `_WORKING_ORDER_STATES` | the ENTER's (or its active EXIT's) effect | `services/bot_carryover.py:142` → `broker/alpaca/clerk/sqlite/runtime.py:1390-1428`, filter `runtime.py:1457-1470, 1551-1552`, set `runtime.py:148-161`; claim at `exit_resolution.py:155` |
| Recovery action `cancel_verified_working_orders` | same function | same | `recovery_execution.py:200-210` |
| Strategy / watchdog / safe-flatten EXIT | every entry linked to the EXIT; cancel sent only if `broker_order_id` is set | the EXIT's own effect | `exit_resolution.py:623-729` |
| Manual order cancel | the one manual order | the CANCEL effect | `manual_order_cancellation.py:337-556` |
| Crash / FEED_DEATH | none | — | #2347 |

### 4. Stop, step by step (`services/bot_runner.py:1216-1300`)

1. Durable `STOPPED` desired state (`bot_runner.py:1235`), `run_gate.clear()`.
2. `commit_stop_before_task_cancel` → `stop_strategy_run` → `RUN_STOPPED` under intake (`services/bot_clerk_lifecycle.py:67-82`, `runtime.py:664-679`, `commands.py:263-323`). It needs only intake, which the ENTER released at step W1.
3. `managed.task.cancel()` (`bot_runner.py:1260`). The bot task is awaiting `asyncio.shield(task)` (`runtime.py:834`), so it is cancelled at once. The shielded ENTER keeps running its POST.
4. `prove_terminal_stop_outcome` → `prove_stop_outcome` (`bot_run_terminal.py:198-222`, `bot_carryover.py:133-176`):
   - `cancel_working_entries_for_instance` — skips any ENTRY whose `broker_state` is `NULL`.
   - `prove_instance_custody` → a full `reconcile_account`. A reconcilable effect whose claim is held is **deferred** and does not change the verdict (`reconcile.py:607-616`).
   - `working_order_refs` again filters on `broker_state` (`runtime.py:1472-1484`). `unresolved_intent_refs` is effects in state `unknown` only (`runtime.py:1486-1493`, `reads.py:758-769`). An `accepted` effect is in neither set.
   - No exposure yet → `STOPPED_FLAT` (`bot_carryover.py:154-163`).

---

## (b) Invariants each side assumes of the other

| # | Assumed by | Invariant | Guaranteed? |
|---|---|---|---|
| X1 | trade-update sink, of intake | An order's row exists before any frame for it. | **Yes** (W1 precedes the POST; D4 I3). |
| X2 | cumulative (REST) fold, of the websocket fold | A concurrent exact slice cannot make the cumulative fold double-count. | **Yes.** The delta is computed against effective fills inside the fold transaction (`folds.py:1285-1320`). The residual risk is a coverage *conflict*, not a double count (#2346, G4). |
| X3 | submit-response fold, of the websocket fold | Nothing about the order is known yet when the response folds. | **No.** A frame can win the race. The success fold is monotone and harmless. The timeout fold regresses `in_progress` → `unknown` (G3). The failure fold terminalizes over recorded evidence (#2304). |
| X4 | Stop, of intake | An ENTER that could still reach the broker is visible as *working* or *unresolved*. | **No.** Between W1 and W3 the ENTER is `accepted` with `broker_state = NULL`: in neither set (**G1, proven**). |
| X5 | Stop, of the sweep | Stop's cancel can always take the entry's claim. | **No.** The 15 s sweep claims every nonterminal ENTER whose order is still working (`reads.py:584-616`) for the length of its exact lookup. A collision raises `OperationClaimError`, which `prove_stop_outcome` turns into `STOPPED_CUSTODY_UNPROVABLE` with the order still working (G2). |
| X6 | EXIT, of the ENTER | An entry being cancelled has a broker id to cancel. | **Not while the ENTER's POST is in flight.** The EXIT skips the cancel and polls, and a young absence folds the EXIT `unknown` (`exit_resolution.py:657-705`). It retries next pass. Only reachable today through #2342's abandoned worker (G5). |
| X7 | STOP proof, of reconcile | A `clean` verdict means no order work is pending for the instance. | **No.** Deferred (claim-contended) effects are silently skipped (`reconcile.py:607-616`); see G1. |

---

## (c) Named suspected gaps

### G1 — Stop during an in-flight ENTER POST cancels nothing and reports `STOPPED_FLAT`: **P0, proven** (bug filed)

**Hypothesis.** A Stop whose `RUN_STOPPED` commits after W1 and before W3 leaves the ENTER out of Stop's cancel set and out of the proof's working/unresolved sets. The proof reads `clean`, empty, no exposure → `STOPPED_FLAT`. The POST then lands; the order is working (or fills) at Alpaca for a run that is stopped. Nothing cancels it later: the sweep only resolves it to `in_progress`, and only Stop and the recovery action cancel entries.

**Probe result** (real facade + repository; fake broker parks `submit` until released):

| Variant | STOP outcome | Cancels sent | After the POST returns |
|---|---|---|---|
| Alpaca has not yet listed the order | **`STOPPED_FLAT`** | 0 | effect `in_progress`, `broker_state=accepted`, `active_run=None`, 1 submission |
| Alpaca already lists the order as open | `STOPPED_CUSTODY_UNPROVABLE` | 0 | order still working |

Both variants leave a live order uncancelled. The first also tells the operator the bot is flat.

**Window.** From intake release (`runtime.py:1116`) to the W3 fold. Normally the POST round trip (100s of ms). Up to 15 s on a slow POST (`client.py:254-266`), and with #2342 the POST itself can land up to about 69 s later. In RTH a marketable limit usually fills at once, which turns the result into an attributed position under a stopped run; in extended hours a resting limit stays working.

**Needs paper:** no (proven in-process). Paper only to measure a real POST's latency distribution.

### G2 — Stop colliding with the sweep's claim leaves a working ENTER uncancelled: **P2**

**Hypothesis.** A working ENTER is reconcilable (`reads.py:603-608`), so each 15 s sweep claims it for its exact lookup (`order_evidence.py:813`). A Stop whose `cancel_and_prove_owned_entry` runs during that lookup raises `OperationClaimError` at `exit_resolution.py:155`. `prove_stop_outcome` catches it and returns `STOPPED_CUSTODY_UNPROVABLE` (`bot_carryover.py:141-152`, the catch at `:144`). Stop does not retry, so the order stays working. It fails closed and honestly (Resume is refused), but the operator's Stop did not cancel. The same happens when the ENTER's own POST claim is still held and a `new` frame has already made the order "working". The same claim collision applies to an entry nested under an active EXIT.

**Prototype.** In process: hold the ENTER's claim (a parked `get_order_by_client_order_id` in the sweep), run `prove_stop_outcome`, and assert zero cancels and `UNPROVABLE`. **Paper:** no.

### G3 — A submit timeout after a websocket ack regresses the ENTER to `unknown`: **P3**

**Hypothesis.** A `new`/`fill` frame folds before the POST times out (the websocket is faster than the HTTP response). The timeout fold moves the effect `in_progress` → `unknown` and opens an unknown-outcome episode over evidence that already proves the order exists (`folds.py:847-880`). The resolver's lookup (`enter.py:430-431`) resolves it at once when the broker answers; if the lookup also fails, the ENTER stays `unknown` until the sweep. This is self-correcting and fails closed. The one cost is that an ENTER already holding a fill is shown as "outcome unknown".

**Prototype.** In process: the parked broker raises `BrokerUnavailable` after the sink folded `new` + `fill`; assert the state sequence and that the next lookup resolves it. **Paper:** no.

### G4 — An EXIT's own cancel-proof creates the cumulative row that the late websocket slice then conflicts with: **P2 (P1 if a slice is lost; same dead end as #2346)**

**Hypothesis.** An EXIT cancelling a partially filled ENTER proves it terminal by REST (`exit_resolution.py:707-729`, `fold_order_evidence` → cumulative `ORDER_FILL_OBSERVED`). If the websocket's exact slices for those executions arrive after that fold, which is ordinary websocket lag and needs **no outage**, a single slice smaller than the cumulative total raises `EXECUTION_COVERAGE_CONFLICT` and blocks `REDUCE`. That can happen at the EXIT's own `require_capability` (`exit_resolution.py:272-281`), so the EXIT stalls until the remaining slices arrive and heal the conflict. If one of those slices is lost (#2305), the block is permanent, as in #2346. This is a new, routine trigger for the #2346 class: #2346 needed an outage, this needs only an EXIT against a multi-execution partial fill.

**Prototype.** In process: a partial of 2 executions (2 + 3), EXIT cancel proven by REST at cumulative 5, then deliver exec-A only; assert `REDUCE` refused and the EXIT nonterminal; then deliver exec-B and assert the heal. Drop exec-B and assert the permanent block. **Paper:** optional, to learn whether Alpaca's `trade_updates` routinely trails the REST order view by more than one round trip.

### G5 — An EXIT running while its ENTER's POST is in flight: **P3 (reachable only through #2342)**

**Hypothesis.** An EXIT whose entry has no `broker_order_id` yet sends no cancel, polls, and folds itself `unknown` on a young absence (`exit_resolution.py:657-705`). When the ENTER's response later folds, the ack is appended under the **ENTER's** effect id although an EXIT is active (`enter.py:404-409` uses `accepted.effect_operation_id`), which is not the #2251 nesting the sink follows (`trade_evidence.py:183-187`). Custody is unaffected. The strategy cannot emit this EXIT while it awaits the ENTER, and the watchdog and safe flatten need exposure, so today this is reachable only when the #2342 abandoned worker lands after a void. Covered by #2342/#2348; listed so the fix for those is checked against it.

**Prototype.** Fold into the #2342 regression test. **Paper:** no.

### Cleared (static)

- **A fill frame between W1 and W3 double-counts or is lost.** No: X1 and X2 hold.
- **REST reconcile and `trade_updates` fold the same order during a cancel and double-count.** No: X2. Only the conflict class remains (G4, #2346).
- **A Stop commits after W1 and the ENTER is still admitted.** The ENTER is already admitted at W1; the risk is G1. A Stop committed **before** W1 wins: `accept_enter`'s active-run fence refuses (`enter.py:214-232`).
- **Stop crossing a fill on an acknowledged ENTER.** The cancel is sent, the exact lookup folds the fill, and the proof reports exposure (`STOP_REQUIRES_FLATTEN` or approved carryover). Correct.
- **A manual order cancel crossing a fill.** The terminal state is proven by exact lookup; a partial fill before the cancel keeps its fill rows (`manual_order_cancellation.py:510-555`). Correct.
- **Stop with an active EXIT.** Stop does not cancel reducing orders by design; the proof counts a working reducing order (`runtime.py:1472-1484`) and reports `UNPROVABLE`.

---

## (d) Defects proven by reading alone

- **G1 (P0): Stop during an in-flight ENTER POST neither cancels nor reports the order.** The static chain is complete (§4 above: `folds.py:459-468` writes `broker_state = NULL`; `runtime.py:1457-1470, 1551-1552` filter it out of the cancel set; `runtime.py:1486-1493` and `reads.py:758-769` exclude `accepted` from the unresolved set; `bot_carryover.py:154-163` returns `STOPPED_FLAT`). The in-process probe reproduced both variants. Filed as a bug; see the resolution comment on #2356.

Nothing else is proven by reading alone. G2–G5 are hypotheses for prototypes.

<details><summary>Probe (the future regression test)</summary>

Placed at `tests/broker/alpaca/clerk/sqlite/probe_2356_test.py` so the autouse `tests/conftest.py` fixtures (market liveness) apply; run with
`DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/alpaca/clerk/sqlite/probe_2356_test.py -s -q`.

```python
async def test_stop_during_inflight_enter_post(tmp_path: Path) -> None:
    repo = ClerkSqliteRepository.initialize(account_id="PA-TEST", artifacts_root=tmp_path)
    broker = _Broker()  # test_runtime's parked broker
    facade = SqliteAlpacaClerkFacade(repo=repo, read=broker, trade=broker, account_mode="paper")
    binding = _binding()
    await facade.register_strategy_run(binding)
    bot_task = asyncio.create_task(facade.execute_for_instance(
        strategy_instance_id=binding.strategy_instance_id, run_id=binding.run_id,
        decision_id="decision-1", purpose=EffectPurpose.ENTER,
        action_plan=binding.action_plan, quantity=binding.quantity))
    await broker.submit_started.wait()          # POST dispatched, response pending
    await facade.stop_strategy_run(             # bot_runner._stop_locked order
        strategy_instance_id=binding.strategy_instance_id, run_id=binding.run_id, reason="operator_stop")
    bot_task.cancel()
    with suppress(asyncio.CancelledError):
        await bot_task
    outcome = await prove_stop_outcome(binding, clerk=facade,
                                       checkpoint_path=tmp_path / "cp.json", now_ms=lambda: 1)
    # observed: outcome == "STOPPED_FLAT", broker.cancellations == []
    broker.release_submit.set()                 # the order lands, working, for a stopped run
```
</details>
