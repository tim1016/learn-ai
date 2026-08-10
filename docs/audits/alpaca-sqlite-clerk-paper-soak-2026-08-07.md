# Alpaca SQLite Clerk supervised paper qualification — 2026-08-07

**Status:** PARTIAL QUALIFICATION / RECOVERED AND IDLE — Qualification A
produced one valid paper round trip, then the restart/UI drill hit the
corruption abort recorded in
[#1413](https://github.com/tim1016/learn-ai/issues/1413). The operator-approved
mirror rebuild subsequently restored the current finalized head without
changing generation or database identity, and a supervised three-bar run plus
normal stop/reconciliation/restart completed flat and order-free. Earlier
defects are tracked by [#1410](https://github.com/tim1016/learn-ai/issues/1410),
[#1411](https://github.com/tim1016/learn-ai/issues/1411), and
[#1412](https://github.com/tim1016/learn-ai/issues/1412). This remains partial
evidence, not #1409 closure evidence.

**Scope:** Alpaca paper account `PA3KWXU1C4C3`, SQLite authority generation 1.
Live-money execution remained disabled. This report carries the remaining
qualification and governance work from issue
[#1409](https://github.com/tim1016/learn-ai/issues/1409).

## Closure rule

Issue #1409 and ADR 0035 may close only after every required live scenario below
has passed across multiple operator-supervised market sessions, the final broker
state is freshly proven flat and order-free, and the report records any abort or
incident (including `none`). Deterministic tests and the first canary's safe
failure are supporting evidence; they do not substitute for the live soak.

## Evidence already complete

- Phase 1 cutover and initial flat/order-free proof:
  `docs/audits/alpaca-sqlite-clerk-phase-1-cutover-2026-08-06.md`.
- First canary and feed-stall incident:
  `docs/audits/alpaca-sqlite-closure-plan-and-session-2026-08-07.md`.
- Adversarial, 1M-row, and latency qualification:
  `docs/audits/alpaca-sqlite-clerk-qualification-full.{json,md}`.
- Backup, non-production restore, and mirror-rebuild rehearsal: recorded in the
  first-canary audit above.
- Invariant traceability:
  `docs/references/alpaca-sqlite-clerk-invariant-traceability.md`.
- Trader/Operator truth language and recovery actions:
  `docs/references/alpaca-sqlite-clerk-recovery-language.md`.
- Recovery and cutover runbook:
  `docs/runbooks/alpaca-sqlite-clerk-recovery-and-cutover.md`.

The dedicated truth-language deliverable already exists on merged `master`; the
unchecked #1409 checklist item saying that no dedicated document was found is
stale.

## Adversarial-review disposition

An independent Claude Opus review returned **REQUEST CHANGES** and correctly rejected
closure of #1409 and #1410–#1413. The accepted P1/P2 findings have been remediated in
the local working tree:

- feed liveness, admission, Clerk submission gates, market pulse, and panel channel
  health are symbol-scoped; a two-symbol regression proves that an advancing sibling
  cannot make a never-advancing symbol healthy;
- the decision journal now records the strategy's `enter_intent` / `exit_intent`
  before Clerk execution, with bar-ref idempotency. It no longer translates pending,
  uncertain, or unprovable custody states into confirmed `entered` / `exited` signal
  claims, and a journal failure prevents the broker mutation;
- an exhaustive repository/projection parity test covers every effect-operation state,
  ENTER/EXIT kind, and absent/new/filled order combination. The projection predicate
  is fail-closed for newly introduced states;
- restore, mirror rebuild, and reset enforce the WAL filesystem guard before a
  read-write connection. An unreadable lease requires fresh independent,
  account-bound process-stop proof instead of being treated as no lease;
- the evidence-station regression explicitly asserts that opening raw evidence does
  not dispatch a lifecycle action; and
- the epsilon predicate, duplicated subscription-stall block, `.cutover/` ignore rule,
  filesystem denylist, and suite-order-dependent test import were hardened.

At capture time, this disposition was code/test evidence only. It had not been
committed, reviewed in a PR, passed CI, or exercised in a new supervised market
session. The remediation was subsequently published as commit `f3436c38` in PR
[#1414](https://github.com/tim1016/learn-ai/pull/1414), where automated review is in
progress and CI remains a required merge gate. The exact historical #1413
evidence-click-to-Resume sequence remains unreproduced, so the regression narrows
the risk but does not close that incident.

## Session register

| Session | Market session | Operator present | Result | Evidence |
| --- | --- | --- | --- | --- |
| Phase 1 | 2026-08-06 | yes | PASS — generation 1 activated; safe START/STOP canary ended flat | Phase 1 cutover audit |
| Initial canary | 2026-08-07 | yes | SAFE FAILURE — one 5-second IBKR print, no closed minute, no decision/order; operator stopped flat | First-canary/feed-stall audit |
| Qualification A | 2026-08-07 | yes | ABORT — Resume-control defect fixed locally, then both IBKR consumers repeated the one-print stall while Broker V2 still presented Live/Healthy; stopped and reconciled flat | This report; #1410; #1411 |
| Qualification A, attempt 2 | 2026-08-07 | yes | PASS for one normal ENTER/EXIT round trip; ABORT for the subsequent truth/restart drill | This report; #1412; #1413 |
| Recovery and Qualification A, attempt 3 | 2026-08-07 | yes | PASS — current mirror rebuilt the corrupt authority, three advancing closed bars produced durable no-action decisions, Stop/Reconcile/restart remained flat and idle | This report; #1413 |
| Qualification B | Later market session | pending | PENDING | This report |
| Qualification C | Later market session | pending | PENDING | This report |

## Qualification A preflight

Read-only inspection at approximately 2026-08-07 11:34 CDT observed:

- authority generation: `1`;
- database identity: `03ed49bd38bb1f3a6462f81706e7dec2`;
- control revision: `297`;
- authority health: `healthy`;
- positions: none;
- working orders: none in the operator panel;
- active holds / uncertainties: `0` / `0`;
- registered bots: `3`, all `OFF_DUTY / STOPPED / STOPPED_FLAT`;
- qualification bot: `sqlite-market-qual-0807`, one-share `SPY`, paper,
  `Resume ready`;
- Alpaca market-data and execution channels: healthy;
- IBKR paper data-plane session: connected;
- fault-injection seam: paper-only permission active, no fault armed;
- initial reconciliation receipt: `reconciliation:297`.

This is readiness evidence, not start authorization. Before resuming, the
operator must capture a fresh reconciliation and broker position/open-order
proof, then prove the feed is advancing. After Resume, the strategy may enter
only after two consecutive green closed minute bars. The panel showed `Last
bar: None` during preflight.

The operator ran `reconciliation:298` at `1786120676084` ms UTC and again
observed a clean, flat, order-free account. The first service restart correctly
failed closed while the previous process's 30-second execution lease was still
live. That lease expired at `1786121043010` ms UTC; the retry acquired the lease
at `1786121058693` ms UTC with generation `1` and DB identity
`03ed49bd38bb1f3a6462f81706e7dec2` unchanged. No second writer was installed
during the wait.

The restarted panel then exposed a separate UI defect: the health projection
said `Flat Resume ready`, but the SQLite adapter had replaced the full action
catalog and omitted Resume. Qualification paused before any broker effect.
Issue [#1410](https://github.com/tim1016/learn-ai/issues/1410) records the defect.
A local regression-first fix preserved stopped-bot Resume as a lifecycle action
while retaining SQLite recovery actions; all 188 Broker V2 panel tests and the
project-wide Python lint check passed. Because that fix was not yet merged, the
resulting live attempt is discovery evidence, not closure evidence.

The repeated live stall promoted H1/H3 from deferred characterization to
release blockers. The local #1411 fix generation-safely invalidates an
expected-session subscription after 60 seconds without distinct timestamp
advancement, transparently resubscribes the decision feed, marks an active feed
without its first closed minute stale immediately, and uses distinct raw
5-second timestamps as a 30-second liveness watermark between closed minutes.
Its 55 focused IBKR/feed/admission tests pass. A broader 567-test run passed
561; six unrelated order-router cases were intercepted by the configured
data-plane control-secret guard (403 before their expected handler response).
Live requalification is still required before #1411 can close.

## Session B/C re-tier (b) acceptance amendment — pre-session

**Status:** OPERATOR APPROVED by **Inkant Awasthi** at `1786370622000` ms UTC on
2026-08-10. This is the one-time policy sign-off for the amendment itself; it
does not authorize a lifecycle action, fault arming, or broker order, and it
does not stand in for in-session supervision. Before Scenario 0 starts, the
first Scenario receipt filed for Session B (and again, independently, for
Session C), using the Scenario receipt template below, must record — in its
"Supervising operator / sign-off" field — the operator who actually
supervises that live session and the contemporaneous sign-off time, which may
differ from the amendment approver and time recorded above.

The campaign adopts the following re-tier (b) rule:

- **Must be live; no synthetic substitute:** a real Alpaca-paper fill at a
  real price; live IBKR feed integration; advancing real SSE evidence; fresh
  Alpaca REST reconciliation following real fills; and evidence from at least
  two distinct NYSE market sessions.
- **Deterministic proof plus one live confirmation:** each seam-induced row
  rehearsed under #1415 (partial/duplicate evidence, lost-submit, cancel/fill
  race, lost-cancel, trade-update gap, uncertainty isolation/default,
  restart-with-work, and evidence-station safety) requires its retained
  rehearsal evidence and one supervised in-session live confirmation. A
  rehearsal may be labelled only "rehearsed — pending one live confirmation",
  or "rehearsed with limitations — pending one live confirmation" when a
  documented limitation applies (for example the feed/UI rows'
  `CLERK_OBSERVATION_CLOCK_NOT_APPLICABLE`, as already used in the Pre-live
  rehearsal table below); no other phrasing is permitted, and neither variant
  ever checks a required-live box.
- **Session order:** For Qualification B and Qualification C, Scenario 0 is
  the live feed gate and runs before every custody row in that session; the
  historical Session A rows in the Required live scenarios table below
  (Scenario 0 `ABORT` alongside custody `PASS` rows) predate this amendment
  and do not satisfy the gate. Qualification B and Qualification C must each
  independently satisfy every still-`PENDING` row in that table with their
  own live confirmation and Scenario receipt — no row is satisfied by
  reusing or referencing the other session's evidence. Qualification B and
  Qualification C are separate NYSE sessions; no retry overwrites a failed
  attempt or collapses the two-session evidence requirement.

Operational-authority expansion and live-money trading remain frozen throughout
this campaign.

### Abort taxonomy

- **Feed-abort:** a live IBKR stream does not produce or advance a required
  bar, becomes stale, or cannot recover through the bounded resubscribe path
  during Scenario 0. Stop the attempt before a custody scenario, preserve
  feed/farm/request/bar/error evidence, capture fresh flat and order-free
  broker proof, and re-attempt only after the feed gate is re-established. A
  feed-abort is neither a custody PASS nor a custody defect when no Clerk or
  broker mutation occurred.
- **Custody-abort:** any mutation without a finalized intent, duplicate
  economic effect, mixed writer, fabricated terminal state, unresolved drift,
  stale evidence authorizing exposure, substituted authority, UI-to-action
  mismatch, or recovery identity/hash failure. Stop all governed bots,
  preserve the evidence, open a focused defect, and require its merge and
  deployment before another qualifying session.
- **Unclassified condition:** fail closed as a custody-abort until the operator
  and reviewer classify it from durable evidence. Infrastructure ambiguity
  never upgrades a custody row or clears a live gate.

## Required live scenarios

Every row must link the operation-first timeline and record the authority
generation, control revision, command/effect/order/receipt identities, source /
observation / durable-record clocks, broker position/open-order proof, expected
versus observed state, operator action, and warning/error-log reference.

| Scenario | Session | Status | Durable evidence / observation |
| --- | --- | --- | --- |
| **Scenario 0** — read-only IBKR feed reproduction with farm/session, request, bar-count, disconnect, and error callbacks; the live feed gate that must pass before any custody row runs | A | ABORT | Farms `usfarm`, `ushmds`, and `secdefil` reported healthy. The chart consumer attached and received one raw 5-second print at about `16:44:31Z`, then warned at `16:45:01Z` and `16:46:01Z`. The decision consumer attached at `16:45:06.724Z`, received one print at `16:45:10.705Z`, then warned at `16:45:36.821Z` and `16:46:36.909Z`. No disconnect callback explained the stall. #1411. |
| Normal process restart and execution-lease failover | A | PASS after recovery; accepted/unknown restart still pending | The first failover refused the live prior lease and acquired it after expiry. The later corrupt authority was rebuilt from the finalized mirror through sequence 323, then normal stopped-state restarts completed healthy and remained idle. #1413. |
| Baseline Resume with two-green closed-minute entry gate | A | ABORT | START succeeded as lifecycle run `558d6e5f2dda4772a96a79f32c8ce851`; no durable last bar, decision, effect operation, order, or fill followed because the decision consumer stalled after one print. |
| ENTER acknowledgement and terminal fill | A | PASS (baseline only) | ENTER `effect:sqlite-market-qual-0807:encoded-MTc4NjEyMjYwMDAwMDpFTlRFUg`; broker order `c09fb7ed-dd7b-4d49-b892-5f62c0130a3a`; BUY 1 SPY filled at about 772.30; source event `1786122606676`, durable record `1786122606786`. |
| Partial fill plus duplicate/redelivered broker evidence | A | PENDING | — |
| Lost submit response resolved by exact client identity | A | PENDING | — |
| Cancel-first EXIT with a fill racing cancellation | A | PENDING | — |
| Lost cancel response retains Clerk custody | A | PENDING | — |
| Trade-update disconnect/gap; REST reconciliation completes before admission reopens | A | PENDING | — |
| BOT uncertainty isolates only the affected bot | A | PENDING | — |
| Unknown/unclassified uncertainty becomes `ACCOUNT_CLERK` and fails closed | A | PENDING | — |
| Stop decision, prepare-safe-flatten evaluation, reconciliation, and operation-first timeline | A | PASS for baseline round trip; later UI truth ABORT | EXIT `effect:sqlite-market-qual-0807:encoded-MTc4NjEyMjc4MDAwMDpFWElU` filled SELL 1 and reached `EXIT_ATTRIBUTED_FLAT`; STOP sequence 320 and `reconciliation:321` left zero exposure/orders. The panel then misstated stop/evidence/intent health (#1412). |
| Service restart with accepted/unknown work | A | PENDING | — |
| Normal ENTER/EXIT/restart path repeated in a later market session | B | PENDING | — |
| Unchanged and advancing SSE revisions keep selection, lens, and history stable | A/B | PARTIAL PASS after recovery | During the original post-restart Operator drill, the evidence-station interaction coincided with an unintended durable Resume action and the projection later failed on DB corruption. After recovery, opening Broker ack and its raw evidence loaded ten custody events without a lifecycle action; 25 focused Angular tests passed. Multi-session selection/history stability remains pending. #1413. |
| Verified online backup after soak; identity/hash verification | B | PENDING | — |

The non-production restore and mirror-rebuild rehearsal is already complete. It
must be referenced in the final verdict but need not mutate production again.

## Pre-live rehearsal (synthetic)

> **Synthetic evidence only.** No live scenario was run or checked off. ADR 0035 remains **Proposed**. Every successful row is rehearsed — pending one live confirmation.

- Report SHA-256: `13f76d01d29de4ef96876f76fbf3ffec839561c6df88a81d738574b53228aa5a`
- File SHA-256: `a2801ef745f1c3fd13b2c6b6dfbab259fed0818b43b2ea66154a31503341e6fd`
- #1409 field-set worksheet: `docs/audits/alpaca-sqlite-clerk-synthetic-rehearsal-2026-08-07.json` (all 13 canonical rows preserve authority identity/revision, aggregate command/effect/order/broker identities, receipts, expected/observed states, operator action, before/after broker proof, logs, and clock provenance; every durable custody/recovery row has all three ordered clocks, while feed/UI rows explicitly mark Clerk observation not applicable)
- Overall: `REHEARSED_WITH_LIMITATIONS_PENDING_LIVE_CONFIRMATION`; abort classification: `NONE`
- Protected authority: `QUAL-1415-1786148415658` / generation `1` / DB identity `3cbba4bc41fba120c58194519068a854`
- Storage implementation acceptance: #1415 deliberately uses a separate VM-local XFS qualification named volume instead of a writable subpath in `alpaca-clerk-data`; production `alpaca-clerk-data` is mounted read-only. This stricter boundary supersedes the same-volume subpath note while preserving the required named-volume/XFS/WAL topology without granting the rehearsal any writable path to generation 1.
- Storage guard: checked on `xfs`; production account `PA3KWXU1C4C3` opened read-write: `false`; production mount `629` is read-only and qualification mount `638` is separately read-write
- Polygon fixture: `36d3c9358f4bcbfa89b29ec12493f566fdbd71a61ee2ef9d6c7f3bdd6e90d961` (4062 minute rows); 48 deterministic 5-second prints drove 3 exact closed minutes through `stream_minute_bars/aggregate_realtime_bar/IbkrMarketDataFeed`; all `3` reached `strategy_evaluations/DeploymentValidationDecisionKernel`
- Recovery: backup `09526afe319dfb8be06999ae590f20f04be0da3a36e45024829b20111eea11e4`; integrity `ok`; backup/restore/rebuild hash heads match
- #1413 browser campaign: `25` interactions across `5` page loads / `4` actual reloads, `25` native EventSource observations, `25` row-level event/request/proof correlations, historical state `OFF_DUTY_STOPPED_FLAT_POST_RESTART` with presented action `resume`, and `0` lifecycle requests; browser-page reload coverage: `PLAYWRIGHT_NATIVE_EVENT_SOURCE_PAGE_RELOAD_CAMPAIGN`

| Synthetic scenario | Result | Fault seam | Abort class | Abort cause | Limitations | Evidence |
|---|---|---|---|---|---|---|
| Polygon minute aggregates traverse the live 5-second aggregation path | rehearsed with limitations — pending one live confirmation | not required | `NONE` | `NONE` | `CLERK_OBSERVATION_CLOCK_NOT_APPLICABLE`: This feed/UI regression does not mutate or pass through the Clerk; its Clerk-observation clock is therefore explicitly not applicable. | `tests/broker/alpaca/clerk/sqlite/test_qualification.py::test_polygon_fixture_replays_through_live_feed_path` |
| One source print then silence fails closed and replaces the line | rehearsed with limitations — pending one live confirmation | not required | `NONE` | `NONE` | `CLERK_OBSERVATION_CLOCK_NOT_APPLICABLE`: This feed/UI regression does not mutate or pass through the Clerk; its Clerk-observation clock is therefore explicitly not applicable. | `tests/services/test_bot_start_admission.py::test_one_print_stall_blocks_admission_then_replaces_subscription`<br>`tests/broker/ibkr/test_bars.py::test_minute_stream_one_print_then_silence_invalidates_without_a_closed_bar`<br>`tests/broker/ibkr/test_bars.py::test_raw_stream_invalidates_a_bounded_stalled_subscription`<br>`tests/marketdata/test_feed.py::test_stalled_subscription_is_replaced_without_ending_the_bot_feed` |
| Never-first-bar feed blocks admission | rehearsed with limitations — pending one live confirmation | not required | `NONE` | `NONE` | `CLERK_OBSERVATION_CLOCK_NOT_APPLICABLE`: This feed/UI regression does not mutate or pass through the Clerk; its Clerk-observation clock is therefore explicitly not applicable. | `tests/marketdata/test_feed.py::test_health_active_without_first_closed_bar_is_stale`<br>`tests/services/test_bot_start_admission.py::test_never_advanced_feed_fails_closed` |
| Lost submit resolves by exact client identity | rehearsed — pending one live confirmation | exercised | `NONE` | `NONE` | none | `tests/broker/alpaca/test_client.py::test_injected_post_sdk_submit_hides_landed_response`<br>`tests/broker/alpaca/clerk/sqlite/test_enter.py::test_lost_submit_atomically_blocks_more_exposure_until_exact_recovery`<br>`transition:5:39eefa3a66d742b567384178a7bf421727c02206ac3ded4da6f8ed8bd5c3a53b` |
| Duplicate/redelivered broker evidence is idempotent | rehearsed — pending one live confirmation | exercised | `NONE` | `NONE` | none | `tests/broker/alpaca/test_trade_updates.py::test_inject_frame_faults_redelivers_last_trade_update`<br>`tests/broker/alpaca/clerk/sqlite/test_enter.py::test_duplicate_fill_observation_does_not_double_count`<br>`transition:5:51ee00507340047f5bfc84188fa712697301b656f5dcc7ff6d8cdb9d365a74fc` |
| Cancel-first EXIT retains proven remainder during a fill race | rehearsed — pending one live confirmation | exercised | `NONE` | `NONE` | none | `tests/broker/alpaca/test_trade_updates.py::test_injected_frame_threads_through_the_real_consumer`<br>`tests/broker/alpaca/clerk/sqlite/test_exit.py::test_partial_fill_during_cancel_uses_only_the_clerk_proven_remaining_quantity`<br>`tests/services/test_alpaca_sqlite_synthetic_drills.py::test_cancel_fill_fault_is_folded_while_cancel_is_in_flight`<br>`transition:12:5e6b4102c49a04dfb0899b3640bea28a262b5e66a18ea8087fb1b7b617ec5b7a` |
| Lost cancel response retains custody | rehearsed — pending one live confirmation | exercised | `NONE` | `NONE` | none | `tests/broker/alpaca/test_client.py::test_injected_post_sdk_cancel_hides_landed_response`<br>`tests/broker/alpaca/clerk/sqlite/test_exit.py::test_lost_cancel_response_blocks_the_closing_order`<br>`tests/services/test_alpaca_sqlite_synthetic_drills.py::test_lost_cancel_sdk_call_is_in_structured_broker_proof`<br>`transition:7:e3ff3f20a42b911644919170e31478dc2d1d085e03939b7e901c434a24f97ca1` |
| Trade-update gap reconciles before admission | rehearsed — pending one live confirmation | exercised | `NONE` | `NONE` | none | `tests/broker/alpaca/test_trade_updates.py::test_injected_disconnect_enters_reconnect_path`<br>`tests/broker/alpaca/test_trade_updates.py::test_reconnect_gap_reconcile_pulls_missed_orders`<br>`tests/broker/alpaca/clerk/sqlite/test_reconcile.py::test_reconciliation_fences_enter_before_reading_broker_truth`<br>`tests/services/test_alpaca_sqlite_synthetic_drills.py::test_gap_reconciliation_blocks_admission_while_rest_snapshot_is_in_flight`<br>`transition:8:9400d87af28b2a84a75363d23268edf30ba2bd5e4befa289183ca40b7ba0f762` |
| BOT uncertainty isolates only the affected bot | rehearsed — pending one live confirmation | not required | `NONE` | `NONE` | none | `tests/broker/alpaca/clerk/sqlite/test_uncertainty.py::test_admit_new_exposure_bot_scoped_uncertainty_blocks_only_that_bot`<br>`transition:5:d430e8d4ea60c6a37a23f430ef3ef2b899a4393c3550581fb30aed57c3a188f9` |
| Unknown uncertainty defaults to ACCOUNT_CLERK | rehearsed — pending one live confirmation | not required | `NONE` | `NONE` | none | `tests/broker/alpaca/clerk/sqlite/test_uncertainty.py::test_raise_uncertainty_default_shape_fails_closed`<br>`tests/broker/alpaca/clerk/sqlite/test_uncertainty.py::test_unknown_cause_blocks_reduction_account_wide`<br>`transition:5:d0131e245d9d4ca71f7756b9776423b0261c56882ad259869da79e3c08418670` |
| Restart resolves accepted/unknown work without duplication | rehearsed — pending one live confirmation | not required | `NONE` | `NONE` | none | `tests/broker/alpaca/clerk/sqlite/test_commands.py::test_state_machine_survives_restart`<br>`tests/broker/alpaca/clerk/sqlite/test_enter.py::test_kill_before_broker_contact_recovery_finds_one_accepted_operation_no_duplicate`<br>`tests/broker/alpaca/clerk/test_active_authority.py::test_valid_activation_opens_and_recovers_only_sqlite`<br>`tests/services/test_alpaca_sqlite_synthetic_drills.py::test_restart_in_flight_uses_active_authority_boot_recovery_for_accepted_and_unknown_work`<br>`transition:13:a054c529be42e4cdda6d9cc4116335f67af60175fdfc057b2a6b428a015db2a5` |
| Evidence controls never dispatch a lifecycle command | rehearsed with limitations — pending one live confirmation | not required | `NONE` | `NONE` | `CLERK_OBSERVATION_CLOCK_NOT_APPLICABLE`: This feed/UI regression does not mutate or pass through the Clerk; its Clerk-observation clock is therefore explicitly not applicable. | `Frontend/tests/e2e/alpaca-clerk-ui-correlation.spec.ts::keeps evidence clicks read-only across real browser reloads and native SSE revisions`<br>`playwright:alpaca-clerk-ui-correlation.spec.ts:chromium:1-passed`<br>`campaign:5-page-loads:4-browser-reloads:25-evidence-interactions`<br>`historical-state:OFF_DUTY_STOPPED_FLAT_POST_RESTART:resume-visible`<br>`native-eventsource:25-instrumented-snapshot-listener-observations`<br>`correlation-ledger:25-event-request-proof-rows`<br>`evidence-responses:25-operation-and-proof-references`<br>`network-action-endpoint:0-lifecycle-requests:0-action-ids`<br>`git-commit:b66ffaebea5dc33c07d82a81ad8f818113b2f089`<br>`test-source-sha256:8d6108b86a3d55c0bab1981842c0f6ba53607acf679ef80f16aa6d815edc9595`<br>`campaign-contract-sha256:544cbea3cebf202806972851c64d0da5136b2c44a444a10d07c4ade8bbe7ffb3`<br>`stdout-sha256:015fa448bae95983e6470814c546877fb59a077e83e8db87c84148b8cae44075`<br>`stderr-sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`<br>`preverified-ui-sha256:9e4f2848f9014548c6b2a72701b69828876c8d01073834628f5fec84ecc81f50` |
| Protected-generation backup, restore, and mirror rebuild | rehearsed — pending one live confirmation | not required | `NONE` | `NONE` | none | `synthetic-runner:protected-generation-recovery-ceremony` |

The required-live scenario table above remains `PENDING`. The content-addressed JSON report preserves the full #1409 field set (authority identity/revision, operation identities, three clock fields, broker before/after proof, expected/observed state, operator action, receipts, and logs). Any unavailable or inapplicable clock remains null and carries a typed clock limitation; no such row is presented as complete three-clock evidence.

## Qualification A attempt 1 receipt

```text
Scenario: Baseline Resume, live-feed observation, safety stop, and reconciliation
Started at (ms UTC): 1786121106666
Ended at (ms UTC): 1786121303876
Authority generation / DB identity / control revision: 1 / 03ed49bd38bb1f3a6462f81706e7dec2 / 298 before START, 300 after STOP, 301 after reconciliation
Strategy instance / lifecycle run: sqlite-market-qual-0807 / 558d6e5f2dda4772a96a79f32c8ce851
Command / effect operation / order ref / broker order / receipt: START cmd:PA3KWXU1C4C3:sqlite-market-qual-0807:558d6e5f2dda4772a96a79f32c8ce851:START:ACTIVE; STOP cmd:PA3KWXU1C4C3:sqlite-market-qual-0807:558d6e5f2dda4772a96a79f32c8ce851:STOP:STOPPED; no effect operation or order; reconciliation:301
Broker source timestamp (ms UTC): first decision-consumer callback logged 1786121110705; no later decision-consumer print arrived
Clerk observation timestamp: START 1786121106666; STOP 1786121283392; reconciliation attempted 1786121303876
Durable record timestamp: START 1786121106666; STOP 1786121283392; reconciliation 1786121303876
Expected state: continuously advancing IBKR bars; entry only after two consecutive green closed minutes; Clerk-owned ENTER then bounded EXIT
Observed state: each IBKR consumer received one print and stalled; Broker V2 presented Live/Healthy while Bot health remained Last bar None; no decision or broker effect
Position proof: final Operator panel Flat; SQLite positions empty; clean reconciliation
Open-order proof: final Operator panel 0 working / 0 unresolved; SQLite orders empty; clean reconciliation
Operator action: Stop bot decisions, inspect stopped-flat receipt, Reconcile now
Warning/error log reference: 16:45:01, 16:45:36, 16:46:01, and 16:46:36 UTC reqRealTimeBars non-delivery warnings
Verdict: ABORT
```

## Qualification A attempt 2 receipt and corruption abort

```text
Scenario: Normal one-share ENTER/EXIT, Stop, reconciliation, truth inspection, and restart
Started at (ms UTC): 1786122492521
Ended at (ms UTC): 1786123725672
Authority generation / DB identity / control revision: 1 / 03ed49bd38bb1f3a6462f81706e7dec2 / 302 before round trip, 321 after reconciliation, finalized mirror sequence 323 after abort shutdown
Strategy instance / lifecycle run: sqlite-market-qual-0807 / 206199d1a0b1405a85cdc8f97db34d5d; unintended later run e271d757f9a24746a92364348221ef46
Command / effect operation / order ref / broker order / receipt: ENTER effect `effect:sqlite-market-qual-0807:encoded-MTc4NjEyMjYwMDAwMDpFTlRFUg`, order ref `learn-ai/sqlite-market-qual-0807/v1:5AMrAr-1SsqXbLyaodnO9g`, broker order `c09fb7ed-dd7b-4d49-b892-5f62c0130a3a`; EXIT effect `effect:sqlite-market-qual-0807:encoded-MTc4NjEyMjc4MDAwMDpFWElU`, order ref `learn-ai/sqlite-market-qual-0807/v1:-dzPQRYbyg7CsMKN49ecng`, broker order `e7dd5d2f-669e-44c7-b008-a9e3e7644b87`; `reconciliation:321`; unintended Resume receipt `8045ab0e-22f7-4ef9-a846-eda169af4c0a`
Broker source timestamp: ENTER 1786122606676; EXIT 1786122787082
Clerk observation / durable record: ENTER fill 1786122606786; EXIT fill 1786122787217; attributed flat 1786122789036; normal STOP 1786122826701
Expected state: normal round trip, stopped-flat truthful panel, evidence drawer read-only, restart preserves authority and remains stopped
Observed state: round trip and flat reconciliation passed; panel truth defects appeared; later evidence interaction recorded Resume; SQLite reads then reported disk I/O error / database disk image malformed
Position proof: independent Alpaca REST after service shutdown returned positions []
Open-order proof: independent Alpaca REST after service shutdown returned open orders []
Operator action: stop data service; do not repair in place; verify finalized mirror read-only
Warning/error log reference: 17:28:15Z projection disk I/O error; later database disk image malformed; POST Stop returned 500
Recovery evidence: mirror identity matches generation 1 and DB identity; contiguous finalized hash chain through sequence 323 / hash 666d23091fd83cdde0e54459d45dd6c99e152b1fbdc8a8795f6407388be5966e / RUN_STOPPED
Verdict: PASS for baseline ENTER/EXIT; ABORT for truth/restart qualification
```

## Corruption mechanism and storage hardening

The incident database lived beneath the compose bind mount
`./PythonDataService/artifacts:/app/artifacts`. Inside the Podman VM that mount
reported `virtiofs`; the container writer used SQLite 3.46.1 while host-side
diagnostic readers used SQLite 3.51.0 from macOS. This placed one WAL authority
across two host/shared-memory and filesystem-locking domains. SQLite's official
[WAL documentation](https://www.sqlite.org/wal.html) requires every process to
run on the same host and states that WAL does not work over a network
filesystem. Its official
[corruption guide](https://www.sqlite.org/howtocorrupt.html#filesystems_with_broken_or_missing_lock_implementations)
also identifies broken or missing filesystem locks with concurrent access as a
database-corruption mechanism. The observed `runs` btree/index damage followed
an immediate container restart plus cross-boundary diagnostic access, matching
that prohibited topology.

The corrective layout masks `/app/artifacts/alpaca_clerk` with the persistent
VM-local `alpaca-clerk-data` named volume while leaving non-SQLite runtime
artifacts on the host bind mount. The migrated Clerk root reports `xfs` inside
the service. Repository initialization and open now refuse known FUSE/remote
filesystem types before creating or opening a WAL authority; focused tests pin
both the new-authority and existing-authority refusal. Recovery tooling is
copied/mounted into the image and the runbook requires default-compose
operations to use a one-shot `python-service` container against the same named
volume. Host and container SQLite clients must never alternate against one WAL
authority.

The sequence-326 database/mirror/decision-journal tree was copied into the
named volume only after a clean service stop. In-volume verification reproduced
`integrity_check=ok`, generation 1, the same database identity, revision 326,
and the same database/mirror head hash. The complete former FUSE-backed source
tree was moved, not deleted, to
`artifacts/sqlite-storage-migration-preserved/alpaca_clerk-fuse-20260807T1249CDT`.
The recreated service opened on XFS, completed boot recovery, remained stopped,
and independent Alpaca REST again returned no positions and no open orders.
A subsequent full Podman VM restart preserved the named volume; a fresh service
container again opened generation 1 / database identity
`03ed49bd38bb1f3a6462f81706e7dec2` at revision 326 with
`integrity_check=ok`, zero exposure, zero working orders, zero active holds,
and zero active uncertainties. Its panel projection remained
`OFF_DUTY / STOPPED / STOPPED_FLAT` with the three durable bar decisions intact.

## Operator-approved recovery and Qualification A attempt 3

```text
Scenario: Mirror rebuild, stopped-state restart, three advancing closed bars, Stop, reconciliation, integrity verification, and restart
Started at (ms UTC): 1786124232857
Ended at (ms UTC): 1786124632009
Authority generation / DB identity / control revision: 1 / 03ed49bd38bb1f3a6462f81706e7dec2 / 323 after rebuild, 326 after final reconciliation
Strategy instance / lifecycle run: sqlite-market-qual-0807 / 4ac1286daea244ed8f9493b77bb4d353
Command / effect operation / order ref / broker order / receipt: Resume receipt d24c7c6f-f2c1-4c3a-96a9-0d62dc7cfe73; RUN_STARTED sequence 324; no effect operation or order; STOP command cmd:PA3KWXU1C4C3:sqlite-market-qual-0807:4ac1286daea244ed8f9493b77bb4d353:STOP:STOPPED / sequence 325; reconciliation:326
Broker source timestamp: advancing minute bars ended at 1786124460000, 1786124520000, and 1786124580000
Clerk observation timestamp: decision receipts 1786124466164, 1786124525722, and 1786124585427; STOP 1786124612829; reconciliation 1786124632009
Durable record timestamp: same decision-journal and custody-transition timestamps; every write completed before the next observation
Expected state: recover only the finalized mirror head; remain stopped after boot; append one decision receipt per closed bar; stop flat; reconcile clean; remain stopped after another restart
Observed state: rebuild preserved the corrupt DB/WAL/SHM and restored all 323 finalized transitions; UI evidence interaction remained read-only; three advancing bars appended decision sequences 2-4 with no_action; stop and reconciliation advanced SQLite to 326; restart remained OFF_DUTY / STOPPED / runtime idle
Position proof: independent Alpaca REST before Resume, during the run, before shutdown, and after Stop returned positions []
Open-order proof: independent Alpaca REST at the same boundaries returned open orders []; operator panel showed 0 working orders
Operator action: approve documented rebuild; inspect recovered UI; Resume; observe three bars; Stop bot decisions; Reconcile now; graceful service stop; offline integrity/mirror verification; restart
Warning/error log reference: none for Alpaca SQLite Clerk recovery/run; separate pre-existing IBKR host-daemon/account-safety observer warnings did not affect the Alpaca authority
Recovery evidence: corrupt files preserved under recovery-preserved/pre-rebuild-1786124232857-4e619722; receipt 1786124232878-rebuild_from_mirror.json; offline PRAGMA integrity_check ok; mirror 326 rows / head hash 259b11dfb514a4f0ec4b1198500dbbe91c1c85e018ef552a43157ee07314ecd0 / file SHA-256 e93f1fefc2d5a071807e92e9def5dbc288fdd1f1204ca2c54ea660963f658693; authority migrated from virtiofs to the XFS-backed learn-ai-alpaca-clerk-data volume with the source tree preserved
Verdict: PASS for recovery, advancing-bar persistence, normal stop/reconciliation, and stopped-state restart; broader multi-session/race qualification remains pending
```

## Scenario receipt template

Copy one block per attempt. Never overwrite a failed attempt with a retry.

```text
Scenario:
Supervising operator / sign-off (ms UTC):
Started at (ms UTC):
Ended at (ms UTC):
Authority generation / DB identity / control revision:
Strategy instance / lifecycle run:
Command / effect operation / order ref / broker order / receipt:
Broker source timestamp:
Clerk observation timestamp:
Durable record timestamp:
Expected state:
Observed state:
Position proof:
Open-order proof:
Operator action:
Warning/error log reference:
Verdict: PASS | SAFE FAILURE | ABORT
Abort class (n/a for PASS or SAFE FAILURE; required for ABORT): feed-abort | custody-abort | unclassified
```

## Abort and incident record

Every new row records one of `feed-abort`, `custody-abort`, or `unclassified`
in Abort class; `unclassified` requires durable operator/reviewer follow-up
classification and fails closed as `custody-abort` until classified.
`N/A — predates the Abort taxonomy amendment` is reserved for the rows below
that predate this amendment; it is not a valid value for a new row.

| Time (ms UTC) | Phase/date label (prose) | Condition | Scope | Abort class | Action | Final broker proof | Defect issue |
| --- | --- | --- | --- | --- | --- | --- | --- |
| — | Initial canary — 2026-08-07 | IBKR feed stopped after one 5-second print; the never-advanced liveness blind window was characterized as unbounded | Feed admission | N/A — predates the Abort taxonomy amendment | Operator stopped the bot; no decision or order occurred | Flat; zero open orders; durable stopped-flat receipt | Existing incident record in the first-canary audit; no new defect per the recorded operator decision |
| — | Before Qualification A broker contact | SQLite panel health said `Flat Resume ready`, but the presented action list omitted Resume | Broker V2 UI/action policy | N/A — predates the Abort taxonomy amendment | Ceremony paused before broker contact; regression-first local fix; service restart and UI verification | Flat; zero working orders; `reconciliation:298` | [#1410](https://github.com/tim1016/learn-ai/issues/1410) |
| 1786121106666–1786121303876 | Qualification A attempt 1 | Both live IBKR consumers delivered one print and stalled; Trader/Operator continued to present Live/Healthy while durable Bot health had no last bar or decision | Feed delivery and UI truth | N/A — predates the Abort taxonomy amendment | Stop bot decisions at `1786121283392`; Reconcile now at `1786121303876`; do not resume | Flat; zero working/unresolved orders; clean `reconciliation:301` | [#1411](https://github.com/tim1016/learn-ai/issues/1411) |
| 1786122826701–1786122842087 | Qualification A attempt 2 | After a valid round trip the panel reported flatten-required, one uncertain intent, empty selected-effect evidence, and no bar/decision | Broker V2 truth projection | N/A — predates the Abort taxonomy amendment | Regression-first local fixes; no broadened exposure | Flat; zero open/working orders; clean `reconciliation:321` | [#1412](https://github.com/tim1016/learn-ai/issues/1412) |
| 1786123676085–1786123725672 | Qualification A corruption abort | Evidence-station interaction recorded an unintended Resume; projection then reported a malformed SQLite database | UI action routing and SQLite authority | N/A — predates the Abort taxonomy amendment | Stop data service; preserve corrupt DB/WAL/SHM; verify current finalized mirror; await human confirmation | Independent Alpaca REST: paper account, no positions, no open orders; mirror ends in `RUN_STOPPED` | [#1413](https://github.com/tim1016/learn-ai/issues/1413) |
| 1786124232857–1786124632009 | Recovery and Qualification A attempt 3 | Operator-approved recovery of the preceding abort | SQLite authority and supervised paper runtime | N/A — predates the Abort taxonomy amendment | Documented `REBUILD_FROM_MIRROR`; preserve corrupt files; verify identity/head; three-bar run; Stop; Reconcile; offline integrity/mirror check; restart idle | Independent Alpaca REST: no positions and no open orders; SQLite revision 326; clean reconciliation; mirror and database heads agree | [#1413](https://github.com/tim1016/learn-ai/issues/1413) |

Any duplicate economic intent, broker mutation without finalized SQLite intent,
mixed custody/lifecycle authority writer, unresolved account drift, regressed custody,
fabricated terminal result, stale evidence authorizing exposure, substituted
authority, UI/execution-policy mismatch, or failed recovery identity/hash check
ends the ceremony immediately. Stop all governed bots, preserve evidence, and
open a focused defect issue.

The activated path does retain a JSONL **signal-decision journal** for read-only panel
evidence. It is not a custody, order, lifecycle, or reconciliation authority; the
earlier blanket phrase "no mixed JSONL/SQLite writer" was imprecise.

## Local validation after adversarial remediation

- The affected Python regression set passed `728` tests, including the new
  multi-symbol, decision-intent/idempotency, recovery-proof/WAL-guard, epsilon-boundary,
  and effect-predicate parity cases.
- The two focused Angular panel files passed `25` tests; the evidence-station case
  asserts that interaction never calls the action dispatcher.
- The OpenAPI contract and generated Angular types were regenerated from the service
  schema. Ruff over `app/` and `tests/`, OpenAPI drift check, compose validation, and
  `git diff --check` passed.
- The normalized repository run completed with `8613` passed, `65` skipped, and `5`
  expected-failure tests unexpectedly passing. Its only two failures were outside this
  change: the opt-in 100k slow performance test exceeded its 60-second wall-clock budget
  under the full local run, and the live Polygon freshness canary received the test
  placeholder API key. The separate local LEAN replay gate was excluded because its
  gitignored cache is missing 66 of 501 required sessions. These are not recorded as a
  clean project-wide pass.
- A thermo-nuclear maintainability review found one immediate-replay-only decision
  deduplication gap; it was expanded to all historical bar references and regression
  tested before this report was finalized.

Residual non-blocking hardening risks remain documented rather than silently claimed
away: bounded source-stall detection depends on IBKR's documented
`reqRealTimeBars` one-bar-per-five-seconds contract and has no independent
per-request heartbeat if the vendor violates that contract; mirror rebuild reconstructs
control revision and does not preserve prior reset provenance; and filesystem-type
detection remains a denylist that cannot enforce the Linux mount check on non-Linux
hosts. Production remains constrained to the Linux/XFS named-volume topology.

## Authority-substrate retirement evidence refresh — 2026-08-10

**Status: COMPLETE for the activated Alpaca paper custody scope.** This status is
limited to retiring the legacy JSONL custody writers, file idempotency ledger, and
Clerk Postgres lifecycle projection in favor of the activation-selected SQLite
authority. It does not complete the separate live-qualification and ADR-acceptance
gate below.

A fresh verified online backup completed at `1786381437981` ms UTC while the Clerk
remained online. The backup ceremony uses SQLite's online-backup API and does not
perform a broker call or author a custody transition. Its receipt recorded:

- account `PA3KWXU1C4C3`, authority generation `1`, schema version `6`, and unchanged
  database identity `03ed49bd38bb1f3a6462f81706e7dec2`;
- control revision, transition count, and finalized mirror sequence all at `326`;
- finalized transition hash
  `259b11dfb514a4f0ec4b1198500dbbe91c1c85e018ef552a43157ee07314ecd0`;
- verified snapshot SHA-256
  `08566ad523bcff4d3aa0b6289af9060b2eab31c437d27682a6e4be8b6ab234d6`;
- bundle
  `accounts/alpaca/PA3KWXU1C4C3/verified-backups/backup-g1-1786381437981-08566ad523bcff4d`.

The focused offline authority-retirement suite passed `82` tests. The exercised
contracts prove that a valid activation opens and recovers only SQLite; an invalid,
substituted, unavailable, or lease-blocked activated database fails closed without
constructing the legacy Clerk; cutover quarantines the exact legacy authority files;
catalog quarantine is account-scoped and content-addressed; the legacy lifecycle
Postgres runtime/contract is retired; SQLite DDL preserves transition immutability and
enforces idempotency at the SQL boundary; and HTTP command retries resolve through the
SQLite authority. The passing files were:

- `tests/contracts/test_alpaca_active_authority_wiring.py`;
- `tests/contracts/test_legacy_lifecycle_projection_retirement.py`;
- `tests/broker/alpaca/clerk/test_active_authority.py`;
- `tests/broker/alpaca/clerk/sqlite/test_cutover.py`;
- `tests/broker/alpaca/clerk/sqlite/test_catalog_quarantine.py`;
- `tests/broker/alpaca/clerk/sqlite/test_schema_parity.py`;
- `tests/routers/test_alpaca_clerk_sqlite.py`.

This evidence does **not** claim that every JSONL or file artifact in the repository
has been deleted. The activated path intentionally retains the write-only finalized
transition mirror for corruption recovery, the account-external established-generation
registry needed to detect authority deletion, and the read-only signal-decision journal
used by the panel. Those artifacts cannot submit an order, own custody, authorize a
lifecycle mutation, reconcile broker state, or act as a fallback authority. IBKR and
generic broker-capture JSONL artifacts are outside this Alpaca-only retirement scope.

## Historical governance before the evidence-driven amendment

These rows preserve the original multi-session closure bar. They are intentionally
not checked retroactively. The approved 2026-08-10 PRD replaced them as the ADR
acceptance gate and moved the unrun scenarios to post-acceptance hardening.

- [ ] All required live scenarios passed across multiple market sessions.
- [ ] Final account proof is fresh, flat, and order-free.
- [ ] Abort/incident record is complete, including `none` where applicable.
- [ ] Recovery runbook reviewed against observed operations and marked final.
- [x] Trader/Operator truth-language specification is published.
- [ ] ADR 0035 status changed from Proposed to Accepted for Alpaca paper only.
- [ ] ADR 0035's precise ADR 0001/0008/0030/0033 supersession text retained.
- [ ] Live-money trading remains disabled.

**Prior verdict (superseded on 2026-08-10): RECOVERED / PARTIALLY QUALIFIED /
REQUEST CHANGES / STILL BLOCKED FROM CLOSURE.** At that point #1409 had to remain
open. The mirror rebuild and three-bar run had recovered a healthy sequence-326
authority, but the changes still required review, merge, and a new live
requalification. That historical judgment remains accurate for the evidence then
available.

## UI-driven acceptance campaign — 2026-08-10

The approved evidence amendment is
[`alpaca-sqlite-ui-paper-acceptance-and-ibkr-control-retirement.md`](../prds/alpaca-sqlite-ui-paper-acceptance-and-ibkr-control-retirement.md).
The campaign used only rendered Alpaca Broker V2 controls and evidence on paper
account `PA3KWXU1C4C3`; no direct HTTP, trading CLI, SQLite inspection, browser
`fetch`, or guard bypass participated in the ceremony.

| Fact | UI-visible evidence |
| --- | --- |
| Identity | Bot `sqlite-ui-accept-0810`; lifecycle run `36eef5961dfa4c6697d3109a065d9742`; Deployment Validation; SPY; Paper; one share; carryover forbidden |
| Deploy | Receipt `alpaca-paper-deploy:PA3KWXU1C4C3:sqlite-ui-accept-0810:1786384614022`; on duty, advancing feed, zero pre-ENTER orders |
| ENTER | Effect `effect:sqlite-ui-accept-0810:encoded-MTc4NjM4NDY4MDAwMDpFTlRFUg`; client order `learn-ai/sqlite-ui-accept-0810/v1:KCq7OE8DSe-UgX5Y1kLaBQ`; Alpaca order `bae47099-fc66-4da9-8de9-c34de9ada8a5`; BUY 1 SPY at `$772.74`, filled `12:58:07` CT |
| ENTER attribution | Bot Trader exposure and Alpaca Account Desk both showed exactly one SPY share at average `$772.74`; no duplicate command, effect, order, or economic fill |
| EXIT | Third-subsequent-bar effect `effect:sqlite-ui-accept-0810:encoded-MTc4NjM4NDg2MDAwMDpFWElU`; client order `learn-ai/sqlite-ui-accept-0810/v1:t5WWgHc6aoonBNHQCrcrdA`; Alpaca order `b513fd2f-28ee-4d65-9f73-a3956d8f616a`; SELL 1 SPY at `$772.83`, filled `13:01:06` CT |
| Terminal chain | **Attributed exposure flat** (`EXIT_ATTRIBUTED_FLAT`) and **Reconciliation completed** at `13:01:13` CT; zero positions and working orders |
| Stop/reconcile | Stop receipt `cmd:PA3KWXU1C4C3:sqlite-ui-accept-0810:36eef5961dfa4c6697d3109a065d9742:STOP:STOPPED` at `13:02:29` CT; final `reconciliation:345` at `13:02:47` CT |
| Reconstruction | Reload remained Off duty / Runtime idle / Stopped flat. Signal raw evidence, Audit trail, and Run evidence were read without mutation; the journal remained `18` events. |
| Final account | Paper; Clerk generation `1` healthy; no position, working-order identity, hold, uncertainty, or account freeze; reconciliation clean |

One contradiction was found during the review: the Trader lens said **No fills today**
while SQLite correctly exposed the legacy fill-history fold as unavailable and the
Account Desk showed the fill. The regression-first frontend fix now renders **Fill
history unavailable from active custody folds** for `fills_today = null`, reserves
**No fills today** for a verified zero, and handles known fills outside the chart
window separately. Fourteen focused tests pass. The live stopped bot rendered the
corrected copy after reload, with no false-zero copy and the journal still at `18`
events.

The live chain is not asked to stand in for injected corruption. It composes with the
existing adversarial qualification artifacts, the human-approved mirror-rebuild
evidence, the verified generation-1 online backup above, and the `82` focused
activation/no-fallback/cutover/schema/retirement tests. Together they cover the
normal broker path and the failure/recovery invariants without claiming the historical
multi-session matrix was run.

## Acceptance governance — 2026-08-10

- [x] UI proved the intended Alpaca Paper account and healthy activated generation.
- [x] Exactly one strategy-owned ENTER and one strategy-owned EXIT retained one
      command/effect/order/fill identity chain and real paper fill prices.
- [x] SQLite attribution and broker account exposure agreed after ENTER and at flat.
- [x] Stop and reconciliation completed with zero working orders, unresolved custody,
      holds, or uncertainty.
- [x] Reload and read-only evidence interactions preserved stopped-flat state and the
      exact `18`-event journal.
- [x] The presentation discrepancy was fixed regression-first and revalidated live.
- [x] ADR 0035 is Accepted for Alpaca paper only and its precise Alpaca-only
      ADR 0001/0008/0030/0033 supersession boundary is retained.
- [x] Live-money remains disabled.
- [x] Unrun multi-session/fault scenarios remain explicit post-acceptance hardening,
      not retroactive passes.

## Final verdict

**ACCEPTED FOR ALPACA PAPER ONLY.** The one-share UI-driven campaign closed the
remaining normal-path proof gap and ended fresh, flat, reconciled, order-free, and
stopped. The existing deterministic/adversarial/recovery/backup evidence remains the
receipt for fault paths. Live-money remains disabled. Issue #1409 remains open only
until this acceptance change merges, per the PRD; #1411, #1413, and #1416 may be
reconciled against their amended evidence bars. Residual multi-session and injected-
fault live rehearsal belongs in one bounded post-acceptance hardening issue.

## Follow-on four-bot churn experiment — 2026-08-10

At the operator's request, the earlier four-symbol paper experiment was repeated after
the acceptance bot had stopped flat. Rendered Alpaca order history identified the
original set as AAPL, NVDA, SPY, and TSLA. All operations again used only Alpaca
Broker V2 UI controls. Each bot used Deployment Validation, Paper, one share, and
carryover forbidden.

| Symbol / bot | Deploy receipt | Initial run |
| --- | --- | --- |
| AAPL / `sqlite-cohort-aapl-0810` | `alpaca-paper-deploy:PA3KWXU1C4C3:sqlite-cohort-aapl-0810:1786386138128` | `322e6c94e9f242688f0850614a248955` |
| NVDA / `sqlite-cohort-nvda-0810` | `alpaca-paper-deploy:PA3KWXU1C4C3:sqlite-cohort-nvda-0810:1786386223600` | `6df49ed4ad0941e6a37c1b24636f00f4` |
| SPY / `sqlite-cohort-spy-0810` | `alpaca-paper-deploy:PA3KWXU1C4C3:sqlite-cohort-spy-0810:1786386279237` | `3cfa570d77e7492c9c4139b7b00a85bb` |
| TSLA / `sqlite-cohort-tsla-0810` | `alpaca-paper-deploy:PA3KWXU1C4C3:sqlite-cohort-tsla-0810:1786386332177` | `b87b776935c34ed49ab3951f94d384af` |

Deployment naturally staggered because each new market-data consumer made the Clerk
channel unhealthy until its first closed-bar observation. The UI blocked the next
launch while Market Data was unhealthy and Execution was healthy; after the next bar,
all six admission gates returned Ready. No gate was bypassed. The roster then showed
all four Working under SQLite Account Clerk custody with both channels healthy.

The supported live tweak was lifecycle churn, not an in-place configuration edit:
strategy instances are immutable. While AAPL held one attributed share and NVDA/SPY
remained live, flat TSLA stopped with
`cmd:PA3KWXU1C4C3:sqlite-cohort-tsla-0810:b87b776935c34ed49ab3951f94d384af:STOP:STOPPED`,
then resumed from the same immutable configuration as new run
`a9ed403b413549f58bf870b9eeff33e1` (UI receipt
`6984c8f7-a73d-40ec-bfd3-cd933b158904`). That new run produced its own complete
ENTER/EXIT cycle. TSLA then stopped flat under the new run and was replaced by GOOGL,
preserving four active bots:

- GOOGL deploy receipt
  `alpaca-paper-deploy:PA3KWXU1C4C3:sqlite-cohort-googl-0810:1786387174010`;
- GOOGL run `e046716277c14b279974a2173b3bc196`;
- replacement active set AAPL, NVDA, SPY, and GOOGL; TSLA off duty.

The account reached a clean peak of three simultaneous one-share positions (SPY,
NVDA, and GOOGL). Account Clerk generation `1` remained healthy and explicitly
reported no active hold or unresolved uncertainty. No working order identity,
unexpected position, custody block, duplicate effect, emergency flatten, or manual
trade appeared.

| Symbol | Observed completed paper cycles |
| --- | --- |
| AAPL | BUY 1 at `$306.74` (`13:27:06` CT) → SELL 1 at `$306.70` (`13:30:05`); BUY 1 at `$306.94` (`13:32:05`) → SELL 1 at `$306.83` (`13:35:06`) |
| SPY | BUY 1 at `$772.61` (`13:29:05`) → SELL 1 at `$772.52` (`13:32:05`); BUY 1 at `$772.58` (`13:40:05`) → SELL 1 at `$772.90` (`13:43:05`) |
| TSLA | Resumed run BUY 1 at `$328.94` (`13:35:05`) → SELL 1 at `$328.48` (`13:38:06`) |
| NVDA | BUY 1 at `$218.64` (`13:41:05`) → SELL 1 at `$218.71` (`13:44:05`) |
| GOOGL | BUY 1 at `$354.81` (`13:41:06`) → SELL 1 at `$355.34` (`13:44:06`) |

Wind-down was flat-only. AAPL, SPY, GOOGL, and NVDA were each stopped after their
own strategy EXIT reached flat; TSLA had already stopped flat for the replacement.
The stop receipts bound the expected run IDs. Final Account Desk proof showed Paper,
equity/cash `$100,002.31`, zero long and short market value, no positions, no verified
working orders, no hold or uncertainty, and healthy generation `1`. Final
reconciliation was `reconciliation:475`. The refreshed roster showed all five
experiment instances Off duty and Flat, with Market Data and Execution healthy and no
custody block.

**Verdict: PASS.** The repeat proved four concurrent immutable SQLite-governed bots,
namespace-separated exposure, flat stop/resume with a new run identity, a live
four-for-four symbol substitution, three-way simultaneous exposure, and a clean
strategy-owned/flat-only wind-down. It did not attempt unsupported in-place parameter
mutation or any live-money action.
