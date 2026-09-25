# PR #2495 retrospective: the policy was reasonable; its consequences were discovered too late

Date: 2026-09-25. Supporting research; implementation follow-up described below. The accepted ADRs remain the decision authority.

Reviewed [PR #2495](https://github.com/tim1016/learn-ai/pull/2495), still open and unmerged when checked, from base `930bc9aafdf53ed0eb385b309315c002ff60a4cf` to head `2b36b7e13c2b7cc63beec9d6f4f88172ef1bdf2b`. Conclusions concern that exact revision, not an assumed deployment. The review includes its 16 commits, original issue and owner decisions, nine listed follow-ups, related research, the relevant ADRs, code, and targeted executable checks.

**Follow-up correction:** After the owner authorized implementation, #2495 was merged at `d4c521b2ecf00a9313fe11224173c48fa4387b71` and the follow-up was based on refreshed master. The initial review accepted #2482's issue premise too readily. Tracing the real path shows no transient pre-acceptance producer: `accept_exit` captures custody before reduction capability is tested, and `resolve_accepted_exit` retains it on transient refusal. The real final-bar stale-snapshot regression passes before removal of the unreachable runner handler. No second pending-intent/retry owner is needed. The classification and Spec finding below are corrected accordingly.

## Judgment

Choosing an after-hours limit for an exit that cannot exist until the final bar closes was defensible. The original defect is real, and the PR fixes the reproduced normal case. The avoidable decisions were treating this primarily as order shaping, discovering required configuration and authority choices during review, introducing a broad test bypass, and promising scheduler behavior from a calendar calculation.

The deeper contract should have been: **once a strategy decides to exit, who owns the remaining exposure, which attempts may be made, and what evidence explains its current state until it is flat or needs the operator?** An accepted EXIT operation is only part of that lifecycle.

Several follow-ups are older defects exposed by the investigation. Their presence does not establish that the new policy caused them. Conversely, a pre-existing function can still leave the newly advertised end-to-end outcome incomplete.

## Verified evidence and limits

- In isolated source snapshots, the real-runner last-bar regression **fails on the base** because the submitted exit is MARKET instead of LIMIT. The same test passes on the PR head.
- **96 targeted tests passed on the head:** 34 exit-session/runner cases and 62 paper-allowance/schema-migration cases.
- A direct call to the new projection reproduces #2493: a next-eligible time of 03:59:55 ET becomes `overdue=True` at 03:59:55 itself. At 03:59:54 it is false. The calculation has no scheduler-pass evidence.
- The always-open runner-harness patch is **new in this PR**; that classification comes from the base-to-head diff, not merely the issue description.
- During the initial review, no real broker calls, trading, paper orders, deployment, or changes to application code were performed. PR-reported larger test totals were read but not independently rerun. Actual venue handling of emergency closures and live fill quality were not reproduced.

Sources: [runner regression](https://github.com/tim1016/learn-ai/blob/2b36b7e13c2b7cc63beec9d6f4f88172ef1bdf2b/PythonDataService/tests/services/bot_runner/test_trade_bot_last_bar_exit.py#L129), [exit-session suite](https://github.com/tim1016/learn-ai/blob/2b36b7e13c2b7cc63beec9d6f4f88172ef1bdf2b/PythonDataService/tests/broker/alpaca/clerk/sqlite/test_exit_send_session.py), [paper configuration suite](https://github.com/tim1016/learn-ai/blob/2b36b7e13c2b7cc63beec9d6f4f88172ef1bdf2b/PythonDataService/tests/broker_configuration/test_paper_extended_hours_allowances.py).

## What the follow-ups actually represent

| Issue | Relationship to #2495 | What should have been done differently |
|---|---|---|
| [#2482](https://github.com/tim1016/learn-ai/issues/2482), refusal before acceptance | Claimed production path is not reachable in the current facade; the runner helper test fabricated the exception | Trace the actual producer before adding machinery. Retain a real runner-plus-reconciliation regression without another bar, and remove the dead handler. |
| [#2483](https://github.com/tim1016/learn-ai/issues/2483), shadow limit fills | New policy consequence, explicitly accepted by the owner as a follow-up | State the evidence requirement and divergence classification before coding. Do not restore an impossible fill at the already-completed decision bar. |
| [#2484](https://github.com/tim1016/learn-ai/issues/2484), Dry Run policy owner | Existing authority mismatch exposed as a loophole in the new Start rule, under failed binding | Admission must read the policy of the authority that actually executes the run. Complete this with the admission change. |
| [#2491](https://github.com/tim1016/learn-ai/issues/2491), runner clocks | Existing inconsistent clocks; **new** blanket always-open workaround | Fix clock/lease composition or isolate unrelated tests. Do not neutralize the rule across the shared integration harness. |
| [#2492](https://github.com/tim1016/learn-ai/issues/2492), numeric form precision | Existing live-form bug copied into new paper fields | Fix the touched field's browser constraint as part of adding it. A broader live-form cleanup can remain separate. |
| [#2493](https://github.com/tim1016/learn-ai/issues/2493), overdue/default pricing | Newly introduced same-feature debt | Finish the notice contract and require explicit pricing before calling the new feature complete. |
| [#2494](https://github.com/tim1016/learn-ai/issues/2494), own working EXIT escalates | Older watchdog defect from #2343, more relevant with resting limits | Distinguish waiting on this strategy's own exit from conflicting work before spending an escalation budget. |
| [#2497](https://github.com/tim1016/learn-ai/issues/2497), live closures/halts | Older calendar-only exit assumption left in the centralized send check | Define reduction-specific liveness behavior, including unknown/stale evidence, at the send boundary. A static calendar cannot establish live tradability. |
| [#2487](https://github.com/tim1016/learn-ai/issues/2487), fill-to-cash lag | Belongs to #2441 / PR #2473 | Do not count this as fallout caused by #2495. |

[#2467](https://github.com/tim1016/learn-ai/issues/2467), final-bar backtest modeling, and [#2411](https://github.com/tim1016/learn-ai/issues/2411), position management after run failure, were already separate work. They remain relevant to the meaning of validation and exposure protection, but they are not new regressions proved by this PR.

## Decisions worth keeping

1. **After-hours limit instead of an accidental next-day market order.** Alpaca documents that non-extended orders submitted after 16:00 queue for the next trading day. An extended-hours limit makes the price constraint explicit; it does not guarantee a fill. The decision is consistent with wanting an immediate attempt without silently selecting next-open market execution. [Vendor order rules](https://docs.alpaca.markets/us/docs/orders-at-alpaca), [owner decision](https://github.com/tim1016/learn-ai/issues/2431#issuecomment-5825762834).
2. **One send-time rule for program and recovery exits.** The same position should not get different session treatment depending on which caller drives the operation. Preserving created-order identity and confirmed operator prices is necessary to retain the existing recovery guarantees. [Resolver](https://github.com/tim1016/learn-ai/blob/2b36b7e13c2b7cc63beec9d6f4f88172ef1bdf2b/PythonDataService/app/broker/alpaca/clerk/sqlite/exit_resolution.py#L739).
3. **Explicit allowances, configurable on paper, with holding Resume preserved.** Those are coherent owner choices. The error was discovering the usable configuration path after making it a prerequisite. The final migration has meaningful old-hash and real-v2-fixture checks. [Owner continuation](https://github.com/tim1016/learn-ai/issues/2440#issuecomment-5833171249).
4. **Explicit pricing authority and durable price evidence.** Commit `5b44f98c` fixed the initial duplicate-input, worker-thread quote access, and missing-provenance mistakes. These are review successes, not remaining defects.
5. **IBKR market data and Alpaca execution.** Nothing found here justifies changing that owner-established provider boundary. The needed correction is coherent use of existing authority and evidence.

## ADR audit

Accepted ADRs own decisions; implementation and tests establish runtime behavior. Later explicit owner decisions remain valid even when canonical documentation is incomplete. These findings call for a focused amendment, not a reversal of the chosen policy.

### An incorrect vendor assumption was left in an accepted ADR

**Direct evidence.** ADR 0059's decision drivers, Context, and introduction to Decision 5 characterize a market order outside regular hours as rejected; Context specifies a 422. The supporting extended-hours note, already present at the PR base, states that a market order submitted after 16:00 queues for the next trading day. Current official Alpaca documentation distinguishes the cases: an order not eligible for extended hours queues after 16:00; requesting extended-hours eligibility with an unsupported order type is rejected. These are materially different outcomes. [ADR 0059](https://github.com/tim1016/learn-ai/blob/2b36b7e/docs/architecture/adrs/0059-real-money-live-behind-shadow-gate-arming-and-cash-bound-envelope.md#L31), [base vendor-fact table](https://github.com/tim1016/learn-ai/blob/930bc9aa/docs/references/alpaca-extended-hours.md#vendor-facts-pinned-2026-09-08), [Alpaca order documentation, checked 2026-09-25](https://docs.alpaca.markets/us/docs/orders-at-alpaca#orders-submitted-outside-of-eligible-trading-hours).

**Inference.** A design that assumes broker rejection naturally underestimates the need for local expiry of an accepted-but-unsent market order. The DAY designation does not identify the session in which the strategy made its decision. The PR's send-time rule closes precisely this gap, but the incorrect ADR context remains available to the next implementer.

**Better decision process.** Capture the broker's accepted, queued, working, expired, and rejected outcomes separately for each order shape and submission phase. Pin the first-party claim and an adapter contract test to the distinction. Correct the ADR's factual error without rewriting its historical owner choices. “The broker rejects this” and “the broker accepts this for later execution” must never be interchangeable safety assumptions.


### The canonical execution-policy record did not catch up with its owner decisions

**Direct evidence.** ADR 0059 D5.4 says an unfilled extended EXIT becomes an uncertainty and the program's next decision reissues it; ADR 0045 D4 describes a 120-second age threshold and three attempts. The supporting reference now documents an owner-approved automatic current-quote redrive, session/price deferrals that consume no attempt, the 04:00 recovery window, and send-time repricing of program exits. PR 2495 amends ADR 0060 for the paper configuration pair, but does not amend ADR 0059 or ADR 0045 for these execution semantics. [ADR 0059 D5.4](https://github.com/tim1016/learn-ai/blob/2b36b7e/docs/architecture/adrs/0059-real-money-live-behind-shadow-gate-arming-and-cash-bound-envelope.md#L84), [ADR 0045](https://github.com/tim1016/learn-ai/blob/2b36b7e/docs/architecture/adrs/0045-exposure-lifecycle-closure.md#decision), [reference's current recovery rules](https://github.com/tim1016/learn-ai/blob/2b36b7e/docs/references/alpaca-extended-hours.md#the-regular-close-an-exit-sent-after-the-session-it-was-decided-in-2440).

This is an incomplete canonical contract, not evidence the code lacked owner authorization. “Never silently re-priced” can reasonably coexist with a newly authorized, durably recorded recovery attempt, but the ADR does not define that distinction. The missing link forces each reader to reconstruct the policy from tickets, a growing reference note, and implementation comments.

**Recommended documentation change.** One concise accepted amendment should name program proposal, created order, uncertain/working order, recovery attempt, permitted repricing, price authority, session expiry, and notification/escalation. Cross-reference it from ADRs 0045, 0059, and 0060, and use the reference note for vendor receipts and implementation evidence. This avoids both a second policy authority and a blanket rewrite of historical ADRs.

ADR 0059 also retains headings and introductory sentences claiming mandatory shadow participation despite its explicit 2026-09-09 amendment making shadow optional. The amendment wins under the repo's rules; the stale summary is a navigation problem, not an argument to restore the old requirement. [ADR 0059 amendment and D2](https://github.com/tim1016/learn-ai/blob/2b36b7e/docs/architecture/adrs/0059-real-money-live-behind-shadow-gate-arming-and-cash-bound-envelope.md#L8).


### Preserve immutable orders while allowing an exposure obligation to get a new attempt

**Direct evidence.** ADR 0035 requires capture before broker contact, an append-only authority, idempotent commands, and durable execution ownership. It does not require a stale price to remain the only way to discharge an exposure obligation. At the head, `_leg_to_create` may reprice an uncreated, non-operator-confirmed leg for the current session. `_created_leg_may_be_sent` instead sends a created order exactly or refuses it. The uncertain-submit path retains custody unless absence is conclusive for that exact identity. [ADR 0035 D1–5](https://github.com/tim1016/learn-ai/blob/2b36b7e/docs/architecture/adrs/0035-alpaca-clerk-sqlite-event-sourced-authority.md#decision), [creation decision](https://github.com/tim1016/learn-ai/blob/2b36b7e/PythonDataService/app/broker/alpaca/clerk/sqlite/exit_resolution.py#L739), [created-order decision](https://github.com/tim1016/learn-ai/blob/2b36b7e/PythonDataService/app/broker/alpaca/clerk/sqlite/exit_resolution.py#L849), [uncertain-order proof](https://github.com/tim1016/learn-ai/blob/2b36b7e/PythonDataService/app/broker/alpaca/clerk/sqlite/exit_resolution.py#L1560).

**Assessment.** This is a good boundary, and it should have been stated before the implementation spread across the runner, sweep, watchdog, safe flatten, and restart paths. A strategy's obligation to reduce is longer-lived than one price proposal; a broker order identity is the immutable object. Expiring a proposal is not proving an order canceled. An unknown order is not an unsent order. A confirmed human price is not permission for a machine to invent another one.

The smallest improvement is an explicit lifecycle table in the governing ADR and corresponding real-clock integration cases. Keep the existing SQLite log and EXIT state machine. Do not add a competing position ledger or relax exact-order proof to make retries easier. The PR's replacement-price provenance and shared `RecoveryPricing` object are useful improvements to preserve. [RecoveryPricing](https://github.com/tim1016/learn-ai/blob/2b36b7e/PythonDataService/app/broker/alpaca/clerk/recovery_reduction.py#L502), [recorded provenance and authority isolation](https://github.com/tim1016/learn-ai/blob/2b36b7e/docs/references/alpaca-extended-hours.md#L94).


### A shared signal contract does not prove shared execution results

ADR 0042 promises one mathematical decision contract across Backtest, Dry Run, and Paper, with separate execution authorities. ADR 0059 explicitly says shadow proves plumbing, not execution quality, and requires future bars for `limit_touch`. The code implements distinct models: Dry Run immediately fills a marketable limit at the decision close, while shadow waits for later retained bars. Those distinctions are deliberate and should remain explicit. [ADR 0042 consequences](https://github.com/tim1016/learn-ai/blob/2b36b7e/docs/architecture/adrs/0042-sealed-signal-and-account-scoped-custody-authorities.md#consequences), [ADR 0059 D2/D5](https://github.com/tim1016/learn-ai/blob/2b36b7e/docs/architecture/adrs/0059-real-money-live-behind-shadow-gate-arming-and-cash-bound-envelope.md#L50), [fill models](https://github.com/tim1016/learn-ai/blob/2b36b7e/PythonDataService/app/broker/alpaca/clerk/fill_models.py#L1).

An RTH strategy with an after-hours exit now needs post-close execution evidence even though it makes no post-close strategy decisions. Shadow settlement depends on its instance's retained bars and can permanently cancel a would-be fill after an evidence-retention stall. #2483 explicitly records this limitation as accepted for the PR with a follow-up, while #2467 asks how final-close backtest fills should change. [ShadowOrderBook contract](https://github.com/tim1016/learn-ai/blob/2b36b7e/PythonDataService/app/broker/alpaca/clerk/shadow_broker.py#L1), [#2483](https://github.com/tim1016/learn-ai/issues/2483), [#2467](https://github.com/tim1016/learn-ai/issues/2467).

**Better validation boundary.** Qualify mathematical decisions separately from the selected live execution policy. Record the applicable fill model, order eligibility window, required evidence window, and missing-evidence outcome when comparing runs. A divergence caused by absent post-close shadow evidence should be reported as such, not as a signal bug or a proven live non-fill. Changing the strategy clock or making shadow fill against the same decision bar would hide this gap rather than resolve it.

Making shadow mandatory would not by itself have caught the old defect: the old regular market shape received the optimistic immediate-close fill. The missing evidence was a realistic execution contract and broker-clock scenario, not merely a required count of shadow sessions.


### Calendar permission and live trading status answer different questions

ADR 0022 makes the calendar authoritative for scheduled structure and live evidence authoritative for operational exceptions. ADR 0067 strengthens entry readiness but explicitly leaves reduction policy unchanged; it also preserves live quote access for reductions during admission failures. Therefore reusing the entire ENTER gate on EXIT would be an unjustified policy change. Conversely, a scheduled RTH result cannot prove an emergency closure or a halt absent. [ADR 0022](https://github.com/tim1016/learn-ai/blob/2b36b7e/docs/architecture/adrs/0022-temporal-authority-calendar-and-timestamp.md#decision), [ADR 0067](https://github.com/tim1016/learn-ai/blob/2b36b7e/docs/architecture/adrs/0067-market-data-readiness-and-subscription-ownership.md#L26).

At the reviewed head, `reducing_leg_verdict` answers from the scheduled session and validity bound; its inputs contain no symbol halt or live clock. The PR records follow-up #2497 for send-time live closures and halts as pre-existing. The lesson is to define which live conditions forbid broker submission while preserving exposure recovery during ordinary stale-data or configuration failures. Retain the separate scheduled and live facts and combine them at the actual execution boundary. [Send verdict](https://github.com/tim1016/learn-ai/blob/2b36b7e/PythonDataService/app/broker/alpaca/clerk/recovery_reduction.py#L647), [PR follow-up record](https://github.com/tim1016/learn-ai/pull/2495).

The chosen IBKR market-data / Alpaca execution boundary is not implicated as a mistaken provider choice. ADR 0062 explicitly retains it and rejects a paid Alpaca data subscription. Work should repair and qualify the existing evidence path, not silently change providers. [ADR 0062 provider decision](https://github.com/tim1016/learn-ai/blob/2b36b7e/docs/architecture/adrs/0062-broker-clerk-fleet-control-plane.md#retained-market-data-provider--owner-decision-2026-09-16).


## The most avoidable implementation choices

### 1. A scheduler promise was inferred from session eligibility

The owner requested a next-attempt explanation. The implementation introduced an overdue verdict as well. `next_redrive_at_ms` can name when session policy allows a try, but cannot know whether a usable quote will exist or the next sweep has run. The projection turns that eligibility directly into a missed-promise signal.

This explains both the earlier “overdue while an exit is working” correction and the remaining normal-sweep-gap defect. Adding one more grace constant can address the immediate flash; the stronger model is a recovery status with **eligible from, working order, last actual attempt/result, deferral reason, and escalation state**. Keep that status backend-owned. An overdue claim should require evidence of a missed operational deadline.

The five-second send guard also deserves more precise language. It is a chosen safety margin, not measured proof of exact arrival at Alpaca. The PR deliberately permits sends just before opening; describing 03:59:55 as guaranteed arrival at 04:00 overstates what the code establishes. A better contract distinguishes earliest eligible submission from the latest safe submission time and states any accepted pre-open queue explicitly. This is a design limitation, not a reproduced new overnight queue.

Sources: [eligibility calculation](https://github.com/tim1016/learn-ai/blob/2b36b7e13c2b7cc63beec9d6f4f88172ef1bdf2b/PythonDataService/app/broker/alpaca/clerk/recovery_reduction.py#L681), [projection](https://github.com/tim1016/learn-ai/blob/2b36b7e13c2b7cc63beec9d6f4f88172ef1bdf2b/PythonDataService/app/broker/alpaca/clerk/sqlite/projections.py#L201), [guard rationale](https://github.com/tim1016/learn-ai/blob/2b36b7e13c2b7cc63beec9d6f4f88172ef1bdf2b/PythonDataService/app/broker/alpaca/clerk/recovery_reduction.py#L91).

### 2. The shared test harness bypassed the changed behavior

The added `regular_session_open = True` patch makes broad runner suites easier to run against historical bars, but prevents those suites from discovering many session-policy errors. The dedicated last-bar test correctly removes that patch and supplies a coherent Clerk clock; it does not make every other caller exercise the real rule.

The appropriate dependency was a shared replay clock that works with the execution lease. This is a concrete case where changing the test environment to make tests pass weakens what the green result means. It does not invalidate the 34 focused cases I ran.

Source: [new harness patch](https://github.com/tim1016/learn-ai/blob/2b36b7e13c2b7cc63beec9d6f4f88172ef1bdf2b/PythonDataService/tests/_helpers/bot_runner/market.py#L88).

### 3. Required policy was checked before its owner was consistently selected

A regular-hours strategy now needs an exit policy that can act outside its decision session. “Regular hours” describes when it decides, not necessarily when its exposure may be reduced. Admission, program shaping, automatic recovery, synthetic execution, and the notice reader must derive their policy from the same exact run authority.

The final pricing object moves in the right direction. The remaining Dry Run admission mismatch and optional projection-pricing defaults show that the boundary was not completed across consumers. Adding another fallback would deepen the ambiguity.

Sources: [#2484](https://github.com/tim1016/learn-ai/issues/2484), [explicit pricing seam](https://github.com/tim1016/learn-ai/blob/2b36b7e13c2b7cc63beec9d6f4f88172ef1bdf2b/PythonDataService/app/broker/alpaca/clerk/recovery_reduction.py#L535).

### 4. Too many dependency discoveries were absorbed into one review unit

The PR contains 117 files and 7,989 additions; 5,003 additions are in test files. Size alone therefore does not prove needless production complexity. There is, however, a concrete review cost: CodeRabbit skipped review because the PR exceeded its 100-file limit. The description also records multiple block/fix rounds after configuration, scheduler, and operator-copy decisions expanded the implementation.

Paper settings, migration, admission, order recovery, and notices are legitimate connected work. They should have been planned as dependent review slices, with an integration gate for the complete user outcome. Splitting by layer alone would still leave broken intermediate behavior.

The two rollback caveats increase the importance of this sequencing: the configuration database becomes v3, and old reducing-order readers reject the newly written facts. Preserving old hashes and upgrading transactionally are good work; they do not make rollback automatically compatible. Reader compatibility or an explicit version/cutover protocol should be considered before enabling new writes, without discarding safety-relevant fields.

Sources: [review skipped](https://github.com/tim1016/learn-ai/pull/2495#issuecomment-5836058643), [PR review and deploy notes](https://github.com/tim1016/learn-ai/pull/2495).

## A better plan before implementation

First, record the chosen behavior for these independently meaningful states:

| Dimension | Cases that expose the important boundaries |
|---|---|
| Signal availability and send time | Final bar closes; decision arrives late; cancellation delays send; restart crosses session |
| Ownership | Decision refused before acceptance; accepted operation; order created; submission outcome unknown; broker absence proved |
| Market evidence | Regular session; after-hours; closed; early close; known halt; stale/unknown status |
| Run authority/configuration | Paper, live, shadow, synthetic; binding healthy/failed; allowance present/missing; flat/holding |
| Remaining exposure | Working limit; partial fill; rejected/expired; fresh attempt deferred; escalation; flat |
| Operator explanation | Eligible later; waiting for evidence; working; last attempt failed; stopped automatically |

Then deliver:

1. **Contract and prerequisites:** amend the governing execution ADR, establish the shared authority/clock seams, and make the required paper configuration usable. Review migration and reader compatibility independently.
2. **Complete reduction lifecycle:** one send rule plus immutable order evidence, with final-bar refusal, delayed acceptance, restart, partial fill, expiry, and operator-notice scenarios. Keep an emergency fix small if necessary, but label exactly which invariant it establishes.
3. **Recovery presentation:** project real recovery state. The initial notice can say the position remains open and becomes eligible at the next session; a speculative overdue verdict need not be part of the safety fix.
4. **Qualification:** record the shadow/live and backtest/live execution differences. A signal-program validation result alone does not prove a fill model that changes from the close to a later limit.

The initial recommended release boundary was to finish the newly introduced #2493 notice/default-pricing work, correct the touched #2492 inputs, and close the #2484 admission loophole. The follow-up verifies #2482's end-to-end guarantee with actual stale-snapshot evidence, closes the obsolete premise and removes the dead runner handler. #2491 can be separately reviewed, but should be an explicit testing limitation until repaired. The authorized #2483 deferral can stand with a named qualification limitation; #2494 and #2497 deserve explicit tracked disposition rather than being lost under “pre-existing.”

This is a recommended delivery boundary, not a request to change the owner's after-hours or 04:00 recovery decisions.

## Standards

Independent standards axis, lightly edited; all source line numbers refer to the pinned PR head. Design smells are judgments, not hard rule violations.

1. **New test blind spot — documented testing-standard concern.** `PythonDataService/tests/_helpers/bot_runner/market.py:88–95` newly forces `regular_session_open = lambda _: True` across its consumers. This bypasses the actual send-time policy the PR changes. The focused regression explicitly undoes it at `tests/services/bot_runner/test_trade_bot_last_bar_exit.py:133–140`; the wider runner suites remain blind (#2491). `.claude/rules/testing.md` requires testing observable behavior and selecting tests by shared-helper consumers. Better: one replay clock shared by feed, Clerk, and lease renewal, with policy left real. The historical-clock mismatch was inherited; this workaround is introduced by #2495.

2. **Incomplete authority boundary — documented interface breach plus design judgment.** `app/broker/alpaca/clerk/sqlite/projections.py:308,382` silently defaults pricing to `UNPRICEABLE_RECOVERY`, despite `recovery_reduction.py:563–565` explicitly requiring pricing at every signature. Current uncertainty readers pass it, so this is a latent wrong-time projection risk, not demonstrated current mispricing (#2493). More broadly, Start/Resume still obtain an argumentless lane policy (`services/bot_start_admission.py:571`, `bot_resume_admission.py:319`) rather than the exact Dry Run authority’s policy (#2484). ADR 0042’s exact-identity authority rule supplies the better seam: resolve the run authority once and derive admission, execution, recovery, and projection from it.

3. **Eligibility represented as a promise — possible Primitive Obsession / Feature Envy.** `sqlite/projections.py:201–206` turns a calendar-eligible instant directly into an overdue attempt; `project_uncertainties:134–155` reconstructs stop/working status from separate episode/effect sets. The actual watchdog has its own deferral/escalation decisions (`exit_watchdog.py:387–395,458–499`). Consequently ordinary sweep delay becomes “overdue” (#2493), while inherited same-strategy work deferral can produce contradictory stopped/working advice (#2494). Better: expose one typed recovery status containing eligibility, working order, actual attempt and deferral reason; render that status instead of reconstructing a promise.

**Do not count repaired review findings as remaining defects.** `5b44f98c` correctly removed duplicate pricing inputs, moved quote acquisition to the event loop, and recorded send-time quote provenance. These were real initial mistakes, but the final tree fixes them. The extensive final send-session tests are valuable; test quantity alone does not repair the shared clock bypass.

## Spec

Independent specification axis, including later owner decisions.

1. **Corrected disposition: pre-acceptance refusal (#2482) is not a demonstrated gap.** The initial review inferred reachability from `bot_trade_strategy.py:1128–1162` and a helper test that manufactured an exception. The actual EXIT capture has identity, ownership and active-run checks; none produces a transient `AdmissionBlockedError`. Reduction capability is tested after acceptance, and `resolve_accepted_exit` returns durable custody to the sweep on a transient refusal. The follow-up adds the real final-bar test and deletes the unused runner catch. This is an evidence/cleanup issue, not justification for another retry system.
2. **New Start rule checks the wrong authority in a degraded lane (#2484).** An absent main Clerk yields regular-only policy (`active_authority.py:498–503`), while synthetic execution gets its own extended-capable policy (`:401`). The existing mismatch becomes a loophole in the newly required all-mode admission rule. Failed binding and loud eventual refusal reduce urgency, not the inconsistency.
3. **Next-attempt claim exceeds evidence (#2493).** The PR says overdue means the watchdog could have acted and has not. The implementation knows session eligibility (`recovery_reduction.py:681–707`), not the actual sweep opportunity or quote availability, but projects overdue immediately (`projections.py:201–206`). This is new same-feature debt and should be completed with the notice. The optional pricing defaults likewise contradict the explicit-input contract, though no current uncertainty reader relies on them.

The shadow follow-up (#2483) was expressly owner-accepted. Paper allowance fields, holding Resume, and 04:00 recovery were authorized dependencies, not scope creep.

Standards: 3 findings; strongest concrete concern is the new shared test bypass. Spec: 2 remaining concerns at the reviewed head, with #2482 reclassified after tracing the real producer. The axes overlap and must not be added as distinct defect totals.

## Implementation disposition

The authorized follow-up addresses #2484, #2492 and #2493 and closes #2482 with a production-path invariant test and dead-code removal. ADR 0059 now distinguishes queued market orders from invalid extended-hours requests and names the decision/send boundary. ADR 0045 now names the Clerk-owned transient retry, current-quote recovery and eligibility semantics. Start/Resume use the chosen authority; failed binding copy remains specific; holding Resume remains permitted. Fractional configuration fields accept backend-valid precision. Projection readers require explicit recovery pricing and a retry is not shown waiting until a sweep interval plus the send guard has elapsed from the current window's eligibility.

#2491 (shared runner clocks), #2483 (shadow execution evidence), #2494 (working-EXIT escalation) and #2497 (live closures/halts) remain separate. The focused final-bar tests use the real session rule, but this follow-up does not remove the shared harness's always-open override or claim that broader gap is solved. It changes no broker subscriptions and performs no deployment or trading.
