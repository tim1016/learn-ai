# Feed reconnect continuity (#1921)

**Spec:** `docs/superpowers/specs/2026-09-02-feed-reconnect-continuity-design.md` (revision 7)
**Decision:** `docs/architecture/adrs/0053-feed-continuity-same-run-recovery.md`
**Status:** fail-closed floor shipped (spec slices 1–3). No substitution path exists in this build.

Every number below carries the spec section it comes from. Nothing here is re-derived.

## Retrieving the evidence

Podman's log driver is journald inside the podman-machine VM, so container logs survive
`./restart.sh` (spec §1). Retained since 2026-08-09.

```
podman machine ssh 'journalctl -o short-iso --utc CONTAINER_NAME=polygon-data-service --since "<VM-local CDT time>"'
```

`--since` and `--until` are parsed in the **VM's local time** (CDT) even with `--utc`, which only
changes the output format. Asking for a UTC window returns nothing, silently.

Podman healthcheck failures are VM-level lines, not container lines, so drop the `CONTAINER_NAME`
filter to see them: `podman[pid]: Error: healthcheck command exceeded timeout of 5s` followed by
`<ctr-id>-<hex>.service: Main process exited, code=exited, status=125`. Counting those per hour is
a process-stall detector independent of the app's own logging, because `/health` is a trivial dict
return — a 5 s timeout on it means the event loop or the cgroup stalled. Log-silence gaps are not
a stall signal: the frontend panel poll gives a ~4.6 s cadence and 10 s gaps occur in healthy
baselines.

## The 2026-09-02 15:00 ET event (spec §1.1)

| UTC | ET | Event | Source |
|---|---|---|---|
| 18:59:59.7 | 14:59:59 | "reqRealTimeBars has not delivered 5-second bars", all six lines at once | `bars._BarDeliveryLogger` |
| 19:00:19 | 15:00:19 | podman healthcheck `curl localhost:8000/health` exceeded 5 s — first of **17 in six minutes** | VM journal |
| 19:00:37.8 | 15:00:37 | Alpaca `/v2/clock` timed out; new exposure blocked | `alpaca.market_liveness` |
| 19:00:44.828 | 15:00:44 | Monitor: "IBKR app-level probe failed; forcing reconnect" → `client.disconnect()` | `auto_reconnect_monitor._probe_if_due` |
| 19:00:44.842 / .869 / .884 | 15:00:44 | Three bots `FEED_DEATH` | `bot_runner._supervise` |
| 19:00:45.162 → .605 | 15:00:45 | TCP connected; recovery complete (777 ms) | monitor |
| 19:01:33.6, 19:02:51.3, 19:04:08.6, 19:04:47.0 | 15:01–15:04 | Four more probe-forced reconnects, 442–938 ms each | monitor |

**Classification: a data-plane process stall, not a network event** (spec §1.1). `/health` returns
a dict; seventeen 5-second timeouts on it while no other container's healthcheck failed means the
process stalled. The monitor's probe is `asyncio.wait_for(reqCurrentTimeAsync(), 4.0)` every 30 s,
so a stall longer than 4 s makes the timeout fire when the loop unblocks, and the monitor then
tears down a **healthy** socket. Reconnect finishes in under a second because the link was never
down. Five teardowns in four minutes, 0.4–1.0 s each.

Bots died within 100 ms because `_check_realtime_subscription_liveness`
(`PythonDataService/app/broker/ibkr/bars.py`) raises on `not client.is_connected()` or
`client.connection_lost`, and that is the one branch `IbkrMarketDataFeed.stream_bars` did not
retry (spec §1.2).

A resubscribe also damaged the minute stream: the open-minute accumulator was a generator local,
so the 15:00, 15:02 and 15:04 minutes were rebuilt from **three** contributions each (TSLA 15:00
volume 7 157 against 32 618) although all twelve raw 5-second bars had arrived across the two
sockets, and `SignalSession.advance` checks only width and monotonicity, so the damaged minute
passed as a decision input (spec §1.3).

## Frequency (spec §2)

Journal window 2026-08-09 → 2026-09-02: **88 mid-process episodes, 11 inside RTH, 12 bot
feed-deaths.** Two probe-forced stall storms dominate — 45 reconnects on 08-27 15:54–16:43 and
5 on 09-02 15:00–15:05. Healthcheck-timeout clusters by VM-local hour: 08-25 08–09h (37),
08-26 09h (22), 08-27 14–15h (170), 09-01 15h (7), 09-02 08h (22), 09-02 14h (24). The root cause
of the stalls is open and belongs to its own issue (spec §2, §7.2); the continuity design does not
depend on the answer.

## Measurements

### 5-second contiguity, 2026-09-02 (spec §3.1)

Source: `artifacts/live_bars/<sym>/5s/2026-09-02.jsonl`.

| Symbol | RTH minutes observed | 12/12 contributions | Exceptions |
|---|---|---|---|
| SPY | 340 | 338 | 15:01 (11 — one bar lost in the 15:01:33 reconnect), 15:31 (7 — stream ended by the container restart) |
| TSLA | 340 | 338 | same two |
| AAPL | 340 | 339 | 15:31 (7) |

**1017 of 1020 RTH minutes were 12/12**, with no whole minute absent and zero duplicate
timestamps. This is the measurement the completeness rule rests on: an RTH minute's completeness
is provable by contribution count, and four of the five blip minutes that day were complete from
real-time data across two sockets.

### What the registered programs read from a consolidated bar (spec §3.2)

Close-only: `ema_crossover_signal`, `rsi_mean_reversion`, `sma_crossover`, `spy_strategy_a`.
`deployment_validation` also reads open (`close > open`); `spy_strategy_b` reads high and low
(Supertrend on the whole bar); `spy_vwap_reversion` reads high, low and volume. So a substituted
*first* minute of a bucket changes the consolidated open, and a substituted *extreme* minute
changes the consolidated high or low.

### Real-time-built minutes vs IBKR historical 1-minute bars, same day (spec §3.3)

| Symbol | Common minutes | close identical | Divergent minutes |
|---|---|---|---|
| SPY | 371 | 371 | 09:47 (partial first minute), 15:00, 15:01 (11), 15:02, 15:04 (rebuilt from 3 bars each), 15:32 (tail) |
| TSLA | 344 | **343** | as SPY, plus **14:14 close 352.27 vs 352.28 (12/12, clean)** and **14:23 open 352.82 vs 352.86 (12/12, clean)** |
| AAPL | 339 | 339 | 15:00, 15:01, 15:02, 15:04 |

**1053 of 1054 common minutes have an identical close.** Read it in three parts (spec §3.3):
the damaged minutes are the resubscribe artifacts the assembler fix removes; **even clean 12/12
minutes diverge occasionally**, which is IBKR's documented historical trade filtering, so the
historical record is a *different observation* of the same minute, not a corrected one; and a
per-field envelope says nothing about whether a program's decision changes. Those two clean TSLA
divergences are the measured reason substitution is fail-closed and why its gate would be
trace parity, never field parity.

## Vocabulary

### Reason codes

A continuity refusal always finalizes the run `FEED_DEATH`; the typed reason below is what says
*which rule* refused it. It survives in the run outcome's `crash_diagnostic.message` (prefixed as
`"<REASON>: …"` by `MarketDataFeedError`) and as a column on the `refused` row in
`source_stream_events`.

| Reason | Meaning |
|---|---|
| `DECISION_BAR_MISSED` | The socket was not healthy again by `next_trigger_ms(L) + delivery_allowance_ms`; the decision bar can no longer arrive in time. Raised from the wait (`app/marketdata/ibkr_continuity.py`). |
| `SUBSTITUTION_NOT_AUTHORIZED` | The grant author refused: no authorization artifact, or one that is expired, revoked, or keyed to a different provider contract / instrument / program seal / decision session. **Every** in-session unresolvable minute takes this path in this build, because the shipped author refuses unconditionally. |
| `SUBSTITUTION_PATH_UNAVAILABLE` | A `SubstitutionGrant` was issued and the feed refused it anyway, because no historical-substitution path exists in this build. Fail-closed backstop (ADR 0053 §4, ruling R3); logged at error level, never a fetch. |
| `SUBSTITUTION_SHAPE_UNPROVEN` | Declared on the port's `SubstitutionRefusal` for the window shape the authorization has not proved (longer than the proven maximum, or a second episode in the same decision session). **No code path produces it in this build**; it exists so the port type does not change when the authorization producer lands. |
| `SUBSTITUTION_WARMUP_TAINTED` | Declared for a run whose retained replay already contains a substitute. **No code path produces it in this build** (same reason as above). |
| `CONTINUITY_EVIDENCE_UNWRITABLE` | The run's `record_event` sink raised — the event or its journal row could not be made durable. Continuing without the promised evidence is forbidden, so the feed raises instead. |
| `DECISION_LATE` | A trigger bar with non-`realtime` provenance was delivered more than `delivery_allowance_ms` after its close. Refused at the consumer, not the feed (`_admit_on_delivery`, `app/services/bot_trade_strategy.py`). Decision lateness is zero. |
| `SOURCE_MINUTE_UNRESOLVABLE` | The **condition**, not a code: an interruption-spanning minute that no real-time data can prove complete (incomplete by count in RTH, or any interruption-spanning minute outside RTH). The spec uses the term in §4.7; the build reports the grant's refusal reason instead, so no literal by this name appears in the code. |

### Event kinds (`FeedContinuityEvent.kind`, `app/marketdata/feed.py`)

Every event is awaited into the ledger **before** the feed continues, so no bar precedes its own
evidence and a run that dies has already said why.

| Kind | Recorded when |
|---|---|
| `interruption` | Delivery stopped — socket down, 1100 soft loss, stall, or a fenced generation. Written before the feed enters the wait, carrying the anchored `deadline_ms` that wait will enforce. |
| `recovered` | Live delivery resumed on a new connection generation. Written before the first post-recovery bar is yielded; its ref is stamped on that bar. |
| `gap` | A window of minutes the live stream never delivered and nothing will fill — an unresolvable minute outside the decision session. Written before the bar that follows the omitted window. |
| `substituted` | A gap window was backfilled under a `SubstitutionGrant`. Unreachable in this build; the kind exists so the ledger schema and the receipt digest do not change when substitution lands. |
| `refused` | A continuity rule refused, with the typed reason above, its window and its deadline. Written before the terminal `MarketDataFeedError` is raised. |

### Provenance tags (`MarketDataBar.provenance`, and the `provenance` column on `source_bars`)

| Tag | Meaning |
|---|---|
| `realtime` | Assembled wholly inside one live connection. The default, and the only tag a pre-#1921 stream produced. |
| `realtime_across_reconnect` | Assembled from live contributions spanning a socket interruption, proven complete by contribution count. Every contribution is still a real print, so the bar is a decision input like any other and needs no grant. |
| `historical_substitute` | Backfilled from the broker's history endpoint to replace a window the live stream missed. Only ever delivered under an explicit `SubstitutionGrant`; **never produced in this build**. |
| `history` | Served by `recent_closed_bars` for warmup. Never itself a decision. |

`bar_set_digest` includes `provenance` only when it is not `realtime` (omit-when-default), so
existing receipts keep their digests and a substituted stream is correctly a different one.

## Where the pieces live

| Concern | File |
|---|---|
| Connection generation | `PythonDataService/app/broker/ibkr/client.py` |
| Lease / registry / cancel fencing | `PythonDataService/app/broker/ibkr/bars.py` |
| Minute assembler, completeness by count, post-flush absorb | `PythonDataService/app/broker/ibkr/minute_assembler.py` |
| Port types (`ContinuityPolicy`, `FeedContinuityEvent`, provenance) | `PythonDataService/app/marketdata/feed.py` |
| Interruption loop, wait, window resolution, event emission | `PythonDataService/app/marketdata/ibkr_feed.py`, `ibkr_continuity.py` |
| Decision clock (ET floor, trigger instants) | `PythonDataService/app/services/decision_clock.py` |
| Policy author, admission on delivery | `PythonDataService/app/services/feed_continuity_policy.py`, `bot_trade_strategy.py` |
| Provenance columns, `source_stream_events`, evidence journal | `PythonDataService/app/services/source_bar_ledger.py` |
| Receipt `continuity_event_digest` / `evidence_end_seq` | `PythonDataService/app/services/run_replay_proof.py`, `app/schemas/run_replay.py` |
| Kill switch `IBKR_FEED_CONTINUITY_ENABLED` | `PythonDataService/app/broker/ibkr/config.py` |

Tests: `tests/broker/ibkr/test_client_connection_generation.py`,
`tests/broker/ibkr/test_minute_assembler.py`, `tests/marketdata/test_feed_continuity.py`,
`tests/services/test_decision_clock.py`, `tests/services/test_feed_continuity_policy.py`,
`tests/services/test_source_bar_ledger.py` (all under `PythonDataService/`).
