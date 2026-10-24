# ADR 0085: Configuration review lives in Golden, and the display term is "Selected baseline run"

Date: 2026-10-24

**Status:** Accepted 2026-10-24

**Vocabulary:** `CONTEXT.md` § "Golden Validation (resolved 2026-09-11)" and § "Places (resolved 2026-10-23)" — **selected baseline run**, **benchmark baseline**, **Configuration review**, **Proof**.

Extends ADR 0084 (the signal strategy place). Presents ADR 0061 (Golden Validation) and ADR 0074 (Golden Search qualified configurations) in one home; it changes neither. Contexts: PRD #3490 (R17, R18, F04, T11, T12) and slice #3510.

## Context

Strategy Validation was one page holding three different facts: the program's proof pipeline, the one strategy-level human flag, and an embedded Golden Validation workbench that accepts an exact configuration (ADR 0061). Backtest → History carried a second way into the same workbench ("Mark golden"). So settings could be approved in two places outside Golden, and moving the whole page to a "Proof" tab would have kept that. Removing the workbench instead would have deleted the manual configuration-review path that ADR 0061 gives the owner. Calling its result "program proof" would have generalised one configuration to other symbols and parameters (F04).

"Validation Golden Run" also read as a claim. ADR 0061 already says "golden" means *user-selected baseline*, not best, profitable or optimal, but the word kept being read beside Golden Search's "golden" configurations and beside the research baselines (buy-and-hold, random-entry) a Research run carries.

## Decision

1. **Four homes, one per kind of evidence** (PRD R18). Each says what it does not claim.

   | Fact or action | Home | What it does not claim |
   |---|---|---|
   | Program proof and the strategy-level human flag | Proof (`/signal-strategies/:key/proof`) | Approval of any symbol or settings |
   | Selected baseline run and the ADR 0061 exact-configuration review | Golden → Configuration review (`/golden/configuration`) | Corpus coverage, or a Golden Search version |
   | Golden Search studies, immutable qualified versions, the active default, re-proof | Golden → Studies and Versions (`/golden/studies`, `/golden/studies/:studyId`, `/golden/versions`) | A profitability guarantee, or permission to bypass Deploy |
   | Numerical reference fixtures | Operations → Math checks | Permission to deploy |

2. **Settings are approved only in Golden.** Explore never approves: Backtest → History links a completed run to Configuration review (`?run=`) and approves nothing itself, and the workbench is not embedded in Explore. Proof shows proof and the flag and links to Configuration review. The Strategy Validation page is gone; its proof pipeline, readiness statement, flag form and evidence are Proof's, and its workbench is Configuration review's.

3. **Nothing about the decision changes.** ADR 0061's classifications — Engine agreement, Reviewed deviations and the conspicuous Manual override — their required notes, the acknowledgement a Manual override needs and the evidence revision a review is checked against are the workbench's own and are unchanged. A technical proof failure of a Golden Search approval stays non-overridable: its only way on is Retry qualification. Qualification criteria, seals, promotion rules and the immutable records are untouched. This is a move of presentation and navigation.

4. **Display term.** The display label "Validation Golden Run" becomes **Selected baseline run**, wherever it was shown: the workbench, the run history's badge and action, the notes under it, the architecture manual and the one server message that names it. Stored types, wire fields (`golden_run_id`, the Golden Validation record), ADR 0061's text and every historical decision keep their wording. A **selected baseline run** is a person's chosen run for one exact configuration. It is not a **benchmark baseline**, the null-hypothesis comparison (buy-and-hold, random EMA windows) attached to a Research strategy run.

5. **Golden keeps each thing's own state.** Lock and versioning, the search, test-over-time, compare, final test and decision controls, immutable study history, exposure disclosures, qualified versions, the active default and re-proof each show their own state. A version is ready, stale, revoked or unverifiable; it covers one symbol and one exact configuration; the default is only what Deploy offers first. There is no generic green "golden" badge and no strategy-wide approval. Versions offers Re-prove for a stale version (the call Deploy already makes) and "Use in Deploy". No control revokes a version: none existed, and adding one is a promotion rule, not a move.

6. **Explore → Golden hands over a plan to review.** From a backtest run, a sweep or a walk-forward study, Studies opens an **unlocked** plan (`?run=`, `?search=`, `?walkForward=`, the evidence's own native ids). A grid becomes the plan's knob ranges, and costs, capital, fill mode and fold lengths carry; a single run is one point and carries only its symbol and costs. Every other field of the source is listed as not carried, with the reason. The destination checks the plan as it checks any edit and starts nothing: it does not lock the plan, run a stage or reuse an exploratory leader as a starting point or as proof, and it never takes the explored window as the study range or chooses the final interval. The explored window is compared with the final test's dates: where they overlap, the plan says the dates were already viewed in Explore, shows recorded research's own answer under that name, and does not offer the dates as unexposed. This agrees with ADR 0074 § 4, where a Grid Search, Walk-Forward Study or Strategy Lab run that overlaps an interval already makes its exposure "history unknown"; the plan's wording covers the window even where a run left no record.

7. **Addresses and redirects (one hop each, keeping query and fragment).** `/golden-search/:studyId` resolves its strategy from the stored study, as does `/signal-strategies/evidence/study/:id` (ADR 0084); `/golden-search?revise=<id>` goes to the plan under the strategy that ran the study. `/golden-search` and `/strategy-validation` with no strategy open a chooser at the address; a study the registry cannot place, a missing study and one the link claims for another strategy fail in place, keeping the id. `/strategy-validation?strategy=` goes to Proof, or to Configuration review when `?view=configuration|golden|review` or a `?case=` asks for the review. `qc_backtest_id`, which Strategy Validation's Strategy Lab link sent and nothing read, is dropped.

8. **Signal Golden Search no longer links to Options Golden Search**, nor the reverse. They share a style of navigation, not an identity, a metric convention, a proof or an execution engine.

9. **Golden → Deploy carries the strategy and the version.** "Use in Deploy" (a study's decision and a ready version) opens the account list with `strategy=<registry key>&golden_qualification=<id>`, Deploy's own keys. Deploy applies the version's own strategy, symbol and settings and the owner still chooses the account; navigation holds no command authority. Deploy's "View validation" link now leads to the strategy's Proof.

## Considered options

- **Move the whole Strategy Validation page to Proof.** Rejected: the embedded workbench would approve settings outside Golden, and its result would pass for program proof (F04).
- **Delete the workbench, keeping only Golden Search.** Rejected: it removes the manual configuration-review path ADR 0061 gives the owner, which Paper and Live experimentation still use.
- **Embed the workbench in Explore's History, as before.** Rejected: Explore never approves settings.
- **Rename the stored types and wire fields.** Rejected: ADR 0061's records and every historical decision keep their wording; only the display term moves.
- **Carry an Explore window as the plan's study range.** Rejected: the final test would sit inside the window the owner already saw, and Explore would be choosing the final interval.
- **Add a revoke control to Versions.** Rejected for this move: revocation is an existing server action with no control today, and a new state-changing control is a promotion rule.

## Consequences

- A strategy's Golden tab has three pages and its Proof tab one, and the Signal strategies menu lists no Golden Search or Strategy Validation entry. The old addresses stay as redirects.
- Strategy Validation's validation-queue counts (total, deployable, blocked) are gone: the strategies list shows each strategy's proof, and the workspace header shows the open one's.
- The served Golden Search manual still links to `/golden-search/<id>`, which resolves the strategy, because the served document names a study and no strategy.
- Re-proof and "Use in Deploy" exist on Versions as well as at Deploy; one version's re-proof state is shown beside that version and no other.
