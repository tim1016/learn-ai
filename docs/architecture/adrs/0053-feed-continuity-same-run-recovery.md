# ADR 0053 — Feed continuity is same-run recovery under the consumer's decision clock, and substitution is fail-closed

**Status:** Accepted 2026-09-02
**Provenance:** Decision ticket [#1921](https://github.com/tim1016/learn-ai/issues/1921). Full record: `docs/superpowers/specs/2026-09-02-feed-reconnect-continuity-design.md` (revision 7, produced by six review rounds; §0 lists every finding and what it changed). Field evidence and every measurement cited below: `docs/references/feed-reconnect-continuity.md`.
**Decision drivers:** On 2026-09-02 at 15:00 ET the data-plane *process* stalled — podman's `curl localhost:8000/health` healthcheck exceeded 5 s seventeen times in six minutes — so `auto_reconnect_monitor`'s 4-second `reqCurrentTimeAsync` probe timed out when the loop unblocked and tore down a socket that was never down, five times in four minutes (0.4–1.0 s each). Three bots recorded `FEED_DEATH` within 100 ms of the first teardown (spec §1.1, §1.2). Over the same day 5-second bars arrived 12/12 in 1015 of 1020 RTH minutes across SPY/TSLA/AAPL (spec §3.1: SPY 338, TSLA 338, AAPL 339 of 340 each), so most interrupted minutes are provably complete from real-time data and need no repair. IBKR's historical 1-minute record is a *different* observation of the same minute: close identical in 1053 of 1054 common minutes, with TSLA's 14:14 close and 14:23 open diverging inside clean 12/12 minutes (spec §3.3).
**Related:** ADR 0018 D5 (recovery reconciles, Resume is operator-only, 1100 ≠ socket-dead) — **untouched**: nothing here auto-Resumes a run, and a run that survives its own feed interruption never reaches Resume. ADR 0046 (`HARD_DOWN` is a breaker OPEN state) — **untouched**: the breaker still owns the connection, and continuity only decides whether one consumer's stream survives while it is closed. ADR 0049 (the lake is the market-data authority; the live feed is the execution-time observation stream) — a substitute would be a vendor history fetch inside the execution-time stream, which is why §4 refuses one. ADR 0023 (a human validation flag gates live behaviour) — the shape the substitution authorization would follow when it is built. Issue [#1921](https://github.com/tim1016/learn-ai/issues/1921).

## Context

Three facts, in the order they were established.

**1. The feed's one fatal branch fired on a healthy socket.** `_check_realtime_subscription_liveness` (`PythonDataService/app/broker/ibkr/bars.py`) raised `IBKRBarStreamError` on either `not client.is_connected()` or `client.connection_lost` (a 1100 soft loss). It was the single branch `IbkrMarketDataFeed.stream_bars` did not retry; it became `MarketDataFeedError` and `bot_runner` finalized the run `FEED_DEATH`. On 2026-09-02 that branch ended three runs on a link that reconnected in 777 ms. Twelve bot feed-deaths across 88 mid-process episodes in the 2026-08-09 → 2026-09-02 journal window (spec §2) all took this path.

**2. Resubscribing silently damages the minute stream.** The open-minute accumulator was a local of `stream_minute_bars`. When the generator raised, the partial minute was lost and the re-entered generator rebuilt the current minute from only the 5-second bars received after resubscribe — the 15:00, 15:02 and 15:04 minutes were assembled from three contributions each although all twelve raw bars had been received across the two sockets. `SignalSession.advance` checks only width and monotonicity, so a damaged mid-bucket minute passes as a decision input without complaint (spec §1.3). This predates #1921 and also affects the stall path and a deploy mid-minute.

**3. The registry could hand out a dead subscription.** `_RealtimeBarSubscriptionRegistry` keyed on `(id(client), conId, barSize, what, useRTH)`, the `IB()` instance is reused across reconnects, `Wrapper.reset()` orphans the old `bars` list, and `Client.reset()` sets `_reqIdSeq = 0` so reqIds are reissued. A re-subscriber could be handed a dead multiplexed list that degrades into a 60 s stall, and a stale lease's `cancelRealTimeBars` could cancel a *new* subscription that had reused the reqId (spec §1.4).

The design's stated non-goal: it does not depend on the root cause of the process stalls. That is open and belongs to a separate issue with an event-loop-lag heartbeat first (spec §2, §7.2).

## Decision

### 1. Same-run continuity lives in the feed, under a policy the consumer authors (D1)

`ContinuityPolicy` (`PythonDataService/app/marketdata/feed.py`) is a broker-neutral contract the consumer hands to `stream_bars(symbol, *, use_rth, continuity=...)`. It carries the consumer's decision-session scope, its `next_trigger_ms` clock, its `substitution_grant` authority and its awaited `record_event` sink. `continuity=None` — the default — is the pre-#1921 fail-fast, unchanged.

The split is deliberate: the feed *evaluates* the deadline but does not author it, and it holds no authority state of its own. The bot layer authors the policy (`app/services/feed_continuity_policy.py`) because only it knows which minutes the run decides on and what its evidence may claim. The pushback carried through every review round stands: the feed must evaluate the deadline during the wait, or a run could be held through an arbitrary outage.

Auto-Resume via `recovery_callbacks` stays rejected. ADR 0018 D5 makes recovery a reconciliation and Resume an operator action; surviving an interruption inside one run is neither, so D5 is untouched rather than amended.

### 2. The deadline is the next trigger plus a 20-second delivery allowance, and decision lateness is zero (D2)

`ContinuityPolicy.deadline_ms(L) = next_trigger_ms(L) + delivery_allowance_ms`, allowance 20 000 ms. The allowance is operational — normal rollover (≤ 5 s) plus one substitute fetch (≤ 15 s) — not a tolerance for late decisions. It keeps its value in this build even though there is no fetch, so the number does not move when a substitution path lands.

The trigger set is calendar-backed (`app/services/decision_clock.py`): for decision session `S` and timeframe `TF`, a trigger is `bucket_end + 60 s` for every bucket, except the session's last bucket whose trigger is `session_close` (the forced flush); after a session's last trigger the next is the first bucket trigger of the next trading day, rolling across weekends and holidays.

Decision lateness is zero at the consumer. `_admit_on_delivery` (`app/services/bot_trade_strategy.py`) refuses a bar with non-`realtime` provenance whose `end_ms` is a trigger instant when `now_ms > end_ms + delivery_allowance_ms`, with reason `DECISION_LATE`. Bars produced wholly inside one live connection are never late by construction and are not checked; a bar the consumer does not decide on cannot be a late decision. Extending the check to *every* trigger bar would also catch decisions delayed by process stalls; that is deliberately out of scope (spec §7.5).

### 3. Completeness is a contribution count; an unresolvable minute is refused inside the decision session and surfaced as a gap outside it (D3, first half)

In RTH a minute is complete iff it holds `60 s / 5 s = 12` contributions (`RTH_CONTRIBUTIONS_PER_MINUTE`, `app/broker/ibkr/minute_assembler.py`), the property measured at 1015/1020 RTH minutes on 2026-09-02. A complete minute that spans an interruption is delivered with provenance `realtime_across_reconnect` and needs no grant — every contribution is still a real print, received over two sockets.

A minute that is incomplete, or wholly missed, is unresolvable from real-time data. Outside RTH the count test is undefined (sparse bars are normal), so every minute an interruption touched is unresolvable there. Which minutes an interruption *touched* is §18.

The order of the two branches is load-bearing (`resolve_unresolvable_window`, `app/marketdata/ibkr_continuity.py`): **outside** the consumer's decision session the minute is *always* omitted and surfaced as a `gap` event, and the substitution authority is never consulted — an authorization is keyed to a decision session and proves nothing about a minute outside it. **Inside** the decision session the window is offered to the consumer's grant function, and a refusal is terminal.

Nothing is interpolated, forward-filled or reordered. An omitted minute is a hole the ledger shows, not a repair.

### 4. Substitution is fail-closed and authority equals proof; this build has no substitution path at all (D3, second half; ruling R3)

The default is unauthorized, for every instrument and every program. The bot layer's grant author refuses every window with `SUBSTITUTION_NOT_AUTHORIZED` (`_refuse_every_substitution`, `app/services/feed_continuity_policy.py`), and the feed refuses a `SubstitutionGrant` outright with `SUBSTITUTION_PATH_UNAVAILABLE` rather than fetching anything — **no historical bar is ever fetched to fill a live gap in this build** (ruling R3). The refusal is logged and journalled before the run is finalized.

The authorization design is recorded, not implemented. It is keyed to `(provider_contract, instrument, configured_signal_hash, decision_session)`; earned by ≥ 20 consecutive paired sessions of **exhaustive settlement-tree** trace-and-intent parity through the session close, failing closed when the branch tree exceeds its bound; encodes a proven shape of **one contiguous episode of at most 5 minutes per decision session with a substitute-free warmup**, enforced before any fetch; renewed nightly, revoked on the first divergence, enabled the first time by an operator flag in ADR 0023's shape. Retrospective replay is monitoring and revocation input, never admission authority. Spec §4.6 and §4.8 are the full statement; §3.3's two clean TSLA divergences are why the gate is trace parity and never a per-field envelope.

### 5. Generation fencing on client, lease, registry, cancel and acquisition (D4)

`IbkrClient.connection_generation` (`app/broker/ibkr/client.py`) increments on every successful `connect()`. Each `_RealtimeBarLease` stores its generation, the registry key includes it, and the liveness check runs on **every** loop iteration and raises when the lease is stale — so a fast disconnect/reconnect can no longer degrade into a 60 s stall behind an `is_connected()` that is already true. `release`/`invalidate` of a stale-generation lease evict the entry and never call `cancelRealTimeBars`, so a reissued reqId cannot be cancelled out from under its new owner. `acquire` re-reads the generation after every await and restarts when it moved.

### 6. A 1100 soft loss is survivable under the same deadline (D5)

`connection_lost` no longer means the run is over. It enters the same wait as a socket-down interruption and is bounded by the same deadline, consistent with ADR 0018 D5's "1100 ≠ socket-dead". A stall (`IBKRBarSubscriptionStalled`) enters the same choreography; its 60 s detection time counts against the deadline.

### 7. Continuity evidence is an explicit, awaited, run-scoped, causally ordered channel (D6)

Every continuity fact is a broker-neutral `FeedContinuityEvent` emitted through `policy.record_event` and **awaited before the feed continues**: `interruption` before entering the wait, `recovered` before the first post-recovery bar, `gap` before the bar that follows an omitted window, `substituted` before a substitute bar, `refused` before the terminal error is raised. No bar precedes its own evidence, and a run that dies has already said why. If the sink raises, the feed raises `CONTINUITY_EVIDENCE_UNWRITABLE` — continuing without the promised evidence is forbidden.

Persistence is one SQLite transaction in `SourceBarLedger` (`app/services/source_bar_ledger.py`): the event row into `source_stream_events` **and** a row into the append-only `source_evidence_journal`, whose single monotonic `evidence_seq` also indexes every bar append, so bars and events share one causal order and every row names its `run_id`. Bars carry `provenance`, `authorization_id` and `continuity_event_ref` as stored columns, never inferred. `bar_set_digest` includes `provenance` only when it is not `realtime`, so existing digests are unchanged and a substituted stream is correctly a different digest. `RunReplayReceipt` (`app/schemas/run_replay.py`) gains `continuity_event_digest` and `evidence_end_seq`, which bounds bars **and** events — a `refused` event recorded after the final bar is inside the bound. `bar_set_digest` and `ledger_end_seq` are preserved.

### 8. A binding that cannot be described truthfully gets no policy (D7)

A legacy unsealed binding has no attested decision clock, so it keeps today's fail-fast rather than being scheduled against a guessed one. `continuity_policy_for` returns `None` and logs why.

### 9. A kill switch that ignores any policy (D8)

`feed_continuity_enabled` (`app/broker/ibkr/config.py`, env `IBKR_FEED_CONTINUITY_ENABLED=false`) makes the feed ignore a policy it was handed and fail fast as before. The switch is the rollback path for a behaviour that changes when a live run dies.

### 10. Cross-session continuation after a substitution is by boundary admission only (D10)

A substitute is a different observation injected into a running program's persistent state, and for the recursive programs (EMA, Wilder RSI, SMA, MACD, Supertrend) that state has no forgetting horizon, the evaluation trace does not serialize it, and nothing in the engine can digest it. So a substituted run ends at its decision session's close, and continuation is only through a run-boundary admission whose warmup policy is declared: policy **B** by default (Resume refused while the retained replay holds a substitute; continue as a fresh instance or after a reviewed ledger rollover), policy **A** (Resume replays the declared substitute) only behind an operator flag. The stop is lifted per program only by a proven complete session reset or by a terminal-state digest proof that does not exist yet (spec §4.8, §7.7).

This is unreachable in this build, since nothing substitutes. It is recorded now so a substitution path cannot land without it.

### 11. The ADR carries the standing decisions; the spec carries the derivation (D9)

This document is the standing record of every decision above — D1–D10 and the implementation rulings it records (R1–R3, P3, P5–P7, and P9–P12 in §18). That is wider than the list D9 itself enumerated, because D7–D9 and every ruling post-date it: D9 asked for a standing record and this is what the record turned out to have to hold. The spec remains the full *design* record: the review rounds, the measurements, the predicate table, the authorization producer and the testing plan. A widening of any decision here is argued in the spec first.

The title also differs from the one D9 proposed. D9's title named the substitution authorization ("only under an as-of, program-keyed, shape-bounded, single-session authorization"), and no such authorization — and no substitution path at all — exists in this build. A title claiming one would misdescribe what was accepted, so it says fail-closed instead, and §4 carries the authorization design as recorded-not-implemented.

### 12. `decision_session="all"` gets no continuity policy (ruling R1)

The canonical trading calendar proves the regular session only; extended windows are broker-proven capabilities owned by `session_authority`, not by the calendar. A binding with `use_rth=False` therefore has no calendar-proven trigger set, so it gets no policy and keeps today's fail-fast; `next_trigger_ms` raises `NotImplementedError` for `"all"` rather than inventing a schedule. Supporting extended-hours decision clocks is a separate piece of work with its own authority question.

### 13. The first minute of a deploy mid-minute keeps today's behavior (ruling R2)

A consumer that starts mid-minute produces an incomplete first minute by construction. That minute is emitted with provenance `realtime`, exactly as today. Only minutes that span an interruption, or fall inside an interruption window, are subject to the unresolvable rule. The spec's alternative — deferring a new consumer's first emitted minute to the next boundary (§9 Q4) — is a change to the warmup-to-live seam and is not made here.

### 14. An emitted minute is never rebuilt (ruling P3)

On interruption the assembler flushes a count-complete open minute immediately. After that flush, a 5-second bar for the same minute is resolved by `_absorb_after_flush` (`app/broker/ibkr/minute_assembler.py`): an **exact** redelivery of a contribution the flushed minute already held is absorbed idempotently and counted, because it carries no new data; **any other** bar inside that minute is refused with an error. An emitted minute can be neither corrected nor rebuilt — downstream has already consumed it. This is the finite/live split of `.claude/rules/temporal-rigor.md` applied one level up: absorbing a redelivery is not repairing a feed.

### 15. Sealed program artifacts were not edited; the ET floor is a parity-tested duplicate (ruling P5)

`app/utils/timestamps.py` and `app/engine/consolidators/trade_bar_consolidator.py` are both listed in every program's `artifact_paths` in `app/engine/strategy/registry.py`. Editing either changes the running artifact digest, `prove_running_program_build` then finds no compatible golden-qualification receipt, and Start admission refuses every deploy until all programs are re-qualified. So neither was touched. The decision clock's ET-anchored floor is duplicated once in `app/services/decision_clock.py` under a provenance block naming the consolidator as the canonical implementation, with a load-bearing parity test (`tests/services/test_decision_clock.py::test_floor_to_period_ms_et_matches_the_consolidators_floor`) — the exception CLAUDE.md guiding philosophy #5 allows for a duplicate that exists for a real reason and names its canonical file.

### 16. Flush, anchor, record, yield, then wait — in that order (ruling P6)

On interruption the feed flushes the complete open minute *first*, anchors the deadline on the post-flush last-delivered bar, records the `interruption` event carrying that deadline, only then yields the flushed bar, and only then waits. Every wait for that interruption enforces the deadline the event recorded and never re-derives it from a moving watermark. Anchoring before the flush would hold the run to a deadline up to a whole decision interval short of the one the journal claims; re-deriving it during the wait would silently extend it past the one the journal claims. Both fail the same invariant: what the evidence says and what the run enforces must be the same number, and no bar may precede its evidence.

### 17. A continuity refusal is a `FEED_DEATH` whose typed reason is durable (ruling P7)

Every refusal finalizes the run through the existing terminal path with `reason_code=FEED_DEATH`, so the duty-outcome vocabulary is unchanged. The typed reason survives in two places: `MarketDataFeedError` prefixes it onto the message (`"<REASON>: …"`), which `capture_bot_crash_diagnostic` stores in the run outcome's `crash_diagnostic.message`; and the `refused` event in `source_stream_events` carries it as a first-class column with its window and deadline. A distinct duty-outcome code per continuity reason is a follow-up, not part of this change.

### 18. Interruption-touched-ness is a loop fact, the missed-window scan is post-recovery, and a refusal says how short the minute was (rulings P9, P11, P12)

Three corrections from the whole-branch review, all in the same place: what the feed may deliver after an interruption.

**Touched, not just spanning (P9).** A minute's `spans_interruption` flag is true only when its *stored* contributions came from more than one connection generation. An interruption that outlives the open minute — every 60 s stall, since the first post-reconnect source bar then lands in a later minute — leaves that minute holding one generation's contributions, so the flag is false and nothing on the bar records that it was cut short. The completeness rule of §3 therefore keys on a loop fact instead: `IbkrMarketDataFeed` records the open minute's start whenever the interruption flush cannot complete it, and any emitted bar with a recorded start is subject to the unresolvable rule whatever its generation set says. Ruling R2 is untouched — a deploy mid-minute records nothing, so its first short minute keeps today's behaviour.

**The missed-window scan is post-recovery, and windows coalesce (P11).** Resolving "everything between the last delivered bar and this one" as unresolvable runs only for the first emitted bar after a recorded interruption. Run on every bar it would make an ordinary RTH gap fatal, contradicting the port's standing promise (and spec §6) that ordinary gaps are silent. Contiguous unresolvable minutes are offered as one window `[start, end)` — one grant offer, one `gap`/`refused` event covering the whole episode — split only where the decision-session verdict changes across the range, so a window straddling the RTH open is a gap outside and a refusal inside. Offering an episode one minute at a time would defeat the authorization artifact's `max_contiguous_minutes` shape bound before it is even built.

**A refusal carries the count (P12).** `FeedContinuityEvent.contribution_count` is persisted as a `source_stream_events` column and covered by `continuity_event_digest`. A `gap` or `refused` event about an emitted-but-incomplete minute says how many contributions it actually held; a window nothing was ever assembled for carries `None`, because absent is a different fact from zero. Without it the journal could say a minute was unprovable but not how far short it fell — the difference between one lost 5-second print and a socket that returned with nothing.

**A queued print is a print (P10).** The liveness gate runs before the bar list is read, so an interruption used to surface with pre-disconnect bars still queued on the lease's list, and those were discarded. `ib_async`'s `Wrapper.reset()` orphans that list on reconnect, so it can only ever hold data the old socket really delivered; `stream_minute_bars` and `stream_raw_5s_bars` now drain it through the assembler before re-raising. That is the difference between a minute the reconnect can prove complete by count and one the run must refuse.

## Consequences

**What the floor delivers before any authorization exists.** Measured against the 2026-09-02 storm: four of the five blips touched only 12/12 minutes and would have been survived on the same run. The 15:01:33 blip lost one 5-second bar, so it would still have ended the three runs — but at the 15:02 rollover, with a typed refusal naming the unprovable minute, instead of `FEED_DEATH` on a healthy socket 100 ms after a teardown. The nightly 1100 reset and the 00:45 gateway logoff fall outside an RTH decision session, so they become recorded gaps rather than kills. And for a run that carries a continuity policy, a minute an interruption touched is never delivered short: the assembler stitches it across the reconnect when the contributions are all there, and the feed refuses it — fatally inside the decision session, as a recorded gap outside it — when they are not. The damaged mid-bucket minute of §1.3, which such a run used to decide on silently, can no longer reach a strategy. Ordinary gaps stay non-fatal, as the port has always promised.

That last improvement is scoped, not fleet-wide. The surviving `MinuteAssembler` is owned by the policy-carrying path; `_stream_bars_legacy` (`app/marketdata/ibkr_feed.py`) calls `stream_minute_bars` without one, and `bars.py` then constructs a fresh assembler per call, so a stall replacement on that path still rebuilds the current minute from only its post-replacement contributions. The populations this excludes are an unsealed binding (§8), a `use_rth=False` binding (§12), a binding whose seal attests no `decision_timeframe_ms` (the `no_decision_timeframe` refusal in `feed_continuity_policy.py`), a log-only-mode run (`bot_runtime._run_log_only_bot` passes no policy at all), and any run while the kill switch is off (§9) — all of which keep pre-#1921 behavior in full, damaged minute included.

**The stall is not fixed, and this design does not claim to fix it.** The monitor still converts a loop stall into a forced disconnect (a 4 s probe against a 60 s keep-alive), the root cause of the stalls is open, and the evidence sink writes SQLite on the event loop exactly as bar appends already do — the same stall exposure this change is built to survive. Those are spec §7.1 and §7.2, with their own issues.

**The cost of fail-closed is real and was chosen.** A single lost 5-second bar inside an RTH decision session still ends the run. Nothing in this build can substitute for it, and nothing will until the authorization producer has run for at least 20 paired sessions after the assembler fix ships — the pairs cannot even start accumulating before then. The alternative was to accept the vendor's historical minute as a stand-in, and §3.3's TSLA divergences inside clean 12/12 minutes are the measured reason not to: the historical record is a different observation, and a per-field envelope says nothing about whether a program's decision changes.

**Not decided here, deliberately.** The substitution authorization and its producer (spec §4.6, slices 4–5) are designed and unbuilt. A per-continuity-reason duty-outcome code is deferred (§17). Extended-hours decision clocks are deferred (§12). A universal decision-timeliness guard covering *every* trigger bar — which would also refuse decisions delayed by process stalls — is out of scope (spec §7.5). A canonical program-state digest, the prerequisite for lifting §10's cross-session stop on recursive programs, re-seals and re-qualifies every program and is a project of its own (spec §7.7).
