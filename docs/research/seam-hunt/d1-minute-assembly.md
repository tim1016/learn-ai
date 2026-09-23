# D1: IBKR 5-second bars into minute assembly

**Seam hunt:** tim1016/learn-ai#2276. **Ticket:** #2278.
**Baseline:** every `file:line` below is at `a14f1df1` (master, 2026-09-23). Paths are relative to `PythonDataService/`.
**Method:** static trace only. Code is the authority; docstrings and ADRs are treated as claims to check. The only things run were the existing unit suites (`tests/broker/ibkr/test_bars.py`, `test_minute_assembler.py`, `tests/test_live_bar_aggregator.py`, `tests/marketdata/test_feed_continuity.py`: 106 passed) and one in-process call to `_session_phase_for_ms`, quoted in P-1. No Gateway, container, Alpaca or Polygon was touched.

## Answer in one paragraph

Inside the open minute, the assembler keeps the promises `temporal-rigor.md` makes for live subscriptions:

- an exact redelivery of the last bar is skipped and counted;
- a same-timestamp correction replaces its stored contribution, and the minute's OHLCV is recomputed from parts;
- anything older than the last accepted timestamp is fatal, whether it belongs to an emitted minute, a minute flushed early, or the open minute itself.

A reconnect does not reset `last_accepted` on the money path, because the assembler outlives each resubscribe. The weak points sit on either side of that core.

**First weak point: the liveness watchdog is off outside RTH.** It decides whether 5-second bars "should be arriving" by asking a session classifier. Since `be1e3f87`, that classifier can only answer RTH or CLOSED. On the `use_rth=False` line that every trade run streams on, the stall watchdog therefore cannot fire in PRE or POST (**proven, P1**).

**Second weak point: when a minute is emitted.** A minute is emitted only when the first bar of a later minute arrives. On the steady-state path nothing bounds that delay. The consumer's lateness gate exempts `provenance="realtime"` bars on the claim that they are "never late by construction". So a minute stranded behind a silent line, or at the 20:00 extended close, can be decided on arbitrarily late. That is suspected, not proven, because it depends on IBKR's delivery behaviour. When the assembler is fatal, the run ends as `FEED_DEATH` with nothing flattened.

## (a) Trace across the boundary

### Producer: IBKR 5-second bars to `MinuteAssembler`

1. **Subscribe.** `_iter_leased_raw_bars` qualifies the contract and leases a shared `reqRealTimeBars(5, "TRADES", useRTH)` line from `_RealtimeBarSubscriptionRegistry` (`app/broker/ibkr/bars.py:699-707`). The line's key includes `(client id, connection generation, conId, bar_size, whatToShow, use_rth)` (`bars.py:145-153`, `bars.py:245-247`).
   - A consumer that joins an existing line starts at the list's current tail (`bars.py:249-262`).
   - A resuming consumer walks back to its watermark (`_resume_index`, `bars.py:653-668`).
2. **Liveness check on every iteration.** `_check_realtime_subscription_liveness` (`bars.py:512-556`) runs before `bars` is touched (`bars.py:773-781`).
   - It raises `IBKRBarInterrupted` for a generation change, socket down, or code 1100.
   - It raises `IBKRBarSubscriptionStalled` when the line was invalidated, or when no source timestamp has advanced for `stall_timeout_s = 60` seconds (`bars.py:59`) *while bars are expected*.
   - "Expected" is `_bars_expected_now(use_rth)` (`bars.py:504-509`). It resets the timer whenever bars are not expected (`bars.py:548-549`).
3. **Drain on interruption (ruling P10).** Queued pre-disconnect bars are yielded before the interruption is re-raised (`bars.py:782-793`).
4. **Watermark.** `_observe` advances `last_source_ms` and calls `on_source_bar` only when the source timestamp strictly increases (`bars.py:756-762`).
5. **Assembly.** `stream_minute_bars` passes each raw bar to the caller-owned `MinuteAssembler.feed` (`bars.py:916-925`, `app/broker/ibkr/minute_assembler.py:423-440`). `feed` always uses `policy="live_idempotent"` (`minute_assembler.py:433`).
6. **`aggregate_realtime_bar`** (`minute_assembler.py:279-360`):
   - `source_ms == last_source_ms` goes to `_handle_duplicate` (`:307-316`):
     - an exact payload is skipped and `skipped_duplicate` is incremented (`:252-266`);
     - a different payload replaces the stored contribution, and `applied_correction` is incremented (`:268-276`);
     - a duplicate that is not in the open minute is fatal (`:244-249`).
   - `source_ms < last_source_ms` is fatal, "Non-monotonic" (`:317-320`).
   - A bar in the same minute is stored under its source timestamp (`:339-342`).
   - A bar in a later minute emits the open minute and starts a new one (`:347-360`).
   - A minute regression is fatal (`:344-345`).
7. **Minute boundary.** `_minute_start_ms(ts) = ts - ts % 60_000` (`minute_assembler.py:100-101`). The 5-second bar is filed under its own timestamp, which IBKR documents as the bar's *start*. The `:55` bar therefore lands in the right minute.
8. **Emitted model.** `to_model` (`minute_assembler.py:163-180`) always sets `end_ms = start_ms + 60_000`, recomputes OHLCV from the stored parts, and sets:
   - `contribution_count = len(contributions)`;
   - `spans_interruption = len(generations) > 1`;
   - `session_phase = _session_phase_for_ms(start_ms)`.
9. **Early flush.** `flush_if_complete` (`minute_assembler.py:442-449`) emits the open minute once it holds 12 contributions (`RTH_CONTRIBUTIONS_PER_MINUTE`, `:56`). It remembers that minute in `_flushed`. After that, `_absorb_after_flush` (`:389-421`) absorbs only an exact redelivery of the last bar and refuses every other bar of the flushed minute.

### Consumer 1: the money path (`IbkrMarketDataFeed` with `ContinuityLoop`)

- **Trade runs always stream `use_rth=False`.** They filter to their own decision session locally (`app/services/bot_trade_strategy.py:271-274`).
- **One assembler per run.** With a continuity policy, `ContinuityLoop` holds a single `MinuteAssembler` for the whole `stream_bars` call (`app/marketdata/ibkr_continuity.py:147-149`). Each resubscribe reuses it (`app/marketdata/ibkr_feed.py:273-283`), so `last_source_ms` survives a reconnect.
- **On interruption** (`ibkr_feed.py:289-294`), the loop:
  1. flushes the open minute if it is complete;
  2. anchors the deadline;
  3. records the interruption;
  4. yields any held bar;
  5. waits for recovery (`ibkr_continuity.py:197-226`, `:328-369`).
- **Emitted minutes that an interruption touched.** They are checked by count alone (`_is_unresolvable`, `ibkr_continuity.py:108-128`). A short one is recorded as a `gap` outside the decision session. Inside it, the run is refused and ends (`ibkr_continuity.py:448-507`).
- **Any other `IBKRBarStreamError`**, including every fatal assembler error, becomes `MarketDataFeedError` (`ibkr_feed.py:297-298`).
- **Legacy path** (no policy, or the kill switch): a fresh assembler per attempt, and a stall is replaced transparently (`ibkr_feed.py:196-244`).
- **Port translation.** `_translate` drops `contribution_count` and maps `spans_interruption` to `realtime_across_reconnect` (`ibkr_feed.py:451-472`).
- **Lateness gate.** `admit_on_delivery` refuses a late trigger bar, but returns early for `provenance == "realtime"` (`app/services/feed_continuity_policy.py:77`).
- **Session-close flush.** The strategy force-flushes when a bar's `end_ms` equals the session close (`bot_trade_strategy.py:590`).
- **What a fatal error does.** `BotRunner._supervise` records `FEED_DEATH` through `finalize_crash` (`app/services/bot_runner.py:1795-1805`). It does not flatten or cancel. Any open position or working order is left to other machinery (seams D3 and D5).

### Consumer 2: the chart (`LiveBarAggregator`)

- **A fresh assembler per stream call.** The chart uses the default `use_rth=True` (`app/services/live_bar_aggregator.py:325`).
- **Partial-first-bar guard.** It drops a first bar whose `end_ms - start_ms != 60_000` (`live_bar_aggregator.py:366-387`).
- **Persistence.** Each bar is written to `BarPersistence` (`live_bar_aggregator.py:389`).
- **Failure handling.** Any exception sets `status = "errored"`, and the task exits (`live_bar_aggregator.py:403-408`).
- **Not on the money path.** It feeds `routers/broker.py:570` and the gallery (`routers/broker_v2_gallery.py:61`).

### Not on this seam

`app/broker/ibkr/market_subscription.py` handles quote, trade-tick and halt evidence (`reqMktData` callbacks, `market_subscription.py:89-120`). It never sees 5-second bars. It matters here only because the ENTER liveness gate reads it (`bot_trade_strategy.py:880-882`).

## (b) Invariants each side assumes, and whether they hold

| # | Assumed by | Assumption | Guaranteed? |
|---|---|---|---|
| I1 | Assembler | A redelivery repeats the **most recent** bar. An older timestamp is corruption. | **By policy, not by vendor.** `temporal-rigor.md` makes an older timestamp fatal on purpose. IBKR does not promise which bar it redelivers. See G-6. |
| I2 | Assembler | After a reconnect, the new line's first bar is at or after `last_source_ms`. | **Only on the money path.** `ContinuityLoop` keeps the assembler (`ibkr_continuity.py:149`). The legacy path (`ibkr_feed.py:219`) and the chart (`live_bar_aggregator.py:325`) start from `last_source_ms=None`, so they rebuild from whatever arrives. That is safe from double emission, but the minute they rebuild is partial (G-4). |
| I3 | Assembler | 12 contributions means the minute is complete. | **Holds only if every 5-second bar is itself complete.** A resubscribed line's first bar may cover only part of its 5-second window (unknown, G-7). |
| I4 | Watchdog | `_session_phase_for_ms` can return PRE, POST or OVERNIGHT. | **False since `be1e3f87`.** It returns only RTH or CLOSED (P-1). |
| I5 | `admit_on_delivery` | A `realtime` bar "is never late by construction" (`feed_continuity_policy.py:73-74`). | **False as a static claim.** Emission waits for the next minute's first bar. That wait is unbounded when the line is silent and the watchdog is off (P-1), and bounded only by the 60 s stall timer in RTH. See G-1 and G-2. |
| I6 | Strategy session-close flush | The bar with `end_ms == close_ms` arrives around the close. | **Only if bars continue past the close.** It is emitted when the *next* minute's first bar arrives. For an extended run closing at 20:00 ET (`app/broker/alpaca/broker.py:44`), that depends on IBKR printing after 20:00 (G-2). |
| I7 | `LiveBarAggregator` guard | A partial first minute has `end_ms - start_ms != 60_000`. | **False.** The producer always emits 60 000 (P-2). |
| I8 | `RunDecisionSession.includes`, RTH runs | `bar.session_phase == "RTH"` exactly for RTH minutes (`app/services/decision_session.py:83-84`). | **Holds.** The calendar gets RTH and half-days right. PRE and POST are mislabelled CLOSED (P-3), but for an RTH run that still means "not RTH". |
| I9 | Consumer | A correction to an already-emitted minute never arrives silently. | **Holds.** It is fatal both in the open-minute path (`minute_assembler.py:317-320`) and after an early flush (`:418-421`). |

## (c) Named suspected gaps

"Needs paper" means the gap can only be settled against a real IBKR paper feed, because it depends on vendor behaviour this code cannot show. Every other gap can be prototyped in-process with synthetic 5-second bars.

**G-1. A realtime minute delayed by a silent line slips past the lateness gate. P1, needs paper.**
- *Hypothesis:* the `use_rth=False` line goes silent while connected, for example on an IBKR data-farm break (2103) with no 1100. In PRE or POST this lasts any length of time, because the watchdog is blind (P-1). In RTH it lasts 20 to 59 s, under the 60 s stall timer. When bars resume on the same line, the minute that was open is emitted short, as `realtime`, with no `spans_interruption`. `admit_on_delivery` passes it (`feed_continuity_policy.py:77`). Stream health reads fresh again because the resuming bar just advanced `last_source_wall_ms` (`ibkr_feed.py:444-448`). The strategy then decides on a minute that ended more than 20 s ago (`delivery_allowance_ms`, `feed.py:271`) and may submit an order.
- *Prototype:* drive `IbkrMarketDataFeed._stream_bars_with_continuity` with a fake client whose list stops appending mid-minute for 45 s of wall time (RTH clock), then resumes. Assert whether the late trigger bar reaches the strategy without a `DECISION_LATE` refusal. Paper half: log raw `reqRealTimeBars` timestamps across a real 2103/2104 blip to see whether IBKR resumes on the same list or needs a resubscribe.

**G-2. The 19:59 minute of an extended run is stranded overnight, then decided at about 04:00. P0 provisional, needs paper.**
- *Hypothesis:* if IBKR's `useRTH=0` TRADES bars stop at 20:00 ET, the 19:59 minute stays open until the first pre-market bar the next trading day. It is then emitted as `realtime`, so it passes `admit_on_delivery`. It passes `includes` too, because its start is inside the previous day's window (`decision_session.py:85-86`). It also fires the session-close flush (`bot_trade_strategy.py:590`). The final bucket's decision, and any order, happens about 8 hours late in pre-market.
- *Prototype:* in-process, feed bars up to 19:59:55, advance the clock to 04:00:00 the next day, and feed one bar. Assert what the strategy stage yields and whether any gate refuses. Paper half: record whether IBKR delivers any 5-second bar between 20:00:00 and 04:00 on SMART with `useRTH=0`.

**G-3. Short minutes are invisible downstream. P2.**
- *Hypothesis:* a minute missing 5-second bars, with no interruption involved, is delivered as an ordinary `realtime` bar. `_translate` drops `contribution_count` (`ibkr_feed.py:459-472`), so neither the strategy nor the source-bar ledger's bar row can tell an 11/12 minute from a 12/12 one. The continuity path counts only touched minutes (`ibkr_continuity.py:126`).
- *Prototype:* feed 11 contributions in RTH, then the next minute. Assert that the delivered `MarketDataBar` carries no completeness signal.

**G-4. Partial minutes after a legacy-path stall replacement or a mid-minute join. P2.**
- *Hypothesis:* the legacy path builds a fresh assembler per attempt (`ibkr_feed.py:219`). A consumer that joins a line starts at the tail (`bars.py:251`). The first minute after either is delivered short, as `realtime`. For the first live minute of generation 1 this is documented ruling R2 (`ibkr_continuity.py:114-116`). After a legacy stall replacement it is not documented.
- *Prototype:* legacy path, stall in the middle of a minute, replacement lands in the next minute's middle. Assert the short minute is yielded as ordinary `realtime`.

**G-5. The chart shows the previous day's 15:59 minute the next morning. P3.**
- *Hypothesis:* the chart streams `use_rth=True` (`live_bar_aggregator.py:325`, default `bars.py:879`). Its last RTH minute is emitted only by the next session's first bar, and it is appended to that day's deque and persistence. Same mechanism as G-2, off the money path.
- *Prototype:* feed through 15:59:55, then 09:30:00 the next day. Assert that the chart deque and the persisted log receive yesterday's minute.

**G-6. A redelivered older bar kills the run with an open position. P2, needs paper.**
- *Hypothesis:* after a reconnect, the new subscription replays a bar older than `last_source_ms` but inside the open minute, or the last bar of a minute that was already emitted or flushed. That is fatal by design (`minute_assembler.py:317-320`, `:418-421`), and the run crashes as `FEED_DEATH` (`bot_runner.py:1795-1805`) with any open position left in place. The rule is compliant. Only the frequency is in question.
- *Prototype:* in-process, reconnect with a new list whose first element is `last_source_ms - 5000`. Assert `FEED_DEATH`. Paper half: record the first 3 bars after 20 forced reconnects.

**G-7. The first bar of a resubscribed line may be partial, so the completeness count lies. P2, needs paper.**
- *Hypothesis:* a line opened mid-bar, for example at `:32`, delivers `:30`-`:35` with only the trades from `:32` on. A spanning minute still reaches 12 contributions, so `_is_unresolvable` passes it (`ibkr_continuity.py:128`) with understated volume and high/low.
- *Prototype:* paper only. Compare the first post-subscribe 5-second bar against the same window from `reqHistoricalData` with 5-second bars.

**G-8. A correction appended behind a resuming consumer's watermark is skipped. P3.**
- *Hypothesis:* on a shared list `[..., :55, :55']`, a resuming consumer with `last_source_ms = :55` walks back only over bars strictly greater than its watermark (`bars.py:666`). It starts after `:55'` and never sees the correction, so the minute emits with the uncorrected payload.
- *Prototype:* call `_resume_index` on that list with `start_index = len(list)` and assert index 3. Then run `stream_minute_bars` end to end.

**G-9. Volume units and fallback. P3, needs paper.**
- *Hypothesis:* `_volume_attr` truncates a float `volume` with `int()`. If `volume` is missing, it falls back to `barCount`, which is a trade count, not shares (`minute_assembler.py:198-199`). IBKR's real-time-bar volume units for US stocks are unverified here.
- *Prototype:* paper. Sum 12 real-time 5-second volumes and compare with the 1-minute historical volume.

## (d) Defects proven by reading alone

**P-1. The stall watchdog cannot fire outside RTH. P1, bug filed as #2299.**
- *Mechanism:* `_bars_expected_now(use_rth=False)` returns `phase in {"PRE","RTH","POST","OVERNIGHT"}` (`bars.py:504-509`). The phase comes from `_session_phase_for_ms` (`minute_assembler.py:104-106`), which calls `session_state_at_ms(now_ms=ts)` with no capability and no extended window. That falls through to `_session_from_nyse_calendar` (`app/services/session_authority.py:159-163`), which returns only `"RTH"` or `"CLOSED"` (`session_authority.py:218-258`).
- *Checked in-process:* on 2026-09-22 ET, it returns CLOSED at 04:30, 08:00, 16:30, 19:30 and 21:00, and RTH at 09:45.
- *Regression:* `be1e3f87` (2026-08-07, "fix: address supervised qualification review") replaced a classifier that did return PRE, POST and OVERNIGHT.
- *Test gap:* the only test covers the weekend (`tests/broker/ibkr/test_bars.py:96-106`), so it passes either way.
- *Consequence:* every trade run streams `use_rth=False` (`bot_trade_strategy.py:271`). For an extended run, a line that is connected but stalled in PRE or POST is never invalidated or replaced, and no continuity interruption or deadline is ever recorded. The run stays blind until IBKR resumes on its own, and then G-1 applies.
- *Why P1:* a liveness gate fails open.

**P-2. The chart's partial-first-bar guard is dead code. P2, no bug issue per the P0/P1 rule.**
- *Mechanism:* `MinuteAssembler.to_model` always sets `end_ms = start_ms + 60_000` (`minute_assembler.py:167`). The guard only drops a first bar whose window differs (`live_bar_aggregator.py:368`).
- *Test gap:* its test builds a 30-second bar that the real producer cannot emit (`tests/test_live_bar_aggregator.py:350-360`).
- *Consequence:* after every chart restart, 1101 resubscribe, or error restart, a short first minute is shown and persisted to the replay log.
- *Likely fix direction:* use `contribution_count`. The fix itself is out of scope here.

**P-3. PRE and POST live minutes are stamped `session_phase="CLOSED"`. P3.**
- *Mechanism:* same root cause as P-1, through `to_model` (`minute_assembler.py:176`) and `_translate` (`ibkr_feed.py:470`).
- *Consequence:* RTH runs are unaffected (I8). Extended runs filter by bounds, not by the label (`decision_session.py:85-86`). The label is wrong in the source-bar ledger and the replay evidence.

## Cleared at this seam (static)

- Exact redelivery of the latest bar in the open minute, and same-timestamp correction before emission, both follow the rule. Counters and logs are emitted: INFO for a skip, WARNING for a correction.
- Any bar touching an emitted or early-flushed minute is fatal.
- Bars straddling a minute boundary: flooring by source timestamp (the bar's start) assigns `:55` to the right minute.
- Half-day close for RTH runs: the calendar respects early closes, and the `use_rth=False` line keeps printing into POST, so 12:59 is emitted at 13:00:05.
- On the money path, a reconnect preserves `last_accepted`: one assembler across generations, bars tagged with their generation, and the P10 drain before the interruption surfaces.
