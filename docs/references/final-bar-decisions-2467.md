# Final-bar decisions: how many backtest trades, and how the backtest should model them (#2467)

**Status:** research note, 2026-09-29, revised the same day after an independent review. Code examined at `8e138f73` (master, after #2440 merged as `d4c521b2`). Parent #2439; owner decision #2431; live change #2440; live bug #2596.

## The answer

**First, a live-money bug this research found: live does not reliably refuse an entry decided on the last bar.** Nothing on the ENTER path compares the decision instant with the session close. The gate trusts the broker clock's OPEN flag, which is polled once a second and trusted for 5 s, and it drops the `next_close_ms` the clock reported. A 16:00 decision lands about 0.6 s after the close. Whenever the freshest clock reading was answered before 16:00:00, the ENTER passes, goes out as a market DAY order, and Alpaca fills it at the next open. That is filed as **#2596 (P1)**, and a fix is in progress. The code path is under [Other findings](#the-final-bar-enter-race-2596).

For the EMA crossover program that runs on the clerk lanes, **7.2% of backtest trades on SPY are decided on the session's final bar** (6 of 83 over 590 sessions: 5 entries, 1 exit). The paper-only 30–70 RSI variant has 9.7% (54 of 559: 36 entries, 18 exits). Deployment Validation has none, because it stops deciding 15 minutes before the close.

The backtest fills those decisions at the final minute's close. Live decides that bar just after the close. Once #2596 lands, live refuses a final-bar ENTER and sends a final-bar EXIT as an after-hours limit (#2440).

The entries matter most. Skipping the five final-bar entries removes **17.56 of the backtest's 50.03 points per share (35%)** for the sealed EMA settings. That rests on 5 trades, 4 of them winners. For the variant it is 49%, over 36 entries. The exits matter far less. The first price an after-close order can get is the open of the first minute that starts after the decision. It sits **3.74 bps** from the close on average (median 2.80). The next open sits **42 bps** away.

**Recommendation:** model the two kinds differently.

- **ENTER on the final bar: skip it.** This models live correctly only once #2596 lands. Until then, live sometimes fills such an entry at the next open.
- **EXIT on the final bar: fill at the after-hours price.** Use the open of the first minute that starts after the decision, floored at the limit live sends. If no minute reaches the limit before after-hours ends, the position stays open.
- **Do not fill at the next open.** It is about 11× further from what live gets for an exit, and it credits entries live is meant to refuse.

Two other findings:

1. **Bar grouping matches.** Given the same complete one-minute bars, the bot's decision seam and the backtest engine produce identical decision traces on every bar held, final bars included.
2. **A broken parity check.** The check built to show that has compared nothing since March 2026, so no receipt ever showed the match.

Follow-ups: [#2607](https://github.com/tim1016/learn-ai/issues/2607) (the backtest model), [#2608](https://github.com/tim1016/learn-ai/issues/2608) (the parity check), [#2609](https://github.com/tim1016/learn-ai/issues/2609) (dead machinery). The entry race is #2596.

## Which strategies run live, and how that was determined

"Runs live" means a strategy deployed on a clerk lane: Paper, Live, or Live Dry Run. Evidence:

| Strategy (program) | Settings | Where it ran | Evidence |
|---|---|---|---|
| `ema_crossover_signal`, SPY, 15 min | gap 0.20, gap_bps 0, RSI 50–70, 1 share | Paper: `pt-ema-spy-0916`, `pt-ema-spy-0918`, `paper-ema-spy-0923`, `paper-ema-spy-0924`. Live: `live-ema-spy-0917`, `-0923`, `-0924` | Paper clerk DB copy (`bot_config`, `runs`, `decision_receipts`); [the 0917 live audit](../audits/live-ema-spy-missed-entry-2026-09-17.md) |
| `ema_crossover_signal`, SPY, 15 min | gap 0, RSI 30–70 | Paper only: `pt-ema-q-0916` (one day) | Paper clerk DB copy |
| `deployment_validation`, SPY, 1 min | trade_symbol SPY | Paper: `pt-dv-spy-0916`, `paper-dv-spy-0928`. Live Dry Run: `live-dry-dv-spy-0928` | Paper clerk DB copy; the 2026-09-28 launch-run notes |

**The paper DB copy.** The paper clerk database copy (account `PA3KWXU1C4C3`) is a copy an earlier session made on 2026-09-28 at 17:37. I copied it again and opened it with `mode=ro&immutable=1`. The live lane's clerk DB lives in a podman volume, which this research may not touch. So the live-lane rows rest on the committed audit, the session notes, and the paper lane's matching settings. The other five registered programs (`sma_crossover`, `rsi_mean_reversion`, `spy_strategy_a/b/c`) have no deployment in any held record.

**Older paper ledgers.** Paper ledgers from the IBKR era (August to 2026-09-10) carry no strategy key. They are used below only as recorded IBKR bars.

## What each side does with the last bar

**Backtest** (Engine Lab, Strategy Lab, grid and walk-forward all default to `signal_bar_close`):

- The regular-session reader keeps minutes inside the canonical calendar's session window (`app/engine/data/lean_format.py:195-215`). A half-day's last minute is therefore 12:59.
- The 15:45–16:00 bucket fires only when a later minute arrives (`app/engine/consolidators/trade_bar_consolidator.py:95-107`). That minute is the next session's 09:30 bar.
- The order then fills at the signal bar's close, stamped at its end (`app/engine/execution/fill_model.py:113-124`; drain at `app/engine/engine.py:407-445`).
- Result: every final-bar ENTER and EXIT fills at the lake's 15:59-minute close.
- The LEAN-compatibility profile alone sets `fill_stale_signal_at_current_open`. That fills the same decision at the next session's first-minute open instead (`app/services/engine_backtest_service.py:358-374`; `app/lean_sidecar/cross_runner.py:353`).

**Live** (paper and real money):

- The runner scans each bar's close (`app/services/bot_trade_strategy.py:558-576`), so it decides the final bucket as soon as the close arrives (`app/services/decision_clock.py:3-9`). The held receipts time it: `paper-ema-spy-0924` recorded its 16:00 decision at **16:00:00.595**, and `paper-ema-spy-0923` at 16:00:05.4.
- **ENTER.** Only the market-liveness gate stands between a final-bar ENTER and the broker (`bot_trade_strategy.py:963-997`; Clerk recheck `app/broker/alpaca/clerk/sqlite/runtime.py:1118-1145`). That gate reads the broker clock only, so a pre-close OPEN reading lets the ENTER through (#2596). The ENTER leg is a market DAY order (`app/broker/alpaca/clerk/program_leg.py:527-528`). #2440's documentation says the gate refuses a closed-market ENTER ([alpaca-extended-hours.md, "The regular close"](alpaca-extended-hours.md#the-regular-close-an-exit-sent-after-the-session-it-was-decided-in-2440)). That holds only once #2596 lands.
- **EXIT.** The EXIT is exempt from both the liveness and lateness gates (`bot_trade_strategy.py:1133-1192`). #2440 sends it as an extended-hours DAY limit at `floor_tick(close × (1 − exit_bps/10⁴))` (`program_leg.py:537-575`, `app/broker/alpaca/marketable_limit.py:51-79`). The limit is anchored on the IBKR decision bar's close and lives until 20:00, or 17:00 on a half-day (`app/services/session_authority.py:172-197`).
- If the EXIT is unfilled, the operator gets `EXIT_NOT_FLAT`, and the next try is the 04:00 pre-market re-drive (owner decisions on #2440).

**Dry Run (`sim:`).** Dry Run fills a limit at the decision bar's close whenever that close is at or through the limit (`app/broker/alpaca/clerk/fill_models.py:47-61`). A final-bar EXIT therefore fills at the close there, as in the backtest. That is not what Paper or Live can get.

## Measurements

Everything below comes from `PythonDataService/scripts/measure_final_bar_decisions.py`. Its full output is committed as [final-bar-decisions-2467.json](final-bar-decisions-2467.json). "Points" means price points per share, summed over trades. Session closes and half-days come from `app/lean_sidecar/trading_calendar.py`. The symbol, the bucket width and the warmup lookback come from the measured strategies and their registered signal-program contracts.

**Window.** SPY, 2024-05-20 to 2026-09-25, from the lake's `polygon_split_adjusted` root. That is 590 sessions, and the calendar expects 590, so none are missing. Six are half-days: 2024-07-03, 2024-11-29, 2024-12-24, 2025-07-03, 2025-11-28 and 2025-12-24. Every zip was verified against its sidecar `file_sha256`. The Postgres catalog receipt was not checked, because the shared Postgres is off-limits to research.

### 1. Share of trades decided on the final bar

A trade counts as final-bar when its entry or exit fill is stamped at the canonical session close.

| Strategy settings | Trades | Final-bar ENTER | Final-bar EXIT | Trades touching the final bar | Share |
|---|---:|---:|---:|---:|---:|
| EMA, sealed default (gap 0.20, RSI 50–70) | 83 | 5 | 1 | 6 | **7.2%** |
| EMA, variant (gap 0, RSI 30–70) | 559 | 36 | 18 | 54 | **9.7%** |
| Deployment Validation (1 min) | 23,389 | 0 | 0 | 0 | **0%** |

None of the 60 final-bar trades fell on a half-day. The classifier would still catch one, because it uses the calendar's 13:00 close. Deployment Validation stops detecting 15 minutes before the calendar close (`app/engine/strategy/algorithms/deployment_validation.py:1-18`), so it can never decide on the final bar.

### 2. What each backtest model does to those trades

| Model | EMA sealed default: trades / points | EMA variant: trades / points |
|---|---|---|
| Close fill (today) | 83 / **+50.03** | 559 / **+62.24** |
| Live: final-bar ENTER skipped, EXIT at the close | 78 / **+32.47** | 523 / **+31.74** |
| &nbsp;&nbsp;plus EXIT at the after-hours price (the open of the first minute after the decision, floored at the limit) | +1.17 more (one exit) | +3.84 more at a 10–50 bps allowance (+3.87 at 5, +4.64 at 0) |
| &nbsp;&nbsp;plus EXIT at the limit itself (the `limit_touch_fill` rule) | −0.27 (5 bps) to −2.64 (50 bps) | −5.67 (5 bps) to −55.83 (50 bps) |
| Next open (the LEAN-compatibility path) | 83 / +49.88 | 559 / +60.05 |
| Skip both kinds (the EXIT retries on the next bar, 09:45 next day) | 78 / +48.28 | 520 / +52.43 |

**The skipped entries.** For the sealed default they are five trades. Four were winners, worth +1.71 to +8.65 points each, and one lost 1.87. Their removal is the whole gap between +50.03 and +32.47. A five-trade sample makes the 35% a measure of this window, not a stable rate.

**The one sealed exit.** The sealed default's only final-bar exit is 2025-04-22. SPY moved after the close that day: the first minute after the decision opened 22 bps above the close, and the next session opened 2.6% higher. That single trade is the whole +1.17. The variant's 18 exits average +4.4 bps at a 0 bps allowance (median +2.2).

**The next-open total is a coincidence.** It lands close to the close-fill total only by chance. Its five entries cost 13.64 points, and its one exit gained 13.49 from the same 2025-04-22 gap.

**The skip-both model** keeps the 2025-04-22 exit alive into the next morning, and that is why it scores higher.

**Unfilled exits.** None of the final-bar exits in the table went unfilled, at any allowance from 0 to 50 bps.

### 3. After-hours price against the next open, every session

589 sessions; the last session has no next open inside the window.

**Which prices an after-close order can get.** Live decides the final bar just after the close (16:00:00.595 in `paper-ema-spy-0924`), so its order exists only partway into the minute that starts at the close. That minute's open is the first print at or after 16:00:00.000, which comes before the order. So the model credits only minutes that start after the close. On an ordinary day that is 16:01 onwards.

| Price, against the final minute's close | Mean abs. diff. | Median abs. diff. | p95 abs. diff. | Max abs. diff. |
|---|---:|---:|---:|---:|
| **Open of the first minute after the decision** (the proxy) | **3.74 bps** | 2.80 bps | 10.59 bps | 33.88 bps |
| Close of the minute that starts at the close (16:00) | 3.64 bps | 2.72 bps | 10.41 bps | 33.74 bps |
| Next session's **open** | 42.28 bps | 28.10 bps | 119.85 bps | 400.39 bps |
| *Not reachable:* open of the 16:00 minute (prints before the order exists) | 0.42 bps | 0.30 bps | 1.04 bps | 27.98 bps |

**No drift.** The proxy's signed mean is +0.17 bps (median −0.17), so it moves the price by a few bps either way, not in one direction.

**The 16:00 minute's open is not established as the closing auction.** The minute bars cannot say which print it is. SPY's 16:00 minute has a median volume of 283K shares, against 1.76M for the 15:59 minute and 112K for the first minute after the decision. The model needs only the fact that the print comes before the order.

**Fill rate.** A sell limit at `marketable_limit_price(close, b)` was reached after the decision and before after-hours ended on:

- **571 of 589** sessions at b = 0 bps;
- **587 of 589** at 5 bps;
- **589 of 589** at 10, 25 and 50 bps.

**Half-days.** The lake holds no SPY minute between 13:01 and 15:59 on any of the six half-days. The first minute after a 13:00 decision is 16:00–16:02, and SPY printed in 25 to 49 minutes before the 17:00 end. The limit was reached on 2 of 6 half-days at 0 bps, 5 of 6 at 5 bps, and 6 of 6 at 10 bps. The lake cannot say whether nothing traded from 13:01 to 15:59, or the vendor's minute bars leave that window out.

**Which proxy to use.** The open of the first minute after the decision is the earliest price the order can be credited with, and every print in that minute comes after the order. The 16:00 minute's close is about as far from the close (3.64 bps), but that minute also holds prints from before the order. On a thin minute its close can be one of them. The next open is an order of magnitude further away.

### 4. The final minute: the lake against what live saw

The comparison uses held IBKR one-minute bars from 246 source-ledger copies (234 of them from the fleet-stress run) plus 17 recorder files, `live_bars/*/1m`. Together they cover 2026-08-25 to 2026-09-10. The lake side is the raw root, because live bars are unadjusted. When a live-observed copy of a minute is held, it is preferred over IBKR history.

| SPY | Compared | Equal | Mean abs. diff. | Max abs. diff. |
|---|---:|---:|---:|---:|
| Every regular-session minute close | 4,768 | 3,635 (76%) | 0.014 bps | 0.79 bps |
| Every 15-minute bucket close | 317 | 241 (76%) | — | — |
| **Final minute close (16:00 bar close)**, 12 sessions: 4 live-observed, 8 IBKR history | **12** | **1 (8%)** | **0.40 bps (≈3¢)** | **0.79 bps (6¢)** |
| Final 15-minute bucket, full OHLC | 12 | 0 | — | — |

**Mostly history, not live.** Only 4 of the 12 final minutes were observed live. The other 8 are IBKR history. The live four differ from the lake by 0.00, +0.03, +0.01 and +0.05 dollars.

**History can stand in for live.** Where both a live and a history copy of the same regular-session minute are held, 2,217 of 2,433 (91%) are identical in OHLCV.

**Other symbols.** AAPL matched on 2 of 4 final minutes, and TSLA on 1 of 2.

**The final minute is special.** It is where the lake and live disagree: 11 of 12 closes differ, against 24% of ordinary minutes. The size is small, a few cents, below one basis point. But it moves live's exit anchor and the last bucket's indicator inputs.

**Receipt replay.** The lake cannot reproduce live decisions digit for digit. Replaying the 85 held EMA receipts that carry a trace digest (paper, 2026-09-16 to 09-24, 5 on the final bar) from lake buckets matches **0 of 85 trace digests**. The decision kind matches for **82 of 84** ordinary receipts, all of which were NO_ACTION live. The two that differ are QQQ on 2026-09-16 at 14:00 and 15:15 ET: from the lake's bars the strategy would have staged an ENTER and then an EXIT that live's bars did not. The 85th receipt is a crash candidate and is not compared; a quarantine receipt carries no digest and is not replayed. The EMA carries every differing bucket close forward, so digests diverge from the first receipt of each run, and near a crossover that drift can flip a decision.

This replay cannot tell grouping apart from vendor data. Section 5 answers the grouping question on the bars live actually used.

### 5. Does the bot's bar grouping match the backtest's? (Codex's open question)

**Yes, for the decision seam.** Given the same complete one-minute bars, the bot's decision seam groups them into buckets and decides exactly as the backtest engine does. Both seams run the same `TradeBarConsolidator`. The live runner adds `scan` at each bar's close, which fires a complete bucket on its closing minute rather than on the next one. The contents are the same (`bot_trade_strategy.py:558-576`); only the timing differs.

**What this does not cover.** The comparison starts from the one-minute bars the ledgers hold. It does not test how live builds those minutes. That happens upstream of this seam, in the 5-second-to-minute assembler and the sparse-minute timer that drops late prints outside regular hours (#2376).

**The test.** The decision clock's floor has a parity test against the consolidator's (`app/services/decision_clock.py:48-81`). I ran both decision seams over the RTH minutes of each held IBKR ledger:

- **Live seam:** `strategy_evaluations` via `qualification_shadow_trace._live_adapter_traces`.
- **Backtest seam:** the production `BacktestEngine`.
- **Judge:** `compare_canonical_traces`.

**The result.** 12 ledgers held AAPL, QQQ, SPY and TSLA from 2026-08-25 to 09-10; overlapping sessions were recorded independently. A symbol was replayed when its ledger held every regular-session minute of at least one session. Across 24 runs, the two seams produced **1,688 EMA traces and 25,278 Deployment Validation traces, identical field for field, with no divergence.** That includes 63 final-bar traces for each program. It also includes one ledger with an incomplete bucket: neither seam decided it, and both produced the same trace sequence around it.

**Why the receipts never showed this.** The production version of this very check compared **zero traces in all 24 runs**. It stopped at index 0 with "reference sequence exhausted". The cause:

- `qualification_shadow_trace._reference_backtest_traces` ran the strategy over its built-in default window (`qualification_shadow_trace.py:202-216` at `f25eb718`, before #2608). For EMA that window is 2024-03-28 to 2026-03-27 (`ema_crossover_signal.py:204-205`); for Deployment Validation it ends 2026-04-15.
- `InMemoryDataReader` drops every bar outside that window (`app/services/spec_strategy_runner.py:65-71`).
- So every run replay receipt after March 2026 failed its engine-parity leg. The saved receipt for `sh-ema-spy-0910` shows exactly this divergence. [Known gaps](../known-gaps.md) called it "an uninvestigated engine-parity sequence-exhaustion failure" until #2608 recorded the cause.
- With the window set to the bars' own dates, the same check passes on every held ledger. Since #2608 (2026-09-29) that is the production check: the reference takes its window from the bars it is handed.

**The committed JSON predates #2608.** [final-bar-decisions-2467.json](final-bar-decisions-2467.json) was produced before #2608 changed the script's output. Its `grouping_parity` rows carry `production_check_*` (the receipt's check, still reading the default window) and `windowed_*` (the same check over the bars' own dates). The script now runs only the production check and reports `compared`, `final_bar_traces` and `divergence`.

## Recommendation, with the numbers behind it

**1. A final-bar ENTER is skipped: discarded, not filled.**

- It is what live does once #2596 lands: refuse the ENTER, settle the staged candidate DISCARD.
- Until #2596 lands, live sometimes fills such an entry at the next open instead, so neither model matches live today.
- A next-open fill would credit trades live is meant to refuse. Such fills are 42 bps from the close on average, and up to 400.
- The skip removes 35% of the sealed EMA backtest's points (5 trades) and 49% of the variant's (36 trades). That is the largest single live-vs-backtest gap found here.

**2. A final-bar EXIT fills at the after-hours price.**

- The limit is `marketable_limit_price` at the decision close with the run's exit allowance.
- It fills on the first minute that starts after the decision and reaches the limit. The fill price is the better of the limit and the open of the first minute that starts after the decision.
- If nothing reaches the limit before after-hours ends (20:00, or 17:00 on a half-day, per `order_session_state_at_ms`), the position stays open into the next session.
- The proxy sits 3.74 bps from the close on average with no drift. At a 10 bps allowance or more the limit was reached on every session in the window.
- Filling at the limit itself would overstate the cost by the whole allowance, up to 55.8 points on the variant at 50 bps. Filling at the next open is wrong by 42 bps on average.

**3. The next-open path stays LEAN-only.** It reproduces LEAN's equity fill for parity runs, and only there.

**4. Where the model applies.**

- It is the default for every run outside the LEAN-compatibility profile: Engine Lab and Strategy Lab runs, grid search, walk-forward, and the grades built on them. It is not opt-in, because an opt-in model would leave those numbers crediting the 35% by default. Each run records which final-bar model it used.
- It applies under every fill mode outside that profile, including the decision-minute open proposed in #2599. The backtest emits the final bucket on the next session's first minute, so without this rule each mode fills the decision either at a close live cannot trade at, or on the next day.
- The LEAN-compatibility profile is exempt. It exists to match LEAN, which fills a stale signal at the next open.
- **Deploy's gate is a LEAN-parity proof.** Deploy admits a strategy on its validation record or a Golden validation case (`app/services/strategy_validation_admission.py`). A Golden case needs a LEAN companion run (`app/research/golden_validation/service.py:190-211`). A run only gets one under the LEAN-compatibility profile (`app/services/parity_companion.py:80`). So the evidence that gates Deploy fills a final-bar ENTER at the next open, and this model does not change what Deploy admits. Whether Deploy should also see the live-model result is an owner decision.

**5. The model needs extended-hours minutes for the fill only.** Decisions keep using regular-session bars. The lake already holds after-hours minutes for all 590 SPY sessions.

**6. Owner decisions the follow-up needs.**

- **Allowance.** Which exit allowance does a backtest use? Options: a run setting, refusing the run like Start does, or the account's sealed value.
- **Proxy.** The open of the first minute after the decision, floored at the limit (recommended)? Or the limit itself (the most conservative, which costs the whole allowance)?
- **Deploy.** Should Deploy show or require a result under the live final-bar model, beside the LEAN-parity proof?

## Other findings in the investigated area

### The final-bar ENTER race (#2596)

Filed as #2596 (P1) after the first draft of this note; a fix is in progress. The code path, at `8e138f73`:

- **The strategy gate.** `bot_trade_strategy.py:969-970` → `_liveness_blocks_entry` → `MarketEntryPolicy.refusal` → `liveness_blocks_entry` (`app/services/market_liveness.py:313-318`). With `use_rth` and a TRADABLE fact, the ENTER passes.
- **The clock reading.** An OPEN clock reading composes to TRADABLE while it is under 5 s old (`app/services/market_liveness.py:35`). The clock is polled every 1.0 s (`app/broker/alpaca/market_liveness.py:43`) and stamped when the answer arrives (`app/broker/alpaca/adapter.py:528`).
- **The dropped close.** The adapter captures the clock's `next_close_ms` (`adapter.py:527`), but `clock_liveness_evidence` drops it (`app/services/market_liveness.py:357-364`). So nothing on the path can see that the close has passed.
- **The Clerk.** The Clerk's recheck (`runtime.py:1125-1144`) and its `before_submit` guard (`app/broker/alpaca/clerk/sqlite/enter.py:388-390`) repeat the same clock-only test.
- **The order.** The ENTER leg is a market DAY order (`program_leg.py:527-528`), which Alpaca queues for the next trading day ([vendor facts pinned in alpaca-extended-hours.md](alpaca-extended-hours.md#vendor-facts-pinned-2026-09-08)). The calendar's send-time rule, `recovery_reduction.market_leg_sendable`, is used only on EXIT and recovery paths (`runtime.py:1062`, `open_replacement.py`, `exit_watchdog.py`).
- **Reproduction** (scratch): `compose_market_liveness` with an OPEN clock observed 300 ms before the close, evaluated 595 ms after it, gives `TRADABLE`, and `liveness_blocks_entry` returns `False`. `market_leg_sendable` returns `False` for the same instant.

**Consequence for this note.** Recommendation 1 (skip a final-bar entry) is the correct model only once #2596 lands.

### The backtest's entry cutoff cannot see the final bucket

- `ExecutionConfig.session_entry_cutoff` compares the current minute's wall-clock time (`app/engine/engine.py:388-405`).
- The final bucket is decided while the engine processes the next session's 09:30 minute, so no cutoff can reach it.
- **Measured:** with a 15:45 cutoff, the sealed EMA run keeps all 5 final-bar entries (82 trades against 83; only the 15:30–15:45 bucket's entry is dropped).
- `force_flat_at` is also a wall-clock literal. A value after 13:00 never fires on a half-day.
- Neither knob has a caller outside the Engine Lab API and its tests. The generated TypeScript types carry them, but no component sets them.

## Dead machinery found

| What | Evidence | Proposed removal |
|---|---|---|
| `app/services/daily_session_schedule.py` and `tests/services/test_daily_session_schedule.py` | No production caller since `69bbeabe` (2026-08-07, "retire legacy execution paths") removed the last `start_boundary_verdict` call. It states a daily stop the live runner does not apply. | Delete both (#2609) |
| `LiveConfig` and its session helpers in `app/engine/live/config.py:19-99`: `force_flat_at = time(15, 55)`, `normalize_allowed_sessions`, `DEFAULT_ALLOWED_SESSIONS`, `_SESSION_ORDER` | Read only by the dead module above and by one defaults test (`tests/engine/live/test_durable_submit_activation.py::test_default_config_cannot_activate`). The 15:55 force-flat contradicts the 16:00 decisions live records. | Delete them, and rewrite that test against `require_durable_submit_activation`'s real inputs. Keep `stock_symbol_from_action_plan`. This settles #2602's `LiveConfig.sizing` criterion by removal: no code outside the dead module constructs a `LiveConfig`, so no run id hashes one today. (#2609) |
| `ExecutionConfig.session_entry_cutoff` / `force_flat_at` and their `EngineBacktestRequest` fields | Wall-clock literals; blind to the final bucket (measured above); miss half-days; no UI or backend caller. Their persisted `*_ms` receipt fields make removal a contract change. | Decide inside #2607: derive them from the calendar, or remove them with the contract regenerated |
| `FinalBarPolicyEngine` in this note's script | It overrides the engine's private `_commit_staged_signal_program` (`app/engine/engine.py:833`). The script checks the hook exists and ran, so a rename fails loudly. | Delete it inside #2607, once the engine models final bars itself |
| The known-gaps line on the "uninvestigated engine-parity sequence-exhaustion failure" | Cause found (section 5) | Replaced with the cause and fix by #2608 (2026-09-29) |

## Follow-ups

- **#2596 (filed, P1):** a last-bar ENTER can pass the market-closed check and fill at the next open.

Drafted, not filed; the orchestrator files them:

- **#2607:** backtest models a final-bar decision the way live executes it.
- **#2608:** run-replay engine parity compares against an empty backtest after March 2026.
- **#2609:** remove the retired daily-stop schedule and the unused `LiveConfig`.

## Method and reproduction

From `PythonDataService/` in any checkout or worktree. The lake lives beside the main checkout, so the command finds it through git. The ledger and clerk inputs are copies, opened with `mode=ro&immutable=1`. `POLYGON_API_KEY` is empty, so nothing can reach a vendor. The run took about 8 minutes.

```sh
MAIN="$(git rev-parse --path-format=absolute --git-common-dir)/.."
POLYGON_API_KEY="" DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m scripts.measure_final_bar_decisions \
  --lake-root "$MAIN/data-lake-volume/lake/polygon_split_adjusted" \
  --raw-lake-root "$MAIN/data-lake-volume/lake/raw" \
  --start 2024-05-20 --end 2026-09-25 \
  --ibkr-ledger <copies of artifacts/**/source_bars.sqlite3> \
  --ibkr-jsonl "$MAIN/PythonDataService/artifacts/live_bars/*/1m/*.jsonl" \
  --receipts-db <copy of the paper clerk.db> \
  --grouping-ledger <the 12 non-fleet ledger copies> \
  --out final-bar-decisions-2467.json
```

**The models.** Each model runs the production `BacktestEngine`, with one change: a subclass settles a final-bar stage DISCARD instead of COMMIT. That is the settlement live applies to a refused ENTER. The script refuses to load if the engine hook it overrides is renamed, and refuses a run in which the engine never called it. The revised run reproduced the first run's share, model, closing-print, receipt-replay and grouping numbers exactly; only the after-hours prices changed. A later fix stopped the receipt replay counting a crash-candidate receipt as a decision match; `receipt_replay` was regenerated from the fixed function over the same clerk copy, and no other section changed.

**Scratch checks, not committed:**

- The ENTER race used `compose_market_liveness`, `liveness_blocks_entry` and `market_leg_sendable` with the timings above.
- The cutoff check ran the sealed EMA with `ExecutionConfig(session_entry_cutoff=time(15, 45))` over the same window. The output was 83 → 82 trades, with final-bar entries at 5 → 5.

**Limits.**

- The lake-vs-live closing comparison covers 12 SPY sessions, 8 of them IBKR history rather than live-observed. The share and model numbers rest on 590 sessions of one symbol, and the 35% on five trades.
- After-hours fill prices are inferred from one-minute trade bars, not from quotes. A minute's open is its first trade, not the bid a sell limit would meet.
- On half-days the lake has no minute between 13:01 and 15:59, so the model's first after-decision price is three hours after the decision there.
- The live lane's own clerk DB was not read.
