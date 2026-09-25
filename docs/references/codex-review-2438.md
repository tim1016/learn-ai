# Independent review: bot trading engine and backtesting

Final resolution of [[Codex] Synthesis](https://github.com/tim1016/learn-ai/issues/2438), under [Map — [Codex] Independent review: bot trading engine & backtesting](https://github.com/tim1016/learn-ai/issues/2413).

**Review baseline:** `10b5f31b529c8507bd19bb24015f3d85fa9aba43`. These are findings about that source revision, not observations of deployed accounts or a claim about changes made after it. Evidence and reproductions are pinned in the source index below.

**Result:** 19 investigations produced 27 material findings: **2 Critical, 19 High and 6 Medium**, merged into **21 recommendations**. The review covered Stocks, Alpaca Accounts and the bot engine, Strategy Tools, and the backtest/live seam. The final five investigations produced no new Critical or High finding, satisfying the requested stopping rule. Investigation is finished; this synthesis introduces no new defect investigation.

The first priorities are to make cash admission prove the cash actually available for the proposed executable order, include the first evaluated session in statistical evidence, and make the daily-loss limit measure loss over its stated time boundary. Strong custody identities and replay controls coexist with these failures: preventing duplicate effects does not prove their economic inputs are correct.

This register ranks **impact only**: potential harm, breadth of affected consumers, and how silently a misleading result can be accepted. Effort, cost and implementation size are not ranking factors. Global ranks remain comparable across the four goal groups; each recommendation appears under its primary goal. Severity describes the demonstrated failure and the supplied review definitions, not its observed production frequency. A Critical validation defect does not imply that a grade can directly authorize an order.

## Systemic patterns

These are inferred explanations across reproduced cases, not additional counted findings. They are the main architectural conclusion of the review.

### 1. Evidence frequently identifies a proxy rather than the fact the consumer needs

At least five independently reproduced boundaries share this shape:

| Evidence available | Stronger conclusion made | Missing premise |
| --- | --- | --- |
| Account/position request bundle finished at time T | Cash includes fills before T | The cash response may have arrived before those fills. |
| Qualified source bytes exist at admission | The process executes that qualified code | Previously imported executable objects can belong to another source generation. |
| A path was hashed before or after a run | The run consumed those exact bytes | The file can be replaced between hashing and reading. |
| A factor file is present and nonempty | This study is split/dividend adjusted | The file must cover the study's actions and belong to the intended revision. |
| CSV rows align | The indicators have adequate comparable evidence | Values can be missing, keys duplicated, or timestamps unrelated. |

The hashes, timestamps and arithmetic can all be internally correct. More checking of the same proxy will still miss the defect. Recommendations 1, 4, 5, 9 and 15 therefore name the required relation: **which observation covers which fill; which process executes which code generation; which read consumed which bytes; which factor revision covers which interval; which valid pairs support which comparison**.

A useful design rule follows: every proof-bearing result needs a subject, version or observation boundary, coverage, and a named consumer claim. If the system cannot establish that relation, it should report a narrower fact or explicit uncertainty. This does not require a universal proof framework or a new central service. The same-buffer sweep reader already demonstrates a stronger relation than several sibling readers.

### 2. A single numerical authority can still answer the wrong economic question

Five cases survive the repository's one-Python-authority rule: first-session resampling omits part of the advertised evaluation window; daily P&L combines session realized results with lifetime unrealized results; a decision-price callback changes a current-time valuation mark; closed-trade parity substitutes for marked portfolio risk; and noon-only bars substitute for full-session return anchors. These are different observation bases, not rounding errors or duplicate implementations.

Recommendations 2, 3, 10, 11 and 14 should preserve an explicit economic definition alongside each value: observation interval, opening baseline, marked versus closed-trade basis, costs, coverage, and sample identity. Reconciliation should test identities that cross the boundary—compounded returns versus opening/final equity, session loss versus carried-position marks, valuation timestamp versus its price—not only agreement between two callers of the same helper.

This extends the earlier [numeric authority census][prior-numeric] rather than rejecting it. Its finding of one FIFO implementation remains valuable. One implementation can faithfully compute an expression that does not mean “today's loss.” Likewise, two engines can agree because both discarded the same risk observations.

### 3. Correct safeguards are local; sibling paths escape their premise

The review found at least six pairs where one path supplies the missing example: recovery EXITs check current session while ordinary program EXITs do not; sweep reads bind a hash to one buffer while other readers use mutable paths; catalog-aware publication reasoning does not cover filesystem-only admission; Engine Lab refuses empty results while Strategy Spec can report success; export computes indicators before trimming while chart computation drops warmup; two-phase recovery feedback checks route identity while ordinary action feedback does not.

Recommendations 5, 7, 12, 13 and 17 should define the invariant at the shared consumer boundary and enumerate every entry point that claims it. Consolidating names or creating another helper is insufficient unless every relevant path reaches it. Conversely, a broad rewrite is not supported: the working sibling behavior supplies both a design reference and focused counter-evidence.

The earlier [strategy-execution research brief][prior-strategy] described “correct mechanisms left unwired, not wrong logic.” The sibling-path pattern remains visible, but that explanation is incomplete for this baseline: the first-session statistical defect and the mixed daily/lifetime P&L definition also require correcting numerical meaning. This is a refinement of a dated diagnosis, not a claim that the earlier review proved all arithmetic correct.

### 4. Uncertainty disappears between internal evidence and the headline result

Six reproduced examples turn absent or contradictory evidence into ordinary-looking outcomes: contradictory fill prices leave “clean” reconciliation; no Spec input leaves successful flat results; insufficient CSV values leave “Excellent”; a save that may still finish becomes “not saved”; late capture subscription loses earlier failures under a replay claim; and a commission input remains editable although the resolved paired model ignores it.

Recommendations 8, 12, 15, 18, 20 and 21 should preserve the distinction through the whole response: absent, partial, contradictory, pending, and resolved-with-a-different-policy are different facts. A nullable identifier or success boolean cannot carry all of them. Several producer controls already distinguish these states; correctness is lost when a downstream projection collapses them. This is why passing backend recovery tests and accurate individual calculations do not alone establish a trustworthy screen.

### 5. Qualification must state the dimension it actually establishes

Decision replay, engine parity, numerical correctness, data coverage, strategy selection confidence, and execution realism are separate questions. Passing one does not establish the others. The review found strong evidence for training-winner selection, durable custody identity, source retention, and terminal accounting; it also found overstated conclusions at boundaries outside those tests.

Recommendations 2, 4, 5, 10, 15 and 16 call for narrower, composable claims. Preserve reference-parity modes and the fixed evidence policy, but identify their basis and limitations where the result is used. A selected sample's high PSR is not selection-adjusted confidence; a common trade ledger is not complete portfolio risk; a decision-source proof is not an economic execution proof. No recommendation requires replacing IBKR data, adopting Alpaca data subscriptions, or treating backtest evidence as permission to trade.

## Ranked register — Best execution

### Rank 1 — Prove cash coverage and bound the executable entry cost

**Severity:** Critical, incorporating one High subfinding. **Prior-work classification:** **Contradicts a prior conclusion** for the hard cash guarantee; the cash-observation race is new. **Related goals:** correctness, runtime intelligence, architecture.

**Evidence — reproduced:** A1/A2 in [[Codex] Account engine execution, custody, and recovery][src-accounts]. A cash response of 1,000 arrives before a first bot's 1,000 fill; positions finish afterward. The later bundle timestamp releases that fill's reservation as though cash already included it, allowing another bot to spend the same 1,000. Independently, 10 market shares admitted at 100 can fill at 101 and cost 1,010. The same-bot entry fence still refuses duplicates.

**Recommendation:** Bind reservation release to proved coverage by the cash observation, not request completion. Retain account-wide reservations across subjects until that relation is known. If the guarantee remains a hard cash bound, reserve an enforceable maximum all-in cost, including the relevant fee treatment; an estimated market price cannot establish a maximum. Record the order-price/fill-probability trade-off through the open owner decision. No arbitrary independent per-order cap is implied.

**Confidence and limit:** High for both synthetic mechanisms. No real borrowing or broker loss was observed. An enforced observation watermark and an executable cost bound would change the conclusion; a newer quote alone would not. The prior guarantee and its rationale are challenged explicitly below.

### Rank 7 — Give every reducing intent a first-submission validity boundary

**Severity:** High. **Prior-work classification:** **Contradicts a prior conclusion.** **Related goals:** correctness, runtime intelligence.

**Evidence — reproduced:** R1 in [[Codex] Reducing execution and recovery across partial fills and sessions][src-reduction]. A normal EXIT accepted at 15:59 first sends a non-extended market DAY at 16:01; an equivalent recovery EXIT refuses. The real resolver was exercised with a recording fake broker. The documented vendor consequence is next-session queueing, not immediate current-session reduction.

**Recommendation:** Preserve accepted intent identity while checking eligibility immediately before every first submission. A proved-unsent expired leg should produce an explicit recoverable outcome; a newly authorized reduction can use the current-session policy. An uncertain or working order still requires exact status/cancellation evidence before replacement. Do not silently reprice an uncertain order. The owner must decide whether next-open queueing is ever intentional.

**Confidence and limit:** High for the missing guard and synthetic send; the complete unattended overnight outcome is inferred from documented broker behavior. No broker was contacted. The previously known queueing behavior and deliberately retained program exception are distinguished below.

### Rank 21 — Display the effective paired commission policy and reject contradictory controls

**Severity:** Medium. **Prior-work classification:** **New.** **Related goal:** correctness.

**Evidence — reproduced:** X1 in [[Codex] Simulated execution causality and live cost assumptions][src-fills]. Paired mode accepts an editable flat commission of 99, while the resolved fixed IBKR parity profile charges 1 for the tested 100-share trade. The profile and actual fees are visible, bounding the defect; no wrong fee-model arithmetic was shown.

**Recommendation:** Make the effective execution policy authoritative in configuration, response and presentation. Disable or reject unsupported overrides and explain the pinned model at the control. Retain the reference-parity fee model; do not silently alter it to satisfy an inapplicable control.

**Confidence and limit:** High for the accepted-input/resolved-model mismatch. Prior work already required cost controls to reflect their effects, but no matching paired-mode defect was found. Full browser behavior and a resulting wrong strategy ranking were not demonstrated.

## Ranked register — Correctness

### Rank 3 — Make the daily-loss hold measure a defined session's economic change

**Severity:** High. **Prior-work classification:** **Contradicts a prior conclusion** about daily-loss meaning; the arithmetic expression was already known. **Related goals:** runtime intelligence, best execution.

**Evidence — reproduced:** A3 in [[Codex] Account engine execution, custody, and recovery][src-accounts]. A carried 100-share position bought at 100, previously marked at 200 and now at 150 has lost 5,000 over the new day. The gate sees positive 5,000 lifetime unrealized P&L and does not raise the tested daily-loss hold.

**Recommendation:** First choose the session/day boundary and treatment of carried exposure, cash flows and fees. Recommended owner answer: use account equity change from that boundary, with explicit coverage, and preserve the account-wide, entry-blocking character of the hold. Keep lifetime open P&L separately named. Make the same session definition govern the risk gate and its explanation.

**Confidence and limit:** High for the time-basis counterexample. This challenges ADR 0059's definition, not an implementation that secretly violated that formula. An owner-confirmed lifetime-based guard with different naming would change the product judgment; it would not establish a daily-loss measure.

### Rank 6 — Validate bar meaning before publishing canonical lake artifacts

**Severity:** High. **Prior-work classification:** **New.** **Related goals:** trading intelligence, architecture.

**Evidence — reproduced:** finding 1 in [[Codex] Stocks data integrity, causality, and analytical claims][src-stocks]. Publication accepts duplicates, wrong-day bars, impossible OHLC and negative volume. The LEAN encoder discards the original date; the reader reconstructs the requested date, so a wrong-day input can become a plausible bar on another day.

**Recommendation:** Make requested-date membership, timestamp uniqueness/order, finite valid prices, OHLC consistency and valid volume part of publication admission. Preserve a reasoned rejection rather than normalize corrupt observations into plausible data. Downstream strict readers remain useful but cannot recover a date already erased at encoding.

**Confidence and limit:** High for the exercised writer/parser boundary with mocked external acquisition/catalog operations. No existing lake corruption was inspected. Older duplicate-bar findings concern other consumers and do not establish this publication check.

### Rank 8 — Preserve conflicting execution economics even when quantities agree

**Severity:** High. **Prior-work classification:** **New.** **Related goals:** architecture, runtime intelligence.

**Evidence — reproduced:** E1 in [[Codex] Economic execution records and correction authority][src-economic]. Exact fills of 10 at 100 are followed by a newer aggregate of 10 at 90 and position basis 900. Actual reconciliation returns clean, retains the old basis and calls economic coverage complete. At a mark of 110, the two bases imply open P&L of 100 versus 200.

**Recommendation:** Treat same-quantity price/cost disagreement as retained contradictory evidence with an explicit economic-coverage disposition. Do not overwrite exact executions merely because an aggregate is newer. Reconciliation must compare economic facts before certifying clean coverage, then resolve the discrepancy through its documented authority rules.

**Confidence and limit:** High for the reconciliation counterexample. Retail correction delivery, ordering and replay guarantees remain unestablished; [[Codex] Retail execution correction protocol and evidence limits][src-corrections] records that boundary. This does not show the frequency of vendor corrections or invalidate the already tested atomic recovery transition.

### Rank 9 — Version historical materialization by coverage and corporate-action basis

**Severity:** High. **Prior-work classification:** **New** merged materialization recommendation. L1 specifically contradicts a prior unconditional product claim; the map-window and mixed-vintage mechanisms are new. **Related goals:** trading intelligence, architecture.

**Evidence — reproduced, with one inferred downstream consequence:** L1–L3 in [[Codex] Historical instrument, adjustment, and materialization lineage][src-universe]. A present factor file ending before a neutral 2:1 split permits a labelled-adjusted −50% return instead of 0%. A map built for an early capture interval is reused for a larger interval. Adjusted day artifacts captured before and after a later action can share the same mode despite different adjustment vintages. The map-cache defect was reproduced; the resulting LEAN history restriction follows pinned reference code, without executing LEAN.

**Recommendation:** Admit materializations against one declared historical-data contract: covered interval, actual listing bounds, factor/action revision, and chosen as-of basis. Do not use capture-window end as delisting truth or mode name as revision identity. Refresh or refuse uncovered factors and incompatible vintages. The owner should choose latest-common-revision versus historical-as-of products explicitly; the recommendation does not assume a complete point-in-time universe already exists.

**Confidence and limit:** High for cache/coverage behavior; medium for the unexecuted LEAN consequence. Prior mode-isolation and factor-lookup tests remain valid. They do not prove that a supplied file covers all actions in the requested study. No real historical dataset was sampled.

### Rank 11 — Separate decision reference prices from current valuation marks

**Severity:** High. **Prior-work classification:** **New.** **Related goal:** trading intelligence and backtest/live equivalence.

**Evidence — reproduced:** S1 in [[Codex] Validated strategy to live bot semantic equivalence][src-seam]. The engine writes the current source close, then a consolidator callback overwrites the same portfolio field with the prior signal close. The current-time snapshot values 100 shares at 100 instead of the just-observed 101, reporting 10,000 rather than 10,100.

**Recommendation:** Give decision sizing/fill references and current portfolio valuation separate authorities, or strictly restore the current observation before every valuation consumer. Preserve intentional signal-close parity semantics. Make each equity/insight value identify its observation clock, and reconcile signal time, fill time and valuation time behaviorally.

**Confidence and limit:** High for intermediate marked equity and a reachable statistics consumer; ranking impact depends on price paths. No wrong final realized P&L was demonstrated. Earlier shared-program/replay recommendations do not establish valuation-clock equality; the exact mechanism was not found in prior work.

### Rank 13 — Apply one analytical window contract to chart and export

**Severity:** High. **Prior-work classification:** **New** merged cross-consumer recommendation; warmup/display separation was already recommended for a different chart route. **Related goal:** trading intelligence.

**Evidence — reproduced:** findings 2–4 in [[Codex] Stocks data integrity, causality, and analytical claims][src-stocks]. The same date intent exports 780 rather than 390 rows; chart EMA(5)’s fifth displayed point is 200 instead of 186.831275720 after fetched warmup is trimmed before calculation; four-hour RTH chart buckets start at 08:30/12:30 with 180/210 minutes instead of 09:30/13:30 with 240/150.

**Recommendation:** Normalize requested dates, session membership, aggregation origin, calculation context and final displayed/exported scope once as an explicit analytical contract. Compute indicators over admitted context before trimming. Verify final membership at each consumer. Shared timestamp units alone do not establish that a date means the same interval.

**Confidence and limit:** High for these chart/export examples. Bot execution was not shown to share the defects. Credit [Chart history: warmup bars for indicator lookbacks beyond the display cap][prior-1611] for the earlier warmup recommendation; its broker-panel display-cap trigger differs from this Stocks route. Existing export warmup is positive evidence.

## Ranked register — Trading intelligence

### Rank 2 — Include every evaluated session in the return vector used for ranking and verdicts

**Severity:** Critical. **Prior-work classification:** **New.** **Related goal:** correctness.

**Evidence — reproduced:** V1 in [[Codex] Strategy validation and backtesting verdict integrity][src-validation]. Daily resampling drops the opening anchor and therefore the first session's return. For returns −20%, +1%, +2%, −1%, +1%, net return is −17.5922416%, but production Sharpe is +9.4618 instead of −5.7766. The real walk-forward verdict changes from “stopped working” to “still worked.” Adding an opening point on the same date does not fix the resampling omission.

**Recommendation:** Define a canonical evaluated return vector that includes opening capital and the first evaluated session. Use it consistently in Sharpe, Sortino, volatility, PSR, grid ranking and fold evidence. Require compounded returns to reconcile with opening and final equity, including each warmup-to-evaluation reset. Correct the evidence, allowing verdict policy to consume it normally.

**Confidence and limit:** High for the actual statistics/verdict functions; no persisted production study was inspected. Earlier initial/terminal-anchor and fixture recommendations are adjacent, not evidence that first-day resampling is correct. This does not claim that the verdict itself grants trading permission.

### Rank 10 — Keep marked portfolio risk separate from closed-trade parity metrics

**Severity:** High. **Prior-work classification:** **New** counterexample and recommended primary-risk separation; the substitution was already intentional and documented. **Related goals:** correctness, architecture.

**Evidence — reproduced:** V2 in [[Codex] Strategy validation and backtesting verdict integrity][src-validation]. Paired mode discards the marked equity curve for platform statistics. With the same closed trades and final equity, a marked 50% drawdown becomes 1% on the all-in reconstructed closed-trade curve. Headline fields and verdict consumers receive the alternate basis.

**Recommendation:** Retain the common closed-trade series as a separately named parity projection. Keep primary portfolio risk on its marked, cost-aware basis; report a cross-engine risk comparison unavailable if one peer cannot supply comparable evidence. Persist basis/sample identity with metrics. The owner's open decision governs primary verdict presentation; no silent alteration of the frozen score policy is proposed.

**Confidence and limit:** High for substitution and the drawdown difference; the example does not establish a full 17-input grade change. Prior equal-input parity success remains valid. It cannot establish the intratrade risk both sides omit. The documented rationale and its explicit scope are preserved below.

### Rank 14 — Require the observed boundary prices claimed by return studies

**Severity:** High. **Prior-work classification:** **New.** **Related goal:** correctness.

**Evidence — reproduced:** finding 5 in [[Codex] Stocks data integrity, causality, and analytical claims][src-stocks]. A session containing only noon bars passes as a complete daily return sample. The controlled full-session return of 22.222% becomes 1%, without the missing/excluded warning that would distinguish the observation scope.

**Recommendation:** Qualify actual anchor timestamps and session coverage before calling a result open-to-close, close-to-close, or another advertised interval. Exclude, label partial, or apply an explicitly chosen boundary policy when the needed observations are absent. File presence and a valid arithmetic formula are insufficient.

**Confidence and limit:** High for the synthetic partial-session case. Existing golden return arithmetic and wholly missing-session reset tests still pass. They concern different premises; no earlier matching finding was located and no claim is made that those oracles are wrong.

### Rank 15 — Gate CSV comparison grades on unique, valid comparable evidence

**Severity:** High. **Prior-work classification:** **New.** **Related goal:** correctness.

**Evidence — reproduced:** C1 in [[Codex] CSV comparison coverage and validation-grade integrity][src-csv]. Of 1,000 aligned rows, 999 missing-value pairs can be discarded and the remaining pair earns “Excellent” with 100% row coverage. Duplicate keys create 200% matches and negative unmatched counts. Differently named, disjoint time columns can fall back to positional comparison and also earn “Excellent.”

**Recommendation:** Establish explicit time-key mapping and uniqueness, separate row alignment from finite comparable-value coverage, and require a declared sufficiency threshold before grading. Refuse ambiguous positional equivalence unless the user explicitly selects and sees that narrower comparison mode. Report the denominator and excluded observations with every grade.

**Confidence and limit:** High for the service reproductions. This is the reachable advisory Data Lab comparison, not a deployment-admission bypass. Earlier duplicate-input warnings in other backtesters do not identify these comparison/grading mechanisms.

### Rank 16 — State selection scope at the point where PSR is interpreted

**Severity:** Medium. **Prior-work classification:** **Already known** recommendation, with a new point-of-use counterexample. **Related goal:** correctness.

**Evidence — reproduced:** S1 in [[Codex] Selection-adjusted confidence and causal study boundaries][src-selection]. Selecting the winner among 1,000 zero-mean Gaussian 252-observation candidates yields PSR 0.999131, Sharpe 3.21 and 18/20 in the corresponding score component, alongside “Near-certain” wording. The PSR calculation itself is not shown wrong; trial selection is outside its input.

**Recommendation:** Label PSR as conditional on the supplied return sample and unadjusted for selection where it is interpreted. Preserve the frozen policy unless explicitly versioned. A future campaign-level confidence claim must carry trial/search history and genuinely independent evaluation; do not retrofit an invented trial count. Keep exploratory test rows distinct from the training winner's test evidence.

**Confidence and limit:** High for the conditional-confidence mismatch, bounded by the UI's existing heuristic/independent-validation caveats. The prior [Build Alpha charter][prior-build-alpha] already requires multiple-testing disclosure. No new alpha claim, universal overfitting finding, or failure of the tested training/test selection seam is asserted.

## Ranked register — Architecture

### Rank 4 — Bind build admission to the executing process generation

**Severity:** High. **Prior-work classification:** **New.** **Related goals:** correctness, backtest/live equivalence.

**Evidence — reproduced:** B1 in [[Codex] Running executable identity versus on-disk qualification][src-runtime_proof]. A fresh process normally imports a changed decision predicate; the script restores qualified source bytes. Production admission returns `PROVEN`, although the imported predicate rejects a gap that the qualified source accepts. The supported no-reload, source-bind-mounted topology permits loaded code and disk to differ. No forged manifest or patched admission function is needed.

**Recommendation:** Keep the narrow signal-decision closure but attest an immutable imported code generation, including lazy imports, and require a controlled process transition when that generation changes. An admission-time disk read must not masquerade as loaded-code identity. Deployment provenance and executable identity can remain separate.

**Confidence and limit:** High for the false proof and configuration reachability; no deployment or live decision was inspected. Earlier fleet work knew about source mounts, and earlier proof work required running-build identity, but the exact import/disk counterexample was not found. This satisfies ADR 0043's intended claim without introducing image introspection or hashing unrelated files.

### Rank 5 — Bind publication, reader admission and advertised receipts to one data generation

**Severity:** High, merging two findings. **Prior-work classification:** **New** reader/receipt gaps; the rename-before-commit failure state was already documented. **Related goals:** correctness, trading intelligence.

**Evidence — reproduced:** R1/R2 in [[Codex] Published data snapshots versus bytes consumed by a run][src-snapshot]. A successful Python run pins receipt A, a production publication replaces the path with B before consumption, and the result reports A while its chart contains B. LEAN's orchestration can hash after consumption instead. Separately, a publish rename followed by failed catalog completion leaves new bytes with a non-complete row; filesystem admission and a new sweep snapshot accept them. The successful Python case and actual file operations were exercised; the catalog transaction and LEAN consumer were controlled substitutes.

**Recommendation:** Existing exact-byte claims must bind to the actual read, using an immutable generation or a same-buffer read/hash relation. Every reader accepting canonical materialization must enforce committed publication status. Distinguish a cheap availability check from an exact-input receipt. Preserve the tested writer-generation fence and stronger sweep reads.

**Confidence and limit:** High for the demonstrated races. No false passing paired-engine verdict was established. This does not reinstate the deliberately cancelled universal durable-run-manifest requirement; ADR 0049's conditional catalog-reader rationale is addressed below.

### Rank 12 — Make Strategy Spec success depend on admitted data coverage

**Severity:** High. **Prior-work classification:** **New.** **Related goals:** correctness, trading intelligence.

**Evidence — reproduced:** V3 in [[Codex] Strategy validation and backtesting verdict integrity][src-validation]. The real Spec route uses a separate legacy root and reports `success=true`, no error, zero trades and unchanged 100,000 equity for an empty synthetic data tree. Instrument-picker lake coverage does not establish coverage in that other reader.

**Recommendation:** Route Spec execution through the shared materialization boundary, with resolved adjustment/session policy and explicit partial-data admission. Reject empty evaluation windows. Return consumed-window evidence so zero trades is distinguishable from zero observations. This enforces the existing lake authority; it does not impose a new refuse-all-partial policy.

**Confidence and limit:** High for the empty-data success; the frequency of partial legacy-root runs is unknown. Earlier direct-Python transport migration demonstrated a faithful route and readable failures, not that the producer recognizes missing input as failure.

### Rank 17 — Bind asynchronous action feedback to the target that owns it

**Severity:** Medium. **Prior-work classification:** **New.** **Related goal:** correctness.

**Evidence — reproduced:** F1 in [[Codex] Command targeting and receipt identity across fleet boundaries][src-fleet]. An actual-source method harness changes account/bot context while an ordinary action awaits. The correct target receives the command, but its “Bot stopped.” response is then shown under the changed target.

**Recommendation:** Carry captured route/subject identity through completion and either retain feedback with that subject or discard it from a different view. Apply the existing two-phase recovery response ownership principle to ordinary actions. Preserve outbound command capture and backend identity fences.

**Confidence and limit:** High for method behavior, bounded by the absence of a full Angular navigation run. No wrong-account command was demonstrated. Prior fleet work specifically proved outbound target capture and two-phase guards; those conclusions are not contradicted.

### Rank 18 — Retain or replay capture evidence on same-tab reattachment

**Severity:** Medium. **Prior-work classification:** **Already known** mechanism, with new executable consequence. **Related goal:** correctness.

**Evidence — reproduced:** C1 in [[Codex] Instrument coverage selection and capture lifecycle][src-coverage]. After an early capture error, navigating away and returning to the still-open shared EventSource resets the store but subscribes only to future events. A completed panel retains day 3, zero failures and a replay claim, losing earlier rows/errors.

**Recommendation:** Give reattached consumers replayable history or retained state, and state the actual coverage if neither is available. Do not imply “everything replayed” from a future-only subscription. Keep event identity/deduplication stable when replay is supported.

**Confidence and limit:** High for the actual-source service/store harness. No lake mutation or picker-admission bypass was shown. Prior [[A5] Run-session and jobs streams: can a run or job show the wrong terminal state?][prior-2293] explicitly recorded that listeners get no replay; this review adds the lost-failure consequence and Medium assessment, not discovery credit for the mechanism.

### Rank 19 — Make sparse job cancellation checkpoints inspect cancellation

**Severity:** Medium. **Prior-work classification:** **New.** **Related goal:** correctness.

**Evidence — reproduced:** J1 in [[Codex] Asynchronous study execution and durable result identity][src-jobs]. The direct LEAN job path calls a cancellation check once using its default 1,000-call throttle, so even a pre-cancelled controlled run continues and completes. Its Python sibling explicitly checks on the first call.

**Recommendation:** Define cancellation semantics at each execution phase. A one-time checkpoint must read the flag; a noninterruptible phase must be represented as such rather than implying an active cancellation guarantee. Preserve attempt fences and durable job identity.

**Confidence and limit:** High for the actual job runner with a fake LEAN body. This is unwanted computation/trust loss, not a demonstrated numerical defect. The older final-Recency-batch cancellation issue explicitly had polling cadence 1 and a different structural trigger.

### Rank 20 — Carry outcome-unknown saves through to the user

**Severity:** Medium. **Prior-work classification:** **Already known.** **Related goal:** correctness.

**Evidence — reproduced:** J2 in [[Codex] Asynchronous study execution and durable result identity][src-jobs]. The real background-loop wait times out while a controlled insert continues; the response has no execution ID and the UI says “not saved,” although the synthetic insert later completes with a saved ID. Insertion used an in-memory substitute, not a database. The code already names `CallerStoppedWaitingError`.

**Recommendation:** Preserve pending/outcome-unknown separately from confirmed failure, correlate the eventual saved identity, and avoid requiring another computation to discover it. Keep best-effort persistence if that remains the chosen policy; make its outcome honest.

**Confidence and limit:** High for the controlled timeout/late completion. [Parity verdict edge cases after #1969: persist_failed with a landed row, freeze on the writer loop, pending-forever paths][prior-1977] already asks for failed versus outcome-unknown. This is a residual producer/UI gap; it does not refute the narrower late-row and parity-settlement repair in the subsequent bug register.

## Decision challenges and prior-work cross-check

Earlier maps, children and `docs/audits/` were first opened only after all 19 investigation resolutions and the stopping-rule decision. “New” means no matching concrete finding was located in the bounded corpus, not that all historical issues were exhaustively read. Acceptance criteria are not proof of implementation; an older issue's closure is not a blanket safety guarantee. The three categories above distinguish matching recommendations from new mechanisms and actual contradictions.

### Contradictions and rationale, stated narrowly

**Cash guarantee — rank 1.** [ADR 0059's cash-bound rationale][adr-0059] says “an Intraday Margin Deficit is impossible by construction”; its rejected-cap rationale says “The cash bound already bounds every order by settled cash.” It also retains regular-session market DAY orders. The attraction is an account-wide settled-cash restriction without an extra arbitrary cap or margin-trading policy. The reproduced 100-to-101 adverse fill disproves the premise that the estimated reservation bounds execution cost; the observation race independently disproves that a later bundle timestamp proves cash coverage. Preserve the account-wide policy, but choose an enforceable price/cost bound or explicitly weaken the guarantee. The owner question remains open.

**Daily-loss meaning — rank 3.** [ADR 0059][adr-0059] deliberately chooses account-wide session realized P&L “plus Σ broker-observed unrealized P&L over every open position,” keeps EXIT available, and avoids additional per-symbol/per-order controls. Account-wide coverage and continued risk reduction remain sound. Broker unrealized P&L is relative to acquisition, not automatically the chosen session opening mark; it can be positive after a large loss today. The [numeric authority census][prior-numeric] correctly registers the same expression as a presentation aggregate and refutes duplicated FIFO. It does not prove the daily-loss interpretation. This is a semantic challenge to the accepted risk definition, requiring owner judgment.

**Program EXIT expiry — rank 7.** [The extended-hours reference][prior-xh] explains preserving durable shape because later resolution lacks the original decision context, and says: “It is a DAY order, so it dies at the session end rather than resting into a market that has moved.” [Clerk: emergency and operator reduces cannot execute during extended hours (shape them from the current instant)][prior-2007] already documents queueing to the next open and deliberately keeps the decision-driven exception. The earlier recovery repair is not disproved. The new witness sends that program order for the first time after its originating close, when the claimed expiry no longer protects it. Separate first-send eligibility from identity preservation after uncertain submission. The no-silent-repricing rationale remains valid.

**Adjusted-history claim — rank 9.** [The return-distribution reference][prior-returns] explains applying LEAN factor-file price factors, then says splits/dividends “are therefore both removed.” That inference requires a factor file covering the study's action horizon. A nonempty but insufficient file disproves the unconditional product claim while leaving the factor lookup parity result intact. This is not evidence that mode isolation failed or that full ticker history already exists. Map interval and mixed-vintage evidence add separate new premises the materialization contract must carry.

### Deliberate decisions retained, not silently reversed

**Narrow build proof — rank 4.** [ADR 0043][adr-0043] chooses a “loaded-file digest set” to avoid “container/image introspection, or a trustworthy `git` state at runtime” and unrelated source in the proof. Those reasons hold. The new counterexample concerns a later disk read being presented as identity of already imported code. An immutable process generation preserves the narrow closure without adding the rejected host powers or whole-repository digest.

**Lake receipts — rank 5.** [ADR 0049 A3/A5][adr-0049] withdraws universal durable run manifests as “overkill for this platform's needs.” It also knowingly accepts rename-before-commit failure because “A reader trusting the catalog never accepts that file.” This review does not revive the cancelled registry or call that conditional statement false. It identifies existing exact-byte receipts that can describe other bytes and readers that do not consult the condition on which crash safety rests. Correct those existing claims/admissions or narrow their declared meaning. A read-only consumer mount does not freeze a path that the writer can replace.

**Common-input parity — rank 10.** [The paired-statistics validation plan][prior-parity] says: “Both sides grade the common closed-trade ledger through the same platform statistics implementation. This closes the readiness-input mismatch.” The plan immediately disclaims a full portfolio-ledger claim, and the implementation notes LEAN's sparse chart. Equal-input reconciliation is valid and intentional. The new risk counterexample shows why its restricted series should not replace primary marked-risk evidence under unchanged headline metrics. Keep the parity view; the owner's choice concerns the primary risk/verdict basis, not whether the documented comparison succeeded.

### Known recommendations and scope of earlier conclusions

- **Ranks 16, 18 and 20 are already known recommendations/mechanisms.** The Build Alpha charter requires multiple-testing disclosure; the earlier jobs-stream investigation records no replay to listeners; the earlier persistence issue explicitly distinguishes failed from outcome-unknown. This review supplies current, bounded examples of where those distinctions still fail.
- **Rank 13 includes known warmup design guidance.** The earlier chart-history issue addresses a different route/display cap. The new chart/export interval, discarded short-window warmup and four-hour aggregation evidence should not be sold as a rediscovery of indicator history dependence or a failed old fix.
- [Map: single-authority and defect register for the Alpaca bot-control ecosystem](https://github.com/tim1016/learn-ai/issues/1588) excludes numerical/math-port rigor and live-money readiness from its control-plane conclusions. Its authority census remains useful, but does not certify economic definitions.
- [Map — Is the multi-clerk broker fleet trustworthy, and what exactly did we build?](https://github.com/tim1016/learn-ai/issues/2052) assesses the fleet control plane. Its dated “Structural — LOW”/proceed judgment concerns isolation and custody fences. The new economic failures do not refute correct account targeting; nor was that judgment a universal economic certificate.
- [Map — Seam bug hunt: where do module boundaries on the money path break?](https://github.com/tim1016/learn-ai/issues/2276) closes named hypotheses and records charted hazards. “No fog” on its map does not clear every scientific calculation, every lake path, or all future interleavings. Current positive custody/temporal controls and the new findings can both be true.

The cross-check also read the relevant computational-fidelity, math-rigor, timestamp, Data Lab, numeric-authority, execution-path and strategy-execution audits, plus targeted issues on metric provenance, terminal anchors, Spec transport, persistence, lake mode isolation and publication fencing. No previous issue was edited. No unresolved old finding was imported as a newly established defect without current evidence.

## Owner queue, ordered by impact on this register

All five tickets remain **open and unassigned**. Recommendations below are the reviewer's advice, not owner decisions or implementation authorization.

| Priority | Open owner ticket | Recommended answer and effect on the register |
| --- | --- | --- |
| 1 | [[Codex] Owner decision: hard cash guarantee and entry fill trade-off](https://github.com/tim1016/learn-ai/issues/2422) | Preserve a hard guarantee with bounded executable price and all-in reservation; explicitly accept the fill-probability trade-off. Determines rank 1's order policy. The observation-coverage race requires correction regardless. |
| 2 | [[Codex] Owner decision: daily-loss boundary and carried exposure](https://github.com/tim1016/learn-ai/issues/2423) | Measure account equity change from a chosen boundary, with carried positions, costs and cash flows treated explicitly. Determines rank 3's economic definition and operator explanation. |
| 3 | [[Codex] Owner decision: expiry of unsent program reductions](https://github.com/tim1016/learn-ai/issues/2431) | Expire proved-unsent intents into explicit recovery, allowing authorized current-session reduction. Preserve exact status proof for uncertain orders. Determines rank 7's session-risk policy. |
| 4 | [[Codex] Owner decision: corporate-action as-of policy](https://github.com/tim1016/learn-ai/issues/2432) | For descriptive studies use one latest-common revision; retain raw observations plus versioned actions and make historical-as-of analysis a separate policy. Determines rank 9's historical product contract. Mixed or uncovered evidence should not silently claim either policy. |
| 5 | [[Codex] Owner decision: portfolio risk versus engine-parity metrics](https://github.com/tim1016/learn-ai/issues/2424) | Keep marked portfolio risk primary and present closed-trade parity separately. Determines rank 10's headline/verdict ownership; narrow parity evidence remains useful under either explicit presentation choice. |

## Satisfied controls, stopping evidence and limits

The results support retaining the existing durable order identities, custody deduplication, exact cancellation proof, account/provider/generation routing fences, source-bar retention and replay distinctions, causal training-winner/test selection, explicit failed-fold handling, and terminal fill/cost disclosures. Evidence for these controls is in the linked investigations; none is a certification of all schedules, strategies or deployed state.

The final five resolutions, in actual closure order, were [[Codex] Simulated execution causality and live cost assumptions][src-fills], [[Codex] Terminal inventory and backtest result semantics][src-terminal], [[Codex] Retail execution correction protocol and evidence limits][src-corrections], [[Codex] Cross-stack numerical and temporal transport][src-transport], and [[Codex] Instrument coverage selection and capture lifecycle][src-coverage]. Each produced **no new Critical or High** finding. Medium findings in the first and last do not invalidate the specified stopping rule.

Testing used isolated clones, synthetic inputs, recording fake brokers, private temporary SQLite/files, and guarded host execution. Some reproductions deliberately fail a safety assertion; others assert the observed defect as a characterization. Passing control counts overlap across tickets and must not be added as a unique-suite count. The pure TypeScript method/service harnesses are not full Angular/browser runs. .NET was examined statically, not executed. LEAN orchestration witnesses used controlled consumers; no LEAN container or real data ingestion was launched. Snapshot catalog transactions were modeled around actual file operations, not exercised against shared Postgres.

No main-checkout source, running stack, live/paper broker state, environment files, clerk volumes, or live-run artifacts were changed or used for these reproductions. No broker/vendor API was called and no market data was fetched; external lookup was limited to public primary documentation. No ephemeral infrastructure was started. Only isolated reports/reproductions were committed and pushed on the authorized research branch prefix. No fixes or PRs were created. Earlier reports' recorded runtime observations were treated as dated claims, not revalidated against the installation.

The unresolved retail execution-correction questions—event/bust semantics, aggregate ordering, replay/retention and identifier equivalence—remain evidence limits, not an invented vendor guarantee or another required owner decision. They do not justify silently discarding a contradiction already received. No strategy's alpha was sought or established. The fixed IBKR-data/Alpaca-order boundary is unchanged.

## Pinned investigation index

Every report supplies the original Claim, Goal, Severity, source `path:line`, evidence label, reproduction, design recommendation, confidence and limiting evidence. The links pin the research commit; source links inside each report pin the review baseline. Finding identifiers below are local to their named report.

| Closure order | Investigation and pinned evidence | Artifact commit | Findings |
| --- | --- | --- | --- |
| 1 | [[Codex] Validated strategy to live bot semantic equivalence][src-seam] | [26b73da8](https://github.com/tim1016/learn-ai/tree/26b73da84718b39f4b06936534c70dc60676c325) | S1: High |
| 2 | [[Codex] Account engine execution, custody, and recovery][src-accounts] | [b13920d5](https://github.com/tim1016/learn-ai/tree/b13920d5c56452cd4ef29edb78f37c0248ddc867) | A1: Critical; A2/A3: High |
| 3 | [[Codex] Stocks data integrity, causality, and analytical claims][src-stocks] | [37121655](https://github.com/tim1016/learn-ai/tree/3712165551d5fda25d29301e7bba5f381627fc78) | Findings 1–5: High |
| 4 | [[Codex] Strategy validation and backtesting verdict integrity][src-validation] | [5c807334](https://github.com/tim1016/learn-ai/tree/5c807334e7abb9a08c64c8375bb855914294598c) | V1: Critical; V2/V3: High |
| 5 | [[Codex] Running executable identity versus on-disk qualification][src-runtime_proof] | [8f484596](https://github.com/tim1016/learn-ai/tree/8f48459626986eec3343f40f8cb0f40df92a6783) | B1: High |
| 6 | [[Codex] CSV comparison coverage and validation-grade integrity][src-csv] | [83881b57](https://github.com/tim1016/learn-ai/tree/83881b57e09ec256d0b1739281826509b4a92a23) | C1: High |
| 7 | [[Codex] Source-bar ownership and decision clocks through interruptions][src-temporal] | [43f1374d](https://github.com/tim1016/learn-ai/tree/43f1374d8f8a4e2c861c5f692230640c7e78e950) | No new finding |
| 8 | [[Codex] Reducing execution and recovery across partial fills and sessions][src-reduction] | [e2cb89bb](https://github.com/tim1016/learn-ai/tree/e2cb89bb7f0db052fa9cef237d4db6365248c813) | R1: High |
| 9 | [[Codex] Selection-adjusted confidence and causal study boundaries][src-selection] | [4de3ba26](https://github.com/tim1016/learn-ai/tree/4de3ba269e2eeff076765ea4a44a7b4b97b4c0fa) | S1: Medium |
| 10 | [[Codex] Historical instrument, adjustment, and materialization lineage][src-universe] | [cce30c46](https://github.com/tim1016/learn-ai/tree/cce30c463f8e476c9a67b8b83081d3a833b71709) | L1–L3: High |
| 11 | [[Codex] Command targeting and receipt identity across fleet boundaries][src-fleet] | [50129ca5](https://github.com/tim1016/learn-ai/tree/50129ca53068fb7d86af1d98e47fe6e798fdc5ff) | F1: Medium |
| 12 | [[Codex] Economic execution records and correction authority][src-economic] | [43e8f8c6](https://github.com/tim1016/learn-ai/tree/43e8f8c6ed8d7c1e1e793d937505b0d0f5f86f49) | E1: High |
| 13 | [[Codex] Asynchronous study execution and durable result identity][src-jobs] | [3634861d](https://github.com/tim1016/learn-ai/tree/3634861ddb386ef6a3ac9a9658cfca285de78bb6) | J1/J2: Medium |
| 14 | [[Codex] Published data snapshots versus bytes consumed by a run][src-snapshot] | [d93f3538](https://github.com/tim1016/learn-ai/tree/d93f35383ccd479443edccbf877933a90ee9c79e) | R1/R2: High |
| 15 | [[Codex] Simulated execution causality and live cost assumptions][src-fills] | [2f5f0c6a](https://github.com/tim1016/learn-ai/tree/2f5f0c6ac6411160cd1cb11b56950887925cf591) | X1: Medium |
| 16 | [[Codex] Terminal inventory and backtest result semantics][src-terminal] | [6c6f0c54](https://github.com/tim1016/learn-ai/tree/6c6f0c54d2e38547008314ce95a14fefc2fc59d8) | No new finding |
| 17 | [[Codex] Retail execution correction protocol and evidence limits][src-corrections] | [56946714](https://github.com/tim1016/learn-ai/tree/5694671407619d56d46980564ad9108469728e95) | No new finding; vendor evidence limits |
| 18 | [[Codex] Cross-stack numerical and temporal transport][src-transport] | [e893e73a](https://github.com/tim1016/learn-ai/tree/e893e73ae1c0346f4b4c014917c3b04e06ceac6d) | No new material finding |
| 19 | [[Codex] Instrument coverage selection and capture lifecycle][src-coverage] | [1c63fa32](https://github.com/tim1016/learn-ai/tree/1c63fa32d9b22c1c98e8af886316b30d935f12c9) | C1: Medium |

**Disposition:** investigation and synthesis complete. The five owner questions remain queued. Stop here; this report does not commence remediation.

[src-seam]: https://github.com/tim1016/learn-ai/blob/26b73da84718b39f4b06936534c70dc60676c325/docs/references/codex-review-2417.md
[src-accounts]: https://github.com/tim1016/learn-ai/blob/b13920d5c56452cd4ef29edb78f37c0248ddc867/docs/references/codex-review-2415.md
[src-stocks]: https://github.com/tim1016/learn-ai/blob/3712165551d5fda25d29301e7bba5f381627fc78/docs/references/codex-review-2414.md
[src-validation]: https://github.com/tim1016/learn-ai/blob/5c807334e7abb9a08c64c8375bb855914294598c/docs/references/codex-review-2416.md
[src-runtime_proof]: https://github.com/tim1016/learn-ai/blob/8f48459626986eec3343f40f8cb0f40df92a6783/docs/references/codex-review-2418.md
[src-csv]: https://github.com/tim1016/learn-ai/blob/83881b57e09ec256d0b1739281826509b4a92a23/docs/references/codex-review-2425.md
[src-temporal]: https://github.com/tim1016/learn-ai/blob/43f1374d8f8a4e2c861c5f692230640c7e78e950/docs/references/codex-review-2426.md
[src-reduction]: https://github.com/tim1016/learn-ai/blob/e2cb89bb7f0db052fa9cef237d4db6365248c813/docs/references/codex-review-2419.md
[src-selection]: https://github.com/tim1016/learn-ai/blob/4de3ba269e2eeff076765ea4a44a7b4b97b4c0fa/docs/references/codex-review-2421.md
[src-universe]: https://github.com/tim1016/learn-ai/blob/cce30c463f8e476c9a67b8b83081d3a833b71709/docs/references/codex-review-2420.md
[src-fleet]: https://github.com/tim1016/learn-ai/blob/50129ca53068fb7d86af1d98e47fe6e798fdc5ff/docs/references/codex-review-2427.md
[src-economic]: https://github.com/tim1016/learn-ai/blob/43e8f8c6ed8d7c1e1e793d937505b0d0f5f86f49/docs/references/codex-review-2428.md
[src-jobs]: https://github.com/tim1016/learn-ai/blob/3634861ddb386ef6a3ac9a9658cfca285de78bb6/docs/references/codex-review-2430.md
[src-snapshot]: https://github.com/tim1016/learn-ai/blob/d93f35383ccd479443edccbf877933a90ee9c79e/docs/references/codex-review-2429.md
[src-fills]: https://github.com/tim1016/learn-ai/blob/2f5f0c6ac6411160cd1cb11b56950887925cf591/docs/references/codex-review-2433.md
[src-terminal]: https://github.com/tim1016/learn-ai/blob/6c6f0c54d2e38547008314ce95a14fefc2fc59d8/docs/references/codex-review-2437.md
[src-corrections]: https://github.com/tim1016/learn-ai/blob/5694671407619d56d46980564ad9108469728e95/docs/references/codex-review-2434.md
[src-transport]: https://github.com/tim1016/learn-ai/blob/e893e73ae1c0346f4b4c014917c3b04e06ceac6d/docs/references/codex-review-2435.md
[src-coverage]: https://github.com/tim1016/learn-ai/blob/1c63fa32d9b22c1c98e8af886316b30d935f12c9/docs/references/codex-review-2436.md
[prior-numeric]: https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/docs/audits/numeric-authority-census-2026-08-17.md#L30
[prior-strategy]: https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/docs/audits/strategy-execution-research-directions-2026-08-24.md#L16
[prior-build-alpha]: https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/docs/audits/auto-research/build-alpha-functionality-validation.md#L163
[adr-0059]: https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/docs/architecture/adrs/0059-real-money-live-behind-shadow-gate-arming-and-cash-bound-envelope.md#L60
[adr-0043]: https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/docs/architecture/adrs/0043-signal-program-build-proof-and-legacy-seal-migration.md#L89
[adr-0049]: https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/docs/architecture/adrs/0049-data-lake-is-the-market-data-authority.md#L181
[prior-xh]: https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/docs/references/alpaca-extended-hours.md#L81
[prior-returns]: https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/docs/references/return-distribution.md#L43
[prior-parity]: https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/docs/references/reconciliations/engine-lab-runs-75-76-statistics-validation-plan.md#L171
[prior-1611]: https://github.com/tim1016/learn-ai/issues/1611
[prior-1977]: https://github.com/tim1016/learn-ai/issues/1977
[prior-2007]: https://github.com/tim1016/learn-ai/issues/2007
[prior-2293]: https://github.com/tim1016/learn-ai/issues/2293
