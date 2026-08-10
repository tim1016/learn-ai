# PRD — Strategy Lab Evidence-Aware Analytical Manual

- **Date:** 2026-08-09
- **Status:** Ready for issue approval
- **Product surfaces:** Strategy Lab Results metric help and `/strategy-lab/docs`
- **Delivery posture:** Five independently verifiable tracer bullets; all five are required for v1
- **Builds on:** `strategy-lab-results-experience.md`, the run-verdict v2 contract, the LEAN native statistics oracle, and the existing Strategy Lab validation analytics
- **Authority:** Python owns metric definitions, numerical policy, calculation metadata, and catalog generation. .NET transports values and explicit provenance. Angular renders, searches, filters, formats, and deep-links without calculating trading results or metric grades.

---

## 1. Executive summary

Strategy Lab exposes a large analytical vocabulary without giving the user one
reliable place to learn what each number means, how it was calculated, which
producer authored it, or what an unavailable value means. The current manual
mixes strategy implementation, engine architecture, validation guidance, and
metric definitions. Several statements have drifted from the canonical code.
The Results page can also display a LEAN-native value under a familiar metric
name while its help text links to the differently defined platform formula.

This PRD replaces that documentation with an evidence-aware analytical manual.
It begins with a short, task-first guide to reading a run, then provides a
searchable reference for every analytical value visible on Results. Formulas
are rendered with the app's existing KaTeX system. Every entry identifies its
producer, input series, units, conventions, edge states, canonical code,
reference evidence, and validating test.

Metric help opened from Results is current-run first. It lands on the exact
producer and calculation contract used for the displayed value. Alternative
variants remain accessible through a **Compare with…** action rather than
competing with the primary explanation.

The existing 0–100 score and letter grade remain. Their presentation is
reframed as strength of backtest evidence, not proof of institutional quality
or permission to deploy capital. Missing required evidence remains ungraded;
it is never coerced to zero.

The manual uses a deterministic synthetic fixture for documentation and
end-to-end tests. Run 130 appears as a clearly dated, non-contractual real-world
case study. The manual does not depend on Run 130 continuing to exist in the
runtime database.

## 2. Decisions captured from product conversation

The following decisions are settled for v1:

1. Use the evidence-aware manual approach rather than a documentation-only
   rewrite or a new-metric research program.
2. Use a stable synthetic run as the tested example and Run 130 as an
   editorial case study.
3. Retain the numerical score and letter grade, but reframe the language around
   evidence strength rather than deployment readiness.
4. Document the complete visible LEAN-native catalog in v1, not only the
   headline or verdict metrics.
5. Render mathematical notation with the existing shared KaTeX implementation.
6. Use a task-first guide followed by a searchable, grouped reference.
7. When a user arrives from Results, show the current run's producer first and
   put alternative variants behind **Compare with…**.

## 3. Product problem

### 3.1 Familiar labels conceal different calculations

Sharpe, Sortino, CAGR, probabilistic Sharpe, profit factor, expectancy, and
drawdown are not single universal values in this product. The platform and
LEAN variants can use different input series, annualization rules, risk-free
rates, units, edge behavior, and fallback values. A label without its producer
is insufficient evidence.

The current GraphQL projection substitutes LEAN-native Sharpe, Sortino, maximum
drawdown, and profit factor into headline fields for LEAN runs. Existing metric
help still points those labels to the platform statistics implementation.

### 3.2 The current manual has drifted from canonical behavior

The current `/strategy-lab/docs` surface is an engine-and-strategy explainer,
not a user manual for Results. Examples of drift include an older all-in equity
reconstruction description, a statement that probabilistic Sharpe is not
computed, and formula help that does not account for the LEAN-native headline
substitutions.

The research draft that motivated this PRD is valuable raw material, but it
also contains formula duplication and claims that must not be published as
facts. The research errata in Appendix A are requirements for the replacement
manual.

### 3.3 Metric states are not a footnote

The UI must distinguish:

- **zero:** a valid computed result equal to zero;
- **undefined:** the formula has no mathematical result for these inputs;
- **unavailable:** required source evidence was not retained or supplied;
- **infinite:** a valid limiting result that cannot be transported as a finite
  JSON number;
- **sentinel:** an engine-defined finite replacement with special meaning;
- **not applicable:** the metric does not apply to this producer or run.

For example, platform Sortino is unavailable when there are no negative return
observations, whereas the pinned LEAN implementation returns zero when annual
downside deviation is zero. LEAN trade profit factor uses a value of 10 when
positive profit exists without loss. The manual must preserve these distinctions.

### 3.4 Verdict language overstates what a backtest establishes

The current verdict can call a high score “institutional-grade” and “ready for
live deployment.” Metric notes also use language such as “likely overfit” or
“data-leak red flag” as though a threshold establishes a cause. A backtest score
can rank the evidence under a frozen product policy; it cannot prove absence of
selection bias, establish live readiness, or diagnose why a result is extreme.

### 3.5 Exhaustive reference content cannot live in a giant component

The LEAN parity contract currently covers 25 portfolio values and 41 trade
values. Results also show runtime snapshot fields, platform metrics, the 17
verdict inputs, and validation-atlas analytics. Encoding all of that as prose
and conditionals in the existing large Angular documentation component would
create another drift-prone source of truth.

## 4. Goals

1. Let a researcher understand the Results page before encountering formulas.
2. Give every visible analytical value a stable, searchable documentation entry.
3. Land contextual help on the exact producer used by the displayed run.
4. Render formulas and variable definitions legibly with the existing KaTeX
   directive and stylesheet.
5. Make calculation conventions and edge states explicit.
6. Connect each numerical claim to canonical code, reference evidence, and a
   validating test or receipt.
7. Explain the 17-metric verdict as product policy without presenting it as a
   scientific deployment decision.
8. Make incomplete verdicts actionable by showing available coverage and the
   exact missing requirements.
9. Eliminate duplicated formula and grading copy from Angular.
10. Prevent catalog drift with generated contracts and completeness tests.

## 5. Non-goals

- Changing a metric formula, threshold, fixed verdict weight, or grade boundary.
- Adding DSR, minimum track-record length, expected shortfall, new VaR models,
  walk-forward optimization, or other new Strategy Lab calculations.
- Claiming that the platform and LEAN variants should numerically agree when
  their contracts intentionally differ.
- Recomputing statistics, grades, causal diagnoses, or validation policy in
  Angular or .NET.
- Turning Run 130 into a required database fixture.
- Documenting metrics that are not visible anywhere in the current Results
  experience merely because they exist elsewhere in the repository.
- Restoring or extending any deprecated Interactive Brokers surface.

## 6. Users and jobs

### Researcher reading a result

- Understand what the score can and cannot establish.
- Learn why a grade is absent without assuming the missing metric was scored as
  zero.
- Open help from a displayed value and see the formula actually used for that
  value.
- Determine whether an extreme number deserves investigation.
- Understand why the recent 500-trade ledger can coexist with full-run totals.

### Quantitative reviewer

- Inspect formula notation, variables, sampling cadence, annualization,
  risk-free-rate source, units, and edge behavior.
- Follow the calculation to canonical code and a validating test.
- Compare the current producer with an alternative definition deliberately.
- Confirm the LEAN source commit and numerical tolerance used by parity evidence.

### Maintainer changing a calculation or UI metric

- Discover the catalog entry and tests that must change with the code.
- Fail CI if a visible metric has no documentation or a catalog entry references
  a missing test/source.
- Generate the frontend contract from Python instead of editing duplicate
  formula strings in TypeScript.

## 7. Product principles

1. **Name the producer.** A metric label is incomplete without its calculation
   namespace.
2. **Current run first.** Contextual help explains the displayed value before
   offering comparisons.
3. **Plain language before notation.** Every entry starts with the decision
   question it helps answer, then interpretation, then formula.
4. **Unavailable is not zero.** Edge states are first-class documented outcomes.
5. **Receipts over prestige language.** Code, inputs, tests, and provenance are
   evidence; “institutional-grade” is not.
6. **One authored definition.** Python generates the catalog contract; Angular
   never becomes a second definition registry.
7. **Stable examples, honest cases.** Tests use synthetic evidence. Runtime
   cases are dated and non-contractual.
8. **Search is navigation.** An exhaustive reference must be usable without
   reading it front to back.

## 8. Information architecture

### 8.1 Route and entry behavior

The canonical route remains `/strategy-lab/docs` so existing bookmarks and
metric links continue to work.

A contextual link may provide:

- `metric`: stable metric identifier;
- `producer`: producer/variant identifier;
- `contract`: calculation or catalog contract identifier;
- an equivalent stable heading anchor for copy/paste and browser navigation.

Opening help from Results focuses and highlights the requested metric, selects
the producer used by that run, and preserves a **Back to run** action. The
documentation route must not fetch the run merely to rediscover context already
provided by the run-detail projection.

A direct visit without run context opens the task-first guide. Search covers all
producers. Selecting a metric chooses its platform variant by default when one
exists and otherwise opens its sole producer. The producer is always visible
and changeable.

### 8.2 Task-first guide

The first section answers these questions without requiring mathematical
background:

1. What did the strategy make or lose?
2. How much path and downside risk did it take?
3. Is the result supported by enough observations?
4. What does the evidence grade mean?
5. Why might the result be ungraded?
6. Which values describe the full run versus the recent ledger?
7. Which curve is realized equity and which evidence feeds risk statistics?
8. What should be validated next?

The guide links into reference entries rather than restating their formulas.

### 8.3 Searchable metric reference

The reference provides:

- full-text search across label, aliases, symbols, interpretation, and source;
- category filters for returns, risk, drawdown, benchmark, trade population,
  trade economics, duration, excursion, statistical confidence, validation
  atlas, runtime snapshot, and verdict policy;
- producer filters for platform, LEAN native, LEAN runtime, validation
  analytics, and verdict policy;
- a **Used by this run** filter when opened contextually;
- a result count and an explicit no-results state;
- stable URLs for every metric variant.

Search and filtering operate on catalog text only. They do not calculate,
rank, or grade numerical values.

### 8.4 Metric entry anatomy

Every entry contains, in this order:

1. Display label and producer badge.
2. One-sentence answer to “What does this tell me?”
3. Current run value when the entry was opened from Results.
4. Interpretation and common misreadings.
5. KaTeX-rendered formula.
6. KaTeX-rendered variable definitions.
7. Inputs, sampling cadence, units, annualization, risk-free-rate convention,
   and cost treatment.
8. Defined edge states and exact UI display for each state.
9. Verdict-policy membership, if any, clearly separated from the mathematical
   definition.
10. Canonical implementation and source reference.
11. Validating fixture/test, contract identifier, tolerance, and parity status.
12. **Compare with…** when another producer exposes a similarly named metric.

The alternative is collapsed by default. Opening it navigates to the other
variant and offers a return link; v1 does not use a default side-by-side layout.

### 8.5 Validation workflow

A separate task-first section explains a recommended reading sequence:

1. Confirm run inputs, data window, fills, costs, and producer.
2. Check evidence completeness before reading the grade.
3. Inspect return and drawdown together.
4. Inspect trade economics and sample size together.
5. Review stability, timing, and seasonality as exploratory diagnostics.
6. Inspect reconciliation and native-parity receipts.
7. Perform independent out-of-sample, forward, or walk-forward validation
   outside the score before making deployment decisions.

This sequence is educational guidance, not financial advice or an automated
deployment gate.

## 9. Versioned metric catalog

### 9.1 Ownership and generated artifact

Python owns a typed, versioned catalog and generates a committed JSON artifact
for Angular. The proposed contract is:

`contracts/strategy-lab/analytical-metric-catalog-v1.json`

The generator must be deterministic. CI regenerates to a temporary target and
compares bytes with the committed artifact. Angular imports or loads the
generated artifact; it does not maintain a hand-authored parallel registry.

The catalog describes calculations but does not contain run values. It is
available even if the Python service or Backend is offline.

### 9.2 Required catalog fields

Each variant entry contains at least:

- `metric_id`: stable concept identifier, such as `sharpe`;
- `variant_id`: stable concept-plus-producer identifier;
- `catalog_version` and calculation `contract_id` when one exists;
- `producer`: `platform`, `lean_native`, `lean_runtime`,
  `validation_analytics`, or `verdict_policy`;
- display label, short definition, interpretation, common misreadings, aliases,
  symbols, and search terms;
- `formula_latex` and ordered variable definitions;
- exact input series and observation selection;
- units, output scale, formatting, sampling cadence, annualization, benchmark,
  risk-free-rate, cost, and timezone conventions;
- supported value states and their display/scoring behavior;
- canonical repository symbol and optional pinned external source;
- validating tests, fixture/receipt identifiers, and numerical tolerance;
- visible Results surfaces and verdict membership;
- alternative variant identifiers.

Optional fields are explicit `null`; the absence of a formula must not be
filled with an invented approximation. Runtime values that are merely reported
by LEAN say so.

### 9.3 Provenance supplied by a run

Run detail must expose explicit documentation context for each displayed metric:

- the value's stable metric and variant identifiers;
- producer namespace;
- calculation/receipt version where persisted;
- whether the producer was recorded or inferred for legacy evidence.

.NET may map and transport this metadata but may not infer a different formula
from the metric label. The existing hidden substitution of four LEAN headline
KPIs must become explicit in this context contract.

For legacy runs that lack a calculation version, the UI says **Version not
recorded for this run; showing the current documented contract**. It must not
retroactively claim a commit or contract that was not persisted.

## 10. Required catalog coverage

### 10.1 Platform and verdict coverage

The catalog covers every headline and More Statistics value, including:

- net P&L, initial cash, final equity, total fees, completed trades, winning and
  losing trades;
- profit factor, expectancy, payoff ratio, win rate, Sharpe, Sortino, maximum
  drawdown, CAGR, Calmar, annual volatility, recovery duration, and maximum
  consecutive losers;
- fee drag, probabilistic Sharpe, sample size, skepticism penalty, and the
  trade-versus-portfolio Sharpe gap;
- all 17 required verdict inputs, their fixed dimension membership, and their
  unavailable behavior.

Score thresholds are documented as `verdict_policy` entries, not as properties
of the underlying statistical definitions.

### 10.2 LEAN-native parity catalog

The 66-value parity inventory is mandatory and exact.

The 25 portfolio keys are:

`alpha`, `annualStandardDeviation`, `annualVariance`, `averageLossRate`,
`averageWinRate`, `beta`, `compoundingAnnualReturn`, `drawdown`,
`drawdownRecovery`, `endEquity`, `expectancy`, `informationRatio`, `lossRate`,
`portfolioTurnover`, `probabilisticSharpeRatio`, `profitLossRatio`,
`sharpeRatio`, `sortinoRatio`, `startEquity`, `totalNetProfit`,
`trackingError`, `treynorRatio`, `valueAtRisk95`, `valueAtRisk99`, and
`winRate`.

The 41 trade keys are:

`averageEndTradeDrawdown`, `averageLosingTradeDuration`, `averageLoss`,
`averageMAE`, `averageMFE`, `averageProfit`, `averageProfitLoss`,
`averageTradeDuration`, `averageWinningTradeDuration`, `endDateTime`,
`largestLoss`, `largestMAE`, `largestMFE`, `largestProfit`, `lossRate`,
`maxConsecutiveLosingTrades`, `maxConsecutiveWinningTrades`,
`maximumClosedTradeDrawdown`, `maximumDrawdownDuration`,
`maximumEndTradeDrawdown`, `maximumIntraTradeDrawdown`,
`medianLosingTradeDuration`, `medianTradeDuration`,
`medianWinningTradeDuration`, `numberOfLosingTrades`,
`numberOfWinningTrades`, `profitFactor`, `profitLossDownsideDeviation`,
`profitLossRatio`, `profitLossStandardDeviation`,
`profitToMaxDrawdownRatio`, `sharpeRatio`, `sortinoRatio`, `startDateTime`,
`totalFees`, `totalLoss`, `totalNumberOfTrades`, `totalProfit`,
`totalProfitLoss`, `winLossRatio`, and `winRate`.

A completeness test compares the catalog variants to the oracle key sets. It
fails for a missing key, undocumented extra key, duplicated variant ID, or a
LEAN entry without the pinned source commit and parity receipt.

### 10.3 LEAN runtime snapshot

Every runtime value rendered in the Native Runtime Snapshot is documented,
including equity, holdings, net profit, total return, unrealized P&L, fees,
volume, order count, runtime probabilistic Sharpe, and attributed trade fees.
These entries are not silently included in the 66-value portfolio/trade parity
count.

### 10.4 Validation atlas

The catalog documents the Python-authored analytics visible in Performance
Memory:

- trailing-horizon net return, coverage, trade count, win rate, and profit
  factor;
- weekday/hour trade count, win rate, and average return;
- calendar-month observation count and median compounded return;
- rolling 20-trade average return and win rate.

The manual states that overlapping rolling windows are correlated exploratory
views, not independent samples. It also distinguishes ET timing buckets from
the local timezone used to render ordinary timestamps.

## 11. Formula rendering and accessibility

The implementation reuses `KatexDirective` for structured metric entries. The
existing global KaTeX stylesheet remains the only math-rendering dependency.
The manual may reuse the existing markdown viewer for editorial sections, but
the searchable catalog is rendered from structured contract fields.

Requirements:

- display equations use KaTeX display mode; short symbols may use inline mode;
- variable definitions follow every equation in reading order;
- formulas never convey meaning by notation alone;
- each formula has an accessible plain-language label or equivalent text;
- horizontal overflow is contained within the formula block on narrow screens;
- a KaTeX render failure displays the escaped source expression and does not
  blank the entry;
- deep-link focus and the highlighted target are keyboard and screen-reader
  discoverable;
- search, filters, compare actions, and disclosures are fully keyboard operable;
- heading hierarchy and landmarks remain valid after filtering.

## 12. Evidence-grade language

### 12.1 Product label

Replace **Production Readiness** on Strategy Lab analytical surfaces with
**Backtest Evidence Grade**. Supporting copy states that the grade is a frozen
17-input product policy and is not authorization to trade.

### 12.2 Grade outcomes

Score boundaries and letter grades do not change. Signals and headlines are
reframed as follows:

| Grade | Evidence-oriented action |
|---|---|
| A+ | Very strong backtest evidence; advance to independent validation. |
| A | Strong backtest evidence; continue forward and out-of-sample validation. |
| B | Promising evidence; investigate identified weaknesses. |
| C | Mixed evidence; revise the hypothesis or validation design. |
| D | Weak evidence; substantial rework is required. |
| F | Insufficient support for the tested strategy hypothesis. |

A failed run remains a run failure, not an F-grade research conclusion.

### 12.3 Diagnostic language

Threshold-triggered notes describe observations and follow-up checks, not
causal verdicts. Examples:

- `Sharpe > 3` becomes **Extreme Sharpe; inspect sampling, annualization, fills,
  data leakage, and selection history** rather than “likely overfit.”
- `Win rate > 85%` becomes **Extreme win rate; inspect sample size, payoff,
  leakage, and fill assumptions** rather than asserting a data leak.
- very high profit factor becomes an out-of-sample and loss-tail review trigger.

The scoring penalty is unchanged in v1.

### 12.4 Incomplete evidence

An incomplete verdict displays:

- available required count over 17;
- the exact missing metric labels;
- the documented reason for each unavailable value;
- a statement that no composite, grade, or action was produced.

Run 130 therefore explains that Sortino is unavailable because its platform
window contains no negative returns. It does not say that Sortino is zero or
that the absence of downside proves a flawless strategy.

## 13. Examples

### 13.1 Synthetic contract example

Create a deterministic `strategy-lab-analytical-manual-v1` fixture with:

- a complete 17-input evidence grade;
- platform and LEAN variants for shared labels;
- at least one unavailable value, one valid zero, one sentinel, and one legacy
  unknown-version state in focused fixture variants;
- full-run statistics and a deliberately shorter displayed trade ledger;
- validation-atlas output;
- timestamps expressed as `int64 ms UTC` at every contract boundary.

All automated documentation screenshots and deep-link tests use this fixture.
The fixture receives a manifest and hashes consistent with existing scientific
fixture practice.

### 13.2 Run 130 case study

The manual may include a dated case study using Run 130 to teach:

- incomplete 16/17 evidence caused by unavailable Sortino;
- why an extreme Sharpe is an investigation trigger, not a diagnosis;
- why 500 recent ledger rows can coexist with 867 authoritative full-run trades;
- why LEAN-native and platform definitions must be named.

The case study is marked **Illustrative snapshot, not a contract fixture**. Its
absence never breaks the manual, tests, routing, or catalog.

## 14. Research and provenance requirements

Create a companion reference note that records the research claim ledger. Each
claim is classified as verified, verified with a producer-specific
qualification, product policy, corrected, or deferred.

At minimum, the reference note cites:

- the pinned QuantConnect LEAN commit used by the native-statistics oracle;
- the platform statistics and run-verdict implementations;
- Sharpe's differential-return definition and annualization assumptions;
- serial-correlation limitations on square-root annualization;
- probabilistic and deflated Sharpe distinctions;
- the repository's validation-analytics formulas and fixtures.

Source citations support the calculation actually documented. General
literature must not overwrite a producer-specific code contract.

## 15. Transport and UI integration

### 15.1 Results metric help

Every documented Results value has an adjacent accessible help action. The
compact popover shows definition, producer, value state, and a **Full formula
and evidence** link. It does not embed an abbreviated formula from a separate
TypeScript registry.

### 15.2 Explicit headline provenance

For a LEAN run, the run-detail response marks the current source of the four
headline substitutions: Sharpe, Sortino, maximum drawdown, and profit factor.
Tests assert both value selection and producer selection.

### 15.3 Removal of client-authored grading

Strategy Lab Results must not call TypeScript functions that assign numerical
bands or make causal claims from persisted metric values. Existing metric-grade
utilities and documentation scorecards are removed from the canonical Results
and manual paths or reduced to non-numerical presentation helpers. Python's
persisted verdict remains the sole grade authority.

### 15.4 Full-run and display projections

The manual and help distinguish:

- authoritative full-run totals and statistics;
- the most recent 500 displayed ledger trades;
- realized equity used for the primary visual narrative;
- canonical mark-to-market or compatibility inputs used by risk statistics.

Angular may format and map display geometry but does not recalculate the
underlying analytical values.

## 16. Loading, empty, and failure states

- Catalog load failure shows a retryable documentation error; the Results value
  remains visible.
- An unknown metric deep link opens the reference with the requested identifier
  preserved in an error message and search prefilled where safe.
- An unknown producer opens the metric's available producer list without
  silently choosing a claimed run source.
- No search matches states which filters are active and offers **Clear filters**.
- No formula is displayed as **Reported value; no local formula contract**.
- A missing source/test reference fails catalog validation rather than rendering
  an empty receipt.
- A legacy run without version metadata is labeled honestly as described in
  Section 9.3.

## 17. Testing and evidence

### 17.1 Python and contract tests

- Catalog schema validation and stable identifier tests.
- Deterministic JSON generation/drift test.
- Exact 25-portfolio plus 41-trade LEAN catalog completeness test.
- Catalog source symbols and referenced fixture/test paths exist.
- Required edge states are present for every metric that can emit them.
- Every verdict input maps to one mathematical metric or explicit policy entry.
- Existing platform, verdict, LEAN parity, and validation analytics tests remain
  green with their pinned tolerances.

### 17.2 Backend tests

- Run-detail headline value and producer provenance agree for Python and LEAN
  runs.
- Missing and legacy contract metadata remain explicit.
- GraphQL transports catalog/run context without calculating a value.
- The full-run versus 500-ledger contract remains unchanged.

### 17.3 Frontend tests

- Search covers labels, aliases, symbols, and source terms.
- Category/producer/current-run filters compose correctly.
- Contextual links focus the requested metric and producer.
- **Compare with…** navigates to the alternative and back.
- KaTeX renders representative inline and display equations and exposes a
  readable fallback.
- Unknown metric, unknown producer, no-result, and catalog-error states render.
- Incomplete evidence shows exact coverage and never renders a grade.
- Strategy Lab canonical paths no longer compute client-side grade bands.
- Metric help links all resolve to catalog entries.

### 17.4 Browser and accessibility evidence

Using the synthetic fixture, verify:

- task-first reading flow at desktop and narrow widths;
- keyboard-only search, filtering, disclosures, deep links, and return-to-run;
- focused target visibility below the sticky application shell;
- long KaTeX formulas do not overflow the viewport;
- screen-reader labels for formulas, producer badges, state, and help actions;
- copied deep links reopen the same metric variant.

## 18. Success criteria

1. One hundred percent of values rendered on canonical Strategy Lab Results
   have a catalog variant or an explicit non-metric display classification.
2. All 66 LEAN portfolio/trade keys are present exactly once in their native
   variant inventory.
3. A help action from a LEAN headline value never lands first on the platform
   formula.
4. No canonical Strategy Lab Angular path authors a trading formula, metric
   grade, or causal diagnosis.
5. An incomplete run explains coverage and missing evidence without displaying
   a composite or grade.
6. Every formula entry renders through the existing KaTeX system and includes
   plain-language interpretation.
7. The committed catalog is reproducible from Python with no diff.
8. The synthetic manual fixture is the only required example dependency.

## 19. User stories and acceptance criteria

### Story 1 — Read a result without knowing the vocabulary

As a researcher, I can follow a short sequence that explains return, risk,
evidence coverage, trade economics, and validation next steps before I open the
metric encyclopedia.

**Acceptance criteria**

- The guide fits a task sequence rather than an engine architecture narrative.
- Each step links to relevant catalog entries.
- The guide explains the score's limits and full-run/display distinction.

### Story 2 — Open the correct formula from a run

As a reviewer, I can open Sharpe help on a LEAN run and land on the LEAN-native
Sharpe definition used by that displayed value.

**Acceptance criteria**

- The producer and contract context are explicit.
- The platform alternative is available through **Compare with…**.
- The source commit, input cadence, risk-free rate, and parity receipt are shown.

### Story 3 — Search the exhaustive catalog

As a reviewer, I can search for `MAE`, `tracking error`, `VaR`, or a code key and
find the relevant visible metrics.

**Acceptance criteria**

- Search works across display names, aliases, symbols, and source keys.
- Filters and result counts are accessible.
- Every one of the 66 native keys is discoverable.

### Story 4 — Understand why no grade exists

As a researcher, I can see that a run has 16 of 17 required inputs, learn which
one is unavailable and why, and understand that no score was produced.

**Acceptance criteria**

- Missing metrics are never shown as zero.
- The related metric entry explains its producer-specific edge state.
- No deployment-oriented instruction is shown.

### Story 5 — Audit a documentation claim

As a maintainer, I can follow a metric entry to canonical code and the test or
fixture that validates it.

**Acceptance criteria**

- Repository paths and external source references are precise.
- LEAN entries identify the pinned commit and tolerance.
- Broken references fail automated validation.

## 20. Delivery slices

### Slice 1 — Catalog trust spine and headline journey

Create the Python-owned schema, deterministic generated contract, task-first
manual shell, search/filter primitives, and catalog entries for headline plus
17 verdict metrics. Demonstrate one contextual platform metric and one
contextual LEAN metric end to end.

### Slice 2 — Exhaustive LEAN-native encyclopedia

Add all 25 portfolio and 41 trade entries, runtime snapshot entries, source
commit/tolerance receipts, and completeness tests. Add current-run-first
navigation and **Compare with…** for shared labels.

### Slice 3 — Validation analytics and display semantics

Document validation-atlas fields, full-run versus recent ledger behavior,
realized versus mark-to-market equity, timezone conventions, and all associated
contextual help actions.

### Slice 4 — Evidence-grade language and authority cleanup

Reframe score/signal/headline copy in Python without changing weights,
thresholds, score boundaries, or letter grades. Expose exact incomplete
coverage. Remove canonical Strategy Lab client-side grading and stale
documentation scorecards.

### Slice 5 — Examples, accessibility, and hardening

Add the deterministic synthetic fixture, optional dated Run 130 case study,
responsive and accessibility behavior, broken-reference checks, full browser
evidence, and final manual editorial review.

Slices 1 and the research claim ledger may start together. Slice 2 consumes the
catalog contract from Slice 1. Slice 3 may proceed once stable identifiers
exist. Slice 4 consumes the same identifiers for help links. Slice 5 closes the
release after all catalog coverage is present.

## 21. Definition of done

- All settled decisions in Section 2 are implemented.
- The stale manual is replaced at the existing route without breaking stable
  Strategy Lab navigation.
- All canonical Results values have source-aware contextual help.
- The 66-value LEAN catalog is complete and parity-linked.
- Platform, runtime, verdict-policy, and validation-atlas coverage is complete.
- KaTeX, search, filtering, deep links, compare navigation, and edge states meet
  their acceptance criteria.
- Score and grade math are unchanged; presentation no longer claims deployment
  readiness or causal certainty.
- Python, Backend, Frontend, contract, browser, and accessibility tests pass.
- Math source-of-truth and engine-authority documents are updated if delivery
  introduces, moves, or retires a calculation path. Documentation-only catalog
  changes do not falsely claim a new math authority.
- The final diff passes the repository's thermo-nuclear maintainability review.

## Appendix A — Required research errata

The replacement manual must encode these corrections:

1. Platform CAGR uses trading duration divided by 252; pinned LEAN CAGR uses
   calendar duration divided by 365.
2. The pinned LEAN statistics builder consumes daily equity/performance inputs;
   it is not documented as tick-by-tick drawdown calculation.
3. Missing platform Sortino makes verdict evidence incomplete; it is not scored
   as zero.
4. Platform and LEAN probabilistic Sharpe use different benchmark conventions.
   Neither alone corrects for an unknown number of strategy trials.
5. Platform profit factor uses summed trade returns. LEAN trade profit factor
   uses account-currency P&L and a no-loss sentinel of 10.
6. Platform expectancy is arithmetic mean trade return. LEAN portfolio
   expectancy is `winRate × profitLossRatio − lossRate`.
7. Extreme Sharpe, profit factor, or win rate is an investigation trigger, not
   proof of lookahead, leakage, or overfitting.
8. In-sample evaluation creates overfitting risk; it does not logically
   guarantee that every evaluated strategy is overfit.
9. Walk-forward and deflated-Sharpe functionality already exists elsewhere in
   the repository but is not currently integrated into Strategy Lab Results.
10. Jensen alpha and Information Ratio formulas must be transcribed correctly;
    duplicated beta/tracking-error equations from the research draft are not
    retained.

## Appendix B — Primary repository receipts

- `PythonDataService/app/engine/results/statistics.py`
- `PythonDataService/app/engine/results/lean_statistics.py`
- `PythonDataService/app/services/run_verdict_service.py`
- `PythonDataService/app/services/engine_validation_analytics.py`
- `PythonDataService/tests/test_lean_statistics.py`
- `PythonDataService/tests/services/test_run_verdict_parity.py`
- `PythonDataService/tests/services/test_engine_validation_analytics.py`
- `docs/references/lean-native-statistics-oracle-v1.md`
- `docs/math-sources-of-truth.md`
- `docs/architecture/engine-authority-map.md`
