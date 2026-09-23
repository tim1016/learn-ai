# D4: Alpaca `trade_updates` → fills → order terminalization

Research ticket #2281, on the seam bug-hunt map #2276. Static trace at `a14f1df1` (master, 2026-09-23). Every citation is `path:line` at that SHA, relative to `PythonDataService/app/` unless it says otherwise.

**Question.** Can Alpaca `trade_updates` leave the clerk believing a different order or position state than Alpaca holds?

**Answer.** Yes, in two proven ways. Both need only one ordinary event.

1. **A dropped frame is never re-derived.** If a `partial_fill` frame fails capture or mapping, the frame is lost. When the order's next frame is terminal (`fill` / `canceled`), the order leaves the reach of per-effect recovery. The clerk's attributed position then stays short by the lost slice. The account-level drift hold catches it within one sweep. "Reconcile now" cannot converge it, though. Only a later EXIT's REST refresh repairs it.
2. **The websocket route bypasses the #2006 fix.** When the websocket reports a vendor cancel of an unfilled ENTER, the ENTER never reaches `failed`. It stays `in_progress` for good, which is the #2006 residue on its dominant route.

The reconnect path itself is sound. On every reconnect a full account reconciliation runs before admission reopens. Duplicate executions are deduplicated durably, and the order projection is monotone. The remaining risk sits in five places:

- in-stream frame loss;
- the reconcilable-effect filter;
- the websocket sink's narrower fold;
- fail-closed coverage conflicts that only an operator can clear;
- the boot-to-first-subscribe window.

The fixed #2251 nesting path (an EXIT cancelling the entry) is prototyped separately under #2282. It is covered here only where it touches the websocket sink.

---

## (a) Trace across the boundary

### A.1 Socket → consumer (transport, capture, parse, in-memory dedup)

| Step | What happens | Where |
|---|---|---|
| Connect | `websockets.connect`, then an auth frame. The consumer requires `authorized`, sends `listen`, then yields the auth frame as the first frame. | `broker/alpaca/trade_updates.py:940-984` |
| Boot wiring | The consumer is built and started only after `select_active_clerk_runtime` returns. Boot recovery (`facade.recover()` → `reconcile_account`) has already run by then. | `main.py:616-627`, `main.py:671-677`; `broker/alpaca/clerk/sqlite/runtime.py:1167-1190` |
| Cycle | `_consume_once` takes the first frame. **Only when `cycles > 0`** does it run `_gap_reconcile()`. Only then does it mark `connected=True` and drain the socket. The first connection after boot never gap-reconciles. | `trade_updates.py:420-425`, `trade_updates.py:479-500` |
| Capture | Each frame is recorded verbatim before it is parsed. On a capture failure, the frame is **dropped**, `evidence_health=False`, and the socket continues (no reconnect). | `trade_updates.py:504-524` |
| Parse / map | JSON / `from_alpaca_trade_update` / `from_alpaca_order` failures work the same way: the frame is dropped, health goes false, and the socket continues. | `trade_updates.py:526-539`, `trade_updates.py:571-592` |
| Dedup key | The key is `exec:{execution_id}` for fills, else `order_id\|event\|occurred_at_ms`. | `trade_updates.py:259-277` |
| Dedup | An exact redelivery is skipped and counted. A changed payload under the same key is journaled under a `:variant:` key. The map of seen keys (`_seen`) and the terminal-order map (`_terminal_orders`) are **in-memory only**, with no upper bound. | `trade_updates.py:617-664`, `trade_updates.py:317-322` |
| Cross-path guard | A REST gap-replay of an already-terminal order is suppressed by its terminal-state fingerprint. The guard applies to the gap path only. | `trade_updates.py:597-615`, `trade_updates.py:224-250` |
| Hand-off | `evidence_sink.record_lifecycle_event(...)`. Only after it returns does the key enter `_seen`. `evidence_health=True` is set on **any** successfully mapped live frame. | `trade_updates.py:666-689` |
| Sink raises | The exception escapes `_handle_frame`. `_consume_once` closes via `finally` (`connected=False`). `run()` logs, backs off, and reconnects. The next cycle gap-reconciles. | `trade_updates.py:424-440`, `trade_updates.py:499-500` |

### A.2 Consumer → SQLite clerk (`SqliteTradeUpdateEvidenceSink`)

`broker/alpaca/clerk/trade_evidence.py:135-231`, under the shared intake lock (`:146`):

1. `local_order = repo.order(client_order_id)` (`:147`). If the order is unknown and the event carries an order, the sink calls `observe_external_order` and raises an account hold (`:148-157`). If the order is unknown and the event has no order, it calls `raise_account_hold` (`:159-181`).
2. **Owner selection:** `owner = repo.active_exit_for_order(order_ref) or repo.effect_operation(order.effect_operation_id)` (`:183-187`). `active_exit_for_order` returns the newest nonterminal EXIT linked to the order (`broker/alpaca/clerk/sqlite/reads.py:555-567`). This is the nesting the ticket names: while an EXIT is live, the entry's evidence is appended under the **EXIT's** effect id.
3. A REST gap-replay fill with no execution id goes to `fold_order_evidence` (cumulative recovery), then returns (`:189-202`).
4. A websocket fill with an execution id goes to `append_exact_execution_slice` (`:204-218`). That delegates to `repo.append_execution_slice_if_absent`, which gives durable dedup on `execution_id` and handles cumulative supersession or a coverage conflict (`broker/alpaca/clerk/sqlite/repository.py:548-679`).
5. **Every websocket event** then goes to `fold_order_acknowledgement(..., append_stale_ack=False)` (`:225-231`). The websocket route does **not** call `fold_order_evidence`, so it never reaches `_fold_enter_unfilled_if_proven` (`broker/alpaca/clerk/sqlite/order_evidence.py:204-212` is the only caller of that gate).

### A.3 Folds (SQLite projections)

- `EXECUTION_SLICE_FILLED` inserts a `fills` row (idempotent on `execution_id`) and adds the signed quantity to `positions` for the owner's `subject_id` (`broker/alpaca/clerk/sqlite/folds.py:936-981`, `folds.py:905-930`). It does **not** touch the effect's state.
- `ORDER_FILL_OBSERVED` (cumulative recovery) records the delta against the order's effective fills and ignores a regressed observation (`folds.py:1234-1320`).
- `ORDER_SUBMIT_ACKED` advances `orders.broker_state` only monotonically: a terminal state never regresses, and ties go to source time, then state precedence. It moves the effect and command to `in_progress` unless they are already terminal (`folds.py:583-597`, `folds.py:624-663`).
- Terminal outcomes go through `_fold_effect_terminal`, which ignores a repeat on an already-terminal effect (`folds.py:666-697`). An ENTER never reaches `succeeded`. It reaches `failed` only via `ENTER_UNFILLED` or submit failure (`folds.py:1493-1502`).

### A.4 Reconciliation (the backstop)

- `SqliteTradeUpdateEvidenceSink.reconcile_gap` calls `reconcile_account(trigger="AUTOMATIC")` and raises on a `stale` verdict, which keeps admission closed (`trade_evidence.py:233-236`; `trade_updates.py:710-712`).
- The `ReconciliationSweep` runs the same `reconcile_account` every **15 s** (`broker/alpaca/clerk/sqlite/reconciliation_sweep.py:58`, `:379-381`). It starts after boot recovery (`broker/alpaca/clerk/active_runtime.py:168-183`).
- `reconcile_account` runs in this order (`broker/alpaca/clerk/sqlite/reconcile.py:784-863`):
  1. Snapshot **open** orders and positions (`:519-535`, `:538-561`).
  2. Fold the open-order snapshot (`:732-782`).
  3. Recover each **reconcilable** effect by exact REST lookup (`:564-622`, `:198-307`). For an ENTER this is `resolve_order_submission` → `fold_order_evidence` (`broker/alpaca/clerk/sqlite/order_evidence.py:773-877`).
  4. Re-snapshot and compare broker positions with attributed positions per symbol. On mismatch it raises an account-wide `POSITION_DRIFT` uncertainty (`reconcile.py:128-195`, `:870-921`, `:372-432`).
- **Reconcilable effects** means nonterminal effects that are `accepted` / `unknown`, **or** EXIT/CANCEL, **or** have a non-terminal or null `broker_state`, **or** are `filled` with *no* effective fill at all (`reads.py:584-616`). An ENTER whose `broker_state` is already `canceled` / `expired` / `rejected`, or is `filled` with at least one fill row, is **not** re-polled.
- `_gap_reconcile` then replays up to 500 **closed** orders through the same `_handle_trade_update` (`trade_updates.py:693-732`, `:779-823`).

### A.5 Downstream consumers of the fills

- `economic_projection.py` computes FIFO P&L from the SQLite `fills` table. Superseded rows are excluded (`broker/alpaca/clerk/sqlite/economic_projection.py:81-110`, `:1005-1111`, `:570-575`).
- `clerk/fills.py` (`project_instance_fills` / `normalize_fill_event`) reads the pre-SQLite `OrderJournalEntry` journal. **No app caller remains** (a grep of `app/` finds references only inside `fills.py`). It is dead on the live path and is noted as P3 cleanup only.
- EXIT sizes the reducing order from `repo.position(...)`, the attributed position (`broker/alpaca/clerk/sqlite/exit_resolution.py:247`, `:279`, `:287`). It refreshes each linked entry by exact REST lookup through `fold_order_evidence` first (`exit_resolution.py:830-866`).
- `stream_health.execution_channel_health` gates submits on `connected` and `evidence_health` (`broker/alpaca/clerk/stream_health.py:70-130`).
- `recovery_reduction.py` prices recovery reductions. It reads no `trade_updates` state; no finding.

---

## (b) Invariants each side assumes of the other

| # | Assumed by | Invariant | Guaranteed? |
|---|---|---|---|
| I1 | Consumer, of Alpaca | The stream does not replay after a disconnect. | True, and handled: reconnect runs `reconcile_account` before `connected=True` (`trade_updates.py:491-495`). |
| I2 | Consumer, of Alpaca | A fill carries a unique `execution_id`, and a redelivery is byte-equivalent. | Not guaranteed by Alpaca. Handled fail-closed: a changed payload with the same exec id raises `EXECUTION_COVERAGE_CONFLICT` (`repository.py:578-599`). |
| I3 | Sink, of intake | An order's row exists before any broker event for it can arrive. | **Guaranteed.** `ENTER_ACCEPTED` inserts `orders` before submit, and submit runs outside intake (`folds.py:407-484`, `broker/alpaca/clerk/sqlite/enter.py:343-379`). Same for the EXIT reducing order (`folds.py:540-566`). |
| I4 | Clerk, of consumer | Every execution Alpaca performs reaches the clerk as an exact slice or a cumulative recovery. | **Not guaranteed.** A frame dropped in-stream is lost, and REST recovery only reaches *reconcilable* effects (gap **G1**, proven). |
| I5 | Submit gate, of consumer | `evidence_health=True` means no evidence has been lost since the last reconciliation. | **Not guaranteed.** Health is restored by the next mapped frame for *any* order, with no reconcile (`trade_updates.py:675-676`, docstring `:45-52`). |
| I6 | #2006 fix, of every observation route | Every route that observes a terminal order passes it through `_fold_enter_unfilled_if_proven`. The claim is in `order_evidence.py:421-423` and the commit message of `1abb3dbb`. | **False for the websocket route** (`trade_evidence.py:225-231`), and the sweep will not revisit the order (**G2**, proven). |
| I7 | Reconcile, of projections | A nonterminal ENTER whose `broker_state` is terminal, or `filled` with at least one fill, needs no fresh evidence. | Only true if every slice was received. Couples to I4. |
| I8 | Consumer's cross-path guard, of REST | The REST gap-replay's terminal fingerprint equals the websocket's for the same transition. | Not guaranteed: the websocket `timestamp` and REST `canceled_at` / `filled_at` can differ. It fails safe, because the duplicate ack is monotone and a no-op (P3). |
| I9 | Clerk, of Alpaca | No fill arrives after an order's `canceled`. | Alpaca's documented order lifecycle says so, but nothing checks it. If violated, the exact slice still folds into position (no effect-state guard), so position stays true (P3). |
| I10 | EXIT nesting, of the sink | The EXIT is the right owner for entry evidence while it is live. | Holds for position (same `subject_id`, `folds.py:905-930`) and for ack monotonicity. The `ENTER_UNFILLED` consequence is #2251 / #2282 territory. |

---

## (c) Named suspected gaps

Severity follows the map's scale (P0: custody or position belief differs from the broker's; P1: a gate fails open, or the operator sees false live state).

### G1 — Lost slice under a terminal frame: **P0, proven** (see (d) D1)

**Hypothesis.** A `partial_fill` frame that fails capture or mapping, followed by the order's terminal frame (`fill` or `canceled`), leaves the attributed position short by the lost quantity. `evidence_health` goes true again on the terminal frame. The effect drops out of `reconcilable_effect_operations`. The sweep raises `POSITION_DRIFT` but never re-derives the slice. The belief converges only when a later EXIT refreshes the entry over REST.

**Prototype.** Drive `TradeUpdatesConsumer` with an injected frame source and a `CaptureJournal` stub that returns `False` once. The sequence is:

1. `partial_fill` exec-A (2 shares): capture fails.
2. `fill` exec-B (3 shares, order `filled_qty=5`).

Then run `reconcile_account` with a fake read port that reports a broker position of 5. Assert:

- `position == 3`;
- the ENTER is not reconcilable;
- the verdict is `position_drift`;
- a second `reconcile_account` still reads 3.

**Paper account needed:** no. The dev fault seam (`fault_injection.FrameFaultKind`) can also drop frames on paper if wanted.

### G2 — Websocket-first cancel of an unfilled ENTER strands the ENTER `in_progress`: **P1, proven** (see (d) D2)

**Hypothesis.** A vendor `canceled` / `expired` / `rejected` with zero fills, observed first on the websocket, sets `broker_state=canceled`. It never folds `ENTER_UNFILLED`. The effect is then outside the reconcilable filter, and the open-order snapshot omits closed orders, so nothing ever terminalizes it. The instance stays in `strategy_instances_with_live_custody` and `market_data_symbols`, even after retirement (`reads.py:128-172`, `reads.py:175-201`). This is the #2006 residue on its dominant route; only the REST route was fixed.

**Prototype.** Feed `new` and then `canceled` frames through a real `SqliteTradeUpdateEvidenceSink`, then run one `reconcile_account` against a fake broker with no open orders. Assert that the effect state is `failed`; today it stays `in_progress`. Repeat with a gap-replay (`from_gap_reconcile=True`) to show that the closed-order replay does not rescue it either (`trade_evidence.py:225`).

**Paper account needed:** no. A paper confirmation during extended hours is optional: at 20:00 ET cancels are daily.

### G3 — Cumulative-then-disjoint-exact deadlock after a disconnect: **P2 provisional**

**Hypothesis.**

1. Exec-A (3 shares) happens during a websocket disconnect.
2. On reconnect, `reconcile_account` records cumulative recovery of 3 for the order.
3. Exec-B (7 shares) then arrives live.
4. `prove_execution_coverage_set` refuses on quantity (7 ≠ 3) (`broker/alpaca/clerk/sqlite/execution_coverage.py:306-311`). This raises `EXECUTION_COVERAGE_CONFLICT` with `allows_reduction=False` (`broker/alpaca/clerk/sqlite/exact_execution_evidence.py:133-165`) and quarantines B.
5. No later exact slice can make the exact set equal the cumulative set, so the conflict never self-resolves. The automated resolver is paper-only (`broker/alpaca/clerk/sqlite/historical_execution_recovery.py:1-8`). Meanwhile reduction (EXIT) is blocked on a correctly filled position.

**Prototype.** Build an in-process SQLite repo:

1. `fold_order_evidence` with a partial cumulative of 3;
2. `record_lifecycle_event(fill, exec-B, 7)`;
3. assert the conflict is active and `decide_capability(REDUCTION)` is refused;
4. run a second REST cumulative of 10, and assert the conflict remains.

**Paper account needed:** no for the logic. Yes to confirm that Alpaca really delivers a disjoint later slice after a reconnect (RTH, paper conditions).

### G4 — Websocket lag behind the 15 s sweep splits one execution burst into a transient reduction block: **P2 provisional**

**Hypothesis.** The sweep records a cumulative delta covering exec-A and exec-B before their websocket slices arrive. Exec-A alone mismatches the cumulative total, so a conflict is raised and reduction is blocked until exec-B arrives. If exec-B is lost (G1), the block is permanent.

**Prototype.** Same harness as G3, with two exact slices summing to one cumulative row, delivered with a gap. Assert the block during the window and its automatic resolution after exec-B. Then drop exec-B and assert that it persists.

**Paper account needed:** no.

### G5 — Boot-to-first-subscribe window has no gap-reconcile: **P2 provisional**

**Hypothesis.** Events between the boot reconciliation's final snapshot (`main.py:616`) and the first `listen` (`main.py:671-676`) are never delivered. The first cycle skips `_gap_reconcile` (`trade_updates.py:425`), yet `connected=True` opens admission at once. The position is stale until the next sweep (≤15 s). If a later frame for the same order arrives on the new socket and is terminal, G1 applies and the stale state is permanent.

**Prototype.** Harness with a fake broker whose REST state advances (a partial fill) between `recover()` and the consumer's first frame. Deliver only the final `fill` frame on the socket. Assert the position after one sweep.

**Paper account needed:** no.

### G6 — Deterministic sink failure in the gap-replay wedges the execution channel: **P2 provisional**

**Hypothesis.** `_gap_reconcile` replays closed orders with no per-order guard (`trade_updates.py:727-732`). If one of the last 500 closed orders deterministically raises in `record_lifecycle_event`, every reconnect fails before `_mark_connection(True)`. Examples:

- `RuntimeError("... has no owning effect operation")` at `trade_evidence.py:187`;
- an `AssertionError` from `fold_order_evidence` (`order_evidence.py:149-152`).

The channel then reconnects forever at the 30 s ceiling. This fails closed, but only an operator notices.

**Prototype.** Stub the sink to raise for one order id in the closed list. Assert that `connected` never becomes true after N cycles and that the counters show reconnects with zero `connects`.

**Paper account needed:** no.

### G7 — Evidence health does not clear after a successful reconcile: **P3**

**Hypothesis.** After one unusable frame on a quiet account, `evidence_health` stays false until the next live trade update. A successful reconnect and `reconcile_gap` do not reset it (`trade_updates.py:491-496`). Admission stays closed on a proven-clean account for hours. This is liveness, not safety.

**Prototype.** Deliver one bad frame, force a reconnect with a clean reconcile, and assert the execution channel is still unhealthy.

**Paper account needed:** no.

### G8 — Unbounded in-memory dedup maps: **P3**

**Hypothesis.** `_seen` and `_terminal_orders` grow for the life of the process (`trade_updates.py:317-322`, `:679-689`). A long-running clerk leaks memory. Durable dedup in `append_execution_slice_if_absent` makes pruning safe.

**Prototype.** Count map size after N synthetic events.

**Paper account needed:** no.

### G9 — Late fill on a terminal EXIT's reducing order: **P3**

**Hypothesis.** The EXIT's reducing order is proven `canceled`, which raises `EXIT_NOT_FLAT` and marks the effect `failed`. A late exact slice then arrives. It folds into position under the failed EXIT (`trade_evidence.py:183-185`; the slice fold ignores effect state). The `EXIT_NOT_FLAT` uncertainty keeps saying "not flat" until the next clean sweep resolves the flat-exit fences (`reconcile.py:904-909`). This is self-correcting within about 15 s.

**Prototype.** In-process: fold a canceled reducing order, then append an exact slice, then run a sweep. Assert the uncertainty is resolved.

**Paper account needed:** no. Alpaca may never emit this.

Not a gap (checked):

- **Out-of-order `partial_fill` after `fill` / `canceled`.** The slice still folds (exec-id dedup) and the ack does not regress (`folds.py:594-597`).
- **Submit response after the websocket ack.** The monotone ack guard prevents regression (`folds.py:624-633`).
- **Events for an order intake has not finished recording.** Not reachable (I3).
- **Process restart losing in-memory dedup.** Durable dedup on `execution_id` covers it (`repository.py:567-604`).
- **Partial fill then cancel on the websocket.** The ENTER correctly stays open with its exposure (`order_evidence.py:464-467`).

---

## (d) Defects proven by reading

The static trace carries each proof. Each was also confirmed by a throwaway in-process probe: a real `SqliteTradeUpdateEvidenceSink` on a temp SQLite repo, reusing the fixtures in `tests/broker/alpaca/clerk/test_trade_evidence.py`. The probe was not committed.

### D1 (G1) — a dropped fill frame is never re-derived: **P0**

- The drop happens at `trade_updates.py:517-524` (capture) and `:526-539` / `:584-592` (parse or map). The socket continues and no reconcile is scheduled.
- Health reopens at `trade_updates.py:675-676` on the next mapped frame.
- The terminal ack sets `broker_state=filled` (`folds.py:624-640`). With at least one fill row, the ENTER then fails every branch of the reconcilable filter (`reads.py:603-612`).
- The sweep's open-order snapshot omits the closed order (`reconcile.py:532`), so only `POSITION_DRIFT` fires (`reconcile.py:372-432`). Its `next_step` ("Reconcile now") cannot converge.
- An EXIT's entry refresh (`exit_resolution.py:830-866`) is the only automatic repair.
- **Probe result:** a single `fill` frame with exec-B for 3 shares, on an order reporting `filled_qty=5`, gave `position=3.0`, `effect.state=in_progress`, `reconcilable_effect_operations()==[]`.

Filed as a bug; see the resolution comment on #2281.

### D2 (G2) — a websocket cancel of an unfilled ENTER bypasses the #2006 fix: **P1**

- The websocket sink calls only `fold_order_acknowledgement` for non-fill events (`trade_evidence.py:225-231`).
- `_fold_enter_unfilled_if_proven` is reachable only from `fold_order_evidence` (`order_evidence.py:204-212`).
- The ENTER then fails the reconcilable filter (`reads.py:603-608`) and the closed order is not in the open snapshot (`reconcile.py:532`). The effect stays `in_progress` indefinitely.
- Its instance stays in the live-custody and market-data sets (`reads.py:128-201`).
- The commit that fixed #2006 (`1abb3dbb`) names "a trade-update frame" as a covered route. Its tests exercise only `fold_order_evidence` (`tests/broker/alpaca/clerk/sqlite/test_unfilled_extended_orders.py:58-180`).
- **Probe result:** frames `new` then `canceled` with `filled_qty=0` gave `effect.state=in_progress`, `broker_state=canceled`, `reconcilable_effect_operations()==[]`.

Filed as a bug; see the resolution comment on #2281.

---

## Method and scope notes

- Static trace plus the two throwaway probes above. Nothing ran against Alpaca, IB Gateway, Polygon or any container.
- Out of scope: the #2251 EXIT-nesting fix itself (#2282), and D3 intake write points. I3 is only confirmed here as far as D4 depends on it.
