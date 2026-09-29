# EMA crossover verdict under live execution terms (#2466)

Research note for [#2466](https://github.com/tim1016/learn-ai/issues/2466)
(parent [#2439](https://github.com/tim1016/learn-ai/issues/2439)). Code
examined at `8e138f73`. Measured 2026-09-29 on the host venv, with SPY bars
already held in the data lake and no vendor fetch.

## Answer

**Yes, on the closest model of live execution.** The validated EMA configuration keeps its verdict
and grades when it runs on the terms a live bot trades on: one share on a
$1,000 budget, filled at the open of the minute in which the bot decides,
Alpaca regulatory fees, and 1–2¢ per share against each fill.

- The walk-forward study still reads **still worked** (5 of 5 folds). The
  median retention is 0.581 with 1¢ and 0.579 with 2¢, against 0.589 on the
  research terms.
- The Backtest Evidence Grade holds on all three windows:

  | Window | Research terms | Live terms, 1¢ | Live terms, 2¢ |
  |---|---|---|---|
  | W3mo | B/59 | B/58 | B/57 |
  | W6mo | A/81 | A/80 | A/80 |
  | 27 months | A/80 | A/77 | A/76 |

Two terms do flip a verdict when they are measured alone. Both flips come from
how the research tools model the term, not from the live terms themselves:

1. **Fill timing.** The engine's only delayed fill mode, `next_bar_open`, fills
   one minute after the minute in which a live bot decides.
   - W3mo drops from B/59 to C/44: net P&L goes from +$136.38 to −$311.40.
   - The 27-month composite falls to 76. Combined with the other live terms
     it falls to 70, the lowest score that still earns an A.
   - The median retention falls to 0.534–0.537, which is 0.03–0.04 above the
     0.5 line where the verdict becomes "got worse".

   A fill at the open of the decision minute matches live more closely. The
   engine cannot express it, and it flips nothing.
2. **The fixed share quantity, with the research default of $1 per order.**
   Every training Sharpe turns negative. The verdict becomes **could not be
   judged** (0 of 5 folds) and every grade becomes **C**.
   - One share earns about $0.65 gross per round trip over the 27 months.
     The flat commission costs $2 per round trip.
   - Under Alpaca's regulatory fees the same one-share runs cost about $0.03
     per round trip, and nothing flips (`qty_1sh_1k_alpaca` in the tables
     below).

Two more things the owner should know:

- **The grade does not measure dollars.** On the live basis the 27 months net
  **$48.97** on the $1,000 budget, about $0.60 per trade, and still grade A/77.
- **The "still worked" verdict is thin.** The median retention of 0.589 comes
  from the three older folds. Fold 3 (March–May 2026) keeps less than 0.21 of
  its training Sharpe. Fold 4 (June–August 2026) loses money under every set
  of terms, with a test Sharpe between −2.30 and −3.29 over 16 trades (−5.5
  for the one-share runs at $1 per order). The live bots began trading after
  fold 4.

## The configuration measured

Only the configuration the live bot runs was measured. The walk-forward
study sweeps that one point, so each fold's winner is the configuration
itself.

- **Strategy:** `ema_crossover_signal` on SPY. It uses regular-session minute
  bars consolidated to 15 minutes, EMA(5)/EMA(10), and a Wilder RSI(14) gate.
  It exits after five bars.
- **Parameters:** gap 0.20, gap_bps 0, and an RSI band of 50–70. These are
  the registry's `validated_settings`
  (`app/engine/strategy/registry.py:497-502`). They are also the parameter
  defaults, and the script refuses to run if the two ever differ.
- **Live bots:** the bots sealed the same point with quantity 1
  (`docs/audits/live-ema-spy-missed-entry-2026-09-17.md`, "Run and strategy").
- **Recorded grades:** two Strategy Lab runs of this configuration were
  graded on 2026-09-27
  (`PythonDataService/artifacts/strategy-lab-validation-2026-09-27/`,
  gitignored).
  - Run 18 covers W3mo on adjusted bars with $1 per order: **B/59**, net
    $136.3804.
  - Run 33 covers W6mo under the pinned `us-equity-raw-ibkr-v1` profile:
    **A/81**, net $2,696.8751.
  - The script reproduces both runs exactly in trades, net P&L, fees, grade
    and composite (see Method). Sharpe now differs by at most 0.007 (0.2437
    against 0.2457, and 1.7301 against 1.7372), because
    [#2525](https://github.com/tim1016/learn-ai/pull/2525) (`236829fb`)
    changed the daily-return convention after those runs were recorded. It is
    the only statistics-path commit in `ac0c2346..8e138f73`.
- **Parity validation:** the strategy's "validated" flag
  (`app/data/strategy_validation_manifest.json`) is a statement about parity
  with LEAN, not about profitability. The verdict and grade studied here are
  the walk-forward study verdict (`app/research/walk_forward_study/verdict.py`)
  and the Backtest Evidence Grade (`app/services/run_verdict_service.py`).

## Research terms and live terms

| Term | Research surfaces today | Live bot | Expressible through request parameters? |
|---|---|---|---|
| Quantity | An ENTER is `set_holdings(symbol, 1)` (`app/engine/execution/signal_intent_executor.py:39`), bound for every engine run (`app/engine/engine.py:189`). It is sized by `SimpleFloorSizing`, `floor(equity/price)` (`app/engine/engine.py:112`, `app/engine/execution/sizing.py:65`). $100,000 by default (`ema_crossover_signal.py:206`, `grid_search/models.py:128`). | The binding's fixed `quantity` (`app/services/bot_trade_strategy.py:1023`, `:1353`), which is 1 for the EMA bots. | **No.** `initial_cash=1000` happens to floor to 1–2 shares for SPY at $482–$779 in these windows (see `params_only_proxy`), but that is not a fixed quantity. |
| Fill timing | `fill_mode` `signal_bar_close` (default) or `next_bar_open` (`app/schemas/engine_backtest.py:52-56`, `app/services/engine_backtest_service.py:190-199`). | A market DAY order sent when the decision bar closes (`app/broker/alpaca/clerk/program_leg.py:386-393`). The committed fill fixture shows 0.772 s from submission to fill (`docs/references/alpaca-order-fill-latency.md`). | **Partly.** `next_bar_open` fills at the open of the minute *after* the decision minute. See below. |
| Fees | A flat `commission_per_order`, $1 by default (`app/schemas/engine_backtest.py:56`, `grid_search/models.py:126`). The only per-fill fee model the seam accepts is IBKR's (`app/engine/execution/fill_model.py:80`). | Alpaca charges no commission, only SEC, TAF and CAT pass-throughs, settled per ET day and rounded up (`app/broker/alpaca/regulatory_fees.py`, [the rate note](alpaca-regulatory-fees.md)). | **No.** `compute_fee(quantity, fill_price)` has no side and no trade date (`fill_model.py:83-87`). The rate note already lists "engine wiring" as a follow-up. |
| Spread | `slippage_per_share`, 0 by default (`app/schemas/engine_backtest.py:57-66`). | Pays the spread on each market order. | **Yes.** |

For $1 per order the research default and the IBKR tier coincide: IBKR
charges $0.005 per share with a $1.00 minimum, which is $1.00 at every
quantity up to 200 shares. The largest research quantity in these windows was
207 shares, which IBKR would price at $1.035.

**Why "decision-minute open" is the live proxy.**

- The 15-minute consolidator emits a bar only when the first minute bar of
  the next period arrives (`app/engine/consolidators/trade_bar_consolidator.py:96-107`).
  That arriving minute is the one in which a live bot decides and sends its
  order.
- `signal_bar_close` fills at the previous minute's close, one print before
  the decision. `next_bar_open` fills at the open of the minute after that.
  The existing regression test pins this: "NEXT_BAR_OPEN fills on the bar
  AFTER the consolidator-fire iteration"
  (`tests/engine/test_engine_fill_modes.py:259`).
- A live market order fills about a second into the decision minute, so the
  decision minute's own open is the nearest bar price.
- The engine already has this rule for one case: the LEAN stale-signal path
  (`fill_stale_signal_at_current_open`) fills at the current minute's open
  when a gap separates the bars (`fill_model.py:114-121`).

## Method

Everything runs through the repository's own entry points. The script is
`PythonDataService/scripts/measure_verdict_under_live_terms.py`.

**The grade.** Each window gets one `execute_engine_backtest` run, the entry
point Strategy Lab uses, with no warmup and `save_study=False`.

**The walk-forward verdict.** The script runs the same steps as the study:

- the study's `prepare_launch` for folds, run-up and a single data snapshot;
- Grid Search's `prepare_launch` bound to that snapshot for each fold window;
- `engine_adapter.default_execute_cell` for each cell;
- the ranking contract's `leader` and the frozen `compute_verdict`.

It skips only the Postgres writes.

**Data.**

- Adjusted SPY minute archives run from 2024-05-20 to 2026-09-25 (590 of
  them). The staged-manifest SHA-256 is `b4c297522cea…`, and the study
  snapshot digest is `7e20d5cf667e229d…`.
- Each archive is checked against its corporate-action receipt
  (`verify_adjustment_receipt`) and then copied into a plain LEAN tree.
- Raw archives run from 2024-05-20 to 2026-09-18 (585 of them) and are used
  only for the run-33 reproduction.
- `POLYGON_API_KEY` is empty and `auto_fetch` is false, so no vendor fetch
  can happen.

**Windows.**

| Window | Dates |
|---|---|
| W3mo | 2026-02-02 to 2026-04-30 |
| W6mo | 2025-11-03 to 2026-04-30 |
| 27 months (the whole walk-forward range) | 2024-06-03 to 2026-08-31 |

**Study.** The study range runs from 2024-06-01 to 2026-09-01, exclusive.
Each fold trains on 12 months and tests on 3, the form's defaults
(`walk-forward-study-form.component.ts:44-45`). The measure is Sharpe, the
minimum is 5 trades, and the fold boundaries fall on sessions. The five test
windows are:

- 2025-06-02 to 2025-09-02
- 2025-09-02 to 2025-12-01
- 2025-12-01 to 2026-03-02
- 2026-03-02 to 2026-06-01
- 2026-06-01 to 2026-09-01

**Two seams the script replaces.** Each replacement is itself a finding.

1. **Data roots.** Every managed lake read is admitted against the Postgres
   catalog (`app/data_lake/admission.py:28-71`). This research may not touch
   the shared database, so `_resolve_lean_data_roots` points at the staged
   tree. Every read is still bound to the study's snapshot manifest, as a
   walk-forward cell's read is.
2. **Engine construction.** The script wraps `_build_backtest_engine` to
   install three terms through the engine's own `sizing_model` and
   `fill_model` seams:
   - `FixedQuantitySizing`, which targets the binding quantity;
   - the decision-minute-open fill;
   - the canonical Alpaca fee model, settled per ET date and component and
     charged in increments so that each day's charges sum to the day's
     settlement.

**The CAT assumption.** The CAT rate is pinned only from 2026-09-01
(`regulatory_fees.py:71-73`), and before that the canonical model raises
`RateNotPinnedError`. The script back-dates the first pinned rate. That is an
upper bound of one cent per ET trading day with a fill. The 27-month research
trade log fills on 86 days, and up to 92 once session-close decisions move to
the next day, so the bound is at most about $0.92.

**Reproduction checks.**

- The research-terms W3mo run equals recorded run 18: 11 trades, $136.3804
  net, $22.00 fees, B/59.
- The `us-equity-raw-ibkr-v1` W6mo run equals recorded run 33: 20 trades,
  $2,696.8751 net, $40.00 fees, A/81.
- For all 22 W3mo fills of the two fill variants, the fill time is the
  decision minute's start or one minute later, and the fill price equals that
  minute bar's open exactly (script run, 2026-09-29).
- A second full run reproduced every figure of the first.

**Runtime.** The full matrix is 14 variants × (3 grades + 10 fold cells), 182
engine runs. It took 585 s of wall time sequentially, 30–74 s per variant.

## Results

Variants:

| Variant | Terms |
|---|---|
| `research` | The research defaults |
| `qty_1sh_1k`, `qty_1sh_100k` | T1: fixed 1 share on $1,000 / on $100k |
| `fill_next_bar_open` | T2a: the engine's `next_bar_open` |
| `fill_decision_minute_open` | T2b: the decision-minute open |
| `fees_alpaca` | T3: Alpaca regulatory fees |
| `spread_1c`, `spread_2c` | T4: 1¢ / 2¢ per share per fill |
| `live_1c`, `live_2c` | T1 on $1,000 + T2b + T3 + T4 |
| `live_1c_next_bar_open` | The same with T2a instead of T2b |
| `qty_1sh_1k_alpaca` | T1 + T3 |
| `params_only_proxy` | What the request parameters can express today: $1,000 all-in, $0 commission, 1¢, `next_bar_open` |
| `costs_all_in` | T2b + T3 + T4 at research sizing |

### Walk-forward verdict (threshold: median retention ≥ 0.5)

| Variant | Verdict | Median retention | Median test Sharpe | Fold retentions (0..4) | OOS trades |
|---|---|---:|---:|---|---:|
| `research` | still worked (5 of 5) | 0.589 | 1.521 | 0.67, 0.59, 1.63, 0.17, -1.39 | 51 |
| `qty_1sh_1k` | could not be judged (0 of 5) | — | -2.820 | —, —, —, —, — | 51 |
| `qty_1sh_100k` | could not be judged (0 of 5) | — | -2.818 | —, —, —, —, — | 51 |
| `fill_next_bar_open` | still worked (5 of 5) | 0.534 | 1.204 | 0.53, 0.68, 1.72, -0.08, -2.50 | 51 |
| `fill_decision_minute_open` | still worked (5 of 5) | 0.597 | 1.413 | 0.62, 0.60, 1.72, 0.19, -2.04 | 51 |
| `fees_alpaca` | still worked (5 of 5) | 0.594 | 1.537 | 0.68, 0.59, 1.64, 0.17, -1.37 | 51 |
| `spread_1c` | still worked (5 of 5) | 0.587 | 1.496 | 0.67, 0.59, 1.65, 0.15, -1.45 | 51 |
| `spread_2c` | still worked (5 of 5) | 0.585 | 1.470 | 0.67, 0.59, 1.65, 0.13, -1.51 | 51 |
| `qty_1sh_1k_alpaca` | still worked (5 of 5) | 0.589 | 1.498 | 0.69, 0.59, 1.70, 0.20, -1.40 | 51 |
| `live_1c` | still worked (5 of 5) | 0.581 | 1.366 | 0.62, 0.58, 1.76, 0.20, -2.12 | 51 |
| `live_2c` | still worked (5 of 5) | 0.579 | 1.341 | 0.62, 0.58, 1.77, 0.18, -2.21 | 51 |
| `live_1c_next_bar_open` | still worked (5 of 5) | 0.537 | 1.192 | 0.54, 0.66, 1.76, -0.08, -2.62 | 51 |
| `params_only_proxy` | still worked (5 of 5) | 0.525 | 1.230 | 0.53, 0.64, 1.74, -0.04, -2.46 | 51 |
| `costs_all_in` | still worked (5 of 5) | 0.601 | 1.404 | 0.63, 0.60, 1.73, 0.18, -2.09 | 51 |

Training → test Sharpe per fold:

| Variant | Fold 0 | Fold 1 | Fold 2 | Fold 3 | Fold 4 |
|---|---|---|---|---|---|
| `research` | 2.75 → 1.85 | 2.58 → 1.52 | 2.46 → 4.03 | 2.81 → 0.47 | 1.65 → −2.30 |
| `live_1c` | 2.47 → 1.54 | 2.35 → 1.37 | 2.26 → 3.97 | 2.63 → 0.52 | 1.53 → −3.24 |
| `fill_next_bar_open` | 2.25 → 1.20 | 2.13 → 1.44 | 2.17 → 3.73 | 2.50 → −0.21 | 1.20 → −3.00 |
| `qty_1sh_1k` | −1.82 → −1.55 | −1.71 → −0.88 | −1.20 → −2.82 | −1.10 → −4.43 | −2.07 → −5.50 |

### Backtest Evidence Grade (grade/composite · net P&L · fees · Sharpe)

The grade thresholds are A ≥ 70, B ≥ 55 and C ≥ 40
(`run_verdict_service.py:495-516`).

| Variant | W3mo | W6mo | 27 months |
|---|---|---|---|
| `research` | **B**/59 · $136.38 · fees $22.00 · Sharpe 0.24 | **A**/81 · $2,706.52 · fees $40.00 · Sharpe 1.73 | **A**/80 · $9,318.26 · fees $162.00 · Sharpe 1.50 |
| `qty_1sh_1k` | **C**/47 · $-20.78 · fees $22.00 · Sharpe -4.27 | **C**/52 · $-21.53 · fees $40.00 · Sharpe -1.92 | **C**/48 · $-109.14 · fees $162.00 · Sharpe -2.54 |
| `qty_1sh_100k` | **C**/46 · $-20.78 · fees $22.00 · Sharpe -4.26 | **C**/51 · $-21.53 · fees $40.00 · Sharpe -1.92 | **C**/50 · $-109.14 · fees $162.00 · Sharpe -2.54 |
| `fill_next_bar_open` | **C**/44 · $-311.40 · fees $22.00 · Sharpe -0.49 | **A**/79 · $2,063.71 · fees $40.00 · Sharpe 1.33 | **A**/76 · $7,951.37 · fees $162.00 · Sharpe 1.17 |
| `fill_decision_minute_open` | **B**/59 · $131.21 · fees $22.00 · Sharpe 0.24 | **A**/81 · $2,705.85 · fees $40.00 · Sharpe 1.73 | **A**/80 · $9,770.13 · fees $162.00 · Sharpe 1.42 |
| `fees_alpaca` | **B**/62 · $155.39 · fees $2.51 · Sharpe 0.28 | **A**/81 · $2,743.58 · fees $2.94 · Sharpe 1.76 | **A**/81 · $9,352.95 · fees $128.19 · Sharpe 1.50 |
| `spread_1c` | **B**/58 · $103.88 · fees $22.00 · Sharpe 0.19 | **A**/81 · $2,645.39 · fees $40.00 · Sharpe 1.70 | **A**/79 · $9,039.30 · fees $162.00 · Sharpe 1.46 |
| `spread_2c` | **B**/58 · $71.38 · fees $22.00 · Sharpe 0.13 | **A**/81 · $2,585.05 · fees $40.00 · Sharpe 1.66 | **A**/79 · $8,740.04 · fees $162.00 · Sharpe 1.41 |
| `qty_1sh_1k_alpaca` | **B**/58 · $0.98 · fees $0.24 · Sharpe 0.26 | **A**/80 · $18.07 · fees $0.40 · Sharpe 1.74 | **A**/77 · $50.25 · fees $2.61 · Sharpe 1.33 |
| `live_1c` | **B**/58 · $0.73 · fees $0.24 · Sharpe 0.19 | **A**/80 · $17.67 · fees $0.40 · Sharpe 1.70 | **A**/77 · $48.97 · fees $2.57 · Sharpe 1.23 |
| `live_2c` | **B**/57 · $0.51 · fees $0.24 · Sharpe 0.14 | **A**/80 · $17.27 · fees $0.40 · Sharpe 1.67 | **A**/76 · $47.35 · fees $2.57 · Sharpe 1.19 |
| `live_1c_next_bar_open` | **C**/44 · $-2.23 · fees $0.24 · Sharpe -0.53 | **A**/78 · $13.46 · fees $0.40 · Sharpe 1.30 | **A**/70 · $38.72 · fees $2.57 · Sharpe 0.97 |
| `params_only_proxy` | **C**/44 · $-1.99 · fees $0.00 · Sharpe -0.47 | **A**/78 · $13.86 · fees $0.00 · Sharpe 1.34 | **A**/74 · $42.71 · fees $0.00 · Sharpe 1.06 |
| `costs_all_in` | **B**/60 · $118.20 · fees $2.51 · Sharpe 0.21 | **A**/81 · $2,682.55 · fees $2.94 · Sharpe 1.72 | **A**/80 · $9,529.58 · fees $128.37 · Sharpe 1.39 |

## What flips, term by term

| Term | Alone | Verdict flip | Grade flip |
|---|---|---|---|
| T1 quantity, with the $1-per-order default | Every training Sharpe is negative | **Yes**: still worked → could not be judged | **Yes**: B/A/A → C/C/C |
| T1 quantity, with Alpaca fees | No material change | No | No |
| T2a `next_bar_open` | The edge thins; fold 3's test Sharpe turns negative | No (0.534) | **Yes**: W3mo B → C |
| T2b decision-minute open | Sharpe falls (the 27-month Sharpe is 1.42 against 1.50) while net rises | No | No |
| T3 Alpaca fees | Cheaper than $1 per order at research size | No | No (W3mo composite rises to 62) |
| T4 1–2¢ spread | 27-month net falls $279–$578; the retention margin shrinks by ≤0.004 | No | No |
| All live terms (T2b) | Composites fall 1–4 points | No | No |
| All live terms (T2a) | W3mo becomes C; 27 months sits at A/70; retention is 0.537 | No | **Yes**: W3mo B → C |

**How the grade reads fees.** Several grade inputs are gross of fees:

- Profit factor, expectancy, win rate and payoff come from each trade's price
  return (`pnl_pct = pnl_pts / entry_price`, `ema_crossover_signal.py:562`,
  summarised in `app/engine/results/statistics.py:139-152`).
- The 1-share runs under the $1 default therefore keep profit factor 2.06
  while losing money.
- Only fee drag, the curve-based Sharpe, CAGR, drawdown and Probabilistic
  Sharpe see fees.

This follows LEAN's trade statistics, where fees are reported separately. It
is noted here because it explains why the fee terms move the grade less than
they move net P&L.

**Session-close signals.** Of the 81 research trades over 27 months, 5
entries and 1 exit fire on the bar that closes the session, and 8 trades are
held overnight.

- The harness fills these decisions at the next session's first minute
  (T2b) or at the minute after it (T2a).
- Live queues the ENTER to the next open. The EXIT instead goes out as an
  extended-hours limit at the close, priced off the decision bar
  (`program_leg.py:508-513`).
- So one exit in 81 is modelled differently from live.

## Adjacent observations (not measured here)

- **Dry Run rehearses the research basis, not the live one.** Its simulated
  fill is the decision bar's close, with no fee and no spread
  (`app/services/bot_trade_strategy.py:1384`). A Dry Run therefore cannot
  show the fill-timing effect measured above.
- **Offline research needs the catalog.** Reading managed lake bytes requires
  the Postgres catalog (`admission.py:28-71`), even for a read-only offline
  study. This research had to stage a receipt-checked copy to stay off the
  shared database.

## Dead machinery found

- **The unwired sizing resolver in `app/engine/execution/order_sizer.py`.**
  The following have no consumer outside `tests/engine/execution/test_order_sizer.py`
  (grep at `8e138f73`):
  - `OrderSizer` and `resolve_set_holdings_quantity`
  - `SizingKindNotWiredError`
  - `PortfolioValueProvider` and `WholeAccountPortfolioValueProvider`
  - `policy_to_ledger_dict`, `governed_by` and `default_sizing_provenance`
  - the "PR1/PR2/PR4 wiring" prose

  The module's own docstring says its broker consumer retired in #1583. Only
  the `SizingPolicy` types and `parse_sizing_policy` remain in use
  (`app/engine/live/config.py`, `audit_copy_allow_list.py`). Meanwhile the
  live Alpaca path sizes from a plain `binding.quantity`, and the backtest
  sizes with `SimpleFloorSizing`. **Proposed:** delete the resolver half and
  its tests, or fold it into the backtest sizing follow-up if that follow-up
  reuses `FixedShares`.
- **Stale roadmap prose in `ExecutionConfig`.** Its docstring still says
  "PR 1 scope … Later PRs will add session_entry_cutoff, force_flat…", and
  those fields exist (`app/engine/execution/execution_config.py:1-18`).
  **Proposed:** trivial; fix inside the fill-timing follow-up.
- **The rate note's follow-up list.** "Follow-ups (not this slice)" in
  [the rate note](alpaca-regulatory-fees.md) names engine wiring as a
  follow-up with no issue behind it. **Proposed:** replace it with a link to
  the fee follow-up.
- **The replacement seams in `scripts/measure_verdict_under_live_terms.py`.**
  Once each follow-up lands, the matching seam replacement becomes dead.
  **Proposed:** each follow-up deletes its seam and reruns the script through
  request parameters.

## Reproduce

From `PythonDataService/`:

```bash
POLYGON_API_KEY="" DATA_PLANE_CONTROL_SECRET="" .venv/bin/python scripts/measure_verdict_under_live_terms.py \
  --lake-volume /absolute/path/to/data-lake-volume --workdir <scratch> --out <scratch>/results.json --jobs 1
```

`--jobs N` runs the variants in parallel processes. The macOS command sandbox
refuses the semaphore check, so `--jobs 1` is the sandbox-safe default. The
script reads the lake only; it writes the staged trees and the JSON under
`--workdir` and `--out`.

The numbers are exact. The run is deterministic: two full runs matched on
every figure. The tolerance that matters is the reproduction checks' exact
trades, net P&L, fees and grades against the recorded runs.
