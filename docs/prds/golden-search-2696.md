# Golden Search research workbench

Revision 3 — adversarial review with owner-confirmed golden-configuration outcome, 2026-09-30.

This revision replaces the implementation plan in [PRD #2696](https://github.com/tim1016/learn-ai/issues/2696). It is a design specification, not a claim that the capabilities below already exist. The owner explicitly authorized better-path decisions in this review, including reconsidering earlier PRD decisions. Changes to accepted ADRs remain explicit implementation prerequisites; this document does not silently rewrite them.

**Decision:** build a guided research workbench that combines Grid Search and one-knob Zoom Search, tests the selection procedure chronologically, and helps the owner decide to keep the current configuration, collect more evidence, or approve one candidate as the stock’s golden configuration, ready to select in Deploy for Paper or Live under the existing admission gates. A profitable search result, a repeatable replay, human approval, and Live eligibility are four different facts.

The required successful outcome is a usable golden parameter set: one exact, versioned tuple for a strategy and stock, with qualification proof and human sign-off. The research procedure makes that choice defensible; “no change” remains an alternative, never a substitute for delivering golden parameters when the owner approves them. The owner can inspect and edit every supported research control. Evidence-dependent edits create a new revision; they cannot rewrite the experiment that produced an attractive result.

The [interactive prototype and implementation handoff](../design/golden-search/README.md) accompany this specification. The [original PRD snapshot](golden-search-2696-original.md) is preserved for comparison. Owner interview decision, 2026-09-30: **“Make golden settings available for Paper or Live in Deploy.”** This explicitly supersedes revision 2’s Paper-nomination-only finish line and its proposed separate Golden Search Live-release approval. The original written override for weak research evidence is retained; technical qualification and existing operational gates cannot be overridden here.

The feature PR begins with this design/prototype slice; its implementation checklist deliberately leaves engine, persistence, Angular integration and release work unfinished.

## What the original plan got right

Keep the single-stock study, immutable data and code receipts, constrained numeric knobs, coarse-to-fine coordinate search, explicit pauses, complete candidate charts, incumbent comparison, one candidate at the final test, resumable jobs, and in-page review. Reuse the actual Grid Search execution and persistence modules. Keep the 5,000-backtest ceiling. Preserve the distinctions in ADR 0061 between fixtures, corpus coverage, and Golden Validation.

## Corrections to the original plan

| Original requirement | Failure or limitation | Revised decision |
|---|---|---|
| Narrow on the entire development period, then walk forward within those ranges from the global winner | Later fold outcomes influenced both the ranges and starting point. A disclosure does not make these tests unseen. | Confirmatory walk-forward starts every fold from the original frozen domain and a predeclared seed, using only that fold's training data. The original workflow remains available as **Exploratory replay**, with no independent-test claim. Supersedes stories 26–32 and the prior “not to reopen” decision. |
| Weighted places 50/30/20 determine every move | Places depend on the comparison set. Adding a dominated alternative can reverse a choice; “strict improvement” across rounds is not a stable objective. Net profit also appears twice through profit/drawdown. | Default to the existing single-measure Sharpe ranking with explicit risk and activity constraints. Keep net profit as another selectable objective. Weighted places can be an advanced exploratory display, never a cross-round improvement claim or the confirmatory selector in v1. Supersedes stories 12–17. |
| Coordinate convergence means a stable result | Coordinate search is local and order-sensitive; a flat one-axis neighborhood can hide a fast/slow interaction. | Call it “no improvement along the tested moves.” Show coverage and edge-of-range warnings. Offer Grid Search for a bounded two-knob interaction audit and predeclared alternate starts when affordable. Neither proves a global optimum. |
| Five trades and three profitable exam months can support promotion | Five trades is a floor for avoiding an empty statistic, not evidence of a durable edge. Sharpe ratios from short windows are unstable. | Make “insufficient evidence” a first-class outcome. Freeze adequacy and risk rules before launch. Use 30 completed trades as a conservative product default per training winner and for the final test, explicitly a heuristic, not a confidence guarantee. Display sessions and concentration as well as trades. |
| One exam per study, same-program overlap count | Another study, strategy name, or deleted result resets the apparent first look. | Persist an append-only exposure ledger across the owner's research family and instrument/calendar intervals. Never erase exposure when deleting work. Unknown historical exposure is unknown, not fresh. |
| Exam Sharpe at least half search Sharpe | A selection-inflated denominator and a short noisy numerator do not establish superiority; a less negative result could confuse a naive implementation. | Show retention only when defined, as context. Judge frozen profitability, drawdown, sample and incumbent-comparison rules; never let null be a pass. |
| DSR from distinct zoom configurations alone | Other starts, grids, revisions, recent-window searches and discretionary choices are omitted. Adaptive trials are dependent. | Record the full trial and selection ledger first. DSR is unavailable until its inputs and implementation are validated; later expose it as a model-dependent diagnostic, not “probability of real edge.” |
| Every chart uses today's configuration | A changing default moves the benchmark under an immutable study. Today's selected configuration is also not necessarily a historical deployable baseline. | Freeze the incumbent ID and full tuple at launch. Label its earlier-period replay retrospective. A later default is a separate comparison and cannot change this study. |
| Approval mints its own proof and unlocks Paper and Live together | Replay proves repeatability on captured inputs, not engine agreement, profitability, or operational readiness. Existing Golden acceptance can satisfy the same scoped gate in both modes. | Separate the evidence facts while connecting the action: **Approve golden configuration** builds proof and atomically publishes the qualified tuple, Golden review and default pointer. The settings become selectable for Paper or Live in Deploy; existing safety gates still decide whether a bot may start. No new Paper-first or separate research Live-release gate is introduced. |
| Replacing the default strands all prior seals | Recommendation change is being used as revocation. It needlessly prevents an otherwise still-authorized bot from resuming. | Keep immutable qualified versions and one active default pointer. Existing seals resolve their pinned version, subject to current safety, revocation and build checks. Explicit revocation is separate and shows affected bots. This supersedes the earlier stranding decision and requires an ADR/seal migration. |
| Runtime estimate assumes concurrency eight | Current sweep execution and the engine gate serialize runs. | Estimate serial work per stage plus queue wait; show a range, measurement age, and cold-start uncertainty. Do not resurrect a thread pool. |
| Approval protects the study from deletion | Unapproved exposed exams and searched failures still influenced selection. | Deletion may hide a study and garbage-collect unreferenced detail; it never removes protocol, trial, exposure, pick or release audit facts. |

These are deliberate path corrections, not statements that the original author made a mathematical error in every case. In particular, the original PRD disclosed its walk-forward leakage; this revision changes the policy because stronger guidance is the requested product outcome.

## Evidence from the current codebase

Review baseline: `6d92419314a4599ff62dff4784032adc3b61bad4` on 2026-09-30. Other uncommitted review notes were left untouched. This is source inspection, not a live deployment audit or a performance measurement.

Publishing update: the feature branch is based on `87b8e261` after [PR #2697](https://github.com/tim1016/learn-ai/pull/2697) added decision-minute-open fill support and changed the new research commission default to zero. The review baseline above remains explicit. The prototype's next-bar-open/$1-per-order values are deliberately frozen illustrative assumptions, not the current defaults or a historical Alpaca fee schedule. Production implementation must use the current fill/cost contract rather than copying those example values.

| Existing module or authority | What it provides and what must change |
|---|---|
| [`grid_search/service.py`](../../PythonDataService/app/research/grid_search/service.py), [`models.py`](../../PythonDataService/app/research/grid_search/models.py) | Preflight, `prepare_launch`, create/execute, snapshot and attempt semantics, serial estimate. Current owner kinds are user/walk-forward. Add study ownership deliberately in schema, router authorization, lifecycle and cleanup. Do not mistake the callable interface for a promise that every child sweep already shares a study-wide warmup. |
| [`sweep/ranking.py`](../../PythonDataService/app/research/sweep/ranking.py) | Single-measure ranking, eligibility and deterministic ties. Extend eligibility with the frozen policy and retain one canonical comparison contract. |
| [`sweep/warmup.py`](../../PythonDataService/app/research/sweep/warmup.py) | Probes actual program readiness. Can carve warmup from the requested range. Golden Search must pin a common evaluation start and sufficient prior history; it must not quietly compare different scored intervals across adaptive steps. |
| [`sweep/execution.py`](../../PythonDataService/app/research/sweep/execution.py), [`engine/run_gate.py`](../../PythonDataService/app/engine/run_gate.py) | One backtest at a time; cancellation between cells and while waiting. The PRD's eight-worker assumption is stale. |
| [`walk_forward_study/service.py`](../../PythonDataService/app/research/walk_forward_study/service.py), [`folds.py`](../../PythonDataService/app/research/walk_forward_study/folds.py), [`verdict.py`](../../PythonDataService/app/research/walk_forward_study/verdict.py) | Fold planner, owned sweeps and frozen verdict. Existing procedure is grid-specific and runs a whole grid in each test period, marking non-winners exploratory. New orchestration evaluates only the already-selected winner in confirmatory test windows; reuse primitives without claiming this is a drop-in algorithm switch. |
| [`programs/ema_crossover_signal.py`](../../PythonDataService/app/engine/strategy/programs/ema_crossover_signal.py), [`algorithms/ema_crossover_signal.py`](../../PythonDataService/app/engine/strategy/algorithms/ema_crossover_signal.py) | Absolute gap, normalized gap in bps, and both RSI gates are already public; fast/slow EMA and hold length are fixed. Declare **gap_bps** explicitly, even when fixed at zero. Both gap floors apply together. Parameterizing periods also changes factory wiring, indicator labels, warmup and sealed identity. |
| [`robustness_stats.py`](../../PythonDataService/app/engine/edge/robustness_stats.py), [`signal/diagnostics.py`](../../PythonDataService/app/research/signal/diagnostics.py) | Two materially different DSR implementations; the first says validation is pending and lacks observed cross-trial dispersion. Neither should be presented as a proven new research gate. |
| [`signal_program_admission.py`](../../PythonDataService/app/services/signal_program_admission.py), [`registry.py`](../../PythonDataService/app/engine/strategy/registry.py) | Coverage, receipt lookup and moved-root checks are tied to registry contracts. A per-stock resolver and versioned qualification are a substantial cross-cutting migration, not just another table. |
| [ADR 0054](../architecture/adrs/0054-corpus-coverage-is-a-stamp-on-paper.md), [ADR 0061](../architecture/adrs/0061-golden-validation-is-a-scoped-human-promotion-over-immutable-run-evidence.md) | Coverage is required in Paper/Shadow/Live; Python-only Golden acceptance is Manual override. Golden review does not bypass other gates. Runtime per-stock qualification and versioned default semantics need explicit ADR amendments before production shipping. Preserve the existing Paper/Live applicability of exact Golden review; do not introduce a Paper-only policy. |
| [`app-menu.ts`](../../Frontend/src/app/shell/app-menu.ts), [`grid-search`](../../Frontend/src/app/components/grid-search/grid-search-page.component.html), [`trading-chart`](../../Frontend/src/app/shared/trading-chart/trading-chart.component.ts) | Existing Strategy Tools shell, Grid Search UI and shared price/indicator/trade chart. Add a lazy `/golden-search` workbench; leave existing tools usable and link them to the study without duplicating their engines. Angular is 22 in the installed manifest, despite the skill's older prose. |

## The research journey

### Plan before seeing results

The launch page answers: What am I testing? Which knobs may change? What would make me reject it? How much work will it take?

Choose a registered production strategy and one instrument through `app-instrument-card`/the shared symbol picker, using the joined listing and lake catalog. Show searchable, fixed and unsupported controls with reasons; do not hard-code an EMA-only data model. EMA is the first complete declaration. Other strategies remain visibly unavailable until their own declaration and qualification coverage are shipped.

Each knob has a trader name, exact parameter name, unit, legal domain, default, search range/list, quantization step and warmup dependency. Show **Search / Keep fixed** per knob. Parameter constraints are typed predicates validated by Python, not translated from free text in Angular. Integer periods and decimal increments use canonical values/hashes, with no float-drift duplicate points.

EMA initially exposes absolute gap, gap_bps, lower/upper RSI gates, fast/slow periods and hold bars. Preserve 5/10/5 defaults and the existing absolute-gap parity point. In the default preset, gap_bps stays fixed at zero; explain that enabling both gap floors requires both to pass. RSI length 14 and decision cadence 15 minutes remain visibly fixed. Reject fast ≥ slow and lower RSI ≥ upper RSI before dispatch. Hold semantics must be explicitly tested across session boundaries; “five bars” must not silently change into five wall-clock intervals when sessions are absent.

Before the first result, freeze the objective, drawdown ceiling, minimum trades, train/test lengths, reserved final interval, fill mode, capital, position sizing, costs, session, adjustment policy, candidate rule, alternate starts, interaction checks, stress scenarios and total budget. Suggested initial policy: rank by Sharpe, at least 30 completed training trades and positive net result, with a user-chosen maximum drawdown. These are user risk preferences/product heuristics, not statistically calibrated guarantees. The UI labels defaults as starting policies, explains units, and lets the owner change them before lock.

### Use the two search tools for different questions

| Tool | Trader question | Appropriate use | Visible limitation |
|---|---|---|---|
| Grid Search | “What happens across all these combinations?” | Small explicit domains; two-knob interactions; inspect tradeoffs and missed regions. | Tests only the listed combinations. Cost grows as their product. A grid winner is in-sample. |
| Zoom Search | “Can I improve this starting point with fewer runs?” | Several numeric controls; narrow one axis, retain improvement, repeat within a budget. | Local and order-dependent; can miss improvements requiring simultaneous changes. |
| Walk-forward | “Would this selection procedure have held up later?” | Evaluate either frozen search procedure chronologically. | Evaluates a sequence of training winners, not the one final candidate. |

Recommended path: use Zoom for a many-knob study; add a predeclared small Grid audit of key pairs (EMA fast/slow, RSI gates) on development data. For a genuinely small grid, choose Grid directly. If the operator uses a discovery grid to redesign the domain, that is a protocol revision; a confirmatory replay must still nest all adaptive range/seed decisions within training windows, or be labeled exploratory. Switching tools or editing weights after results is another selection trial, never a free reset.

Default Zoom uses five points, up to three refinements, up to three passes, original declaration order, and an explicit starting point. Always include the incumbent value when comparing a move. Stop for no strict objective improvement, quantization limit or budget; distinguish these reasons. Ties retain current value, then prefer nearest/lower value. Multi-start and alternate order are optional, frozen in advance and counted. No claim of a global optimum is made.

### Validate the procedure without leaking its future

Run non-overlapping forward test windows after rolling training windows. Default six months train/two months test is editable before launch; the canonical planner supplies legal ends. Each fold repeats the complete selected search procedure using the original ranges and predeclared seed. Never seed a past fold from the all-period winner or today's newly qualified tuple. Any automatic narrowing or pair-search selection happens inside that fold's training interval.

Use one frozen dataset for reproducibility, with an evaluation capability restricting each worker to authorized intervals. Snapshot availability does not confer permission to score final-test bars. Indicator warmup may use preceding bars only; it cannot inherit trades, portfolio state or future-fitted indicator state. Reserve enough warmup for the legal domain and refuse a comparable-window claim if it cannot be supplied.

Retain the existing five-label verdict as a named legacy summary with defined-retention coverage (“based on D of S folds”). It is neither candidate certification nor a new statistical confidence measure. Show fold failures, sparse trading and the chosen settings. The dominant chart is **the selection procedure's test history**. Its line links non-overlapping return segments under a documented capital convention, with fold resets and terminal liquidation costs explicit. Never splice raw dollar equity curves from independently reset portfolios. Python computes the curve and any summary; if path data are unavailable, show fold results instead of inventing a continuous curve.

All-period and recent-window candidates are then fit on development data under the frozen rule. Their retrospective equity/monthly/neighbor views are labeled **used to choose settings**. They do not inherit the procedure's walk-forward return. Human choice between them is itself part of development selection. Deduplicate identical configurations, and always offer the frozen incumbent plus “keep current settings.”

### Compare candidates and weaknesses

Lead with a sentence the owner can act on: “The recent fit earns more in this replay, but nearby settings lose money. Inspect that sensitivity before using your final test.” No universal robustness score or winner crown.

Keep the same comparison scope, capital, costs and time range visible above every candidate table. Show net return/profit, worst drawdown, trades, Sharpe, activity and explicit unavailable states. Optional measures reveal definitions on demand. Drawdown is a historical loss from a peak, not a future loss limit.

Evidence views: development equity and drawdown; month-by-month net results (call this **performance over time**, not alpha decay unless a risk-adjusted alpha model is actually specified); parameter neighborhood and pair grid; fold-level selected settings; execution and fee assumptions; trades with dynamic candidate indicator names in the shared trading chart. Put parameter values beside the selected trade so the owner can explain an entry or exit.

Neighborhood cells show the actual objective and risk metrics, not incomparable weighted places. Distinguish tested, untested, invalid and failed cells. Report boundary hits and one-sided neighborhoods. A plateau is a local observation, not a probability statement. Stress comparisons use predeclared cost/fill scenarios, rerun in the engine, with their units and coverage shown. Missing historical fees must remain an explicit approximation/blocker as appropriate; “Alpaca costs” cannot label a flat $1/order run.

### Open one final test

Final dates are fixed at plan lock, by canonical completed sessions, not a moving “last three months” at each resume. Default three months is a starting duration, not guaranteed sample adequacy. With low trading frequency, reserve a longer interval before launch or wait for new prospective data. Do not extend the exam after seeing a poor result and reuse the original claim.

The page explains “held back by this study,” not an impossible promise that the owner has never seen the market. Exposure states are **not opened in recorded research**, **previously used**, or **history unknown**. Record overlap across strategies on the instrument, study revisions and viewed result intervals, with study/program-specific detail. Existing Grid Search/Strategy Lab/exports require exposure integration for a strong claim; until instrumented, older/outside activity means history unknown. A future period reserved before it exists provides the strongest attainable separation. Known reuse or unknown exposure cannot be called a fresh confirmatory test; it may still run as exploratory evidence.

Pick and final-test launch are separate commands. At launch, atomically reserve the exact candidate, benchmark, protocol, interval and exposure claim before dispatch. Concurrent studies cannot both claim the same untouched exposure. A crash/cancel leaves it consumed or reserved; Finish resumes the exact evaluation, never another candidate. A duplicate request returns the original job/result. All final outputs, including errors and partial reads, stay in the ledger.

Final checks use the frozen policy: positive net result after stated costs; drawdown within the chosen ceiling; sample floor; candidate versus frozen incumbent under the predeclared objective and risk constraints. Retention is descriptive only. Outcomes are **meets the stated rules**, **does not meet the rules**, **not enough evidence**, or **could not evaluate**. Never collapse the last two into pass or measured zero. Passing is not a forecast. If the candidate does not improve the declared tradeoff, recommend keeping the incumbent.

### Review and promotion

The primary action is **Approve golden configuration**. Before approval, show the complete tuple, strategy/program version, stock, incumbent replacement and affected bots. Approval is an actionable workflow: build and verify the immutable proof, record the exact Golden Validation decision, and atomically publish the qualified version plus the stock's active default. No code edit, qualification script or restart is required of the owner.

The success screen says **Golden configuration ready in Deploy**, displays every parameter including fixed values, and offers **Use in Deploy**. That handoff carries the immutable qualification ID, stock, program and exact parameter tuple into the canonical Alpaca account desk. The owner chooses account and Paper or Live there; both may use this Golden qualification under existing coverage, broker, custody, arming, canary, risk and account-mode gates. Clicking approval does not place an order or automatically create/start a bot. Do not add a second research sign-off or a mandatory Paper-first release gate.

Show four separate rows: research outcome; reproducibility/build coverage; independent engine agreement; Golden acceptance and Deploy availability. Python-only evidence remains **Manual override** under ADR 0061 even when the research checks pass. A passed exam may prefill a factual note, but the acknowledgement starts unchecked and names missing parity and the exact program version.

Retain the original owner-controlled override: a failed, sparse or reused research test may still be approved with a written reason and a separate explicit acknowledgement of those deficiencies. Preserve the failed/insufficient/exposed labels forever. The resulting golden settings are usable in Paper or Live under the same existing gates; an override does not turn weak evidence into strong evidence. Proof mismatch, missing/corrupt required artifacts, code-identity failure or an operational refusal cannot be bypassed by this research override. Technical failure leaves approval pending/failed and leaves the old active default unchanged.

Alternative decisions remain **keep current settings**, **wait for fresh data**, and **retain as exploration**. These are recommendations the owner can act on, not a mandatory dead end when the owner deliberately approves another tuple.

Research never changes market-data-provider ownership. IBKR supplies live market data and trading-status evidence; Alpaca supplies account/order/execution facts. Preserve read-only Gateway settings and distinct client IDs. Polygon historical research is not proof of live-feed equivalence. Existing fill, fee, data-adjustment and live decision-timing gaps must be disclosed and verified against current code before release; do not assume the original PRD's references prove they are already solved.

## Architecture and contracts

### Small interfaces with substantial behavior behind them

Use a Python study module as the authority for plan, lifecycle and evidence. Its external interface is `preflight`, `launch`, `continue_stage`, `select_candidate`, `open_exam`, `review`, `cancel`, `finish`, and read-only history/detail. Stage changes require expected revision and an idempotency key. Each command returns the current study, permitted actions, refusal reasons and operator copy; Angular must not infer eligibility from metrics.

Behind this interface, separate actual seams:

- **Search procedure:** Grid and Zoom are two concrete adapters over the same evaluator and canonical ranking contract. Inputs include domain, seed, objective/policy, window, budget and evaluation context. Outputs include trial requests/path and winner. Keep deterministic planning separate from persisted side effects; do not call an I/O callback “pure.”
- **Evaluator:** owns full execution identity, common evaluation boundary, data capabilities, cache, metrics and cost accounting. Cache identity includes program/schema/build/environment, symbol, all parameters including fixed ones, snapshot, evaluation/runup interval, fill/fees/slippage, sizing/capital/session/adjustment and metric-convention version. `params_hash` alone is only unique inside that complete context. Summary evidence cannot satisfy a full-detail request without an explicit rerun/enrichment receipt.
- **Evidence module:** authors folds, candidate comparisons, monthly diagnostics, constraints and explanatory copy in Python. .NET transports/persists where already canonical; Angular formats and charts. Do not follow the outdated layer-neutral passage in the validation skill: AGENTS.md requires Python numerical authority.
- **Qualification module:** prepares proof artifacts and commits versioned qualification/review/default facts, returning the exact Deploy handoff. Default selection and seal resolution are separate operations; one resolver mode cannot safely answer both questions.

Owned sweeps carry study, stage, fold, step and attempt IDs. Do not create a parallel jobs framework: reuse worker leases, attempts, cancellation and interrupted presentation from ADRs 0055/0065. Child IDs are persisted before execution. Stale workers cannot write results, consume extra budget or approve.

### Persistent records

Python owns research tables. Define immutable records for protocol revisions, stages/steps, evaluation receipts, candidate snapshots, trial events, exposure reservations/results, qualification versions, proof artifacts and approval events; mutable rows are projections/pointers with revision checks. These are domain records, not a demand for one table per noun.

Qualification key is an immutable ID bound to program/version/symbol/complete parameters/proof identity. A separate `(program, symbol)` pointer selects one default for new deploys. Existing seals pin the qualification ID, never whichever row is currently default. Legacy seals remain interpreted under their original schema; use append-or-clone migration, never rewrite old facts. Missing/unreadable tables or evidence fail closed with “cannot verify,” not a false claim that no approval exists. No runtime-approved row silently falls back to the registry after becoming stale or revoked.

Proof creation is not one database transaction with a filesystem write. Stage content-addressed immutable bars and manifests, verify durability and checksums, compute traces under loaded executable identity, then atomically commit DB references/review/qualified version/default pointer with expected incumbent revision. Use recoverable pending work/outbox and idempotency. A storage or DB failure leaves no deployable half-approval. Two simultaneous approvals cannot overwrite each other; show the changed incumbent and require a new review.

Runtime proof is explicitly **repeatability evidence for these recorded inputs**. A fixed trace replay detects some drift; identical traces do not prove equivalence on every future input. Re-proof appends evidence, checks program/schema/data/metric convention applicability and validates reference tests; it does not automatically revive a revoked qualification or a changed program version. Back up artifacts and test restoring them before trusting runtime-only qualification.

### Workload and progress

Preflight computes a conservative upper bound including the extra incumbent point where a round's five samples omit it, all starts/orders, fold training/test winners, candidate detail, pair/neighbor audits, stress scenarios, the benchmark, the final pair, and proof work. State whether proof runs count toward the same 5,000 cap; in this design every engine evaluation in the study, including proof, counts. Reserve final-test/proof budget before launch. Cache hits and invalid configurations are displayed separately from actual evaluations.

Enforce the cap while dispatching with an atomic consumed/reserved ledger, not only an estimate. Retries are tracked as attempts with a separate bounded retry allowance; they cannot silently produce unlimited work. When budget is exhausted, preserve evidence and label the procedure incomplete; no silent narrowing or smaller search that changes the protocol. Re-proof is a separately authorized job with its own displayed budget.

Display completed, cached, invalid, failed, running and remaining work by stage. Separate waiting for the engine from running. Estimate with current serial throughput and update the remaining range from completed comparable cells; queue delay stays separate. No 100% success indicator for a run with holes.

### Study states

`draft → locked → search_running → awaiting_validation → validation_running → awaiting_candidate → candidate_locked → exam_running → awaiting_review → qualification_pending → approved / qualification_failed; awaiting_review → retained / closed`.

Running stages may be queued, cancelling, cancelled, failed or interrupted. A pause owns no worker lease. Finish resumes the same protocol/identity and exact committed steps. An edit after lock forks a linked protocol revision and retains exposure/trial history. Reused/unknown-exposure exams follow the same execution lifecycle with an exploratory evidence status. Qualification has pending, ready, stale, revoked and failed states. `approved` means the exact tuple and required proof/review are committed and selectable in Deploy for Paper or Live; permission to start a bot is still evaluated there. An approval retry resumes the same proof and review intent idempotently.

## Buildable screen specification

Use the existing Botasur shell and Strategy Tools navigation. A visible “Illustrative data” banner belongs to the mock only. Production uses server evidence with no fabricated status or metrics.

The workbench has five steps: **Plan**, **Search**, **Test over time**, **Compare**, **Final decision**. Each shows the decision to make and one primary next action. History and evidence details are available without turning the page into a wall of tabs. Advanced controls expand in place; they are not hidden behind a novice/expert identity.

| Screen region | User behavior | Build path |
|---|---|---|
| Study heading and data strip | See strategy, instrument, fixed development/final intervals, study revision and exposure state | Shared asset identity, timestamp display and Python study receipt |
| Search choice | Compare Grid and Zoom in plain language; see budget/limitations change | Same parameter-range editor; server preflight, debounce and stale-response protection |
| Knob table | Search/fix, range, step, order; identify fixed cadence/RSI | Registry declaration and generated schemas; no hard-coded symbol list |
| Protocol controls | Objective, risk floor/ceiling, costs, folds and final duration | Python validation and estimate; post-lock edits produce a new revision |
| Parameter map | Select a tested neighborhood; inspect both axes and all held-fixed values | Python-authored cells plus accessible table; no browser recomputation of metrics |
| Procedure evidence | Fold timeline, aggregate verdict with coverage, per-fold winner/results | Existing planner/verdict; new procedure-aware orchestration and path evidence |
| Candidate comparison | Pick a candidate, inspect return versus drawdown/trades, keep baseline | Full-detail persisted runs; deterministic recommendations with reasons, no opaque score |
| Chart drilldown | Equity/drawdown/months/trades; inspect indicator state | Shared trading-chart module and existing run detail; unavailable outputs stay unavailable |
| Final test lock | See what will be consumed; select, then explicitly open once | Transactional exposure reservation and idempotent server command |
| Decision panel | Approve the exact golden tuple, keep current, or collect more data; then use the golden settings in Deploy | Server-authored approval/proof state and qualification ID; separate research-override acknowledgement; existing Deploy mode/gate evaluation |

Initial visual direction: existing canvas `#0b0e14`, surface `#131722`, blue `#2962ff`, readable secondary `#b2b5be`, teal `#26a69a`, and amber `#ff9800`, with corresponding light-mode tokens. Use the existing system sans typography, tabular numeric columns, restrained 4–8px corners and a compact navigation rail. The distinctive visual is a **parameter landscape beside the reason to prefer or reject a candidate**, not a decorative profit dashboard. Color is paired with symbols/text. Chart scope and missing evidence remain visible.

Desktop structure:

```text
Strategy Tools | Golden Search / instrument / revision / exposure
               Plan — Search — Test over time — Compare — Final decision
               [scope: development data | final test still locked]
               [candidate comparison table] [what this choice implies]
               [parameter landscape / curves / folds / trades]
               [keep current]                 [lock candidate for final test]
```

At narrow widths, the rail becomes compact navigation and the decision panel follows the comparison. Controls wrap; tables scroll only where their semantics require it. Every chart has a table alternative, every input a label/unit, every status a text explanation, and every action keyboard support. No essential tooltip-only content. Production symbols use `app-asset-identity`, coded receipts use `receiptLabel`, and date-anchored values use the shared `date-et` rendering. UI outcome prose comes from Python or a closed operator-copy map.

## Delivery sequence and acceptance gates

| Slice | Deliverable | Must be true before moving on |
|---|---|---|
| 0 | Capability and protocol/exposure contracts, ADR proposals, mock and source map | No claim that current code supports nested Zoom or runtime qualification. Identify fee/fill limitations and qualify all mock numbers as illustrative. |
| 1 | Research-only workbench using existing Grid and candidate records | Select instrument through shared picker, freeze assumptions and incumbent, display full cost estimate, save protocol and exposure facts. No deploy mutations. |
| 2 | EMA knob exposure and deterministic Zoom | Default parity unchanged; non-default periods/hold semantics proven; dynamic labels/warmup correct. Pin reference fixtures rather than regenerating to hide regressions. Inventory actual seals before migration; the old “no bot running” statement is not current runtime evidence. |
| 3 | Study-owned evaluator/cache/budget and resumable stage orchestration | Common scored boundary across steps, collision-safe identity, attempt fencing, cancel/Finish, exact upper bound and runtime enforcement tested. |
| 4 | Procedure-aware chronological validation and diagnostics | Future-data perturbation cannot change earlier fold selection; failures/undefined results stay visible; candidate/procedure provenance distinct; pair grid and stress audits counted. |
| 5 | One-shot exam and decision record | Exposure ledger survives deletion/revision, concurrent launch fenced, pick immutable after reservation, partial exam cannot be shopped. The owner can keep incumbent or record insufficient evidence. This is the research-evidence milestone, not the complete user-facing feature. |
| 6 | Usable golden configuration: versioned qualification, durable proof, Golden review, active default and Deploy handoff | ADR/seal schema migration, restore exercise and CAS approval complete. Exact approved settings selectable for Paper and Live under existing admission gates. No script/restart or second research approval. Current default change does not silently revoke old immutable qualification. This is the minimum complete feature. |
| 7 | Optional validated statistical extensions | DSR only after reference provenance and complete trial assumptions are testable. This work is not required merely to make an approved golden tuple available in Deploy. |

No new runtime dependency is required for the research MVP. Prefer existing charts, jobs, PostgreSQL and engine modules. Do not add an optimizer library or a second workflow framework for a deterministic coordinate search. No broker control/navigation work is needed outside canonical Alpaca Broker V2 Deploy integration.

## Tests that challenge the design

1. **Leakage:** mutate only a future fold or reserved exam's prices; every earlier selection/path remains identical. Mutate the global development winner; a fold's predeclared seed remains unchanged. Verify full-detail chart requests cannot read sealed exam metrics.
2. **Selection:** rank-reversal counterexample for blended places; interacting two-knob landscape where coordinate search misses the best joint move; alternate order/start; cycle/budget/quantization termination; no eligible candidate. Never test only a separable landscape with an easy peak.
3. **Comparability:** an adaptive child needing longer warmup cannot shorten its scored period; fixed gap_bps belongs in identity; same parameters under another date/cost/snapshot/metric convention cannot hit cache; detail enrichment retains the original summary receipt.
4. **Temporal accounting:** early close, DST, window boundaries, hold across overnight, flat portfolio at evaluation start, terminal exit costs, fold equity linking and no-trade/null Sharpe. Timestamps remain int64 ms UTC at all boundaries.
5. **Evidence:** recent winner identical to full-period winner collapses to one; observed neighbor failures remain failures; too few trades is not a pass; procedure curve never appears under a fixed candidate heading; changing default does not replace the benchmark.
6. **One-shot:** two tabs/studies reserve the same exposure concurrently; cancel after first exam cell; delete failed study; clone under another strategy; unknown old research; lost response after commit. All retain honest exposure and idempotent outcomes.
7. **Persistence:** kill a worker between child creation and dispatch, final cell and cancellation, proof artifact and DB commit, approval and response. No orphan deployable approval, stale write, silent rerun or duplicate budget consumption.
8. **Admission:** successful approval publishes one exact golden tuple selectable in Paper and Live; preserved failed-research labels plus written override do not change technical/operational gates; technical proof failure publishes no tuple and preserves the prior default; absent qualification fails closed; new default leaves old nonrevoked pinned approval resolvable; revoked/stale build still blocks; unrelated stock unaffected; human override cannot bypass corpus or custody.
9. **Frontend:** accessible method and knob controls, server refusal/estimate, keyboard map selection and table alternative, explicit exam action, irreversible candidate lock, manual-override acknowledgement, golden approval with exact Deploy handoff, proof failure, explicit research override and retained incumbent. Validate at 360/768/1024px and dark/light appearance.
10. **Math provenance:** new numerical modules require reference/golden fixtures, explicit justified tolerance and registry updates in the same PR. A rounded paper example cannot justify nine-decimal precision. Reference tests and behavior tests are distinct obligations.

## Research sources and limits

The [methodology evidence note](../research/golden-search-methodology-evidence-2026-09-30.md) records concrete rank-reversal and two-knob counterexamples, inspected source paths, and the distinction between a rounded published DSR example and an independent high-precision equation oracle. It is supporting research, not a replacement for the repository's math authorities.

The architectural findings above are grounded in the linked repository files. Statistical direction is consistent with [the authors' Deflated Sharpe Ratio paper](https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf) and [scikit-learn's nested versus non-nested validation explanation](https://scikit-learn.org/stable/auto_examples/model_selection/plot_nested_cross_validation_iris.html): adaptive selection requires accounting for trials, and evaluation data must not drive the selection procedure. Those sources do not endorse this product's 30-trade floor, three-month default, six/two-month folds or drawdown preference. Those are explicitly editable design policies.

A mock demonstrates interactions and information hierarchy, not tested trading performance. Its return paths, candidate values and statuses are illustrative fixtures. This revision does not run a real strategy study, establish a profitable configuration, amend accepted ADRs, or deploy a bot.
