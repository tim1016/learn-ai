> **Internal field manual — protocol `spy-ema-normalized-gap` V1.0**
> Use this guide to run the study, interpret its evidence, and understand what must still be built before the normalized-gap strategy can enter Alpaca paper execution.

## Start here {#start-here}

### The one-minute explanation

This analysis asks a narrow, useful question:

> If we choose an EMA-gap threshold using only the previous 180 calendar days, freeze that choice, and trade it during the next 30 days, does the strategy continue to work on data the selection process did not see?

It repeats that experiment through time. The combined result is therefore evidence about the **threshold-selection process**, not just a backtest of one hand-picked threshold.

The current implementation is a **research pipeline**. It does not automatically change a strategy, deploy a bot, or submit an order. That separation is intentional. A good-looking walk-forward result is one input to a promotion decision; it is not deployment permission.

### What you can do today

1. Open **Research Lab → Backtests → EMA Walk-Forward**.
2. Select **Run canonical study**.
3. Let the background job complete. You can leave the page and return later.
4. Read the headline evidence, then open the walk-forward and individual fold receipts.
5. Use the decision checklist in this guide before treating the result as useful research evidence.

### What you cannot do today

- Change V1's dates, candidate grid, fill assumptions, costs, or split sizes from the page or API.
- Treat the threshold selected in one historical fold as a permanent live parameter.
- Promote the normalized-gap specification directly into the Alpaca V2 paper runner.
- Claim deployment readiness from V1 alone: V1 deliberately uses zero commission and zero slippage.

---

## What was implemented {#what-was-implemented}

### The strategy under study

The signal uses 15-minute regular-session SPY bars.

An entry requires all three conditions on the same decision bar:

1. EMA(5) has freshly crossed above EMA(10).
2. The normalized EMA gap is at least the selected threshold.
3. Wilder's RSI(14) is between 50 and 70, inclusive.

The strategy is long-only, permits one position, and exits after five consolidated bars. Research fills occur at the next bar's open.

The normalized gap is:

$$
\operatorname{gap}_{bps}=10{,}000\times\frac{EMA_5-EMA_{10}}{EMA_{10}}
$$

For example, if EMA(5) is 500.25 and EMA(10) is 500.00, the gap is 5 basis points:

```text
10,000 × (500.25 − 500.00) / 500.00 = 5 bps
```

### Why normalize the gap?

The original control requires an absolute EMA difference of \$0.20. That requirement changes meaning as SPY's price changes: \$0.20 is 5 bps at \$400, but about 3.33 bps at \$600. A basis-point gap describes the EMA separation relative to price, so the filter is comparable across price regimes.

### Frozen V1 research contract

| Decision | V1 value |
|---|---|
| Protocol identity | `spy-ema-normalized-gap` version `1.0` |
| Study window | 2024-08-01 through 2026-08-01 |
| Instrument and bars | SPY, 15-minute, regular NYSE session |
| Training window | Rolling 180 calendar days |
| Test window | Following 30 calendar days |
| Step | 30 calendar days |
| OOS folds | 18 contiguous, non-overlapping test windows |
| Candidate thresholds | 1, 2, 3, 4, 5, 7.5, and 10 bps |
| Candidate eligibility | At least 5 training trades and a non-null training Sharpe |
| Selection | Highest training Sharpe; then return; then declaration order |
| Fill model | Next-bar open |
| Costs | Zero commission and zero slippage baseline |
| Initial cash | \$100,000 per engine child run |
| Fold positions | Start flat and finish flat |

The job performs **145 canonical engine runs**:

```text
1 full-window absolute-\$0.20 control
+ 18 folds × (7 training candidates + 1 frozen out-of-sample test)
= 145 runs
```

Every run produces a receipt. Python persists the control, every training candidate, each test child, and the walk-forward aggregate. The UI renders those persisted results; it does not choose thresholds or calculate research metrics.

---

## How the analysis works {#how-the-analysis-works}

### Learn → freeze → prove

For each fold, the pipeline does the following in order:

```text
TRAIN: previous 180 days
  ├─ run 1 bps candidate
  ├─ run 2 bps candidate
  ├─ run 3 bps candidate
  ├─ run 4 bps candidate
  ├─ run 5 bps candidate
  ├─ run 7.5 bps candidate
  └─ run 10 bps candidate
          │
          ▼
SELECT: highest eligible train Sharpe
          │ exact StrategySpec is frozen
          ▼
TEST: next 30 days, no parameter changes
```

After the test window, the entire 30-day step advances and the process repeats. Old history falls out of the rolling training window.

### Deterministic candidate selection

The winner is selected by:

```text
highest (training Sharpe, training total return, earliest grid position)
```

The return and declaration-order rules are only tie-breakers. Because the declared grid is ascending, a complete tie selects the lower threshold.

If every candidate fails, has fewer than five training trades, or has a null Sharpe, the fold has no winner. The pipeline fails closed: it records the failed selection and does not test a fallback threshold.

### Why the test is genuinely forward

The test period is never used to select its threshold. The selected candidate's complete `StrategySpec` is hashed and frozen before its test run begins.

Indicators still need history at the train/test boundary. Each test child therefore reads bars from the start of its training window to pre-roll EMA, RSI, and fresh-cross state, but it cannot place an entry until the test window begins. Metrics, trades, curves, and consumed-bar counts include only the test period.

This avoids two common errors:

- a cold-start indicator at the first test bar;
- a false “fresh” crossover caused by forgetting the prior EMA relationship.

### Flat fold boundaries

Every test fold starts flat and is forced flat at the end. A position never crosses from one fold into another because the next fold may select a different threshold.

The combined OOS curve compounds the return of these independently flat folds. It answers “what would the sequence of OOS fold returns look like if compounded?” It is **not** a literal brokerage statement with continuous position ownership.

### One pinned data revision

At job start, Python resolves the market-data revision once. The control and all 144 child runs use that same revision, so data cannot silently change halfway through the protocol. Individual receipts still record the exact data window they consumed.

---

## How to run it effectively {#how-to-run}

### Before starting

- Confirm the local SPY minute-data lake covers the full frozen study window.
- Treat V1 as a reproducible baseline; do not expect current-date selection or realistic cost testing.
- Run one canonical job at a time. The page disables duplicate starts while it sees an active job.

### During the job

The page shows progress for the control, training candidates, and OOS tests. The work runs through the background jobs boundary, so page navigation does not cancel it. You may explicitly cancel the job; cancellation is checked between engine runs.

The current implementation is not mid-fold resumable after a process crash. A new canonical run should be started if the worker dies.

### After completion

Read the evidence in this order:

1. **Protocol identity and fold count** — confirm V1.0 and the expected 18 folds.
2. **Run status** — a failed-closed receipt is evidence of a problem, not a zero-return strategy result.
3. **Auditable folds** — inspect completed versus failed folds.
4. **Training parameter matrix** — compare every candidate on each fold's training window and identify unstable or ineligible regions.
5. **Selected-gap OOS journey** — follow the frozen winner and its immediately subsequent unseen return.
6. **Mean and median OOS Sharpe** — compare the center and the effect of outliers.
7. **Profitable-fold percentage** — ask whether results are repeatable across time.
8. **Retention and alpha decay** — inspect stability, not just level.
9. **Warnings and child receipts** — resolve missing data, failed persistence, or eligibility issues.
10. **Control receipt** — use the original \$0.20 strategy as context, not as the optimizer's score.

Do not stop at the aggregate card. A result dominated by one excellent month is different from a result that is modestly positive across many folds, even when the headline mean is similar.

---

## How to read the page {#how-to-read}

### Control metrics

The control is the original full-window strategy with an absolute \$0.20 EMA gap. Its return, Sharpe, drawdown, and trade count are comparison context.

The control is **not** one of the seven normalized candidates, cannot win a fold, and is not the denominator of normalized-gap retention. Also remember that its full-window metrics and the optimized strategy's OOS metrics describe different samples; compare them as context, not as a perfectly aligned A/B test.

### Training parameter matrix

The detailed walk-forward receipt displays candidates as rows and folds as columns. Use the metric switch above the matrix to inspect four persisted training measures:

- **Sharpe** — annualized average daily return divided by daily-return volatility. It measures risk-adjusted consistency, not portfolio growth.
- **Net return** — net P&L divided by starting cash for that training window. This is the closest of the four measures to portfolio growth.
- **Max drawdown** — the largest peak-to-trough equity decline. Lower is better.
- **Trades** — the number of closed training trades supporting the result.

Every cell is **training evidence**; it is not an OOS result. Positive and negative Sharpe/return cells use market-semantic theme colors, ineligible candidates are marked explicitly, and the outlined **chosen** cell is the winner that advanced to the next test window. Drawdown and trade counts stay neutral because they do not have a meaningful positive/negative sign.

Fold numbers are chronological: **F1 is the oldest OOS window and the highest-numbered fold is the most recent inside the receipt**. Select any fold heading to open its explanation modal. The modal shows the exact training and OOS dates, how many newer folds follow, the selected parameter, and the resulting OOS return, trades, and Sharpe.

“Most recent” is relative to the frozen study. It does not mean the fold uses today's market data or that its selected threshold is the current production parameter.

Read across a row to see whether one threshold remains useful through time. Read down a column to see whether a fold contains a broad plateau or one narrow winner. A broad group of similar eligible scores is usually more stable evidence than one isolated peak. The matrix does not answer which unselected candidate would have performed best OOS, because the protocol intentionally tested only the frozen winner.

### Selected gap → next OOS outcome

The selection journey shows the threshold selected using only that fold's trailing training data and the subsequent OOS return. All bars share the same scale and extend from a 0% center rule. Up/down arrows, signed values, and text labels preserve the meaning without relying on color alone. Select a fold to open its persisted OOS child receipt.

Look for:

- whether one threshold wins consistently;
- whether selected thresholds jump between extremes;
- whether a threshold's good training score repeatedly fails in test;
- whether profitability is concentrated in one market regime.

Threshold variation is not automatically bad—the protocol is adaptive—but violent, unexplained variation can reveal a noisy selection objective.

### Mean and median OOS Sharpe

- **Mean OOS Sharpe** is the arithmetic mean of non-null completed-fold test Sharpes.
- **Median OOS Sharpe** is the middle fold Sharpe.

If the mean is much higher than the median, a few unusually strong folds may dominate the result. If both are negative, the historical selection process did not generalize over the tested period.

Sharpe is not a guarantee and is especially noisy in short 30-day windows. Read it alongside trade counts, returns, drawdowns, and fold consistency.

### Profitable folds

This is the fraction of auditable completed test folds with positive total return. It measures time consistency, not return magnitude. A strategy may win in many tiny folds and lose heavily in a few; always check the curve and fold returns.

### Mean fold retention

For each eligible completed fold:

$$
retention_{fold}=\frac{test\ Sharpe}{selected\ winner\ training\ Sharpe}
$$

The displayed retention is the equal-weight arithmetic mean of those fold ratios.

- Around 1 means test Sharpe was similar to selected training Sharpe on average.
- Between 0 and 1 means some performance survived, but less than training suggested.
- Near 0 means little of the selected training performance survived.
- Negative means the sign reversed in test.
- Above 1 can be encouraging, but may also be caused by noisy or small training denominators.

Retention is a ratio, not a literal percentage of profits retained. It excludes folds with null test Sharpe or zero selected training Sharpe.

### Alpha decay

The detailed walk-forward receipt records the ordinary least-squares slope of fold Sharpe against fold number.

- Negative slope: later folds tended to have weaker Sharpe.
- Positive slope: later folds tended to improve.
- Null: fewer than two usable Sharpe observations.

Alpha decay is directional evidence, not a built-in pass/fail rule. Inspect the fold sequence before drawing a conclusion.

### Combined OOS equity curve

The curve concatenates and compounds each completed fold's test equity. Failed or unverifiable folds are excluded. Because each fold is independently flat at its boundaries, the curve is a research visualization rather than a reconstruction of a continuously invested account.

### Warnings and failures

Warnings are part of the result. A missing test-data window, an unpersisted child receipt, or a failed candidate selection cannot be silently converted into valid OOS evidence.

For internal promotion, require all expected folds to be auditable unless a separately documented review explains the exception. “Completed” means the aggregate contains at least one auditable test fold; it does not by itself mean “18 of 18 folds passed every internal promotion requirement.”

---

## Exhaustive Run candidate comparison {#exhaustive-run}

The detailed walk-forward page includes a second, optional study named
**Exhaustive Run**. It starts from the candidate TRAIN receipts already created
by the canonical walk-forward; it does not invent a new parameter grid.

For every fold, Python ranks eligible candidates using equal parts percentile
rank of training Sharpe and training net return, then retains at most five.
Repeated gap specifications are deduplicated, so a gap selected by many folds
appears once in the final sortable table with its selection frequency and most
recent selection.

The table deliberately separates two kinds of evidence:

- **Full two-year fit · look-ahead** shows net return, Sharpe, drawdown, total
  trades, and recent-trade concentration over all available history. These
  values are descriptive, because the candidates were chosen after looking at
  folds inside that history.
- **Forward stability · 18 OOS folds** tests the same fixed gap on every
  one-month TEST window. Use profitable-fold percentage, mean/median OOS
  Sharpe, alpha decay, mean retention, and the all-fold drill-down to judge
  robustness.

Sharpe is not portfolio growth. It measures return relative to return
volatility. **Net return** is the displayed percentage change from initial to
final equity and is the growth-oriented statistic.

The recency columns use completed-trade exit dates. “Recent” means the final
six calendar months of the frozen study. Recent share answers what fraction of
trades occurred there; rate ratio adjusts for the unequal lengths of the recent
and earlier windows. A rate ratio above 1 means trades arrived more frequently
per unit of calendar time recently.

Sort first by a forward statistic, not by full-period net return. A sensible
reading sequence is profitable folds, median OOS Sharpe, mean OOS Sharpe,
retention, alpha decay, OOS trade counts in the 18-fold dialog, and only then
the descriptive full-period return/drawdown/recency columns. The zero-cost V1
assumption still applies.

This comparison helps identify stable regions of the gap grid. It does not
authorize promoting the best-looking row to execution. Any chosen promotion
rule must be declared and tested with realistic costs and newer untouched or
paper data.

## The decision gate {#decision-gate}

### Integrity gates — required before interpretation

- [ ] Protocol ID is `spy-ema-normalized-gap` and version is `1.0`.
- [ ] The receipt has the intended study window and 180/30/30 rolling policy.
- [ ] All 18 expected OOS folds are present and auditable, or an exception is explicitly reviewed.
- [ ] No warning indicates missing test bars, persistence failure, corrupted lineage, or selection failure.
- [ ] The control, train candidates, frozen test specs, and aggregate receipts are linkable.
- [ ] Data snapshot and strategy-spec hashes are present.

If an integrity gate fails, stop. Do not reinterpret infrastructure failure as strategy performance.

### Research judgement — decide before looking for a winner

Set your acceptance policy before inspecting the next result. At minimum, decide:

- the minimum acceptable mean and median OOS Sharpe;
- the minimum profitable-fold fraction;
- the maximum acceptable OOS drawdown and loss concentration;
- the minimum acceptable retention;
- how much negative alpha decay is tolerable;
- the minimum OOS trade sample;
- what realistic-cost stress must pass.

The system intentionally does not invent these business thresholds. Writing them after seeing the result invites cherry-picking.

### V1 cannot be the final execution gate

V1 uses zero commission and zero slippage and ends on a fixed historical date. Therefore, even a strong V1 result should mean **“continue validation”**, not **“deploy now.”** A deployment candidate still needs realistic costs, current data, execution parity, and paper evidence.

---

## From research to future strategy execution {#future-execution}

### The important distinction: promote the process, not a historical winner

The 18 folds may select different thresholds. There is no single V1 output called “the permanent best threshold.” Choosing the most frequent winner or the best full-period threshold after inspecting all folds would create a new, untested rule.

The process validated by this study is:

> At a scheduled decision time, evaluate the frozen seven-threshold grid on the immediately preceding 180 days, select by the pinned eligibility and ranking rules, freeze the exact winner for the next 30 days, and do not change it mid-window.

That adaptive policy is what a future execution pipeline should reproduce.

### Recommended promotion pipeline

```text
Canonical historical WFA
        │
        ▼
Research acceptance policy
        │  no unresolved evidence gaps
        ▼
Current-date selection run
        │  latest trailing 180 days
        ▼
Immutable promotion receipt
        │  selected spec hash + validity window + evidence refs
        ▼
Execution-equivalence validation
        │  same signals in research and runtime adapters
        ▼
Alpaca V2: log_only → dry_run → paper trade
        │
        ▼
Monitor drift, fills, costs, and expiry
        │
        └── at 30-day boundary: reselect, freeze, and issue a new receipt
```

### What must be built before Alpaca paper execution

#### 1. A current-date, versioned selection protocol

V1's window is immutable for reproducibility. A new protocol version should select on a declared “as-of” boundary using the most recent 180 days, apply realistic cost assumptions, and preserve the same no-peeking rule. Its inputs and outputs must remain server-owned and receipt-backed.

#### 2. An immutable promotion receipt

The promoted artifact should record at least:

- source walk-forward protocol ID, version, and aggregate receipt ID;
- current selection run ID and every candidate receipt ID;
- selected threshold and full materialized `StrategySpec`;
- strategy-spec hash and data-snapshot revision;
- training start/end and selection time as `int64` UTC milliseconds;
- activation and expiration boundaries;
- fill, commission, slippage, and sizing contracts;
- human acceptance event and the policy version it satisfied.

Execution should accept the artifact, not a loose numeric threshold typed into a form.

#### 3. One canonical normalized-gap runtime implementation

The current Alpaca V2 paper runner supports the canonical `ema_crossover_signal`, whose gap is the original absolute \$0.20. The normalized-gap `StrategySpec` is currently a research/shadow specification and is not an admitted Alpaca strategy key.

Add the normalized parameter to the canonical Python decision owner or make the canonical strategy consume the promoted typed spec. Do not copy the basis-point formula into Angular, .NET, or a second runtime algorithm. The research evaluator and Alpaca adapter must produce the same ENTER/EXIT intent sequence for identical bars and the promoted threshold.

#### 4. Validation and admission evidence

Before the new strategy appears in Alpaca V2 deployment choices, require:

- golden formula and strategy parity tests;
- research-runtime signal equivalence on pinned data;
- current accepted strategy-validation evidence;
- no gating divergences;
- a current settings/spec hash;
- realistic fill and cost reconciliation.

#### 5. A staged paper rollout

Use the existing Alpaca V2 execution progression:

1. `log_only` — observe decisions without simulated or broker effects.
2. `dry_run` — exercise the intent and custody workflow without submitting an order.
3. `trade` on a paper account — submit only after evidence and broker-readiness gates pass.

Live-money trading is a separate validation program and is outside this research pipeline.

#### 6. Expiry and reselection behavior

The selected spec should expire at its declared boundary. At expiry:

- finish or safely manage any already-open position under an explicit policy;
- run the next train-only selection;
- freeze and validate the new spec before allowing new entries;
- fail closed if no candidate is eligible or the receipt cannot be persisted.

Do not silently keep an expired threshold, select during an open test/execution window, or switch thresholds mid-position without a separately validated transition policy.

### What to monitor after paper promotion

- expected versus observed 15-minute bar boundaries;
- research/runtime signal parity;
- selected threshold, activation time, and expiry time;
- order decision, submission, acknowledgement, and fill receipts;
- realized slippage and fees versus the research model;
- trade count and no-signal anomalies;
- drawdown and fold/regime drift;
- data revision or strategy hash changes.

Any unexplained decision divergence should block further promotion.

---

## Applying the pipeline to another strategy {#other-strategies}

Walk-forward analysis is reusable, but the protocol must be designed rather than copied mechanically.

For each new strategy, define and version:

1. **The fixed logic** — conditions that are not optimized.
2. **The candidate parameters** — a small, economically motivated grid.
3. **The training objective** — Sharpe, return, or another Python-owned metric.
4. **Eligibility** — minimum trades and required non-null statistics.
5. **Tie-breakers** — deterministic rules chosen before results are seen.
6. **Split policy** — rolling, anchored, or one chronological split, with a rationale.
7. **Warmup/state policy** — exactly what crosses the train/test boundary.
8. **Position boundary policy** — flat or continuous, with execution consequences.
9. **Cost/fill model** — representative of the intended execution venue.
10. **Promotion and expiry policy** — what is frozen, for how long, and what fails closed.

Keep the parameter grid small. Walk-forward testing reduces look-ahead leakage, but repeatedly redesigning the grid, dates, objective, or acceptance rules after seeing OOS results turns the OOS set into another training set.

---

## Technical details {#technical-details}

### System flow

```text
Angular research page
  └─ POST /api/jobs/spy_ema_walk_forward  (empty request body)
       └─ .NET jobs boundary: identity, progress, result, cancellation
            └─ Python internal worker
                 ├─ run_spy_ema_pipeline
                 ├─ canonical strategy engine (145 runs)
                 ├─ per-run RunLedger persistence
                 └─ walk-forward aggregate persistence
  └─ GET persisted protocol/control receipts
       └─ render Python-authored metrics and curves

Detailed walk-forward page
  └─ POST /api/jobs/spy_ema_exhaustive  (source walk_forward_id only)
       └─ Python ranks persisted TRAIN evidence, maximum five per fold
            ├─ one full two-year run per selected unique gap
            ├─ one 18-fold fixed-gap walk-forward per unique gap
            └─ persisted sortable Exhaustive Run artifact
```

The frozen public job request accepts an empty body so a client cannot quietly relabel custom dates or costs as canonical V1 evidence. The generic walk-forward API remains available for other research protocols, but it is not the V1 SPY entry point.

### Time boundaries

All wire and storage timestamps are Unix epoch milliseconds in UTC. Fold intervals are half-open: `[start_ms, end_ms)`. The engine consumes date-inclusive inputs, so the runner converts each exclusive fold end to the preceding New York calendar date. This prevents one boundary day from appearing in adjacent V1 folds.

### Persistence and lineage

Logical artifact layout:

```text
<artifacts_root>/
├── <run_id>/
│   ├── ledger.json
│   └── result.json
├── walk-forward/<walk_forward_id>/
    ├── config.json
    └── result.json
└── exhaustive-run/<exhaustive_run_id>/
    ├── config.json
    └── result.json
```

The full-window control's `run_id` is the aggregate's `parent_run_id`. Training and test child ledgers use the `walk_forward_id` as their parent. The aggregate records candidate specs and hashes, selected parameters, training metrics, test metrics, selection failures, warnings, and the combined OOS curve.

### Fail-closed behavior

- Invalid or empty splits produce a persisted failed aggregate.
- No eligible training candidate stops the pipeline before that fold's test.
- A training receipt persistence failure prevents selection.
- A test with zero reported-window bars is failed evidence, not a flat return.
- An unpersisted test receipt is excluded and fails the aggregate.
- If every test fold fails, no headline aggregate is reported as completed.

### Numerical ownership

Python owns:

- EMA, RSI, fresh-cross state, and the basis-point formula;
- candidate eligibility and ranking;
- fold metrics, retention, alpha decay, and curve compounding;
- reproducibility hashes and receipts.

.NET transports long-running job state. Angular formats, charts, and links to evidence. Neither transport layer recalculates strategy outcomes.

### Implementation map

| Responsibility | Canonical location |
|---|---|
| Frozen SPY protocol | `PythonDataService/app/research/walk_forward/spy_ema.py` |
| Fold orchestration and aggregation | `PythonDataService/app/research/walk_forward/runner.py` |
| Split policies | `PythonDataService/app/research/walk_forward/splits.py` |
| Deterministic selection | `PythonDataService/app/research/walk_forward/selection.py` |
| Result contracts | `PythonDataService/app/research/walk_forward/result.py` |
| Normalized strategy spec | `PythonDataService/app/engine/strategy/spec/fixtures/spy_ema_normalized_gap.spec.json` |
| Basis-point primitive | `PythonDataService/app/engine/strategy/spec/primitives.py` |
| Jobs integration | `PythonDataService/app/routers/jobs.py` and `Backend/Jobs/JobsApi.cs` |
| Research page | `Frontend/src/app/components/research-lab/spy-ema-walk-forward/` |
| Exhaustive Run selection and recency | `PythonDataService/app/research/exhaustive_run/` |
| Exhaustive Run read API | `PythonDataService/app/routers/exhaustive_runs.py` |
| Sortable comparison and fold drill-down | `Frontend/src/app/components/research-lab/walk-forward/exhaustive-run-*/` |

---

## Limitations you should remember {#limitations}

- One instrument: SPY.
- One historical window: 2024-08-01 to 2026-08-01.
- One bar/session model: 15-minute regular-session bars.
- Seven predeclared thresholds; no continuous optimization.
- Zero costs in V1.
- Short test windows can make Sharpe unstable.
- The research engine starts every OOS fold flat.
- The aggregate is historical evidence, not a probabilistic forecast.
- Walk-forward analysis reduces selection bias; it does not eliminate multiple-testing or researcher degrees of freedom.
- Reusing the same OOS history to repeatedly redesign V2, V3, and V4 eventually contaminates that history.
- No current automated promotion, expiration, or monthly reselection path exists for normalized-gap Alpaca execution.

---

## Glossary {#glossary}

| Term | Meaning here |
|---|---|
| **Train / in-sample** | Historical window used to score and select a threshold. |
| **Test / OOS** | The immediately following window hidden from that fold's selection. |
| **Fold** | One train → freeze → test experiment. |
| **Rolling window** | Fixed-length train window that moves forward; old data drops out. |
| **Candidate** | One fully materialized strategy spec with a declared gap threshold. |
| **Frozen spec** | Exact selected strategy JSON and hash used for the test window. |
| **Control** | Original full-window \$0.20 absolute-gap strategy. |
| **Retention** | Fold test Sharpe divided by that fold winner's train Sharpe, averaged across eligible folds. |
| **Alpha decay** | Directional slope of fold Sharpe over time. |
| **Pre-roll** | Feed historical bars into stateful indicators while suppressing entries before test. |
| **Receipt / ledger** | Persisted inputs, lineage, hashes, status, and results for audit and replay. |
| **Fail closed** | Refuse to create favorable-looking evidence or execution permission when proof is incomplete. |

---

## Final mental model {#final-mental-model}

Use a normal backtest to ask, “Did this fixed strategy work over this history?”

Use this walk-forward analysis to ask, “Did this **rule for choosing a strategy parameter from trailing data** keep working on the next unseen period?”

Use a future promotion pipeline to ask, “Is the latest frozen choice, with current data and realistic execution assumptions, sufficiently evidenced and safely admitted to Alpaca paper execution?”

Those are three different decisions. Keeping them separate is the design.

For scientific provenance, see the internal walk-forward references in `docs/references/walk-forward.md` and `docs/references/spy-ema-normalized-gap-walk-forward.md`. The original design discussion is preserved in the [SPY EMA research conversation](https://chatgpt.com/c/6a7ff340-8f44-83ea-9ccf-bb8b13ca2eee).
