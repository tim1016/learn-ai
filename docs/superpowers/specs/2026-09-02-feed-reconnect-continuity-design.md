# Feed reconnect continuity for bot runs (#1921) — design, revision 7

**Status:** revision 7 (2026-09-02). The numerical-authority branch closed at revision 6; revision
7 makes continuity evidence an explicit, awaited, run-scoped, causally ordered channel whose
digest the replay receipt commits to (§0, §4.3, §4.4). Approved by the operator for
implementation of slices 1–3 and 6–7 (§10); slices 4–5 wait on twenty paired sessions.
Revision 5 resolved cross-session carryover and was rejected on the within-session proof: the
authorization replay followed a single all-COMMIT settlement trajectory while production
settles COMMIT or DISCARD per staged intent. Revision 6 replaces it with **exhaustive
settlement-tree equivalence through session close, failing closed when the tree cannot be
exhausted** (§0, §4.6).
Revision 4 passed on shape-bounded authority and gap-first ordering and was rejected on the
carryover residual: the warmup-taint rule rested on a false premise (`warmup_lookback_days` is
not the retained replay horizon) and, more fundamentally, recursive indicator state has no
forgetting horizon. Revision 5 **rejects the residual**: a substituted run never decides in a
later session; continuation is only by run-boundary admission; lifting that stop requires a
proven complete session reset or prospective terminal-state equivalence (§0, §4.8).

**Issue:** [#1921 Bot runs die permanently on a sub-second market-data reconnect](https://github.com/tim1016/learn-ai/issues/1921)

**Standing decisions respected:** ADR 0018 D5 (recovery reconciles; Resume is operator-only;
1100 ≠ socket-dead), ADR 0046 (`HARD_DOWN` is a breaker OPEN state), ADR 0049 (the lake is the
validation authority; the live feed is the execution-time observation stream), ADR 0023 (human
validation flag gates live behaviour), `.claude/rules/temporal-rigor.md`, `numerical-rigor.md`
("a port is not done until proven equivalent"; loosening is never the fix), #1411 (`f3436c38`).

## 0. Response to the review rounds

### Round 7 (implementation review of PR #1922)

The reviewer read the built code against this revision and ADR 0053.

| Finding | Verified | Change |
|---|---|---|
| After a count-complete flush the landing minute is not touched: a reconnect landing at :10 delivered a 10/12 minute as `realtime` inside the decision session | Yes — rule 3's touch conditions named the open minute and wholly-missed minutes but not the minute the resubscribed line lands in, although rule 4 already says that bar "decides which minute the live stream can complete" | Rule 3 gains the landing minute as a touched minute; `ContinuityLoop.observe_source_bar` records it from the raw-progress callback (ADR 0053 §18). Also closes the fail-open half of #1923 |
| The wait checked the deadline only while the socket was unhealthy; a line healthy again after the deadline (a stall detected late) passed as ordinary `realtime` | Yes — rule 7 says "raises when `now ≥ policy.deadline_ms(L)`", unconditionally | The wait checks the deadline before health (ADR 0053 §16) |
| `NotConnectedError` on the resubscribe after a recorded recovery only waited again: the second generation's death and the third generation's recovery left no evidence, and the bar was stamped with the wrong recovery | Yes — rule 9 requires every continuity fact to be emitted | Recorded as a second `interruption`/`recovered` pair under the first interruption's deadline (ADR 0053 §16) |
| `docs/math-sources-of-truth.md` still named `bars.py::aggregate_realtime_bar`; the decision clock's schedule functions carried no provenance blocks | Yes | Registry rows for the moved fold, the parity-tested floor and the trigger schedule; provenance blocks on `rth_trigger_instants` / `next_trigger_ms` |
| Dispute of ruling P3: absorbing an exact redelivery of *any* contribution of a flushed minute is broader than temporal-rigor's live relaxation (most-recently-accepted element only) | Yes | `_absorb_after_flush` absorbs only the flushed minute's most recent contribution; every other print of it is fatal (ADR 0053 §14) |
| Non-blocking: `flush_if_complete` flushes a 12-contribution pre-market minute although "outside RTH the count test is undefined" | The inconsistency is real, but between the two rules the wrong one was the touched rule: 12 five-second contributions is every print a minute can hold in any session phase, so the flushed minute *is* complete, while a touched 12-contribution pre-market minute was being omitted as a gap. The "undefined" count test only ever concerned a *short* minute where sparse bars are normal | Rule 3 restated: the count proves completeness in every phase; the phase decides only gap vs refusal for an unprovable minute. `flush_if_complete` unchanged (ADR 0053 §3) |

### Round 6 (revision 7)

| Finding | Verified | Change |
|---|---|---|
| `MarketDataFeed` exposes bars, history and health — no event channel; `ContinuityPolicy` has a grant callback but no sink; interruption, recovery, gap and refusal usually produce no bar, so the retained wrapper cannot infer them | Yes (`feed.py` Protocol; rev-6 §4.3/§4.4). | Broker-neutral `FeedContinuityEvent` and an **awaited, bot-owned `record_event` sink** on the policy. The feed emits facts and does not continue, deliver a recovered or substituted bar, or raise its terminal refusal until the sink has made the event durable (§4.3, §4.2 rule 9). |
| A substituted bar carries provenance but no `authorization_id` although the event schema requires one | Yes | `MarketDataBar` carries `authorization_id` and `continuity_event_ref` (the durable event identity returned by the sink); the retained layer persists the association without inference (§4.3). |
| `source_stream_events` has no `run_id`; the ledger outlives runs | Yes (`RetainedSourceBar` has no `run_id` either; the ledger is scoped by `ledger_account_id_for(binding)`, not by run) | Every event and, from now on, every bar append is stamped with `run_id` in the ledger's new evidence journal (§4.4). |
| Event `seq` is not ordered against `source_bars.seq`; the replay end bound (`bounded_replay_bars`, bars only) cannot bound or position a refusal after the final bar | Yes | One append-only **evidence journal** with a single monotonic `evidence_seq` referencing each bar or event row, written in the same SQLite transaction; the receipt's end bound becomes an `evidence_end_seq` (§4.4). |
| `bar_set_digest` authenticates bars only; "replay reads events to annotate" is not proof | Yes | `RunReplayReceipt` gains `continuity_event_digest` and `evidence_end_seq`; `bar_set_digest` and `ledger_end_seq` are preserved (§4.4). |
| Event-write failure must be typed and fatal | Accepted | `CONTINUITY_EVIDENCE_UNWRITABLE` (§4.2 rule 9, §6). |
| Stale revision debris: `SUBSTITUTION_WARMUP_TAINTED` still said "inside the warmup lookback"; tests distinguished inside vs outside that window | Yes | Corrected: the reason is "the retained replay already contains a substitute"; the test refuses any retained substitute (§4.3, §8). |
| Question: an explicit, awaited, run-scoped, causally ordered evidence channel with its digest in the replay receipt? | — | **Yes.** |

### Round 5

| Finding | Verified | Change |
|---|---|---|
| The authorization replay follows one settlement trajectory: `run_shadow_trace_evaluation` commits every staged evaluation | Yes: `qualification_shadow_trace.py:248` settles every stage `COMMIT`. | The all-COMMIT replay is no longer the gate; it stays as a cheap pre-check only (§4.6). |
| Production settles differently: Pause discards, market-liveness refusal discards ENTER, Clerk rejection and transient admission failure discard | Yes: `bot_trade_strategy.py:682, 704, 728, 798, 894` are `DISCARD` paths; `595, 699, 800` are `COMMIT`. `commit_signal_decision`/`discard_signal_decision` mutate `_in_position` and `_bars_until_exit`, so later evaluations branch on the settlement. | The proof explores both settlements at every matched staged intent (§4.6). |
| "One-cent difference away from a crossing → pass" is not generally true: the EMA trace carries exact EMA and RSI values | Yes: `SignalDecision(... rsi=float(rsi_val), facts={"ema_fast": …, "ema_slow": …})` (`ema_crossover_signal.py:317-388`) is hashed into the trace root, so any change to a consumed consolidated field changes the root even without an intent change. | §8's producer tests are corrected: a substituted **non-last** minute of a bucket leaves the consolidated close unchanged and passes; a substituted last minute whose close differs by one cent fails regardless of any crossing. |
| Even session-resetting programs have settlement-dependent state within the session | Yes (`deployment_validation` keeps a streak and position; `spy_vwap_reversion` keeps position). | No program is exempt from the settlement-tree gate (§4.6). |
| Question: replace all-COMMIT with exhaustive settlement-tree equivalence through session close, failing closed when the tree cannot be exhausted? | — | **Yes** (§4.6 "Counterfactuals"; §8; §10 slice 4). |

### Round 4

| Finding | Verified | Change |
|---|---|---|
| `warmup_lookback_days` is not the retained replay horizon | Yes: `_RetainedSourceBarFeed.recent_closed_bars` returns the **entire** retained ledger when it has rows and ignores `lookback_days` (`bot_trade_strategy.py:228-247`). A substitute is replayed for the ledger's lifetime, across every Resume. | The rev-4 taint rule is withdrawn. Taint is the run's remaining lifetime, with no time-based expiry (§4.4). |
| "Lookback" is not a forgetting horizon; EMA, Wilder RSI/ADX, ATR, Supertrend carry recursive state indefinitely; truncated reconstruction would not reproduce the continuous bot | Yes. Only `spy_vwap_reversion` (`_maybe_reset_session`) and `deployment_validation` (`_reset_day`) reset per session; `ema_crossover_signal`, `rsi_mean_reversion`, `sma_crossover`, `spy_strategy_a/b` do not. | A substituted run **may not make decisions in a later session** (§4.8). |
| Trace equality does not prove terminal-state equality | Yes: `EvaluationTrace.semantic_payload` is `asdict(self)` — decision meaning, not indicator internals (RSI's smoothed gain/loss are not in it). No program-state digest contract exists in the engine. | Cross-session continuation of a substituted run requires a proof the repo cannot produce today (§4.8 (iii)); the cost of building it is stated. |
| Nightly verification is retrospective; revocation follows a possibly harmful decision | Yes | Retrospective replay is **monitoring evidence only**, never admission authority (§4.6, §4.8 (iv)). |
| The fresh-deploy comparison is invalid: historical warmup at a declared run boundary is an existing admission policy; injecting a different observation into a running program's persistent state is a new authority expansion | Accepted. | The residual paragraph is deleted. Continuation after a substitution is only through a run-boundary admission whose policy is declared (§4.8 (ii)). |
| Question: reject the carryover residual and require prospective terminal-state equivalence, or a proven complete session reset, before a substituted run may decide in a later session? | — | **Yes.** In v1 no such proof exists for any program, so a substituted run ends at its decision session's close with a typed outcome, and recursive programs cannot carry a substitution across sessions at all. Within-session decisions after a substitution remain covered by the trace-parity counterfactual for the tested shape, which runs through the session close; §9 Q1 asks the reviewer to confirm that reading. |

### Round 3

| Finding | Verified | Change |
|---|---|---|
| Outside-session minutes consulted authorization first and the pre-market example substituted "for evidence" | Yes (rev-3 §4.2 rule 3 order; §4.5 row 6). An authorization is keyed to a decision session and proves nothing about minutes outside it. | Rule 3 reordered: outside the decision session the minute is **always** a surfaced gap; authorization is never consulted there (§4.2 rule 3, §4.5). |
| Runtime authority (2 h windows, any number of episodes) wider than the proven envelope (single minutes, 2–5-minute windows, all-substituted) | Yes. The programs are stateful and nonlinear; passing the tested shapes and the all-substituted endpoint does not imply intermediate or combined shapes pass. | The artifact **encodes the proven shape** and runtime enforces it: at most **one contiguous episode of 1–5 minutes per decision session**. A sixth minute or a second episode raises `SUBSTITUTION_SHAPE_UNPROVEN`. `max_substitution_window_ms` is deleted. All-substituted stays as stress evidence, not authority (§4.6). |
| Question: narrow to one contiguous 1–5-minute episode per decision session and fail closed outside that shape? | — | **Yes.** Encoded in the artifact, enforced before any fetch, tested (§8). |
| (Self-found, same principle) A substituted bar persists into the ledger and becomes **warmup input for later sessions** — a shape no counterfactual tested at the time of the decision | Yes: Resume and every later session warm up from the retained ledger (`_RetainedSourceBarFeed.recent_closed_bars`), so a substitute from session S is inside session S+1's indicator state. | Rev 4 proposed a lookback-bounded taint rule and a "carryover residual"; **both were superseded in rev 5** (taint is the run's lifetime; the run ends at its session close; §4.8). |

### Rounds 1–2 (retained)

Round 2: D3's field-parity gate withdrawn (TSLA diverges from history in clean 12/12 minutes;
`deployment_validation` reads open, `spy_strategy_b` reads high/low, `spy_vwap_reversion` reads
volume); substitution fail-closed under a per-(provider contract, instrument, sealed program
hash, decision session) as-of artifact produced by counterfactual trace/intent parity.
Round 1: deadline framing, session-close trigger, rollover latency, stall path, closed receipt
vocabulary, `use_rth=False` on the retained feed, generation insufficiency plus `Client.reset()`
restarting `_reqIdSeq`, single-flight identity, ordinary gaps, Pause, no `L` before the first
bar, pacing wording, manual proof. All verified; all carried forward below.

Pushback, one item, unchanged: the bot layer authors the deadline function, but the feed also
evaluates it during the wait; otherwise a run could be held through an arbitrary outage.

## 1. What actually happens today

Container logs survive `./restart.sh`: podman's log driver is journald inside the podman-machine
VM (`podman machine ssh 'journalctl CONTAINER_NAME=polygon-data-service …'`, `--since` parsed
in VM local time, CDT), retained since 2026-08-09.

### 1.1 The 2026-09-02 15:00 ET event

| UTC | ET | Event | Source |
|---|---|---|---|
| 18:59:59.7 | 14:59:59 | "reqRealTimeBars has not delivered 5-second bars", all six lines at once | `bars._BarDeliveryLogger` |
| 19:00:19 | 15:00:19 | podman healthcheck `curl localhost:8000/health` **exceeded 5 s** (first of 17 in six minutes) | VM journal |
| 19:00:37.8 | 15:00:37 | Alpaca `/v2/clock` timed out; new exposure blocked | `alpaca.market_liveness` |
| 19:00:44.828 | 15:00:44 | **Monitor: "IBKR app-level probe failed; forcing reconnect"** → `client.disconnect()` | `auto_reconnect_monitor._probe_if_due` |
| 19:00:44.842 / .869 / .884 | 15:00:44 | Three bots `FEED_DEATH` | `bot_runner._supervise` |
| 19:00:45.162 → .605 | 15:00:45 | TCP connected; recovery complete (777 ms) | monitor |
| 19:01:33.6, 19:02:51.3, 19:04:08.6, 19:04:47.0 | 15:01–15:04 | Four more probe-forced reconnects, 442–938 ms each | monitor |

`/health` returns a dict (`main.py:736`). Seventeen 5-second timeouts on it, while no other
container's healthcheck failed, means the data-plane **process** stalled. The monitor's probe is
`asyncio.wait_for(reqCurrentTimeAsync(), 4.0)` every 30 s; after a stall longer than 4 s the
timeout fires when the loop unblocks and the monitor tears down a healthy socket. Reconnect
succeeds in under a second because the link was never down.

### 1.2 Why bots die within 100 ms

`_check_realtime_subscription_liveness` (`bars.py:459-488`) raises `IBKRBarStreamError("IBKR
connection lost …")` on either `not client.is_connected()` or `client.connection_lost` (1100).
That is the one branch `IbkrMarketDataFeed.stream_bars` does not retry (`ibkr_feed.py:166`);
it becomes `MarketDataFeedError` → `FEED_DEATH` (`bot_runner.py:1504-1513`). The only
`recovery_callback` re-subscribes the chart aggregator (`main.py:349`).

### 1.3 What a resubscribe does to the minute stream

The open-minute accumulator is a local of `stream_minute_bars`. When the generator raises, the
partial minute is lost; the re-entered generator starts at `len(bars)` and emits the current
minute from only the 5-second bars received after resubscribe. Today's aggregator output shows
it: the 15:00, 15:02 and 15:04 minutes were assembled from three contributions each (volume
7 157 vs 32 618 for TSLA 15:00) although all twelve raw 5-second bars were received across the
two sockets (§3.1). `SignalSession.advance` checks only width and monotonicity
(`signal_program.py:196-199`), so a damaged mid-bucket minute passes silently. Pre-existing for
the stall path and for a deploy mid-minute.

### 1.4 Registry and reqId hazards

`_RealtimeBarSubscriptionRegistry` keys on `(id(client), conId, barSize, what, useRTH)`
(`bars.py:212`); the `IB()` instance is reused across reconnects and `disconnectedEvent` is not
hooked. After `Wrapper.reset()` the old `bars` list is orphaned, and `Client.reset()` sets
`_reqIdSeq = 0`, so reqIds are reissued after reconnect. A re-subscriber can be handed a dead
multiplexed list; a fast disconnect/reconnect can leave an existing lease reading a dead list
with `is_connected()` already true (it degrades into a 60 s stall); a stale lease's
`cancelRealTimeBars(old_bars)` can cancel a new subscription that reused the reqId.

## 2. How often, and when

Journal 2026-08-09 → 2026-09-02; `run_outcomes` under `live_state` (149: 144 `STOPPED_FLAT`,
5 `FEED_DEATH`, older instances pruned); `~/Jts/launcher*.log`.

| Pattern | When (ET) | Count | Recovery | Bots killed |
|---|---|---|---|---|
| Nightly IBKR reset (1100 → 30 s link wait → reconnect) | 00:17–00:22 daily | ~1/day | 8 s | 4 (08-21, 08-22 ×3) |
| Nightly gateway auto-logoff (`Daily auto-restart is not enabled` still in today's launcher log) | 00:45 daily | 1/day | hours in `HARD_DOWN` | 0 |
| **Stall storm A** — probe-forced reconnects | 08-27 15:54–16:43 | **45** | 1–7 s typical | 2 |
| **Upstream event B** — 1100 soft loss, farm 2103/2105, Alpaca timeouts, no healthcheck timeouts | 08-31 16:33–16:37 | 1 + 2 forced | 26 s, 32 s | 2 (post-close; the retained feed streams all sessions) |
| **Stall storm C** — probe-forced reconnects | 09-02 15:00–15:05 | **5** | 0.4–1.0 s | 3 |
| Gateway down in RTH | 08-26 09:50, 08-27 09:16, 09-02 07:46 | 3 | 24 min, 6.4 h, 1.7 h | 0 |

88 mid-process episodes, 11 inside RTH, 12 bot feed-deaths. Healthcheck-timeout clusters (VM
local hour): 08-25 08–09h (37), 08-26 09h (22), **08-27 14–15h (170)**, 09-01 15h (7),
**09-02 08h (22), 14h (24)**. The stall's root cause is open (no backtest, LEAN, replay-proof or
lake request in any window; 2-CPU/2 GiB cgroup; synchronous SQLite with a 5 s busy timeout on
the loop thread in `SourceBarLedger.append`; host CPU starvation) and belongs to a separate
issue with an event-loop-lag heartbeat first. This design does not depend on the answer.

## 3. Measurements

### 3.1 Are 5-second bars contiguous? (`artifacts/live_bars/<sym>/5s/2026-09-02.jsonl`)

| Symbol | RTH minutes observed | 12/12 contributions | Exceptions |
|---|---|---|---|
| SPY | 340 | 338 | 15:01 (11 — one bar lost in the 15:01:33 reconnect), 15:31 (7 — stream ended by the container restart) |
| TSLA | 340 | 338 | same two |
| AAPL | 340 | 339 | 15:31 (7) |

No whole minute absent, zero duplicate timestamps. Completeness of an RTH minute is provable by
contribution count for these instruments, and four of the five blip minutes today were complete
from real-time data across the two sockets.

### 3.2 What the registered programs read from a consolidated bar

| Program | open | high | low | close | volume |
|---|---|---|---|---|---|
| `ema_crossover_signal`, `rsi_mean_reversion`, `sma_crossover`, `spy_strategy_a` | | | | ✓ | |
| `deployment_validation` | ✓ (`close > open`) | | | ✓ | |
| `spy_strategy_b` (Supertrend on the whole bar) | | ✓ | ✓ | ✓ | |
| `spy_vwap_reversion` | | ✓ | ✓ | ✓ | ✓ |

A substituted first minute of a bucket changes the consolidated open; a substituted extreme
minute changes the consolidated high or low.

### 3.3 Real-time-built minutes vs IBKR historical 1-minute bars (same day, read-only fetch)

| Symbol | Common minutes | close identical | Divergent minutes and their 5-second contribution count |
|---|---|---|---|
| SPY | 371 | 371 | 09:47 (partial first minute), 15:00, 15:01 (11), 15:02, 15:04 (aggregator rebuilt from 3 bars each), 15:32 (tail) |
| TSLA | 344 | **343** | as SPY, plus **14:14 close 352.27 vs 352.28 (12/12, clean)** and **14:23 open 352.82 vs 352.86 (12/12, clean)** |
| AAPL | 339 | 339 | 15:00, 15:01, 15:02, 15:04 |

Read correctly: (a) the damaged minutes are the resubscribe artifacts §1.3 describes and the
assembler fix removes them; (b) even clean minutes diverge occasionally, which is IBKR's
documented historical trade filtering, so the historical record is a **different observation**
of the same minute; (c) a per-field envelope says nothing about whether a program's decision
changes. These three readings are why substitution is fail-closed and why its authority is only
ever the shape that was replayed.

## 4. Design

### 4.1 Ownership

- **IBKR layer** (`bars.py`, `ibkr_feed.py`, `client.py`): interruption detection, connection
  generation fencing, a minute assembler that survives interruptions, completeness by count,
  substitution only against a **grant** the consumer issues per window, ordering/buffering,
  single-flight fetches, typed refusal reasons. It evaluates, but does not author, the
  consumer's deadline, and it holds no authority state of its own.
- **Port** (`feed.py`): `ContinuityPolicy` (authored by the consumer) and `MarketDataBar.provenance`.
- **Bot layer** (`bot_trade_strategy.py`, `source_bar_ledger.py`, `bot_run_terminal.py`,
  admission): the trigger/deadline function, the substitution authorization fact and every
  grant decision (as-of validity, proven shape, episode count, warmup taint), admission on
  delivery, durable per-run evidence, ledger provenance, `FEED_DEATH` finalization.
- **Authorization producer** (`scripts/`, nightly): paired-session capture, counterfactual
  replay, artifact renewal and revocation (§4.6).

### 4.2 IBKR layer rules

1. **Generation fencing.** `IbkrClient.connection_generation` increments on every successful
   `connect()`. Every `_RealtimeBarLease` stores its generation; the registry key includes it;
   `_check_realtime_subscription_liveness` runs on **every** loop iteration and raises
   `IBKRBarInterrupted(generation_changed)` when the lease is stale, so a fast
   disconnect/reconnect never degrades into a 60 s stall. `release`/`invalidate` of a
   stale-generation lease evict the entry and **never** call `cancelRealTimeBars`. `acquire`
   re-reads the generation after every await and restarts if it moved. A generation change
   invalidates every registry entry of the old generation.
2. **The minute assembler survives.** `_MinuteAccumulator` and `last_source_ms` move out of the
   generator into a per-consumer `MinuteAssembler` owned by the feed's `ContinuityLoop` for the life of one `stream_bars` call
   and handed to each `stream_minute_bars` generation. Contributions are keyed by source
   timestamp, so bars received before and after an interruption merge deterministically; the
   `live_idempotent` policy already covers a redelivered 5-second bar. On interruption, a
   count-complete open minute is flushed immediately.
3. **Completeness, and what happens without it — in this order.**
   - In RTH a minute is complete iff it holds `60 s / 5 s = 12` contributions (§3.1). A complete
     minute spanning an interruption is emitted from real-time data with provenance
     `realtime_across_reconnect`; it needs no grant — it is the same vendor real-time record,
     received over two sockets.
   - A minute is **touched by an interruption** when any of these holds: its stored
     contributions span connection generations (`spans_interruption`); it was the open minute
     when the interruption began (the feed records that minute's start, because an interruption
     that outlives it leaves it holding one generation's contributions and the flag then reads
     false); it lies wholly inside the interruption window (the first post-reconnect source
     bar lands more than one minute after `last_source_ms`); or it is the **landing minute** —
     the one the first post-reconnect source bar falls in (rule 4), whose earlier prints the
     interruption lost and which likewise holds one generation's contributions (round 7). A
     minute nothing interrupted — a mid-minute deploy's first minute included — is not touched
     and keeps today's behaviour.
   - A touched minute that cannot be proven complete by count is **unresolvable from
     real-time data**. Twelve contributions is every print a minute can hold, so a touched
     minute holding them is complete in any session phase (round 7). Fewer is unprovable
     everywhere: short in RTH, where IBKR delivers 12/12; undecidable outside it, where sparse
     bars are normal. The phase decides only what an unresolvable minute becomes (next bullets).
   - The wholly-missed scan runs **after a recovery only**, for the first emitted bar following
     a recorded interruption, and is then cleared. A gap no interruption explains is an ordinary
     gap and stays non-fatal (§6). Contiguous unresolvable minutes are **coalesced into one
     window** `[start, end)`: one grant offer, one `gap`/`refused` event covering the episode.
     A window is split only where the decision-session verdict changes across it — one straddling
     the RTH open is a `gap` for its pre-market part and a refusal for its RTH part.
   - An unresolvable window **outside the consumer's decision session** (pre-market for an RTH
     program) is **omitted** and surfaced as a `gap` event. No grant is requested; authorization
     is never consulted for a window it could not prove anything about. An omitted window is a
     hole the ledger shows, not a repair.
   - An unresolvable window **inside the decision session** is offered to the consumer; the feed
     asks `policy.substitution_grant(start, end)`. On a grant it
     substitutes the vendor's historical 1-minute bar(s) for exactly that window, provenance
     `historical_substitute`, delivered in order before any later live minute, and writes the
     grant's `authorization_id` into the evidence. On a refusal it raises
     `MarketDataFeedError(<refusal reason>)` and the run finalizes `FEED_DEATH`.
   - Every `gap` and `refused` event about a single emitted-but-incomplete minute carries that
     minute's `contribution_count`; a window nothing was ever assembled for carries `None`.
   - If the vendor has no historical bar for a granted window there was nothing to deliver:
     an ordinary gap, non-fatal. Nothing is interpolated, forward-filled or reordered.
4. **Cutover from raw progress, not from emissions.** After resubscribe, the first source bar's
   timestamp decides which minute the live stream can complete; earlier minutes in the gap
   resolve by rule 3. Substitutes are fetched only when their window has closed (forming-bar
   cutoff reused from `recent_closed_bars`).
5. **Single-flight.** Historical fetches are keyed by `(generation, conId, useRTH, what,
   window)`; consumers whose granted windows coincide share one request. IBKR's one-minute
   historical pacing is soft-throttled rather than hard-limited; a fleet burst of identical
   requests is exactly what trips it.
6. **`L` before the first live bar.** Continuity begins when generation 1 has received its first
   source bar; `L` is then the start of that minute. Before that, failures are fatal as today.
   The first minute of generation 1 is incomplete by construction and follows rule 3 (§9 Q4
   proposes deferring a new consumer's first emitted minute to the next boundary instead).
7. **Wait bound.** While interrupted the feed polls every 250 ms for `client.is_connected() and
   not client.connection_lost` and, when a monitor is installed, `recovery_state == "HEALTHY"`.
   It raises `MarketDataFeedError(reason)` when `now ≥ policy.deadline_ms(L)` or when a granted
   substitute cannot be obtained (`BACKFILL_FAILED`, 15 s timeout).
8. **Stall = interruption.** `IBKRBarSubscriptionStalled` enters the same choreography; its
   60 s detection counts against the deadline (§9 Q3).
9. **Evidence gates progress.** Every continuity fact is emitted through `policy.record_event`
   and awaited: `interruption` before entering the wait, `recovered` before the first
   post-recovery bar is delivered, `gap` before the bar that follows an omitted window,
   `substituted` before the substitute bar is delivered (the returned event reference and the
   grant's `authorization_id` are stamped on that bar), `refused` before the terminal
   `MarketDataFeedError` is raised. If the sink raises, the feed raises
   `MarketDataFeedError(CONTINUITY_EVIDENCE_UNWRITABLE)` — continuing without the promised
   evidence is forbidden. With `continuity=None` no events exist, as today.

### 4.3 Port

```python
class SubstitutionGrant(BaseModel):
    authorization_id: str
    window_start_ms: int
    window_end_ms: int

class SubstitutionRefusal(BaseModel):
    reason: Literal[
        "SUBSTITUTION_NOT_AUTHORIZED",     # no artifact, expired, revoked, or key mismatch
        "SUBSTITUTION_SHAPE_UNPROVEN",     # window longer than proven, or a second episode this session
        "SUBSTITUTION_WARMUP_TAINTED",     # the retained replay already contains a substitute
    ]

class FeedContinuityEvent(BaseModel):     # broker-neutral fact, emitted by the feed
    model_config = ConfigDict(frozen=True)
    kind: Literal["interruption", "recovered", "gap", "substituted", "refused"]
    feed_id: str
    symbol: str
    observed_at_ms: int
    cause: Literal["socket_down", "soft_loss_1100", "stall", "generation_changed"] | None = None
    generation_from: int | None = None
    generation_to: int | None = None
    window_start_ms: int | None = None   # gap / substituted / refused windows
    window_end_ms: int | None = None
    bar_identity: str | None = None      # substituted: the substitute's identity
    authorization_id: str | None = None  # substituted: the grant
    reason: str | None = None            # refused: the typed reason
    last_delivered_end_ms: int | None = None
    deadline_ms: int | None = None

class ContinuityEventRef(BaseModel):     # returned by the sink once the event is durable
    model_config = ConfigDict(frozen=True)
    run_id: str
    evidence_seq: int                    # position in the ledger's evidence journal
    def ref(self) -> str: return f"{self.run_id}:{self.evidence_seq}"

class ContinuityPolicy(BaseModel):            # broker-neutral, frozen
    decision_session: Literal["rth", "all"]   # decision-session scope, NOT the subscription scope
    delivery_allowance_ms: int = 20_000       # operational: normal rollover (≤5 s) + one substitute fetch (≤15 s)
    next_trigger_ms: Callable[[int], int]      # consumer-authored: smallest trigger instant > L
    substitution_grant: Callable[[int, int], SubstitutionGrant | SubstitutionRefusal]
                                               # consumer-authored; default implementation refuses everything
    record_event: Callable[[FeedContinuityEvent], Awaitable[ContinuityEventRef]]
                                               # consumer-owned, awaited; stamps run_id, persists, returns the ref
    def deadline_ms(self, last_delivered_end_ms: int) -> int:
        return self.next_trigger_ms(last_delivered_end_ms) + self.delivery_allowance_ms

class MarketDataBar(BaseModel):
    ...
    provenance: Literal["realtime", "realtime_across_reconnect", "historical_substitute", "history"] = "realtime"
    authorization_id: str | None = None        # historical_substitute only
    continuity_event_ref: str | None = None    # "<run_id>:<evidence_seq>" of the causal event, when any
```

`stream_bars(symbol, *, use_rth=True, continuity: ContinuityPolicy | None = None)`; `None` is
today's fail-fast. `PauseAwareFeed` and `_RetainedSourceBarFeed` forward it unchanged.

Three temporal inputs, assigned: **subscription scope** (`use_rth=False`, the retained feed
captures all sessions for evidence); **decision-session scope** (`decision_session`, from the
binding, drives the trigger set and rule 3's in/out-of-session branch); **real-time liveness**
(halts, emergency closes — the Alpaca clock gate at ENTER, unchanged; never the calendar).

### 4.4 Bot layer

- **Trigger function** (`bot_trade_strategy`, calendar-backed): for decision session `S` and
  timeframe `TF`, trigger instants are `bucket_end + 60 s` for every bucket of `S`, except the
  session's last bucket whose trigger is `session_close` (the forced flush); after a session's
  last trigger the next is the first bucket trigger of the next trading day.
  `next_trigger_ms(L)` = smallest trigger `> L`. Buckets use the consolidator's ET-anchored
  floor, moved to a shared temporal home so both import one function.
- **Grant function** (`substitution_grant`), evaluated per offered window, in this order:
  1. the authorization fact for `(provider_contract, instrument, configured_signal_hash,
     decision_session)` must be present, `authorized_from_ms ≤ now < expires_at_ms`, not
     revoked — else `SUBSTITUTION_NOT_AUTHORIZED`;
  2. the window length must be ≤ the artifact's `proven_shape.max_contiguous_minutes` and this
     decision session (trading date, ET) must have had **no** prior granted episode — else
     `SUBSTITUTION_SHAPE_UNPROVEN`; an episode is one contiguous granted window; a
     count-complete interruption is not an episode;
  3. the run's retained replay (the **whole** ledger, which is what Resume warms up from) must
     contain no row with provenance `historical_substitute` — else `SUBSTITUTION_WARMUP_TAINTED`.
     This can only be true for a run that has never been granted and whose ledger was never
     substituted; a grant therefore taints the run for its remaining lifetime, and the taint has
     no time-based expiry. Rule 2 already refuses a second episode in the session; rule 3 makes
     the same refusal survive a Resume onto the same ledger.
  The fact is read at Start/Resume exactly as `current_strategy_validation_fact` re-reads the
  validation proof, and re-read at every grant (as-of). The episode counter and taint state are
  the bot layer's, derived from the ledger, so the feed holds no authority state.
- **Session-end stop after a grant.** A run that received a grant is marked
  `must_end_at_session_close`. When the decision session's last trigger has settled (the forced
  flush at `session_close`, or the last delivered bar if the session ends without one), the bot
  layer finalizes the run `STOPPED / SUBSTITUTION_CARRYOVER_UNPROVEN` through the existing
  terminal path (`bot_run_terminal`), exactly as an operator Stop would, exposure semantics
  included (`EXPOSURE_CARRYOVER_STRATEGY_KEYS` and Resume's checkpoint fact govern any open
  position as they do today). The run never streams the next session's bars (§4.8).
- **Admission on delivery.** `_RetainedSourceBarFeed.stream_bars` receives every bar with
  provenance. For a bar with non-`realtime` provenance whose `end_ms` is a trigger instant it
  requires `now_ms ≤ end_ms + delivery_allowance_ms`, else `FeedContinuityRefused(DECISION_LATE)`
  → `FEED_DEATH`. Decision lateness is zero. Realtime-provenance bars are not checked (§9 Q5).
- **Evidence channel.** The bot layer's `record_event` closure (one per run) stamps `run_id`
  on each `FeedContinuityEvent` and persists it through `SourceBarLedger.append_event(event,
  run_id=…)` before returning its `ContinuityEventRef`; the feed awaits it (§4.2 rule 9).
  Persistence is one SQLite transaction (`BEGIN IMMEDIATE`, as bar appends are) that inserts
  the row into `source_stream_events (seq, run_id, kind, …all event fields…)` **and** a row into
  the new append-only **evidence journal** `source_evidence_journal (evidence_seq PK, run_id,
  kind ∈ {bar, event}, bar_seq NULL, event_seq NULL, observed_at_ms)`. Bar appends
  (`append`/`append_history`, now taking `run_id`) journal a `bar` row in the same transaction,
  so bars and events share one monotonic causal order and every row names its run. Existing
  ledgers are migrated by creating both tables and back-filling a `bar` journal row per retained
  bar in `seq` order with `run_id` NULL (pre-channel rows never had one).
  A substituted bar's `continuity_event_ref` and `authorization_id` are persisted as columns on
  `source_bars` (nullable), so the association is stored, never inferred. Decision receipts are
  untouched.
- **Receipt.** `RunReplayReceipt` gains `continuity_event_digest` — sha256 over the run's events
  in `evidence_seq` order with canonical JSON, excluding storage-only fields — and
  `evidence_end_seq`, the journal position snapshotted at Stop or at terminal finalization, which
  bounds bars **and** events (a `refused` event after the final bar is inside the bound).
  `bar_set_digest` and `ledger_end_seq` stay for compatibility; `bounded_replay_bars` prefers the
  journal bound when present.
- **Evidence-write failure is fatal and typed** (`CONTINUITY_EVIDENCE_UNWRITABLE`). The sink
  runs its SQLite write on the event loop exactly as bar appends do today; that shared stall
  exposure is noted in §2 and belongs to the stall issue.
- **Ledger provenance.** `source_bars` gains `provenance TEXT NOT NULL DEFAULT 'realtime'`
  (idempotent `ALTER TABLE` guarded by `PRAGMA table_info`); warmup rows carry `history`.
  `bar_set_digest` includes `provenance` only when it is not `realtime` (omit-when-default), so
  existing digests are unchanged and a substituted stream is, correctly, a different digest.
- **Legacy unsealed bindings** get no policy: fail-fast as today.
- **Pause** keeps consuming in observe-only mode; **Stop** cancels through the wait.

### 4.5 The predicate

Given last delivered minute end `L`, `T = next_trigger_ms(L)`, allowance `A`: the feed may wait
while `now < T + A`; the bar closing at `T` must reach the consumer by `T + A` — live if the
socket is back before its rollover, otherwise as a granted substitute fetched at `T`; a decision
is never made later than `A` after its trigger; refusal is `FEED_DEATH`.

| Interruption (ET) | L | T | Without a grant | With a valid grant |
|---|---|---|---|---|
| 15:00:44.8 → 15:00:45.6 (measured; 15:00 minute 12/12) | 15:00 | 15:01 | emitted `realtime_across_reconnect` at rollover; 14:45–15:00 decision on time | same |
| 15:01:33.6 → 15:01:34.4 (measured; 15:01 minute 11/12) | 15:01 | 15:16 | unresolvable minute at the 15:02 rollover → refused with the grant's reason (`SUBSTITUTION_NOT_AUTHORIZED` when no artifact exists) → `FEED_DEATH` | 1-minute episode substituted at 15:02; no decision affected |
| second lost 5-second bar later the same day | — | — | refused | **`SUBSTITUTION_SHAPE_UNPROVEN`** (second episode) → `FEED_DEATH` |
| 15:10:00 → 15:16:30 (6 minutes unresolvable) | 15:09 | 15:16 | refused | **`SUBSTITUTION_SHAPE_UNPROVEN`** (window > 5) → `FEED_DEATH` |
| 15:15:40 → 15:16:10 | 15:15 | 15:16 | refused (15:15 minute incomplete) | substitute fetched 15:16:10, delivered ~15:16:11 ≤ 15:16:20 → admitted |
| 15:15:40 → 15:16:30 | 15:15 | 15:16 | refused | deadline 15:16:20 passes → `DECISION_BAR_MISSED` |
| 15:59:20 → 16:00:05 | 15:59 | **16:00** | refused | substitute at 16:00:05 → closing decision admitted |
| 05:10 → 05:12, RTH program | (pre-market) | 09:46 | 05:10–05:11 omitted, `gap` events; run continues | **same — no grant is requested outside the decision session** |
| 05:10 → 05:12, all-session program, `TF = 15 min` | 05:10 | 05:16 | refused | 2-minute episode substituted |
| session close of a day on which a grant was made | 16:00 | — | — | run ends **`STOPPED / SUBSTITUTION_CARRYOVER_UNPROVEN`** after the closing decision settles; next day only via boundary admission (§4.8) |
| Resume onto a ledger that retains a substitute, then an unresolvable minute | — | — | — | **`SUBSTITUTION_WARMUP_TAINTED`** → `FEED_DEATH` (rule 3) |
| `TF = 1 min`, 0.8 s blip at 15:00:44 (12/12) | 15:00 | 15:01 | on time | same |

### 4.6 Substitution authorization — the artifact

**Default: unauthorized.** No artifact, no substitution, for every instrument and program.
**Authority equals proof:** the artifact authorizes exactly the shapes its counterfactuals
replayed, and runtime refuses everything else.

- **Key:** `(provider_contract, instrument, configured_signal_hash, decision_session)`.
  `provider_contract` = `"ibkr:reqRealTimeBars/5s/TRADES→1m | reqHistoricalData/1min/TRADES"` plus
  the `ib_async` version and the assembler's module digest; `instrument` = qualified `conId` +
  symbol; `configured_signal_hash` = the seal's `configured_signal_hash` (a re-sealed program
  starts from zero); `decision_session` ∈ {rth, all}.
- **Paired session:** one completed trading day for which both records exist for the instrument:
  the real-time minute stream with provenance `realtime`/`realtime_across_reconnect` (the
  deployed instance's own ledger, or the aggregator's persisted 1m stream once the assembler fix
  is in) and the vendor's historical 1-minute bars fetched **after** the session closed, both
  stored with `bar_set_digest`s. A day on which the real-time record has any unresolvable
  minute is not a valid pair.
- **Counterfactuals per paired session — settlement-tree equivalence.** For each substitution
  shape (every single minute; every contiguous window of 2–5 minutes) the producer drives a
  **baseline** program (real-time record) and a **counterfactual** program (the same record with
  the shape substituted) in lockstep, bar by bar, through the same warmup the ledger held before
  that session, using the `SignalSession` protocol directly (`advance` → staged intent →
  `settle`), never `run_shadow_trace_evaluation`'s all-COMMIT path:
  - at every bar both sides must produce identical evaluation traces; if exactly one side stages
    an intent, or the staged intents differ, the counterfactual **fails immediately**;
  - at every matched staged intent the exploration **forks**: one branch settles both sides
    `COMMIT`, the other settles both `DISCARD` (the only two settlements production applies —
    Pause, liveness refusal, Clerk rejection and transient refusal all reduce to `DISCARD`);
  - every reachable branch is followed to the session's last trigger (the forced flush at
    `session_close`); the counterfactual passes iff traces and intents are identical on **every**
    branch;
  - programs are not cloneable today, so each branch is replayed from the session start; the
    producer enforces explicit bounds — `max_branches_per_counterfactual` (proposed 256) and
    `max_program_replays_per_session` (proposed 500 000) — and **fails the authorization** when a
    bound is exceeded. Sampling is never substituted for exhaustion. A future canonical
    state digest (§7.7) may deduplicate equivalent branches; it is not required for the proof.
  - The all-COMMIT replay may run first as a cheap rejection pre-check; passing it authorizes
    nothing. The all-substituted counterfactual remains **stress evidence only**, recorded,
    never authorizing.
  Field-level differences are diagnostics, never the gate. No program is exempt: session-resetting
  programs carry settlement-dependent position and countdown state within the session.
- **Proven shape, encoded in the artifact and enforced at runtime (§4.4):**
  `proven_shape = {max_episodes_per_session: 1, max_contiguous_minutes: 5, warmup_taint_free: true,
  cross_session: "none", settlement_proof: "exhaustive_tree", branch_bound: 256}`. Widening any of these requires the producer to replay that wider
  shape (for example, pairs of separated windows, or the terminal-state proof of §4.8) and to
  bump the artifact schema; the runtime check reads the artifact's fields, never constants of
  its own. `cross_session: "none"` is the v1 value for every program and means §4.8's stop.
- **Gate:** `≥ 20` consecutive valid paired sessions with zero divergence → `authorized_from_ms`
  = the artifact's production time (never retroactive). **Renewal:** nightly after the session
  close; `expires_at_ms` = end of the next trading day. **Revocation:** the first counterfactual
  divergence, a `provider_contract` change, or a re-seal, sets `revoked_at_ms` and the count
  restarts from zero.
- **As-of check** at grant time `t`: key matches, `authorized_from_ms ≤ t < expires_at_ms`, not
  revoked. The `authorization_id` is written on the `substituted` event and into replay.
- **Retrospective replay is monitoring, not authority.** The nightly producer's counterfactuals
  over a completed session can revoke the artifact for future grants; they never admit a
  decision already made and never lift §4.8's stop.
- **Artifact:** `artifacts/feed_continuity/substitution_authorizations/<key-digest>.json`,
  hash-chained like the validation flag ledger, schema-versioned, produced only by
  `scripts/renew_feed_substitution_authorizations.py` (nightly, alongside the auto-research
  tick), with paired-session digests, every counterfactual's trace root, and the proven shape
  inline so a reviewer can re-derive the decision from the artifact alone. Methodology and
  status in `docs/references/ibkr-minute-substitution-parity.md`.
- **Human gate (ADR 0023 shape):** enabling substitution for a key the first time requires an
  operator flag event referencing the artifact; renewals and revocations are automatic.

Timeline consequence: valid pairs accumulate only after the assembler fix ships, so no
instrument can be authorized sooner than ~20 trading days after slice 2 lands.

### 4.7 What continuity delivers before any authorization exists

Measured against today's storm: four of the five blips (12/12 minutes) would have been survived
on the same run; the 15:01:33 blip lost one 5-second bar and would still have ended the three
runs, at the 15:02 rollover, refused with the grant's typed reason (`SUBSTITUTION_NOT_AUTHORIZED`; "source minute unresolvable" names the condition, not an emitted code — ruling P8) instead of `FEED_DEATH` on
a healthy socket. Nightly 1100 resets and the gateway logoff outside the decision session no
longer kill RTH runs. Every damaged minute is eliminated from the ledger from day one. With an
authorization, a storm day is survived up to its first lost 5-second bar plus one more, and the
run then ends at the session close; a second loss still ends the run, because nothing has
proven that shape, and no substituted run carries into the next session.

### 4.8 Cross-session continuation after a substitution — rejected in v1

A substitute is a different observation injected into a running program's persistent state.
For the recursive programs (EMA, Wilder RSI, SMA, MACD, Supertrend) that state has no
forgetting horizon, the evaluation trace does not serialize it, and nothing in the engine can
digest it. Therefore, in v1:

(i) **The run ends at its decision session's close** (§4.4). Its within-session decisions after
the grant are the shape the artifact proved — one episode, trace and intent parity through the
session close — and nothing after that is proven.

(ii) **Continuation is only by run-boundary admission**, where the warmup source is a declared
policy rather than an in-flight state change. Two policies are possible; v1 proposes **B**:
- **A — Resume replays the retained ledger including the provenance-marked substitute**, and
  declares it in a new `RetainedSubstituteAdmissionFact` (bar identities, `authorization_id`s)
  next to `ResumeCheckpointAdmissionFact`. Structurally the same class as today's
  Resume-after-crash, whose replay contains the crash gap; substantively it replays one different
  observation among thousands and is therefore a behavioral, not strict, admission. Allowed only
  behind an operator flag (ADR 0023 shape).
- **B — Resume is refused while the retained replay contains a substitute** (rule 3's fact,
  surfaced as a Resume admission refusal). Continuation is a fresh instance whose warmup is the
  existing all-historical admission policy, or a reviewed ledger rollover (#1740's concept, no
  tooling yet) that archives the substituted stream before a Resume. No unproven state anywhere.

(iii) **What lifts the stop**, per program, encoded as `proven_shape.cross_session`:
- `"session_reset"` — the sealed program attests a complete session reset and the producer
  proves it without a digest: over every paired session, a program instance created fresh at
  the session open yields traces and intents identical to the continuously running instance.
  `spy_vwap_reversion` and `deployment_validation` are candidates; the recursive programs are not.
- `"terminal_state_digest"` — the counterfactual ends with a canonical program-state digest
  identical to the baseline's, covering every indicator's internals (RSI's smoothed gain and
  loss, EMA seeds, Supertrend bands) and lifecycle state. This needs a `state_digest()` contract
  on indicators and programs that does not exist. Because the program files are the sealed
  identity (`registry.artifact_paths`), adding it re-seals and re-qualifies every program — a
  project of its own (§7.7), not a slice of this design.

(iv) **Retrospective replay** (§4.6) remains monitoring evidence and revocation input only.

## 5. Decisions

- **D1 Same-run continuity inside the feed, policy authored by the consumer.** Auto-Resume via
  `recovery_callbacks` stays rejected.
- **D2 Deadline = next trigger + operational delivery allowance (20 s); decision lateness = 0.**
- **D3 Substitution is fail-closed and authority equals proof.** Outside the decision session a
  minute is always a surfaced gap. Inside it, an unresolvable minute is substituted only under a
  per-window grant that requires a valid as-of artifact keyed to provider contract, instrument,
  sealed program hash and decision session; the artifact's proven shape — **one contiguous
  episode of at most 5 minutes per decision session, warmup free of substitutes** — is enforced
  before any fetch; anything else is `FEED_DEATH` with the typed reason. The artifact is earned
  by ≥ 20 paired sessions of **exhaustive settlement-tree** trace/intent parity through the
  session close, failing closed when the tree exceeds its bound, renewed nightly, revoked on the
  first divergence, enabled the first time by an operator flag. **A grant taints the run for its
  remaining lifetime with no expiry; the run ends at its decision session's close; no
  substituted run decides in a later session** (§4.8). Retrospective replay is never authority.
- **D4 Generation fencing on client, lease, registry, cancel and acquisition.**
- **D5 1100 soft loss is survivable** under the same deadline.
- **D6 Evidence is an explicit channel.** Provenance, `authorization_id` and
  `continuity_event_ref` on `MarketDataBar` and the ledger; every continuity fact is a
  `FeedContinuityEvent` awaited through the run's sink; bars and events share one evidence
  journal with `run_id`; the replay receipt commits to `continuity_event_digest` and
  `evidence_end_seq`; an unwritable event is fatal.
- **D7 Legacy unsealed bindings: fail-fast.**
- **D8 Kill switch** `IBKR_FEED_CONTINUITY_ENABLED=false` makes the feed ignore any policy.
- **D9 ADR** "Feed continuity: same-run recovery under the consumer's decision clock; historical
  substitution only under an as-of, program-keyed, shape-bounded, single-session authorization"
  records D1–D6 and D10.
- **D10 Cross-session continuation after a substitution is by boundary admission only**, policy
  B by default (fresh instance or reviewed rollover), policy A only behind an operator flag;
  the stop is lifted per program only by a proven session reset or a terminal-state digest proof.

## 6. Error handling matrix

| Event | Feed | Bot layer | Run |
|---|---|---|---|
| Interruption recovered before `T + A`, every affected minute complete by count | resume | `recovered` event | running |
| Unresolvable minute outside the decision session | omit, continue | `gap` event | running |
| Unresolvable minute inside the decision session, grant refused `SUBSTITUTION_NOT_AUTHORIZED` | raise | `refused` event | `FEED_DEATH` |
| … grant refused `SUBSTITUTION_SHAPE_UNPROVEN` (window > 5 min or second episode this session) | raise | `refused` | `FEED_DEATH` |
| … grant refused `SUBSTITUTION_WARMUP_TAINTED` | raise | `refused` | `FEED_DEATH` |
| … granted, substitute delivered in time | substitute | `substituted` event with `authorization_id` | running |
| Decision session closes on a day with a grant | — | finalize after the closing decision settles | `STOPPED / SUBSTITUTION_CARRYOVER_UNPROVEN` |
| Resume attempted onto a ledger retaining a substitute (policy B) | — | admission refusal `RETAINED_SUBSTITUTE_REPLAY_UNPROVEN` | not started |
| Trigger bar (non-realtime provenance) delivered after `T + A` | — | `DECISION_LATE` | `FEED_DEATH` |
| Not recovered by `T + A` | `DECISION_BAR_MISSED` | `refused` | `FEED_DEATH` |
| Granted fetch fails / times out / wrong window | `BACKFILL_FAILED` | `refused` | `FEED_DEATH` |
| Vendor has no bar for a granted window | ordinary gap | — | running |
| `record_event` sink raises (journal or event write fails) | `CONTINUITY_EVIDENCE_UNWRITABLE` | terminal path records the write failure | `FEED_DEATH` |
| Invariant violation | raise as today | — | `FEED_DEATH` |
| `continuity=None` | today's fail-fast | — | as today |
| Pause / Stop | streams / cancels | observe-only / — | running / `STOPPED` |

## 7. Adjacent findings — separate issues

1. The monitor converts loop stalls into forced disconnects (4 s probe vs 60 s keep-alive and
   stall detector). This design makes bots survive it regardless.
2. Root cause of the process stalls (§2): loop-lag heartbeat + `cpu.stat nr_throttled` sampling.
3. Client-id 42 collision / 38-second session.
4. `_BarDeliveryLogger.maybe_log_no_bar` logs 30 s after every subscribe regardless of delivery.
5. Universal decision-timeliness guard (§9 Q5).
6. Fresh-deploy warmup source (historical) vs live (real-time) is an unproven mixed-source shape
   today; the producer's counterfactual harness could prove or refute it per program.
7. A canonical program-state digest contract (`state_digest()` on indicators and programs), which
   re-seals and re-qualifies every program — the prerequisite for `cross_session:
   "terminal_state_digest"` (§4.8 (iii)).
8. Session-reset attestation and its digest-free proof for `spy_vwap_reversion` and
   `deployment_validation` (§4.8 (iii)).
9. Ledger rollover tooling (#1740's concept) so policy B has a path that keeps the instance.

## 8. Testing plan

Feed unit (`tests/marketdata/test_feed.py`): interruption with 12/12 → `realtime_across_reconnect`,
no grant requested, no fetch; 11/12 outside the decision session → omitted with a `gap`, no grant
requested; 11/12 inside it with a refusing grant → the refusal's reason raised, no fetch; with a
grant → one fetch, `historical_substitute`, delivered before the next live minute; wholly missed
minute; vendor returns no bar → gap; deadline → `DECISION_BAR_MISSED` naming `T`; fetch
failure/timeout/wrong window; 1100 set then cleared / still set at deadline; two consumers with
staggered starts sharing one fetch per granted window; `continuity=None` unchanged; stall enters
the same path.

`bars.py`: generation on lease; stale lease raises on the next iteration with the socket back;
stale release never sends a cancel; reconnect during `acquire`'s await restarts under the new
generation; old-generation entries evicted.

Predicate, pure: `next_trigger_ms` across bucket edges, session close, early close, DST day,
next-session rollover, `TF = 60_000`, rth vs all; shared floor equals the consolidator's floor.

Grant function: no artifact → `NOT_AUTHORIZED`; expired / revoked / key mismatch (different
seal hash, instrument, session) → `NOT_AUTHORIZED`; artifact produced later today does not grant
a window timestamped earlier today (as-of); 5-minute window granted, 6-minute refused
`SHAPE_UNPROVEN`; second window on the same trading date refused `SHAPE_UNPROVEN`, first window
on the next trading date granted; any `historical_substitute` row in the retained replay refuses
`WARMUP_TAINTED`, a ledger with none grants; the proven-shape numbers come from the artifact
(an artifact declaring `max_contiguous_minutes: 3` refuses a 4-minute window).

Authorization producer: a substituted non-last minute of a bucket with different open/high/low
and an unchanged consolidated close → every branch identical → pass for a close-only program;
a substituted last minute whose close differs by one cent → trace root differs on the first
bar after it (exact EMA/RSI values are in the trace) → fail, with or without a crossing;
`deployment_validation` with a differing bucket open → fail; a constructed pair that matches on
the all-COMMIT trajectory and diverges only after a `DISCARD` at the first staged ENTER (both
sides flat, different countdown) → fail — the case the old gate missed; exactly one side staging
an intent → immediate fail; a session whose staged intents exceed the branch bound → authorization
fails closed and the artifact records `BRANCH_BOUND_EXCEEDED`, never a sampled pass; the
all-substituted counterfactual diverging alone does **not** block authorization but is recorded;
renewal appends a session and extends expiry; a re-seal resets the count; the artifact carries
the proven shape and settlement-proof fields it was produced for.

Session-end stop: a run granted at 15:02 makes its 15:00–15:15 and later decisions, settles the
16:00 forced flush, and is finalized `STOPPED / SUBSTITUTION_CARRYOVER_UNPROVEN` before any
next-session bar is delivered; the same for an all-session program at its session close; a run
with no grant is not stopped. Boundary admission: Resume onto a ledger with a substitute is
refused under policy B and admitted with a `RetainedSubstituteAdmissionFact` under policy A only
when the operator flag is present; an artifact declaring `cross_session: "none"` never lifts the
stop even when otherwise valid.

Evidence channel: with a recording sink, an interruption produces `interruption` then
`recovered` events in that order **before** the first post-recovery bar is yielded; a gap
produces its event before the next bar; a substitute's bar carries the `continuity_event_ref`
and `authorization_id` of the `substituted` event that preceded it; a refusal produces its
`refused` event before the error is raised; a sink that raises makes the feed raise
`CONTINUITY_EVIDENCE_UNWRITABLE` without yielding the pending bar; with `continuity=None` no
event is ever emitted. Ledger: bar and event appends interleave in `evidence_seq` order within
one run; a pre-channel ledger migrates with one `bar` journal row per retained bar and
`run_id` NULL; `append_event` and `append` roll back together on failure. Receipt:
`continuity_event_digest` is stable across regeneration, changes when an event is added, and
`evidence_end_seq` bounds a `refused` event recorded after the final bar; a receipt without the
new fields still verifies its `bar_set_digest`.

Bot layer: refuses a late non-realtime trigger bar, admits a late non-trigger bar; ledger
provenance migration on a pre-existing file; `bar_set_digest` unchanged for all-realtime streams;
`source_stream_events` rows including `refused` with each reason; replay proof aligns with a
substituted stream and shows the `authorization_id`.

Lifecycle (`_FakeFeed` mode `interrupt`): run survives a count-complete interruption; each
refusal reason records `FEED_DEATH`; Pause during a wait keeps the run; Stop stops it.

Manual proof before merge (paper IBKR, script under `scripts/`): three symbols, two decision
clocks, consumers started at staggered minutes, five forced disconnects in four minutes, one
interruption straddling a trigger, one across the 16:00 close, one during a consumer's
acquisition, one with the historical endpoint stubbed slow, one with a lost 5-second bar and no
authorization (expect the typed refusal), then the same with a fixture authorization, then a
second lost bar the same day (expect `SHAPE_UNPROVEN`); assert run identities, events, 12/12 or
refused/substituted for every affected minute, and a passing replay proof afterwards.

## 9. Questions for the reviewer

1. **Authorization parameters.** 20 paired sessions, daily renewal, 1-day expiry, proven shape
   `{1 episode, ≤ 5 minutes, taint-free warmup, cross_session none}`, settlement-tree bounds
   (256 branches per counterfactual, 500 000 replays per session), operator flag on first
   enablement. Tighten any?
2. **Reading of the within-session envelope.** After a grant, the rest of the session's decisions
   are covered by the trace-parity counterfactual (one episode, through the session close). Is
   that acceptable as-is, or must recursive programs be refused any substitution until the
   terminal-state contract exists?
7. **Boundary policy.** B (fresh instance or reviewed rollover; no unproven state) by default,
   A (Resume replays the declared substitute) behind an operator flag. Keep A at all?
3. **Stall detection time.** Under rule 8 a 60 s stall overlapping a trigger is refused where
   today it is survived with a damaged minute. Keep 60 s, or lower it for bot subscriptions?
4. **Deploy mid-minute.** Rule 6 makes the first live minute unresolvable; without a grant an RTH
   deploy would be refused unless it starts on a minute boundary. Proposed: the feed defers a new
   consumer's first emitted minute to the next boundary, leaving the warmup→live seam as today.
5. **Universal timeliness guard.** Extending the bot-layer check to every trigger bar would also
   refuse decisions delayed by process stalls. Proposed: not in this change.
6. **Long outages for RTH programs.** The overnight gateway logoff no longer violates the RTH
   decision clock; pre-market minutes are gaps. Acceptable, or should a maximum interruption
   still bound it?

## 10. Implementation slices

1. `connection_generation`; lease/registry/cancel/acquisition fencing; tests. Safe alone.
2. `MinuteAssembler` extraction; completeness by count; interruption flush; tests.
3. `ContinuityPolicy` with a refuse-everything default grant and the awaited `record_event`
   sink, `FeedContinuityEvent`, `MarketDataBar.provenance`/`authorization_id`/
   `continuity_event_ref`, the feed's interruption loop **without any substitution path**;
   bot-layer trigger function, admission on delivery, the run-scoped sink closure, ledger
   migration (provenance and event columns, `source_stream_events`, the evidence journal),
   receipt `continuity_event_digest`/`evidence_end_seq`; kill switch; tests. This slice delivers
   §4.7's floor.
4. Authorization producer: paired-session capture, the lockstep settlement-tree driver on the
   `SignalSession` protocol with explicit bounds, artifact with proven shape and settlement-proof
   fields, nightly renewal/revocation, operator flag, reference doc. Runs for ≥ 20 sessions
   before anything can be authorized.
5. Grant function (as-of, shape, episode, taint) and the feed's substitution path; single-flight;
   tests.
6. ADR, operator-manual line (regenerated), `docs/references/` notes.
7. New issues: §7.1, §7.2, §7.5, §7.6.
