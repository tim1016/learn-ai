# Golden Search: methodological evidence for PRD #2696

Reviewed 2026-09-30 against [PRD #2696](https://github.com/tim1016/learn-ai/issues/2696) and checkout `6d92419314a4599ff62dff4784032adc3b61bad4`. This is an adversarial architecture/research review, not a numerical validation of a strategy. No production code or GitHub issue was changed by this note.

**Conclusion:** retain the guided research workspace and shared sweep engine. Change the evidence procedure before implementing promotion. The original procedure can find interesting settings, but its narrowed walk-forward cannot establish an unseen record, its one-shot restriction does not survive repeated studies, and its statistical displays overstate what the available evidence supports. The recommendations below are proposed product decisions under the user's present request to correct the plan; they do not claim the original owner had already approved these changes.

## 1. Freeze the experiment before showing its outcomes

**Finding.** Stories 26–32 select fold lengths after observing full-period search results, narrow every fold from that search, and initialize every fold from its winner. A later fold-test result can therefore influence an earlier fold's search space and starting point. Restricting the final scoring call to the test dates does not remove that information path. Generalization evaluation must keep model selection inside the training partition; this is the same selection/evaluation separation explained by [scikit-learn's nested evaluation example](https://scikit-learn.org/stable/auto_examples/model_selection/plot_nested_cross_validation_iris.html). This application to the PRD is our inference, not a claim that random shuffled cross-validation is appropriate for trading.

**Correction.** At launch, freeze original ranges, initial settings, objective, constraints, search order, budget, training/test lengths, costs, and boundary policy. In each chronological fold run the entire declared search using only its training history; test only its selected configuration. Do not import a whole-period narrowed range or current approved setting whose creation depended on a later fold's data. A pre-existing configuration may be a retrospectively frozen comparison, but that does not make its own historical creation point-in-time.

Keep the first whole-period zoom search as an **exploration** display and candidate generator. Keep the existing frozen retention verdict as a heuristic within properly isolated folds; do not relabel the contaminated variant as unseen. Current [`walk_forward_study/service.py`](../../PythonDataService/app/research/walk_forward_study/service.py) already distinguishes training-winner evidence from exploratory test cells, and [`verdict.py`](../../PythonDataService/app/research/walk_forward_study/verdict.py) explicitly calls its thresholds judgment calls.

**Required test:** mutate bars strictly inside an outer test window; that fold's ranges, starting point, training-selected parameters, and training metrics must remain unchanged. An identical test over the original whole-period narrowing should demonstrate the leak.

## 2. Make Grid Search and zoom search complementary

**Finding by counterexample.** Coordinate search can stop at `(0,0)` when objective values are `f(0,0)=10`, `f(1,0)=9`, `f(0,1)=9`, `f(1,1)=20`. Neither individual move improves the objective. One-dimensional neighboring checks do not reveal the beneficial joint move. A completed unchanged pass means coordinate stability at the tested resolution, not a global optimum or a robust strategy.

**Correction.** Use zoom search for inexpensive refinement and bounded Grid Search for declared interacting pairs, initially fast/slow EMA and RSI lower/upper gates. Configure those checks before outcomes; count them in the same study budget and run them within training windows whenever they affect selection. A heat map is evidence about the displayed pair with other parameters fixed, not a proof of a six-dimensional plateau. Report boundary winners, valid/invalid cells, tested coverage, and search-limit termination distinctly. Offer **keep current settings** and **insufficient evidence** as normal decisions.

## 3. Replace round-dependent ranks as the optimization objective

**Finding by counterexample.** With metric order `(profit, Sharpe, profit/drawdown)` and weights `(0.5,0.3,0.2)`, configurations `A=(10,1,1)` and `B=(9,2,2)` both have weighted competition rank 1.5. Add `C=(9.5,0.5,0.5)` and A wins, 1.5 versus B's 2.0. Add `D=(8,1.5,1.5)` instead and B wins, 1.5 versus A's 2.0. A and B's measurements did not change. A round can therefore report improvement solely because the comparison set changed. The same score cannot support a comparable path or convergence claim across rounds.

**Correction (product policy).** Default to one declared primary measure, initially the existing canonical Sharpe ranking, with separately declared risk and sample constraints. Show profit, drawdown and trade count alongside it. The existing [`sweep/ranking.py`](../../PythonDataService/app/research/sweep/ranking.py) already supplies a deterministic single-measure ordering; extend that seam rather than introducing an unrelated authority. If the rank blend remains, label it a preference ordering within the displayed candidate set and do not claim monotonic objective progress.

Define return/drawdown using consistent units and a named formula; dollar profit divided by a drawdown fraction is not a dimensionless recovery ratio. The present [`statistics.py`](../../PythonDataService/app/engine/results/statistics.py) returns drawdown as a fraction. Reject undefined ratios rather than manufacturing an unbeatable value for zero observed drawdown. Freeze objective/risk settings per study; later edits create a new protocol revision.

## 4. A holdout belongs to a research history, not a study row

Repeated use of a holdout can itself become selection: a failed test prompts another strategy or parameter search and eventually a favorable test appears. The authors explicitly demonstrate the weakness of repeated holdout use in [their backtest-overfitting study](https://www.davidhbailey.com/dhbpapers/overfitting.pdf). A count restricted to earlier studies of the same program and symbol misses cross-strategy inspection, other research pages, deleted studies, and evidence the user examined elsewhere.

**Correction (product policy).** Preserve an append-only exposure ledger independently of study deletion. Record intervals/data identity, research family, strategy, symbol, viewed results and protocol revisions across research tools. Distinguish **reserved**, **first reveal in the recorded family**, **previously exposed**, and **unknown prior exposure**. The application cannot prove the trader never saw that market history outside it. Same-window retries remain useful exploration, but never reset their freshness by creating or deleting a study. Permit an identical technical retry to recover the original result; refuse a new candidate masquerading as that retry.

Before reveal, freeze one candidate, the incumbent comparison, all pass/fail rules, costs, and interpretation. Used or unknown history cannot support the strongest untouched-evidence badge. A fresh later paper observation can provide another independent chronological check; simply extending an already observed interval is not a clean new holdout.

## 5. DSR requires a real measurement contract

The [original DSR paper, equation 2 and appendix 3](https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf) uses cross-trial Sharpe variance and independent trial count, plus the selected return series' length, skew and Pearson kurtosis. Raw configuration count is not independent-trial count. Its numerical example reports approximately `0.9004`; the printed precision cannot support a `1e-9` comparison to that number. Its probability-style statistic is conditional on the model's assumptions, not a probability of future profitability.

**Repository finding.** [`edge/robustness_stats.py`](../../PythonDataService/app/engine/edge/robustness_stats.py) explicitly has no validated reference fixture and uses unit variance for the expected maximum. [`signal/diagnostics.py`](../../PythonDataService/app/research/signal/diagnostics.py) substitutes approximately `1/T` rather than observed trial variance. They are not interchangeable, and the edge caller currently uses trade count as observation count and period Sharpes as trials in [`cross_asset_runner.py`](../../PythonDataService/app/engine/edge/cross_asset_runner.py). Do not copy either caller into Golden Search.

**Correction (implementation policy).** Canonicalize and fixture the formula before exposing it. Use a consistent per-observation return basis and moment estimator; never insert annualized Sharpe into a daily-observation formula. The canonical backtest metrics already use daily equity returns when available in [`statistics.py`](../../PythonDataService/app/engine/results/statistics.py). Record all selection-influencing trials: grid/zoom searches, recent-window selection, changed weights/ranges, relevant prior studies and any diagnostics used to select. Cache hits are not new independent experiments. Do not pool Sharpes from different windows into one variance estimate without a defined common evaluation basis.

An adaptive, correlated, incompletely recorded research history may not admit a credible DSR estimate. Show **not estimable** with its missing inputs, or an explicitly assumption-limited diagnostic; do not show a green probability-of-success badge. Reserve return-series storage and trial lineage in the data contract now. Omit DSR from promotion gates in the first slice until its scope and assumptions are validated.

**Fixture correction.** Independently evaluating the published example gives `SR0=0.11317200186513217`, `DSR=0.9003968344493902`: `N=100`, daily Sharpe variance `0.002` (annual variance `0.5` divided by `250`), `T=1250`, annual Sharpe `2.5`, skew `-3`, kurtosis `10`. These are our computed values, not extra digits printed by the authors. Use a rounding-aware paper check plus a separately attributed, high-precision independent equation oracle for tight numerical parity. That is a new reference artifact, not permission to widen a failing implementation's tolerance.

## 6. Five trades and half-retention are policies, not confidence

The [minimum-track-record research](https://www.davidhbailey.com/dhbpapers/sharpe-frontier.pdf) relates evidential strength to sample length, return characteristics and the chosen threshold; [Lo's Sharpe research summary](https://alo.mit.edu/publications/page/18/) also explains why serial dependence affects uncertainty. Neither supplies a universal five-trade guarantee.

**Correction.** Keep five trades only as a computational eligibility floor. Display days observed, trades, exposure, calendar coverage and dependence/uncertainty limitations. Let **insufficient evidence** differ from failure. A fixed three-month exam is a scheduling default, not a promise of statistical power. Freeze its duration before reveal; a sparse outcome may require a new forward observation period.

Half-retention can pass a poor strategy or fail a better one: training Sharpe 4, exam 1.5, incumbent 0.5 fails; training 0.2, exam 0.11, incumbent 1 passes. Keep retention descriptive. The actual replacement decision needs a predeclared comparison on the same dates, capital, sizing, fees, slippage and fill assumptions, with risk tolerances. Rename monthly P&L/win-rate charts **performance by month**: the PRD has not specified an alpha model, and monthly variation alone does not establish causal edge decay.

## 7. Distinguish repeatability, research evidence, and deployment permission

The proposed runtime replay proves future code reproduces a stored trace on stored bars; it does not independently validate the original outputs or demonstrate transfer to live data. Existing [ADR 0061](../architecture/adrs/0061-golden-validation-is-a-scoped-human-promotion-over-immutable-run-evidence.md) already separates scoped human promotion, corpus coverage, and Python-only Manual override from profitability claims.

**Correction (product policy).** Present separate statuses for replay, engine parity, research evidence, human decision, and deployment readiness. Paper observation can be recommended, but the owner interview on 2026-09-30 explicitly chose golden settings available for either Paper or Live in Deploy. Approval must produce the qualified tuple and scoped Golden review; account/mode choice and existing operational admission still happen in Deploy. The earlier recommendation for a separate Golden Search Live-release approval is superseded. Preserve the owner-mandated IBKR market-data → Alpaca-order boundary. Costs and live-data reconciliation remain visible unresolved evidence where untested, rather than an omitted assumption hidden behind a successful replay.

Freeze the incumbent at launch so comparisons do not silently change after another study is approved. Distinguish **unchanged historical comparison** from a genuinely point-in-time incumbent. Do not stitch per-fold dollars into an apparent continuously tradable curve without defining cash normalization, positions at boundaries, liquidation costs and reset/carry rules. Until continuous replay exists, label the chart **linked fold returns under reset assumptions** and disclose those assumptions.

## 8. Correct feasibility claims before drawing the estimate

The PRD cites eight-way concurrency; current [`sweep/execution.py`](../../PythonDataService/app/research/sweep/execution.py) deliberately runs one cell at a time and documents the earlier memory failure at eight. [`grid_search/service.py`](../../PythonDataService/app/research/grid_search/service.py) estimates sequential duration. Reuse that implementation with per-stage window lengths; show an estimate/range, not a stopwatch promise. Enforce the 5,000 ceiling at dispatch as well as preflight, including baseline, interaction, full-detail, holdout, and proof executions under an explicitly defined counting rule.

## Build acceptance essentials

1. Fold selection does not change when its future test data changes.
2. Pairwise-trap and rank-reversal examples above remain documented regression cases.
3. Failed/deleted/restarted studies cannot erase holdout exposure or trial lineage.
4. DSR reports absent assumptions as unavailable; paper rounding and independent numeric precision are tested separately.
5. A good replay cannot change insufficient research evidence into a pass.
6. The page can end honestly at keep current, gather more observations, or approval of an exact golden configuration available in Deploy; it never forces a winner.
