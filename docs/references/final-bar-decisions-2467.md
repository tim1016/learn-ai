# Final-bar decisions: how many backtest trades, and how the backtest should model them (#2467)

**Status:** research note, 2026-09-29. Code examined at `8e138f73` (master, after #2440 merged as `d4c521b2`). Parent #2439; owner decision #2431; live change #2440.

## The answer

For the EMA crossover program that runs on the clerk lanes, **7.2% of backtest trades on SPY are decided on the session's final bar** (6 of 83 over 590 sessions: 5 entries, 1 exit). The paper-only 30–70 RSI variant has 9.7% (54 of 559: 36 entries, 18 exits). Deployment Validation has none, because it stops deciding 15 minutes before the close.

Live cannot trade those decisions the way the backtest does. The backtest fills them at the final minute's close. Live decides that bar just after the close. Its gate then refuses an ENTER, and it sends an EXIT as an after-hours limit (#2440).

The entries matter most. Skipping the five final-bar entries removes **17.56 of the backtest's 50.03 points per share (35%)** for the sealed EMA settings, and 49% for the variant. The exits barely matter. On all 589 sessions, the first after-hours print sits **0.42 bps** from the close on average. The next open sits **42 bps** away.

**Recommendation:** model the two kinds differently.

- **ENTER on the final bar: skip it.** That is what live does when its gate works.
- **EXIT on the final bar: fill at the after-hours price.** Use the first after-hours minute that reaches the limit live would send.
- **Do not fill at the next open.** It is 100× further from what live gets for an exit. It also credits entries that live never takes.

Two live findings sharpen this:

1. The ENTER refusal is a race against the broker clock's once-a-second poll, not a rule.
2. The check built to prove that live and backtest group bars the same way has compared nothing since March 2026. With its date window corrected, the two groupings match on every bar held.

Follow-up drafts: [#FOLLOWUP-A](#follow-ups) (the backtest model), [#FOLLOWUP-B](#follow-ups) (the ENTER race), [#FOLLOWUP-C](#follow-ups) (the parity check), [#FOLLOWUP-D](#follow-ups) (dead machinery).

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
- **ENTER.** Only the market-liveness gate stands between a final-bar ENTER and the broker (`bot_trade_strategy.py:963-997`; Clerk recheck `app/broker/alpaca/clerk/sqlite/runtime.py:1118-1145`). #2440's own documentation assumes this gate refuses it ([alpaca-extended-hours.md, "The regular close"](alpaca-extended-hours.md#the-regular-close-an-exit-sent-after-the-session-it-was-decided-in-2440)). The ENTER leg stays a market DAY order (`app/broker/alpaca/clerk/program_leg.py:527-528`).
- **EXIT.** The EXIT is exempt from both the liveness and lateness gates (`bot_trade_strategy.py:1133-1192`). #2440 sends it as an extended-hours DAY limit at `floor_tick(close × (1 − exit_bps/10⁴))` (`program_leg.py:537-575`, `app/broker/alpaca/marketable_limit.py:51-79`). The limit is anchored on the IBKR decision bar's close and lives until 20:00, or 17:00 on a half-day (`app/services/session_authority.py:172-197`).
- If the EXIT is unfilled, the operator gets `EXIT_NOT_FLAT`, and the next try is the 04:00 pre-market re-drive (owner decisions on #2440).

**Dry Run (`sim:`).** Dry Run fills a limit at the decision bar's close whenever that close is at or through the limit (`app/broker/alpaca/clerk/fill_models.py:47-61`). A final-bar EXIT therefore fills at the close there, as in the backtest. That is not what Paper or Live can get.

## Measurements

Everything below comes from `PythonDataService/scripts/measure_final_bar_decisions.py`. Its full output is committed as [final-bar-decisions-2467.json](final-bar-decisions-2467.json). "Points" means price points per share, summed over trades. Session closes and half-days come from `app/lean_sidecar/trading_calendar.py`.

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
| &nbsp;&nbsp;plus EXIT at the after-hours price (first after-hours minute's open, floored at the limit) | +0.04 more | +0.18 more (5–50 bps allowance) |
| &nbsp;&nbsp;plus EXIT at the limit itself (the `limit_touch_fill` rule) | −0.27 (5 bps) to −2.64 (50 bps) | −5.67 (5 bps) to −55.83 (50 bps) |
| Next open (the LEAN-compatibility path) | 83 / +49.88 | 559 / +60.05 |
| Skip both kinds (the EXIT retries on the next bar, 09:45 next day) | 78 / +48.28 | 520 / +52.43 |

**The skipped entries.** They were mostly winners: four of the five for the sealed default, worth +1.71 to +8.65 points each. Their removal is the whole gap between +50.03 and +32.47.

**The next-open total is a coincidence.** It lands close to the close-fill total only by chance. Its five entries cost 13.64 points, and its one exit gained 13.49 from a +2.6% overnight gap on 2025-04-22.

**The skip-both model** keeps the 2025-04-22 exit alive into the next morning, and that is why it scores higher.

**Unfilled exits.** No after-hours exit went unfilled at any allowance from 0 to 50 bps.

### 3. After-hours price against the next open, every session

589 sessions; the last session has no next open inside the window.

| Price, against the final minute's close | Mean |Δ| | Median |Δ| | p95 |Δ| | Max |Δ| |
|---|---:|---:|---:|---:|
| First after-hours minute's **open** | 0.42 bps | 0.30 bps | 1.04 bps | 27.98 bps |
| First after-hours minute's **close** | 3.64 bps | 2.72 bps | 10.41 bps | 33.74 bps |
| Next session's **open** | 42.28 bps | 28.10 bps | 119.85 bps | 400.39 bps |

**Fill rate.** A sell limit at `marketable_limit_price(close, b)` was reached before after-hours ended on **589 of 589** sessions, for every b in {0, 5, 10, 25, 50} bps. That includes all six half-days, where after-hours ends at 17:00 and SPY printed in 26 to 50 of those 240 minutes.

**Which proxy is closer.** The 16:00 minute's open is probably the closing cross, which a limit sent at 16:00:00.6 cannot reach. That minute's close is the later bound. A real fill most likely lands between the two, within a few bps of the close. The next open is an order of magnitude further away.

### 4. The final minute: the lake against what live saw

The comparison uses held IBKR one-minute bars from 12 clerk source ledgers (copies) plus the recorder's `live_bars/*/1m` files. Both cover 2026-08-25 to 2026-09-10. The lake side is the raw root, because live bars are unadjusted. When a live-observed copy of a minute is held, it is preferred over IBKR history.

| SPY | Compared | Equal | Mean |Δ| | Max |Δ| |
|---|---:|---:|---:|---:|
| Every regular-session minute close | 4,768 | 3,635 (76%) | 0.014 bps | 0.79 bps |
| Every 15-minute bucket close | 317 | 241 (76%) | — | — |
| **Final minute close (16:00 bar close)** | **12** | **1 (8%)** | **0.40 bps (≈3¢)** | **0.79 bps (6¢)** |
| Final 15-minute bucket, full OHLC | 12 | 0 | — | — |

**Live-observed minutes only.** Four of the 12 final minutes were observed live; the rest come from IBKR history. The live four differ by 0.00, +0.03, +0.01 and +0.05 dollars.

**History can stand in for live.** Where both a live and a history copy of the same regular-session minute are held, 2,217 of 2,433 (91%) are identical in OHLCV.

**Other symbols.** AAPL matched on 2 of 4 final minutes, and TSLA on 1 of 2.

**The final minute is special.** It is where the lake and live disagree: 11 of 12 closes differ, against 24% of ordinary minutes. The size is small, a few cents, below one basis point. But it moves live's exit anchor and the last bucket's indicator inputs.

**Receipt replay.** The lake cannot reproduce live decisions digit for digit. Replaying the 85 held EMA receipts (paper, 2026-09-16 to 09-24, 5 on the final bar) from lake buckets matches **0 of 85 trace digests**. The decision kind matches for **83 of 83** ordinary receipts (all NO_ACTION). The other two are a crash candidate and a quarantine receipt. The EMA carries every differing bucket close forward, so digests diverge from the first receipt of each run.

This replay cannot tell grouping apart from vendor data. Section 5 answers the grouping question on the bars live actually used.

### 5. Does the bot's bar grouping match the backtest's? (Codex's open question)

**Yes.** Both seams run the same `TradeBarConsolidator`. The live runner adds `scan` at each bar's close, which fires a complete bucket on its closing minute rather than on the next one. The contents are the same (`bot_trade_strategy.py:558-576`); only the timing differs.

**The test.** The decision clock's floor has a parity test against the consolidator's (`app/services/decision_clock.py:48-81`). I ran both decision seams over the RTH minutes of each held IBKR ledger:

- **Live seam:** `strategy_evaluations` via `qualification_shadow_trace._live_adapter_traces`.
- **Backtest seam:** the production `BacktestEngine`.
- **Judge:** `compare_canonical_traces`.

**The result.** 12 ledgers held AAPL, QQQ, SPY and TSLA from 2026-08-25 to 09-10; overlapping sessions were recorded independently. Across 24 runs, the two seams produced **1,688 EMA traces and 25,278 Deployment Validation traces, identical field for field, with no divergence.** That includes 63 final-bar traces for each program. It also includes one ledger with an incomplete bucket, which both seams quarantined the same way.

**Why the receipts never showed this.** The production version of this very check compared **zero traces in all 24 runs**. It stopped at index 0 with "reference sequence exhausted". The cause:

- `qualification_shadow_trace._reference_backtest_traces` runs the strategy over its built-in default window (`qualification_shadow_trace.py:202-216`). For EMA that window is 2024-03-28 to 2026-03-27 (`ema_crossover_signal.py:204-205`); for Deployment Validation it ends 2026-04-15.
- `InMemoryDataReader` drops every bar outside that window (`app/services/spec_strategy_runner.py:65-71`).
- So every run replay receipt after March 2026 fails its engine-parity leg. The saved receipt for `sh-ema-spy-0910` shows exactly this divergence. [Known gaps](../known-gaps.md) calls it "an uninvestigated engine-parity sequence-exhaustion failure".
- With the window set to the bars' own dates, the same check passes on every held ledger (#FOLLOWUP-C).

## Recommendation, with the numbers behind it

**1. A final-bar ENTER is skipped: discarded, not filled.**

- Live's documented behaviour is to refuse it.
- A next-open fill would credit trades live is not meant to take. Such fills are 42 bps from the close on average, and up to 400.
- The skip removes 35% of the sealed EMA backtest's points (49% for the variant). That is the largest single live-vs-backtest gap found here.
- The skip is only honest once live refuses deterministically (#FOLLOWUP-B).

**2. A final-bar EXIT fills at the after-hours price.**

- The limit is `marketable_limit_price` at the decision close with the run's exit allowance.
- It fills on the first extended-hours minute that reaches the limit, at the better of that minute's open and the limit.
- If nothing reaches the limit before after-hours ends (20:00, or 17:00 on a half-day, per `order_session_state_at_ms`), the position stays open into the next session.
- This sits within 0.42 bps of today's close fill on average, and was reached on 589 of 589 sessions.
- It is the only model that is also right when after-hours is thin. Filling at the limit itself would overstate the cost by the whole allowance, up to 55.8 points on the variant at 50 bps. Filling at the next open is wrong by 42 bps on average.

**3. The next-open path stays LEAN-only.** It reproduces LEAN's equity fill model for parity runs, and only there.

**4. The model needs extended-hours minutes for the fill only.** Decisions keep using regular-session bars. The lake already holds after-hours minutes for all 590 SPY sessions.

**5. Owner decisions the follow-up needs.**

- **Allowance.** Which exit allowance does a backtest use? Options: a run setting, refusing the run like Start does, or the account's sealed value.
- **Proxy.** Should the fill use the first after-hours minute's open (recommended), or its close (more conservative)?

## Other findings in the investigated area

**The final-bar ENTER race (live).**

- The gate reads the Alpaca clock, polled every 1.0 s (`app/broker/alpaca/market_liveness.py:43`, `:153-156`), and trusts a reading for 5 s (`app/services/market_liveness.py:35`).
- A reading answered just before 16:00:00 still says OPEN at a 16:00:00.595 decision. The gate then passes the ENTER, and the Clerk's recheck repeats the same test. The leg is a market DAY order, which Alpaca queues for the next trading day ([vendor facts pinned in alpaca-extended-hours.md](alpaca-extended-hours.md#vendor-facts-pinned-2026-09-08)).
- **Reproduction:** `compose_market_liveness` with an OPEN clock observed 300 ms before the close, evaluated 595 ms after it, gives `TRADABLE`, and `liveness_blocks_entry` returns `False`. The calendar's own send-time rule, `recovery_reduction.market_leg_sendable`, returns `False` for the same instant. With the clock observed 200 ms after the close, it gives `MARKET_CLOSED` and blocked.
- The window is the poll interval, so how often it happens is unmeasured. No held receipt shows a final-bar ENTER.
- **Fix direction:** refuse a regular-hours ENTER when `market_leg_sendable` is false, which is the rule every EXIT already obeys (#FOLLOWUP-B).

**The backtest's entry cutoff cannot see the final bucket.**

- `ExecutionConfig.session_entry_cutoff` compares the current minute's wall-clock time (`app/engine/engine.py:388-405`).
- The final bucket is decided while the engine processes the next session's 09:30 minute, so no cutoff can reach it.
- **Measured:** with a 15:45 cutoff, the sealed EMA run keeps all 5 final-bar entries (82 trades against 83; only the 15:30–15:45 bucket's entry is dropped).
- `force_flat_at` is also a wall-clock literal. A value after 13:00 never fires on a half-day.
- Neither knob has a caller outside the Engine Lab API and its tests. The generated TypeScript types carry them, but no component sets them.

## Dead machinery found

| What | Evidence | Proposed removal |
|---|---|---|
| `app/services/daily_session_schedule.py` and `tests/services/test_daily_session_schedule.py` | No production caller since `69bbeabe` (2026-08-07, "retire legacy execution paths") removed the last `start_boundary_verdict` call. It states a daily stop the live runner does not apply. | Delete both (#FOLLOWUP-D) |
| `LiveConfig` and its session helpers in `app/engine/live/config.py:19-99`: `force_flat_at = time(15, 55)`, `normalize_allowed_sessions`, `DEFAULT_ALLOWED_SESSIONS`, `_SESSION_ORDER` | Read only by the dead module above and by one defaults test (`tests/engine/live/test_durable_submit_activation.py::test_default_config_cannot_activate`). The 15:55 force-flat contradicts the 16:00 decisions live records. | Delete them, and rewrite that test against `require_durable_submit_activation`'s real inputs. Keep `stock_symbol_from_action_plan`. (#FOLLOWUP-D) |
| `ExecutionConfig.session_entry_cutoff` / `force_flat_at` and their `EngineBacktestRequest` fields | Wall-clock literals; blind to the final bucket (measured above); miss half-days; no UI or backend caller. Their persisted `*_ms` receipt fields make removal a contract change. | Decide inside #FOLLOWUP-A: derive them from the calendar, or remove them with the contract regenerated |
| The known-gaps line on the "uninvestigated engine-parity sequence-exhaustion failure" | Cause found (section 5) | Replace it when #FOLLOWUP-C lands |

## Follow-ups

Drafted, not filed; the orchestrator files them.

- **#FOLLOWUP-A:** backtest models a final-bar decision the way live executes it.
- **#FOLLOWUP-B:** a final-bar ENTER can pass the market-closed gate on a pre-close clock reading.
- **#FOLLOWUP-C:** run-replay engine parity compares against an empty backtest after March 2026.
- **#FOLLOWUP-D:** remove the retired daily-stop schedule and the unused `LiveConfig`.

## Method and reproduction

From `PythonDataService/`. The ledger and clerk inputs are copies, opened with `mode=ro&immutable=1`. `POLYGON_API_KEY` is empty, so nothing can reach a vendor.

```sh
POLYGON_API_KEY="" DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m scripts.measure_final_bar_decisions \
  --lake-root ../data-lake-volume/lake/polygon_split_adjusted \
  --raw-lake-root ../data-lake-volume/lake/raw \
  --start 2024-05-20 --end 2026-09-25 \
  --ibkr-ledger <copies of artifacts/**/source_bars.sqlite3> \
  --ibkr-jsonl 'artifacts/live_bars/*/1m/*.jsonl' \
  --receipts-db <copy of the paper clerk.db> \
  --grouping-ledger <the 12 non-fleet ledger copies> \
  --out final-bar-decisions-2467.json
```

**The models.** Each model runs the production `BacktestEngine`, with one change: a subclass settles a final-bar stage DISCARD instead of COMMIT. That is the settlement live applies to a refused ENTER. Two runs of the full measurement produced identical backtest and session-wide numbers.

**Scratch checks, not committed:**

- The ENTER race used `compose_market_liveness`, `liveness_blocks_entry` and `market_leg_sendable` with the timings above.
- The cutoff check ran the sealed EMA with `ExecutionConfig(session_entry_cutoff=time(15, 45))` over the same window. The output was 83 → 82 trades, with final-bar entries at 5 → 5.

**Limits.**

- The lake-vs-live closing comparison covers 12 SPY sessions (4 of them live-observed). The share and model numbers rest on 590 sessions of one symbol.
- After-hours fill prices are inferred from one-minute trade bars, not from quotes.
- The live lane's own clerk DB was not read.
