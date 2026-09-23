# D5 — After a Gateway blackout, can a bot decide on bars from before the gap as if nothing happened?

Research ticket [#2283](https://github.com/tim1016/learn-ai/issues/2283), part of map [#2276](https://github.com/tim1016/learn-ai/issues/2276).
Baseline: `a14f1df143b67aad672949b5922f9aef68e740aa` (master, 2026-09-23). Every `file:line` below is at that SHA; paths are under `PythonDataService/` unless noted.
Method: static trace. The only thing executed was a four-line probe of `_bars_expected_now` on the host venv (shown under G1). No live service, container, IB Gateway, Alpaca or Polygon was touched.

## Answer

**Inside one run that carries a continuity policy, no.** A blackout the broker signals (socket down, 1100 soft loss, reconnect generation change) or an RTH stall of 60 s or more enters ADR 0053's wait. The wait lifts only when the socket and the monitor are both `HEALTHY` again. It never lifts on a timer; the only timer is a deadline that *kills* the run. After the wait, every minute the interruption touched has to prove itself by a 12-contribution count, and every minute it swallowed is scanned. Inside the decision session such a minute is refused (`FEED_DEATH`). Outside it, it is journalled as a `gap`. The nightly Gateway logout is on this path: it surfaces as `socket_down` or `soft_loss_1100`. If the Gateway is not back before the next decision trigger plus 20 s, the run dies with `DECISION_BAR_MISSED`.

**"Decision quarantine" (`bot_decision_quarantine.py`) is not a blackout mechanism.** It records `SignalSession.advance` refusals: width, monotonicity, unsettled stage. It trips on a gap only by accident, when the gap cuts off the *tail* of a bucket.

**Across the seam, yes, in three proven ways and one strongly suspected way:**

1. **Extended-session runs in PRE/POST (G1, proven).** An extended-session run has no stall detection in PRE/POST. A silent line there is never an interruption, and when prints resume the hole is an ordinary gap.
2. **Resume after a refusal (G2, proven).** A run Resumed after a continuity refusal warms up on the instance's retained pre-gap bars. It then decides live across the very gap its predecessor refused, and nothing checks the join.
3. **Short silences and the no-policy population (G4/G5).** An RTH silence shorter than 60 s with no connectivity code, and every run with no policy, deliver short or missing minutes silently.
4. **Stale tail bucket (G3, suspected).** The retained tail's unfired working bucket is evaluated in `DECIDE` mode on the first live bar, possibly hours later.

## (a) Trace across the boundary

### A1. The broker link: signals and the recovery state machine

- **IBKR error codes set client flags.** `app/broker/ibkr/client.py:310-409`:
  - 1100/1300/2110/504 set `_connection_lost=True` (`client.py:370-393`).
  - 1101/1102 clear it (`client.py:394-409`), and 1101 also sets `_subscriptions_stale=True` (`client.py:401-402`).
  - Data-farm 2103/2105 (and 2104/2106) only toggle `_data_farm_degraded` (`client.py:336-369`). They do **not** mark the connection lost.
  - Every successful `connect()` increments `connection_generation` (`client.py:547`).
- **The state machine is a pure table.** `app/broker/ibkr/recovery_state_machine.py:46-72`. `recovery_state_from_connection_state` maps `degraded_data_farm` to `HEALTHY` (`recovery_state_machine.py:79-80`).
- **The `AutoReconnectMonitor` loop drives the table** (`app/broker/ibkr/auto_reconnect_monitor.py:279-309`):
  - A healthy-looking socket runs `_recover_subscriptions_if_stale` and then advances `restored_data_maintained` → `HEALTHY` (`:296-303`).
  - 1100 waits for IBKR's own restore, then forces a reconnect (`:311-333`).
  - A down socket enters the backoff ladder and, once that is spent, `HARD_DOWN` with a 60 s slow probe (`:489-522`, `:635-674`).
  - `HEALTHY` is reached only after `recovery_callbacks` succeed (`:577-591`, `:593-615`). The only production callback is `LIVE_BAR_AGGREGATOR.resubscribe_all` (`app/main.py:777-783`), which restarts *chart* streams, not bot streams (`app/services/live_bar_aggregator.py:233-275`).

### A2. The 5-second line: liveness gate, lease and registry

- `_iter_leased_raw_bars` (`app/broker/ibkr/bars.py:671-814`) runs `_check_realtime_subscription_liveness` on every iteration (`bars.py:771-793`). The check (`bars.py:512-556`) raises in this order:
  1. `IBKRBarInterrupted(generation_changed)` when the lease's generation is stale.
  2. `socket_down` when `not is_connected()`.
  3. `soft_loss_1100` when `connection_lost`.
  4. `IBKRBarSubscriptionStalled` when the lease was invalidated by another consumer, or when there has been no source progress for `REALTIME_BAR_STALL_TIMEOUT_S = 60.0` (`bars.py:59`), **but only while `_bars_expected_now(use_rth)`** (`bars.py:548-555`).
- Before re-raising, it drains bars still queued on the orphaned list (ruling P10, `bars.py:782-793`).
- `_bars_expected_now(use_rth=False)` returns `phase in {PRE, RTH, POST, OVERNIGHT}` (`bars.py:504-509`). The phase comes from `_session_phase_for_ms` → `session_state_at_ms(now_ms=ts)` with no capability and no window (`app/broker/ibkr/minute_assembler.py:104-106`). That falls through to `_session_from_nyse_calendar` (`app/services/session_authority.py:157-161`), which can only return `RTH` or `CLOSED` (`session_authority.py:211-251`). See G1.
- The registry key includes the generation. Older generations are evicted without a cancel (`bars.py:222-349`). A same-generation re-acquire multiplexes onto the existing list (`bars.py:249-261`).

### A3. The minute: `MinuteAssembler`

- Contributions are keyed by source timestamp and tagged with a generation (`minute_assembler.py:120-180`, `:279-360`). A minute emits only when the first 5-second bar of a *later* minute arrives (`minute_assembler.py:339-360`), so the minute that is open when a blackout starts is held.
- `flush_if_complete` emits the open minute early only at 12/12 (`minute_assembler.py:442-449`). Afterwards, `_absorb_after_flush` refuses to rebuild it (`minute_assembler.py:389-421`).

### A4. The feed port: policy path vs. legacy path

- `IbkrMarketDataFeed.stream_bars` picks the legacy path when `continuity is None` or the kill switch is off. Otherwise it takes the policy path (`app/marketdata/ibkr_feed.py:154-161`, `:181-194`).
- **Legacy path** (`ibkr_feed.py:196-244`). A stall is replaced silently with a **fresh `MinuteAssembler` per attempt** (`:219`). Any other interruption becomes `MarketDataFeedError`, so the run ends `FEED_DEATH`.
- **Policy path** (`ibkr_feed.py:246-298`). One `ContinuityLoop` owns the assembler across resubscribes (`app/marketdata/ibkr_continuity.py:131-155`).
  - **On `IBKRBarInterrupted`/`IBKRBarSubscriptionStalled`**, `open_interruption` runs these steps in order (P6, `ibkr_continuity.py:197-226`):
    1. Flush a 12/12 open minute.
    2. Anchor `deadline_ms = next_trigger_ms(last_delivered_end_ms) + 20_000` (`app/marketdata/feed.py:273-275`).
    3. Record the touched open minute.
    4. Record `interruption`.
    5. Yield the held bar.
  - **`await_recovery`** then polls `_healthy` every 0.25 s (`ibkr_continuity.py:228-254`, `:328-369`). `_healthy` means connected, not `connection_lost`, and the monitor in `HEALTHY` (`ibkr_continuity.py:96-100`). It checks the deadline *before* health and raises `DECISION_BAR_MISSED` when the deadline has passed (`:345-366`).
  - **`NotConnectedError`** after a recovery counts as a second episode under the same deadline (`ibkr_continuity.py:256-272`).
  - **After recovery**, the first source bar marks the landing minute (`ibkr_continuity.py:274-291`). The first emitted bar runs the missed-window scan (`:168-170`, `:432-446`). Touched or spanning minutes must hold 12 contributions (`:108-128`). Unresolvable windows become a `gap` outside the decision session or a refusal inside it (`:448-507`). The substitution grant always refuses (`app/services/feed_continuity_policy.py:104-109`).

### A5. The bot boundary: retained feed, admission and strategy

- **`continuity_policy_for`** returns `None` for an unsealed binding or one with no decision timeframe (`app/services/feed_continuity_policy.py:112-127`). Otherwise the policy carries the run's `RunDecisionSession`, its clock, the always-refuse grant and the ledger sink (`:129-139`).
- **Wiring.** Trade mode (`app/services/bot_trade_strategy.py:812-829`) and dry run (`:1146-1149`) wrap the shared feed in `_RetainedSourceBarFeed`. It always streams the source with `use_rth=False` (`:271`).
  - Each bar goes through `admit_on_delivery` (`:272`, body at `feed_continuity_policy.py:64-101`). That refuses `DECISION_LATE` only for **non-`realtime`** trigger bars.
  - The bar is then appended to the ledger and filtered by the session (`:273-282`).
- **Log-only runs get no policy** (`app/services/bot_runtime.py:156`).
- **Warmup.** `replay_warmup_bars` calls `feed.recent_closed_bars` (`app/services/bot_trade_strategy_warmup.py:144-172`). On a retained feed that returns **every retained bar for the provider/symbol** whenever the ledger is non-empty (`bot_trade_strategy.py:292-319`). It filters neither by `lookback_days` nor by `run_id`. The ledger is **instance-scoped**, not run-scoped (`app/services/bot_binding_authority.py:125-132`; `app/broker/alpaca/clerk/account_authority.py:250-265`; `SourceBarLedger.bars`, `app/services/source_bar_ledger.py:516-523`). A Resume mints a new `run_id` on the same `strategy_instance_id` (`app/services/bot_start_admission.py:388-410`).
- **Live loop** (`bot_trade_strategy.py:577-596`). `replay_closed_bar` feeds the consolidator (`:146-157`, `:491-496`). Warmup does **not** force-flush the session-close bucket; only the live loop does (`:588-596`).
- **The consolidator fires the working bucket lazily**, on the first input of a later period (`app/engine/consolidators/trade_bar_consolidator.py:84-133`). The fired bucket's `end_ms` is the last *input's* `end_ms` (`:130`, `:141-143`).
- **`SignalSession.advance`** refuses only `UNSETTLED_STAGE`, `TIMEFRAME_MISMATCH` (`end_ms - start_ms != timeframe_ms`) and `NON_MONOTONIC_DECISION_CLOCK` (`app/engine/strategy/signal_program.py:187-199`). `QuarantineJournal` counts and receipts those (`app/services/bot_decision_quarantine.py:75-169`).
- **Gates after a decision.**
  - ENTER re-reads live market liveness (`bot_trade_strategy.py:874-906`). For extended phases it also requires `feed.health()` to be live (`:360-417`).
  - The clerk's `StreamHealthGate` pauses submission while market data is stale (`app/broker/alpaca/clerk/stream_health.py:193-240`). `feed.health()` goes stale after 30 s without a source bar (`ibkr_feed.py:382-438`).
  - EXIT is exempt from the liveness gate (`bot_trade_strategy.py:874-879`).
- **Run death.** `MarketDataFeedError` → `finalize_crash(reason_code="FEED_DEATH")` (`app/services/bot_runner.py:1795-1805`).

### A6. Charted-fixed context: #2256

`MARKET_CLOCK_INVALID` fired on a healthy feed because the panel captured its evaluation instant before its awaits (fix `4b931272`). The live checks are `app/services/market_liveness.py:87-125` and `:188-207`. This sits on the ENTER-liveness side (A5), not in the continuity wait. Nothing in the continuity loop reads the market clock, so the race cannot hold or release a blackout wait. It can only have refused an ENTER on a healthy feed. D5 finds nothing that re-opens it.

## (b) Invariants each side assumes of the other

| # | Assumer → provider | Invariant assumed | Guaranteed? |
|---|---|---|---|
| I1 | Continuity loop → bars.py liveness gate | Every loss of delivery surfaces as `IBKRBarInterrupted` or `IBKRBarSubscriptionStalled` inside the consumer's decision session | **No.** The stall arm is gated by `_bars_expected_now`, which is calendar-RTH-only (G1). A silence under 60 s is never an interruption (G4). Data-farm codes never raise (`client.py:336-369`). |
| I2 | Continuity loop → monitor | `recovery_state == "HEALTHY"` means the resubscribed line will deliver | **Mostly.** `HEALTHY` follows callbacks that restart chart streams only. The loop's own lease was released on interruption, so its re-acquire is a fresh `reqRealTimeBars` unless another consumer still holds a same-generation lease (G8). The throttle can report `HEALTHY` with `subscriptions_stale` set (G6). |
| I3 | Continuity loop → assembler | A touched minute is identifiable by the loop (open/landing) or by generations | Yes for signalled interruptions (`ibkr_continuity.py:76-78`, `:221-223`, `:274-291`). No for unsignalled silence: nothing marks the minute touched (G1/G4). |
| I4 | Deadline → decision clock | `next_trigger_ms` is the consumer's next decision instant | Yes. It is calendar-backed and rolls over weekends and holidays (`app/services/decision_clock.py:198-241`), and the deadline is anchored once and never re-derived (`ibkr_continuity.py:328-344`). |
| I5 | Bot → feed | A bar with `provenance == "realtime"` is complete and on time, so `admit_on_delivery` skips it | **No.** A short minute from an unsignalled silence (G4) or a legacy-path replacement (G5) is tagged `realtime` (`ibkr_feed.py:450-472`). |
| I6 | Strategy warmup → retained ledger | Retained bars plus live bars form one contiguous series | **No.** No join check exists anywhere between the last retained bar and the first live bar (G2). The ledger is instance-scoped across runs. |
| I7 | `SignalSession.advance` → consolidator | A full-width bucket was built from contiguous data and is current | **No.** Width equals the timeframe whenever the bucket's last minute is present, whatever is missing before it (`trade_bar_consolidator.py:92-133`). Nothing bounds the bucket's age against wall clock (ADR 0053 §2 leaves the universal timeliness check out of scope). See G3. |
| I8 | ADR 0053 Consequences → code | "The nightly 1100 reset and the 00:45 gateway logoff … become recorded gaps rather than kills" | Yes for RTH runs and for extended runs whose window excludes the outage, **if** the Gateway is restored before the next trigger plus 20 s. Otherwise the run is killed `DECISION_BAR_MISSED`, which is fail-closed and correct. |
| I9 | ADR 0053 §6 → code | "A stall (`IBKRBarSubscriptionStalled`) enters the same choreography" | Only in calendar RTH (G1). |

## (c) Named suspected gaps

Severity follows the map's scale. **Needs paper?** says whether the prototype needs the Alpaca paper account; every one below can run in-process.

**G1 — Silent extended-hours line is never an interruption.** *(proven, P1, bug [#2313](https://github.com/tim1016/learn-ai/issues/2313))*
- **Hypothesis.** An extended-session run (`use_rth=False`, declared window) whose 5-second line goes silent in PRE or POST with the socket up and no 1100 is never interrupted. Examples: a data-farm 2103 break, or a line IBKR stopped feeding. The run records no `interruption` and never evaluates its deadline. If prints resume on the same line, the minutes in between are an ordinary, unjournalled gap. The minute open when the line went silent is delivered short as `realtime`, and the strategy decides across the hole. If prints never resume, the stall fires only 60 s after the *RTH* open, long past the deadline.
- **Evidence.** `bars.py:504-509`, `:548-555`; `minute_assembler.py:104-106`; `session_authority.py:157-161`, `:211-251`.
- **Regression.** Introduced by `be1e3f87` (2026-08-07), which replaced a clock-based PRE/POST/OVERNIGHT classifier with the calendar-only authority. ADR 0053 (2026-09-02) and extended decision sessions (ADR 0059 D5.2) were then built assuming stalls are detected.
- **Probe on the host venv:** 07:30 ET Tue → `CLOSED`, `expected(use_rth=False)=False`; 09:45 → `RTH`, `True`; 17:00 → `CLOSED`, `False`. The only test, `tests/broker/ibkr/test_bars.py:96-106`, covers a weekend.
- **Design tension to settle in the fix.** ADR 0053 §3 says sparse prints are normal outside RTH, so re-arming a 60 s timer in PRE/POST would turn thin-symbol quiet minutes into refusals.
- **Prototype.** A fake `IbkrClient` whose real-time list stops appending at 07:30 ET and resumes at 07:40 under an extended `RunDecisionSession`. Assert that no `interruption`/`gap`/`refused` event is recorded, that the 07:30 minute is delivered `realtime` with fewer than 12 contributions, and that the next bar reaching the session is 07:40.
- **Needs paper:** no.

**G2 — Resume decides across the gap its predecessor refused.** *(proven, P1, bug [#2314](https://github.com/tim1016/learn-ai/issues/2314))*
- **Hypothesis.** A run dies `DECISION_BAR_MISSED` or `SUBSTITUTION_NOT_AUTHORIZED` at 10:07 ET because the continuity floor refused a hole. The operator Resumes at 13:00. The new run warms up on the instance's retained bars through 10:07, then consumes live bars from 13:00. The new `ContinuityLoop` has no interruption open (`ibkr_continuity.py:168-170`), so the 10:07–13:00 hole is an ordinary gap. No event is recorded, no refusal is raised, and nothing tells the operator. Indicators decide on a series with a 3-hour RTH hole: the exact input ADR 0053 exists to refuse. The same holds for boot recovery of a run after a process crash.
- **Evidence.** `bot_trade_strategy.py:292-319` (retained bars win, lookback and run ignored); `bot_binding_authority.py:125-132` (instance-scoped ledger); `bot_start_admission.py:388-410` (new run, same instance); `bot_trade_strategy_warmup.py:144-172`; `bot_trade_strategy.py:577-581`. No continuity term appears in `bot_resume_admission.py`, `bot_start_admission.py` or `run_admission.py`.
- **Prototype.** Seed an instance ledger with RTH bars to 10:07 plus a `refused` event. Start a new run with a fake feed whose live stream begins at 13:00. Assert the strategy consumes 10:07 then 13:00 with no continuity event, and that an EMA value differs from the contiguous reference.
- **Needs paper:** no.

**G3 — The retained tail's working bucket is decided live, late.** *(suspected, provisional P0)*
- **Hypothesis.** Warmup never force-flushes the session-close bucket (`bot_trade_strategy.py:588-596` runs only in the live loop). The consolidator fires lazily (`trade_bar_consolidator.py:96-108`). So when the retained tail ends on a bucket's last minute, the first live bar of the next run fires that pre-gap bucket. Two cases:
  - **Every normal next-day Resume:** yesterday's 15:45–16:00 bucket.
  - **After a blackout, 1/N of the time:** the bucket is full-width.

  The bucket passes `advance` (`signal_program.py:195-199`) and is evaluated in `DECIDE` mode, with `decision_bar_close_ms` hours or a day in the past, and flows to the clerk. `admit_on_delivery` never sees it, because the triggering minute is `realtime`. A tail-truncated bucket is instead quarantined `TIMEFRAME_MISMATCH`: the one place decision quarantine incidentally trips on a gap.
- **Unproven.** Whether clerk intake dedupes it against the predecessor's captured evaluation of the same bucket, or submits it.
- **Prototype.** A registered 15-minute program with a retained ledger ending at 16:00. Warmup, then one live 09:30 bar. Assert a `StrategyEvaluation` whose `decision_bar_close_ms` is the prior close. Then drive it through an in-process clerk on a temp volume and see whether an order intent is admitted.
- **Needs paper:** no. Overlaps D2 (#2276's warmup seam): coordinate with that ticket.

**G4 — A silence shorter than the stall timeout is silent in RTH.** *(suspected, P2)*
- **Hypothesis.** A data-farm 2103/2104 blip, or any unsignalled silence of 5–59 s inside RTH, raises nothing (`bars.py:548-555`; `client.py:336-369`). The affected minute emits with fewer than 12 contributions, untouched, as `realtime` (`ibkr_continuity.py:108-128`), and the strategy decides on it. ADR 0053 §3's count rule is enforced only for signalled interruptions.
- **Prototype.** A fake line in RTH omits five consecutive 5-second bars with no error codes. Assert the minute is delivered `realtime` with `contribution_count=7` and no continuity event.
- **Needs paper:** no.

**G5 — Runs with no policy keep the damaged minute.** *(charted hazard, P2)*
- **Affected runs:** log-only runs (`bot_runtime.py:156`), unsealed or no-timeframe bindings (`feed_continuity_policy.py:123-127`), and any run while `IBKR_FEED_CONTINUITY_ENABLED=false` (`ibkr_feed.py:181-194`).
- **Hypothesis.** A stall replacement rebuilds the current minute from post-replacement prints only (`ibkr_feed.py:219`). Swallowed minutes vanish silently. Both are delivered `realtime`. ADR 0053's Consequences section acknowledges this.
- **Prototype.** The kill switch off, one stall mid-minute. Assert a short `realtime` minute and no events.
- **Needs paper:** no.

**G6 — The monitor claims HEALTHY while subscriptions are stale.** *(suspected, P3)*
- **Hypothesis.** A second 1101 within `subscription_recovery_interval_s` (10 s, `auto_reconnect_monitor.py:112`) makes `_recover_subscriptions_if_stale` return `False` on throttle (`:347-351`). The tick then advances `restored_data_maintained` → `HEALTHY` (`:299-300`) with `subscriptions_stale` still set, for up to 10 s. Continuity is unaffected, because its lease was released and its re-acquire is fresh. Health and the cockpit misreport.
- **Prototype.** Drive the monitor with two 1101s 3 s apart. Assert `recovery_state == "HEALTHY"` while `client.subscriptions_stale` is `True`.
- **Needs paper:** no.

**G7 — Retained warmup ignores lookback, and extended bars are labelled CLOSED.** *(proven, P3)*
- `recent_closed_bars` returns the whole instance ledger regardless of `lookback_days` (`bot_trade_strategy.py:292-319`).
- `_session_phase_for_ms` labels PRE/POST minutes `CLOSED` (`minute_assembler.py:104-106`, `:176`). These labels are stored in the ledger and in replay proofs (`app/services/run_replay_proof.py:98`, `:190`). RTH filtering, which trusts the label, still behaves correctly. Extended filtering uses bounds (`app/services/decision_session.py:82-85`).
- **Prototype.** Read back an extended run's ledger and assert that PRE rows are `CLOSED`.
- **Needs paper:** no.

**G8 — A lease held across 1100→1101 is not invalidated.** *(suspected, P2)*
- **Hypothesis.** The liveness gate never reads `subscriptions_stale`, and 1101 keeps the generation (`bars.py:512-556`). A consumer suspended at a `yield` across a whole 1100→1101 episode keeps its same-generation lease, for example during the event-loop stalls behind #1921. Then:
  - That consumer resumes on a list IBKR no longer feeds.
  - Any consumer re-acquiring during that window is multiplexed onto the same dead list (`bars.py:249-261`).
  - In RTH the result is a 60 s stall and probably `DECISION_BAR_MISSED`. In PRE/POST, combined with G1, the line stays silent indefinitely.
- **Prototype.** Two consumers of one symbol. Suspend one at a yield, emit 1100 then 1101 on the fake client, then re-acquire with the other. Assert both read the same list and nothing appends.
- **Needs paper:** no.

## (d) Defects proven by reading alone

- **G1 (P1).** Stall detection is disabled outside calendar RTH, so the ADR 0053 choreography never starts for a silent extended-session line in PRE/POST. Filed as #2313.
- **G2 (P1).** A new run of an instance warms on retained pre-gap bars and decides across the gap with no join check, bypassing the continuity refusal that ended its predecessor. Filed as #2314.
- **G7 (P3).** The retained warmup ignores `lookback_days`, and extended minutes are labelled `CLOSED`. Listed only.

Seam status proposal for the map: **charted hazard.** G1 (#2313) and G2 (#2314) are open bugs; G3, G4 and G8 go to prototype.
