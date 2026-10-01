# Golden Search original proposal

Superseded by [the corrected PRD](golden-search-2696.md). Preserved verbatim from [issue #2696](https://github.com/tim1016/learn-ai/issues/2696), before this review; issue last updated 2026-09-30T21:36:41Z. This snapshot is comparison material, not current implementation authority.

## Problem Statement

I trade the EMA crossover on one golden configuration: gap 0.20 with RSI gates 50–70, and EMA 5/10 with a 5-bar hold fixed in code. I have no disciplined way to find a better configuration, prove it would have held up, and put it into Paper or Live under my own sign-off.

The pieces exist but don't connect:

- **Grid Search** tries every combination of the values I list. That is too many backtests once a strategy has half a dozen knobs, and it doesn't refine.
- **Walk-Forward** tells me whether a grid's winner kept working, but it can't hand a result to anything that approves it.
- **Moving the golden configuration** means editing code constants, running two qualification scripts, and restarting. There is one configuration per strategy for every stock. Each move has stranded every bot sealed to the old one, which happened on 2026-09-01 and again on 2026-09-17.
- **Golden Validation** records my human sign-off, but only for a hand-picked Strategy Lab run. It never changes what Deploy offers as the qualified configuration.
- **The evidence is scattered.** It sits across pages as summary numbers only. No page shows me, in one place, how a proposed configuration was found, how it held up on data the search never saw, whether its edge is fading, and how it compares with what I trade today.

## Solution

A new **Golden Search** page under Strategy Tools. A study there finds, tests, and puts forward a new configuration for one Signal Program on one stock, then lets me sign it off from the evidence. It runs in stages and pauses after every one:

1. **Zoom-in.** Starting from the stock's current qualified configuration, the search tunes one knob at a time, coarse to fine, and keeps each improvement. It goes round all the knobs until nothing changes. It never sees the last 3 months of data, which are held back as the final exam.
2. **Walk-forward.** In each rolling training window, it re-runs the one-knob search inside the narrowed ranges the zoom-in ended with, then tests the fold winner on the next unseen window. The verdict is the existing frozen one.
3. **Pick.** Two candidates are offered: the zoom-in winner and the latest-window winner. Each comes with parameters, key metrics, month-by-month alpha decay, a spike-or-plateau check, luck-adjusted Sharpe, equity and drawdown curves, and trades drawn on the price chart. Today's qualified configuration is the benchmark everywhere. I pick one.
4. **Final exam.** My pick, and only my pick, runs once on the held-back 3 months, beside the benchmark. Three checks are marked: lost money, fewer than 5 trades, kept less than half its search-period Sharpe.
5. **Sign-off.** I approve from the page. A failed exam needs my written reason. Approval:
   - replaces that stock's qualified configuration immediately, with no code change or restart;
   - builds its own golden proof from the study's frozen data;
   - records the matching Golden Validation approval;
   - unlocks Paper and Live together.

   Bots sealed to the replaced configuration are listed first. Running ones finish their current run and then need a new deploy.

The winner of every search step is chosen by blending three scores: net profit, Sharpe, and profit ÷ worst drawdown. Each score becomes a place among the values tested, and the places are weighted by one global setting (default 50 / 30 / 20). The page shows the estimated backtest count and run time before launch, and refuses any study estimated above 5,000 backtests.

## User Stories

### Knobs and setup

1. As the owner, I want each strategy to declare its own canonical search knobs — with a default range, a default starting step and a default tuning order — so that a study starts from settings we have agreed on rather than whatever I type that day.
2. As the owner, I want the EMA crossover's canonical knobs to be gap, RSI low gate, RSI high gate, fast EMA length, slow EMA length and hold time, so that the search can tune everything that shapes the strategy except the RSI length.
3. As the owner, I want fast EMA length, slow EMA length and hold time to become real settings whose defaults are today's 5, 10 and 5 bars, so that the strategy's behaviour at its defaults does not change and its LEAN parity fixture still passes.
4. As the owner, I want the launch form pre-filled with the strategy's canonical ranges and order, and editable, so that I can experiment without losing the agreed defaults.
5. As the owner, I want the study to record the exact ranges, order, steps and weights it ran with, so that I can later see precisely what was searched.
6. As the owner, I want to pick the stock for a study from the shared symbol picker, so that a study is for exactly one stock.
7. As the owner, I want the page to refuse a stock whose lake data does not cover the search period plus the final-exam months, naming the missing sessions, so that a study never runs on holes.
8. As the owner, I want the last 3 months of available data held back automatically as the final exam, so that the search never sees them.
9. As the owner, I want to see, before launch, the estimated number of backtests and the estimated run time, broken down by stage, so that I know what I am starting.
10. As the owner, I want a launch refused when the estimate is above 5,000 backtests, with the reason, so that a study cannot run away.
11. As the owner, I want knob combinations that break a rule (fast EMA not shorter than slow EMA, RSI low not below RSI high) skipped and shown as skipped, so that invalid settings never produce results.

### Scoring

12. As the owner, I want one global setting holding the weights for net profit, Sharpe, and profit divided by worst drawdown, so that every study scores the same way until I change it.
13. As the owner, I want those weights to default to 50 / 30 / 20, so that profit leads but the other two still count.
14. As the owner, I want every step to turn each score into a place (1st, 2nd, …) among the values tested and blend the places with my weights, so that scores in different units can be combined fairly.
15. As the owner, I want a value with fewer than 5 trades, or a score that cannot be computed, to be unable to win a step, so that a lucky handful of trades never becomes golden.
16. As the owner, I want a tie to keep the knob's current value, so that the search only moves when something is strictly better.
17. As the owner, I want each study to keep a snapshot of the weights it used, so that changing the global weights later never rewrites an old study.

### Stage 1 — Zoom-in

18. As the owner, I want the zoom-in to start from today's qualified configuration for that stock, so that the search begins where I trade today.
19. As the owner, I want the zoom-in to tune one knob at a time, keeping each improvement before moving to the next knob, so that every tested set is a real, complete configuration.
20. As the owner, I want each knob tuned coarse-to-fine — a spread of values across its range, then a finer spread around the best, for a few rounds — so that the search is cheap but precise.
21. As the owner, I want the zoom-in to go round all the knobs again until a full round changes nothing (with a maximum number of rounds), so that the result is stable.
22. As the owner, I want the zoom-in to never re-run a configuration it has already scored, so that repeated points cost nothing.
23. As the owner, I want the study to pause after the zoom-in and show me the result before anything else runs, so that I can stop a study that is going nowhere.
24. As the owner, I want a Zoom-in tab that shows, for every knob and every round, the score of each value tried and which one won, so that I can see the path the search took.
25. As the owner, I want the Zoom-in tab to show the final narrowed range for each knob, so that I know exactly what walk-forward will search.

### Stage 2 — Walk-forward

26. As the owner, I want to set the walk-forward training and test lengths at the pause after the zoom-in, so that I choose them with the zoom-in evidence in front of me.
27. As the owner, I want walk-forward to re-run the one-knob search inside each training window, only within the narrowed ranges and starting from the zoom-in winner, so that it checks whether that area keeps working.
28. As the owner, I want each fold winner tested on the following unseen window, so that I see how the picked settings would have done.
29. As the owner, I want walk-forward to reuse the existing frozen verdict (still worked / got worse / stopped working / too few trades / could not be judged), so that its verdict means the same as on the Walk-Forward page.
30. As the owner, I want a Walk-forward tab listing each window's training winner, its training result, its test result and its kept share of Sharpe, so that I can study every fold.
31. As the owner, I want the Walk-forward tab to show a combined equity line over all the unseen test windows, so that I see the procedure's out-of-sample record as one curve.
32. As the owner, I want the dashboard to state plainly that the walk-forward ranges were narrowed using the whole search period, so that I remember those results look better than a fully unseen test.
33. As the owner, I want the study to pause after walk-forward, so that I pick the candidate myself.

### Candidates and the pick

34. As the owner, I want two candidates offered — the zoom-in winner and the latest-window winner — so that I can choose between the best set over the whole search period and the best set on the most recent data.
35. As the owner, I want the latest-window winner defined as the one-knob search over the most recent training-length window before the exam, so that it answers "what would I pick today".
36. As the owner, I want a card for each candidate and for today's qualified configuration showing every parameter plus profit, Sharpe, worst drawdown, win rate, trade count and trades per day, so that I can compare them side by side.
37. As the owner, I want an alpha-decay chart showing each candidate's and today's results month by month, so that I can see whether an edge is fading.
38. As the owner, I want a spike-or-plateau check showing how much each candidate's score drops when each knob moves one step either way, so that I can spot a winner that sits on a narrow spike.
39. As the owner, I want a luck-adjusted (deflated) Sharpe for each candidate that accounts for how many configurations were tried, so that I can see how much of its Sharpe could be luck.
40. As the owner, I want equity and drawdown curves for each candidate and for today's set, so that I can see the path, not just the totals.
41. As the owner, I want every trade drawn on the stock's price chart with the candidate's EMA lines and RSI underneath, so that I can check the entries and exits by eye.
42. As the owner, I want to pick exactly one candidate to take the final exam, so that the exam stays a one-shot check.

### Stage 3 — Final exam

43. As the owner, I want only my pick, and today's qualified configuration as the benchmark, to run on the final-exam months, so that the exam is an honest first look.
44. As the owner, I want the other candidate to be barred from the exam once my pick has taken it, so that I cannot shop for a set that passes.
45. As the owner, I want the exam to mark three checks — lost money, fewer than 5 trades, or kept less than half of its search-period Sharpe — so that a bad exam is unmistakable.
46. As the owner, I want the exam tab to show how many earlier studies of this strategy and stock have already used any of these exam months, so that I know how "unseen" they still are.
47. As the owner, I want the exam to show the pick's equity, drawdown and trades beside today's set, so that I can see whether the new set is better than what I trade today.
48. As the owner, I want the study to pause after the exam, so that sign-off is always a separate, deliberate step.

### Stage 4 — Sign-off

49. As the owner, I want to approve the examined set from the Sign-off tab, so that signing off happens where the evidence is.
50. As the owner, I want a failed exam to be approvable only with a written reason, and that reason plus the failed checks recorded permanently on the approval, so that an override is always visible.
51. As the owner, I want approval to make the set the stock's qualified configuration immediately, with no code change or restart, so that I can deploy it straight away.
52. As the owner, I want approval to replace the previous qualified configuration for that strategy and stock, so that there is exactly one qualified configuration per strategy per stock.
53. As the owner, I want the Sign-off tab to list every bot sealed to the configuration being replaced before I confirm, so that I know which bots will need a new deploy.
54. As the owner, I want a bot that is running on the replaced configuration to finish its current run, but not start or resume on it again, so that approval never causes a surprise exit mid-trade.
55. As the owner, I want approval to also record the Golden Validation approval for that exact configuration, so that one click clears every Deploy check that needs my sign-off.
56. As the owner, I want the Golden Validation note written for me when the exam passed (naming the study, the exam result and the checks passed), and my own written reason used when it failed, so that every record carries a note without extra typing on a clean pass.
57. As the owner, I want that Golden Validation record to say honestly that it is a Manual override over Python-only evidence, so that nobody mistakes it for a LEAN-paired engine agreement.
58. As the owner, I want an approved configuration usable in Paper and Live alike, so that I decide where to deploy it first.
59. As the owner, I want Deploy's "Use qualified configuration" to offer the approved set for that stock, so that deploying the new golden set is one click.
60. As the owner, I want stocks without their own approval to keep today's registry configuration, so that approving SPY does not change AAPL, QQQ or TSLA.
61. As the owner, I want the approval to build its own golden proof from the study's frozen data, so that the configuration is covered without scripts or a commit.
62. As the owner, I want a code change to the strategy to put every approved configuration of that strategy back to "needs re-proof" until its stored proof replays identically on the new code, so that an approval can never vouch for code it did not see.
63. As the owner, I want a one-click re-proof that replays the stored proof on the current code and either restores coverage or tells me the approval no longer holds, so that routine code changes don't force a new study.

### History, lifecycle, and the future

64. As the owner, I want a history list of Golden Search studies with strategy, stock, stage, status, candidates and outcome, so that I can revisit past research.
65. As the owner, I want to stop a study at any pause, and cancel it while a stage is running, so that I control the machine.
66. As the owner, I want a stage interrupted by a restart to resume from its completed backtests, so that I never pay twice for the same work.
67. As the owner, I want a study that led to an approval to be undeletable, so that the evidence behind a live configuration never disappears.
68. As the owner, I want a list of current qualified configurations per strategy and stock with a link to the study that produced each, so that I can see at a glance what is approved and why.
69. As the owner, I want every chart and table to include today's qualified configuration as the benchmark, so that each view answers "is this better than what I have?".
70. As the owner, I want the stages to be a list the study walks through, so that a Monte Carlo clustering stage can be added later without redesigning the study.
71. As an agent implementing a new strategy, I want a clear declaration format for search knobs, so that the strategy becomes searchable in Golden Search by declaring them.

## Implementation Decisions

### Vocabulary

- **Golden Search** is the page and the study, meaning "a Golden Search study".
- **Qualified configuration** is the existing term behind Deploy's "Use qualified configuration": the exact parameter set whose corpus covers a (Signal Program, stock) pair. This PRD makes it **per stock** and lets a sign-off set it at runtime.
- **"Golden"** keeps the three separate meanings ADR 0061 already separates: golden fixture, golden-qualification corpus, and Golden Validation. Approval in Golden Search writes a qualified configuration *and* a Golden Validation record. The two stay distinct facts.
- New terms go into `CONTEXT.md`: Golden Search study, search knob, zoom step, candidate, final exam, exam reuse count, runtime golden proof, re-proof.

### Search-knob declaration (registry)

- A Signal Program registration gains an optional **search-knob declaration**: an ordered list of knobs. Each knob has a name matching a public numeric parameter, a low and high bound, the points per zoom round, a minimum step (1 for integer knobs), and an integer/decimal kind.
- The declaration also names pairwise ordering constraints. For EMA these are fast EMA < slow EMA and RSI low < RSI high.
- A strategy appears in Golden Search if and only if it registers a Signal Program and declares search knobs. The existing sweep eligibility predicate stays the base rule.
- Defaults: 5 points per round, 3 zoom rounds, at most 3 passes. All three are declared per strategy and editable on the launch form.

### EMA crossover: expose the fixed knobs

- The fast EMA length, slow EMA length and hold bars become real parameters of the EMA crossover Signal Program, with defaults 5, 10 and 5 bars. Today they are fixed in the algorithm and in the contract's signal declaration.
- The RSI length stays fixed at 14. The 15-minute decision cadence is unchanged.
- The hold duration is derived from hold bars times the decision cadence, not from a literal.
- Parameter units gain a bars unit.
- This bumps the parameter-schema version and the program version. The committed corpus is regenerated at the registry point, and the build receipts are re-minted through the existing scripts.
- Every existing EMA seal is stranded once. The owner confirmed no bot is running.
- The ENG-007 LEAN parity fixture must still pass bit-exact at the defaults.
- EMA's declared knobs, in order: gap, RSI low, RSI high, fast EMA, slow EMA, hold bars. Default ranges are declared in the registry and editable per study.

### The one-knob zoom-in search (pure module)

- The search is a pure, deterministic function. It takes the knob declarations, a starting point, the knob ranges, and a batch scorer (a list of configurations in, a list of cell metrics out). It returns the full search path and the winner. It performs no I/O itself.
- **Pass:** for each knob in declared order, run the zoom rounds, lock in the winner, then move to the next knob. Repeat passes until a whole pass changes nothing, or the pass limit is reached.
- **Zoom rounds:**
  - Round 1 spreads the declared number of points across the knob's range, and always includes the current value.
  - Each later round spreads the same number of points across one previous-round spacing either side of the round winner, clipped to the range.
  - Integer knobs stop refining once the spacing would drop below 1.
- **Constraints:** a value that breaks a declared constraint against the current values of the other knobs is skipped, and the skip is recorded on the path. It never counts as a loss.
- **Cache:** a configuration already scored in the same window is reused, never re-run. The cache key is the existing `params_hash`.
- **Step winner:** decided by the blended-places rule (below) over every value scored in that round, including the current value. On a tie the current value is kept. Among non-current values that tie, the one nearest the current value wins, then the lower value.
- **Narrowed ranges:** for each knob, the span of the last round it ran, around its final value.

### Blended-places ranking (shared ranking contract)

- The canonical ranking module gains a blended-places ranking beside the existing single-measure `leader`. There is one implementation, which Golden Search and any future caller use.
- **Eligibility:** a cell is eligible only if it completed, has at least the minimum trade count (default 5), and has finite values for all three scores.
- **Scores:** net profit, Sharpe, and profit ÷ max drawdown. A zero-drawdown cell with positive profit ranks first on that score; with zero profit it is ineligible for it.
- **Blend:** each eligible cell gets its competition place (1 = best, with ties sharing a place) on each score. The weighted mean of the places, using the study's weight snapshot, is the blended place. The lowest blended place wins.
- **Weights:** a single global **research scoring setting**, in a new Python-owned research table under ADR 0055, holding the three weights and a revision. The defaults are 50 / 30 / 20. The weights must be non-negative, and they are normalized to sum to 1.
- Each study snapshots the weights and revision into its receipt at launch. A GET/PUT pair exposes the setting, and the page edits it in a small global panel.

### A zoom step is an owned Grid Search sweep

- Each zoom round is one Grid Search sweep. The moving knob takes a value list of the round's uncached values, and every other knob takes a single-value list at its current value. The sweep goes through Grid Search's existing callable interface (prepare launch, create, execute), exactly as Walk-Forward does.
- This inherits unchanged:
  - one frozen data snapshot and one code identity per study;
  - uniform run-up sizing;
  - summary-only cells and cancellation;
  - the attempt fence;
  - Finish/resume and its refusals.
- Grid Search's owner kind gains `golden_search`, through both the Python owner-kind type and the table's owner-kind check constraint (a new research schema version). Owned sweeps stay out of Grid Search history, the Grid Search router refuses them as owned by a study, and they are deleted with the study.
- The step winner is chosen by Golden Search with blended places over the round's cells plus the reused cached cells. Grid Search's single-measure leader is not used for step selection.

### Walk-forward stage

- Folds come from the existing walk-forward fold planner, with the study's **search-period end** as the exclusive end, so the final-exam months are never inside a fold. Training and test lengths are set at the pause after the zoom-in (default 6 and 2 months). A range that doesn't divide into whole folds is refused with the nearest valid ends, as today.
- Per fold: run the one-knob search on the training window, inside the narrowed ranges, starting from the zoom-in winner. Then run the fold winner on the test window as a single-cell owned sweep.
- The verdict is the existing frozen walk-forward verdict (median fold retention, coverage floor, 0.5 threshold, five labels), over the fold winners. The study doesn't re-implement it.
- **Latest-window winner:** one more one-knob search over the most recent training-length window ending at the search-period end, with the same narrowed ranges and starting point.
- The dashboard carries a fixed disclosure: the walk-forward ranges were narrowed using the whole search period.

### Candidates, full-detail evidence, and the extra checks

- At the start of the pick stage, three configurations are re-run as **full-detail** backtest runs over the search period, persisted to `research_backtest_runs` with trades, equity and program version (the existing `save_study` path, not summary-only): the zoom-in winner, the latest-window winner, and the benchmark (the stock's current qualified configuration). These runs feed every chart, and Golden Validation designation.
- **Alpha decay:** per ET calendar month of the search period (months come from the canonical calendar module), each run's net profit, trade count and win rate, plus a trailing 3-month Sharpe. It is computed server-side from the persisted trades and equity.
- **Spike or plateau:** for each candidate and each knob, one backtest a single final-step either side of the candidate's value, over the search period, skipping any value that breaks a constraint. The page shows the change in each of the three scores.
- **Luck-adjusted (deflated) Sharpe:**
  - The number of trials is the count of distinct configurations the zoom-in scored, with the variance of Sharpe across those trials. Skew, kurtosis and observation count come from the candidate's full-detail returns.
  - The repo has two deflated-Sharpe implementations, and the canonical one is marked unvalidated and takes no cross-trial variance. This PRD designates one canonical implementation, gives it the cross-trial variance term from Bailey & López de Prado (2014), and validates it with a golden fixture from the paper's worked example (`atol=1e-9`). The other implementation delegates to it or carries a parity test.
- The pick names one of the two candidates. It is recorded, and it is irreversible within the study.

### Final exam

- **Window:** the last 3 months of the study's frozen snapshot, which is the span after the search-period end. The run-up comes from bars before the exam start.
- **Runs:** the pick and the benchmark run once each, as full-detail runs over the exam window. The exam stage can run at most once per study. The unpicked candidate can never be examined in that study.
- **Checks:**
  - lost money (net profit ≤ 0);
  - fewer than 5 trades;
  - kept less than half its search-period Sharpe (exam Sharpe < 0.5 × the pick's search-period Sharpe).

  When the search-period Sharpe is not positive, the retention check fails closed.
- **Exam reuse count:** the number of earlier Golden Search studies for the same Signal Program and stock whose exam window overlaps this one. It is shown on the exam and sign-off tabs.

### Study lifecycle (a paused, owner-driven state machine)

No "awaiting owner" state exists anywhere in research jobs today. The study adds its own. Each running stage is an ordinary research job, with the worker lease under ADR 0065, cancellation, and Finish. A pause is simply the study holding a stage result and no job:

| Study state | Next by | Next state |
|---|---|---|
| `zoom_in_running` | job completes | `awaiting_walk_forward_setup` |
| `awaiting_walk_forward_setup` | owner sets fold lengths | `walk_forward_running` |
| `walk_forward_running` | job completes | `awaiting_pick` (candidate evidence runs are part of this job) |
| `awaiting_pick` | owner picks | `exam_running` |
| `exam_running` | job completes | `awaiting_sign_off` |
| `awaiting_sign_off` | owner approves | `approved` (terminal) |
| any awaiting state | owner closes | `closed` (terminal) |
| any running state | cancel / failure / lost worker | stage `cancelled` / `failed` / presented `interrupted`, resumable by Finish under the existing refusals |

- **Persistence:** a new Python-owned `research_golden_search_studies` table under ADR 0055. It holds the request, the receipt (weights snapshot, knob declarations as used, the data snapshot and code identity frozen once, the exam window), the stage records written after every step (sweep ids before a sweep runs, as Walk-Forward does), the pick, the exam result, and the approval id. The attempt fence and write checks match Walk-Forward's.
- **Launch limit:** a study is refused when its estimated upper bound exceeds 5,000 backtests. It is checked at launch using the default fold lengths, and again at walk-forward setup with the chosen lengths. The upper bound counts:
  - zoom-in passes × knobs × rounds × points;
  - the per-fold searches and test runs;
  - the latest-window search;
  - the 3 candidate runs plus the plateau runs;
  - the 2 exam runs.

  Estimated run time uses the existing Grid Search estimate. Both are shown before launch, broken down by stage.
- **Deletion:** a study that led to an approval can't be deleted. Any other study deletes with its owned sweeps and full-detail runs.
- **Job dispatch:** a new job type on the existing jobs dispatch, mapped to a Python jobs-internal endpoint as Grid Search and Walk-Forward are. That touches one mapping in the .NET jobs API. It adds no .NET-owned table.

### Qualified configuration per stock (runtime) and the runtime golden proof

- **Table:** a new Python-owned `research_qualified_configurations` table, keyed by (Signal Program key, stock). It holds:
  - program version, parameters and golden trace root;
  - the approving study and approval ids;
  - approver identity, approval time, and the note;
  - the exam outcome and failed checks.

  A replacement writes the new row and moves the old one into an append-only history. It is never edited in place.
- **One resolver** answers "what is the qualified configuration for (program, stock)?": the approved row when one exists, otherwise the registry point when the stock is in the registry's validated symbols, otherwise none. All four current readers of the registry point go through it:
  - configured-signal seal construction (golden trace root and the coverage boolean);
  - the program build proof's receipt lookup;
  - the seal check that detects a moved root;
  - Deploy's "Use qualified configuration".
- **Runtime golden proof:**
  - At approval, the approved configuration is replayed with the decision-identity engine over the study's frozen minute bars for the whole study range (search plus exam).
  - The inputs are copied into an immutable proof artifact on the data-plane volume, with sha256s recorded.
  - The resulting decision traces define the configuration's golden trace root.
  - A **runtime program build receipt** is minted binding the current artifact and wiring digests to (program version, root). The committed receipts file remains the authority for registry points. Runtime receipts live in a new Python-owned research table.
- **Replacement semantics:** approving moves the stock's root. Any bot sealed under the old root for that program and stock is refused at Start and Resume with the existing moved-root refusal, whatever parameters it was sealed with. That includes Dry Run explorations on that stock. The sign-off tab lists those bots, read through the existing fleet roster, before the owner confirms. Running runs are untouched: seal checks run only at Start and Resume, and ADR 0054's amendment added no ENTER gate.
- **Drift and re-proof:** a change to the program's bytes leaves each approved configuration without a receipt for the new digest. Its build proof reads UNPROVEN, with a "needs re-proof" next step. A **re-proof** action replays the stored proof inputs through the current bytes:
  - identical traces mint a new runtime receipt, appended;
  - any difference marks the approval "no longer holds", and the stock falls back to no qualified configuration until a new study is approved. It does not fall back to the registry point, because the owner replaced that.

  A program-version bump always ends in "no longer holds".
- **Clerk access:** Clerks read the new tables SELECT-only, as they already read Golden Validation facts. The data plane creates the tables. A Clerk that sees a missing table fails closed to "no approved configuration", and never to covered.
- **Golden Validation:**
  - Approval designates the pick's full-detail **exam** run as the Validation Golden Run, then records an accepted review in one transaction with the qualified-configuration write.
  - With no paired LEAN run the evidence is Python-only, so the classification is **Manual override** (ADR 0061 §3), with the program version explicitly authorized.
  - **Note:** on a passed exam, the system writes it (study id, exam window, exam result, checks passed). On a failed exam, it is the owner's written reason, which is required.
  - Deployment-scope applicability already compares only strategy, program version, stock and parameters, so the approved configuration satisfies the strategy-validation gate in Paper and Live.
- **Coverage:** a deploy at the approved (program, stock, parameters) seals `parameters_match_validated_settings = true` and reads COVERED, so Paper, Shadow and Live admit it under ADR 0054's 2026-09-27 amendment. Every other safety gate is unchanged: broker, custody, arming, risk envelope, the canary pairing, and the Live exclusion for harnesses.

### HTTP surface (Python)

A new router under the research prefix covers:

- preflight/estimate, launch, list/history, and detail (per stage);
- walk-forward setup, pick, exam launch, approve, close, and delete;
- stage Finish and cancel, through the jobs surface;
- the global scoring setting GET/PUT;
- the list of current qualified configurations, with their approving study;
- re-proof.

Every temporal field is `int64 ms UTC` with the shared maximum-timestamp bound. The OpenAPI contract snapshot and the generated frontend types are regenerated.

### Frontend

- **Route:** a new lazy route and menu item "Golden Search" in the Strategy Tools group. The existing Grid Search and Walk-Forward pages are unchanged.
- **Page:**
  - a study history;
  - a launch form: strategy, stock through the shared symbol picker, knob ranges and order pre-filled from the declaration and edited with the existing parameter-range input, and a live estimate;
  - the global weights panel;
  - the qualified-configurations list.
- **Study view:** one tab per stage (Setup, Zoom-in, Walk-forward, Candidates, Final exam, Sign-off). The benchmark appears in every chart and table.
- **Chart reuse:** the shared trading chart (price, EMA and RSI indicator panes, trade markers, equity) and the existing drawdown chart are reused. The small new charts are:
  - per-knob zoom paths;
  - alpha-decay bars;
  - spike or plateau.
- **Display rules:**
  - Timestamps render through the shared timestamp display: months in `date-et`, trade instants in `local`.
  - Reason codes, check names and verdict codes render through `receiptLabel`.
  - All copy about outcomes arrives from the backend.

## Testing Decisions

- **What makes a good test:** assert what a caller or the owner observes, meaning the stage a study lands in, the configuration it proposes, the refusal it returns, and what Deploy admits. Never assert private state. Every regression test is checked against the specific *wrong* variant it guards, not just against unfixed code (the #1928 lesson).
- **1. Study procedure through its service (highest seam).** Run against an ephemeral Postgres with a fake backtest engine keyed by window and configuration, the way the Walk-Forward study service is tested today. Covers:
  - stage order and every pause;
  - the one-knob path landing on the fake landscape's known optimum;
  - the cache never re-running a configuration;
  - the exam window never touched before the exam stage;
  - the exam running once, with the unpicked candidate barred;
  - the exam reuse count;
  - the 5,000 refusal at launch and at walk-forward setup;
  - Finish after cancel and after a lost worker;
  - approval replacing the qualified configuration and moving history;
  - an approved study refusing deletion.
- **2. Zoom-in search, pure.** Synthetic score landscapes: a single peak, a plateau, ties (keep the current value), integer knobs stopping at a step of 1, constraint skips (fast ≥ slow), range clipping, and pass-limit termination. Plus determinism: the same inputs give the same path and winner.
- **3. Blended-places ranking, pure.** Place computation with shared places, weight normalization, zero-drawdown handling, ineligibility (fewer than the minimum trades, non-finite values), and the documented tie-break. It sits beside the existing ranking-contract tests.
- **4. Deploy admission.** Using the existing Signal Program admission and run-admission test harnesses:
  - after an approval, a SPY deploy at the approved parameters is PROVEN and COVERED;
  - a bot sealed under the old SPY root is refused as moved-root;
  - AAPL still resolves to the registry point;
  - a changed program digest makes SPY UNPROVEN with "needs re-proof";
  - re-proof restores it when traces match and marks "no longer holds" when they differ;
  - a Clerk with no table fails closed;
  - the approved Golden Validation record is applicable in Paper and Live and reads Manual override.
- **5. Luck-adjusted Sharpe golden fixture.** Input and output from Bailey & López de Prado (2014)'s worked example, `atol=1e-9, rtol=0`, a `docs/references/` note with its provenance block, and a parity test for the non-canonical implementation.
- **6. EMA knob exposure.** The ENG-007 LEAN fixture stays bit-exact at defaults 5/10/5. The regenerated corpus is pinned by the existing qualification-matrix test. Non-default EMA lengths and hold bars change decisions as expected on a small synthetic series. Before merging, every test that deploys the EMA program without explicit parameters is swept (the validated-settings blast-radius lesson).
- **7. HTTP.** Endpoint tests with an ASGI transport, mirroring the Walk-Forward study endpoint tests: refusal codes, owned-sweep refusal on the Grid Search router, and the setting GET/PUT.
- **8. Frontend.** Angular Testing Library specs per tab and for the launch form (estimate shown, over-limit refusal rendered, pick locks after exam), each asserting rendered output with DI-level fakes. AXE-clean.

## Out of Scope

- The Monte Carlo clustering stage. The stage list must let it slot in later, but nothing of it is built.
- Scheduled or automatic studies. Launch stays manual.
- Searching several stocks as one basket, and one configuration approved for many stocks.
- Keeping more than one approved configuration per strategy and stock, and one-click redeploy of stranded bots.
- Pairing the exam or candidates with LEAN runs. The Golden Validation record stays a Manual override until a later paired attempt is reviewed separately.
- Changing the existing Grid Search or Walk-Forward pages, their verdict, or the registry points of stocks without an approval.
- Making RSI length, the decision cadence, or other strategies' fixed constants searchable. Only EMA's declaration ships. Other strategies follow by declaring knobs.
- Reconciling the research data provider (Polygon split-adjusted, regular session) with the live data provider (IBKR). This is a pre-existing gap, shared with Grid Search.

## Further Notes

- **ADR required: supersedes ADR 0054 §5 in two bullets.** ADR 0054 rejected "deploy-time qualification" *for now*, because a self-minted root has no human review and no drift detection, and it would need committed price fixtures per symbol. It also kept "one point per program". It explicitly said to revisit once live trading exists (ADR 0059). The owner revisited on 2026-09-30:
  - the Golden Search sign-off is the human review over displayed evidence;
  - re-proof is the drift detection;
  - the study's frozen snapshot copied into an immutable proof artifact replaces committed per-symbol fixtures;
  - qualified configurations are per stock.

  A new ADR records this and amends ADR 0054 §5. ADR 0061 §5 ("a Golden Validation record never moves `validated_settings`") still holds: the qualified-configuration write, not the Golden Validation record, moves the stock's configuration.
- **Owner decisions not to reopen (2026-09-30 grilling).** Zoom-in runs first over the whole search period and walk-forward runs only inside the narrowed ranges. The owner kept this twice, including after being shown that the fully nested version fits under 5,000 backtests. The honest number is therefore the final exam, which is why it is one-shot and counted. The remaining decisions:
  - the search moves strictly one knob at a time (not pairs);
  - the cap stays at 5,000;
  - the weights are global, not per account;
  - replacing a stock's configuration strands its old seals, and running bots finish their current run;
  - a passed exam gets an automatic note.
- **Known consequence.** A one-knob search can miss configurations where two knobs only work together, such as fast/slow EMA or RSI low/high. The spike-or-plateau check and the benchmark comparison are the visible safeguards.
- **Suggested build order** (tracer-bullet slices):
  1. EMA knob exposure plus corpus regeneration.
  2. Blended-places ranking plus the scoring setting.
  3. The pure zoom-in search.
  4. The study table, lifecycle and zoom-in stage, driving owned Grid Search sweeps, with the page's Setup and Zoom-in tabs.
  5. The walk-forward stage and tab.
  6. Candidate full-detail runs, alpha decay, plateau, deflated Sharpe (fixture first), and the Candidates tab.
  7. The exam stage and tab.
  8. Qualified-configuration resolver, runtime proof, re-proof, the admission changes and the ADR.
  9. Sign-off: Golden Validation write, stranded-bot list, and the qualified-configurations list.
- **Measured throughput (2026-09-04, this machine):** about 1.4 s fixed plus 0.3 s per month of data per backtest at the existing concurrency of 8. A default EMA study (6 knobs, 5 points, 3 rounds, at most 3 passes, 9 folds) has an upper bound of about 3,000 backtests. The real count is usually well under that, because searches converge early and the cache absorbs repeats.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
