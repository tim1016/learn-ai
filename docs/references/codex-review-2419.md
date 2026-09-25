# Accounts reducing execution and recovery — research #2419

Research ticket: [#2419](https://github.com/tim1016/learn-ai/issues/2419). Parent map: [#2413](https://github.com/tim1016/learn-ai/issues/2413). Reviewed on 2026-09-24 against **`10b5f31b529c8507bd19bb24015f3d85fa9aba43`**. This is a bounded follow-up to the account execution/custody pass, not a claim of exhaustive trading safety. No source fixes or operational changes were made.

**Result:** one new **High** finding, R1. Existing cancellation, partial-fill, identity, recovery-session and claim controls passed the selected synthetic checks. A delayed confirmed quote was investigated and retained as a documented trade-off, not promoted to a defect. The cash-observation race, market-entry price bound and day-P&L findings from the first account pass are not repeated here.

## R1 — A deferred program EXIT can first submit an ineligible market order after close

**Claim.** A strategy EXIT accepted during regular hours can first send its durable `MARKET / DAY / extended_hours=False` reducing order after the regular close. The send-time session safeguard applies only to recovery EXITs. The result is a queued next-session order rather than current-session risk reduction.

**Goal.** A strategy's decision to close should either produce an eligible reduction now, or leave an explicit, recoverable refusal whose disposition is visible. It should not silently change from an immediate regular-session exit into overnight exposure merely because cancellation proof, scheduling, an outage or restart deferred first submission.

**Severity.** **High.** This is a reachable execution-policy defect in a risk-reducing path. It can leave a real position exposed overnight and then execute at a later market opening. Neither actual broker acceptance nor an actual loss was observed; no live or paper account was contacted.

**Evidence.**

- **Proven from source:** [program leg shaping](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/broker/alpaca/clerk/program_leg.py#L485-L509) chooses the regular market shape for an RTH binding or an RTH decision bar. The [actual facade](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/broker/alpaca/clerk/sqlite/runtime.py#L1122-L1135) durably accepts that shape because later cancellation proof may drive submission. The [creation guard](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/broker/alpaca/clerk/sqlite/exit_resolution.py#L254-L269) and [submission guard](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/broker/alpaca/clerk/sqlite/exit_resolution.py#L1085-L1117) both condition session eligibility on recovery classification. [Classification](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/broker/alpaca/clerk/sqlite/exit_resolution.py#L1192-L1194) uses the recovery decision prefixes, so a normal program decision does not receive that check.
- **Reproduced:** `research/codex-2419/test_reduction_validity.py::test_no_unpriced_market_reduction_is_sent_after_regular_close[program]` creates a synthetic filled 10-share SPY entry, accepts a normal EXIT at 15:59 ET, advances to 16:01 and drives the real SQLite resolver through a recording fake broker. It sends exactly one 10-share market DAY SELL with `extended_hours=False`; the attributed position remains 10 without a fill. The invariant requiring no ineligible market submission fails. Replacing the acceptance with a recovery EXIT at the same instants sends nothing and passes. The test proves the durable state-machine boundary, not the entire strategy scheduler or a real broker response.
- **Documented broker consequence:** Alpaca documents that orders not eligible for extended hours submitted after 16:00 ET are queued for the next trading day; ordinary DAY orders are regular-hours orders. An after-close first submission therefore does not expire at the close that already passed. [Alpaca order documentation](https://docs.alpaca.markets/us/docs/orders-at-alpaca), accessed 2026-09-24.
- **Inferred recovery consequence, supported by source:** a normally acknowledged queued order stays nonterminal. [EXIT finalization](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/broker/alpaca/clerk/sqlite/exit_resolution.py#L307-L364) does not produce `EXIT_NOT_FLAT` until appropriate terminal evidence; the [repricing watchdog](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/broker/alpaca/clerk/sqlite/exit_watchdog.py#L167-L187) scans that uncertainty. Thus the existence of automatic extended-hours repricing does not itself repair the newly queued case. This inference was not expanded into a full overnight broker simulation.

**Why.** The implementation deliberately preserves a decision's shape, but conflates durable order identity with continuing eligibility to first send that order. The [lateness exemption](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/services/bot_trade_strategy.py#L1141-L1174) says holding back a late EXIT would retain the position “overnight if the delay straddles the close.” Sending a regular-session market DAY after that close still retains it overnight.

**Decision rationale and challenge.** [ADR 0059 D5.3–4](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/docs/architecture/adrs/0059-real-money-live-behind-shadow-gate-arming-and-cash-bound-envelope.md#L81-L85) keeps market DAY inside the regular session and says an unfilled extended EXIT is “surfaced, never silently re-priced.” The [implementation reference](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/docs/references/alpaca-extended-hours.md#L81-L82) explains durable replay because the later pass knows nothing about the decision, and asserts, “It is a DAY order, so it dies at the session end rather than resting into a market that has moved.” Preserving identity and avoiding silent repricing are sound for uncertain submissions. The expiry premise does not cover a first submission after the original session ended: broker eligibility now refers to the next session. The [explicit decision-driven exemption](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/docs/references/alpaca-extended-hours.md#L98) confirms this is a policy gap, not a forgotten recovery implementation detail.

**Recommendation.** Retain immutable accepted intent and exact identities, but carry a session/validity boundary for every program reduction and enforce it immediately before broker submission. For an order proved never submitted, expiry should release an explicit recoverable outcome; an authorized new reduction can then use the existing current-session pricing policy. For uncertain or working broker orders, establish exact status and cancellation proof before any new identity or price. Do not silently mutate the existing uncertain order. Cover delayed initial execution, cancel-and-prove, restart, PRE→RTH, RTH→POST, POST→closed and early closes. If next-open queued execution is intentional, record that owner decision and show that disposition explicitly; it conflicts with the cited immediate risk-reduction rationale.

**Confidence.** **High** in the missing guard and the synthetic submission. **High** in the documented queue rule. **Medium** in the full unattended overnight consequence because live transport, all scheduler interleavings and operational intervention were deliberately not exercised.

## Counter-evidence and bounded trade-offs

**Accepted quote age is not a newly demonstrated policy violation.** The second probe accepts a POST-session recovery SELL at 17:00 with a fresh bid/ask and confirmed 99.80 limit, then drives it 20 seconds later. It sends the identical limit. After 20:00 it sends nothing. [Acceptance](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/broker/alpaca/clerk/recovery_reduction.py#L380-L423) checks quote age and price band, then records session expiry; [send-time verdict](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/broker/alpaca/clerk/recovery_reduction.py#L528-L539) uses that expiry. The [reference](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/docs/references/alpaca-extended-hours.md#L89-L98) explicitly places quote checks before acceptance and retains the confirmed limit until session end. Treating a ten-second broker-arrival guarantee as established would overstate the product contract. The trade-off is that a pending accepted limit can cease to be marketable; the price protection remains, and the session guard still works. No owner reversal is proposed here.

**Observable execution quality is narrower than universal arrival-price quality.** The [panel adapter](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/services/broker_v2_panel/sqlite_panel_adapter.py#L687-L739) reports slippage against the durable bid/ask used to price a recovery flatten. [Reference selection](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/broker/alpaca/clerk/sqlite/exit_resolution.py#L1164-L1189) returns no reference when none was retained or the actual created leg differs. That is useful cost evidence with honest missing values, but is not a benchmark of the book when the broker actually received every program order. The delayed-quote probe makes that distinction concrete. Missing program benchmarks are not presented as an invented zero cost or a new material defect. Exchange fees, correction events and lot accounting were not re-audited in this follow-up.

## Satisfied controls and coverage

| Surface / failure mode | Evidence and bounded conclusion |
| --- | --- |
| Partial entry fills during cancellation | The [resolver](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/broker/alpaca/clerk/sqlite/exit_resolution.py#L384-L425) proves the entry set terminal and refreshes exact entry identities before fixing reduction quantity. Selected partial-fill/cancel tests passed. |
| Terminal broker state with missing execution slices | [Finalization](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/broker/alpaca/clerk/sqlite/exit_resolution.py#L315-L348) holds uncertainty when cumulative broker fills exceed recorded slices; it does not infer flatness from a terminal acknowledgement alone. |
| Lost submit/cancel, absence, late reports and competing EXITs | Selected `test_reconcile.py` and `test_exit.py` cancellation, watchdog, duplicate/late and overlap cases passed; `test_exit_lost_submit_incident.py` passed, including durable fill evidence defeating an absent lookup. These are committed synthetic cases, not access to incident/runtime storage. |
| Interrupted ownership | [Claimed broker I/O](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/broker/alpaca/clerk/sqlite/claimed_broker_io.py#L20-L105) renews claims around I/O, represents cancelled transport as uncertain, and distinguishes loss after broker action. Its selected tests passed. No process-kill fault campaign was run. |
| Operator-confirmed reduction | `test_safe_flatten_execution.py` passed for expired plans, changed quantity, wrong side/leg, resumed bot, missing quote and session expiry. The new recovery-control probe also refuses market submission after close. |
| Automatic repricing with unavailable/stale/wide data | Recovery pricing tests passed. [Watchdog](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/broker/alpaca/clerk/sqlite/exit_watchdog.py#L259-L293) leaves the episode raised and sends nothing when no priceable reduction exists. This does not establish liveness if quotes remain unavailable. |
| Stable replay versus repricing | `test_exit_reducing_shape.py` passed, including deferred cancel shape persistence and identical replay. Preserving identity is a satisfied control; R1 concerns session eligibility before that order first reaches the broker. |
| Fill-quality evidence | Pricing/slippage tests and wrong-side replacement suppression passed. The Python projection computes metrics and preserves absent references; no comprehensive frontend contract/rendering test was run in this follow-up. |

## Reproduction and validation

Artifacts: [`test_reduction_validity.py`](../../research/codex-2419/test_reduction_validity.py), [`guarded_run.py`](../../research/codex-2419/guarded_run.py). The host virtualenv was read only. All source imports came from the isolated clone; tests used private SQLite files beneath its `tmp/`. The guard was installed before application imports and denies socket connections/DNS/binds, subprocesses, protected `.env`/`live_runs`/audit reads, main-checkout source reads and writes outside the review tree. It is an additional audit-hook guard, not a claim of an OS security boundary. Test initializers were inspected before execution. The dummy Polygon setting exists only to satisfy settings construction; no vendor calls were made.

From `/Users/inkant/codex-review-20260924/accounts/PythonDataService`, the common launcher was:

```sh
env -i PATH="$PATH" \
  HOME=/Users/inkant/codex-review-20260924/accounts \
  PYTHONDONTWRITEBYTECODE=1 POLYGON_API_KEY=review-placeholder \
  DATA_PLANE_CONTROL_SECRET='' PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  TMPDIR=/Users/inkant/codex-review-20260924/accounts/tmp \
  /Users/inkant/learn-ai/PythonDataService/.venv/bin/python -B \
  ../research/codex-2419/guarded_run.py -m pytest -p pytest_asyncio.plugin \
  --confcutdir=../research/codex-2419 --basetemp=../tmp/reduction-review \
  ../research/codex-2419/test_reduction_validity.py -q --tb=short
```

Result: **1 intended safety-invariant failure, 3 controls passed, 0.92 s**. The only failure is the normal program EXIT's after-close market submission. An earlier exploratory assertion demanding fresh quotes again at broker submission was replaced with a passing characterization after the explicit acceptance/session policy was read; it is not counted as a defect. A fixture numeric-type assertion was also corrected before this final run.

With the same sanitized launcher, omitting `--confcutdir` and using separate private basetemps:

```sh
--basetemp=../tmp/reduction-controls \
tests/broker/alpaca/clerk/test_recovery_reduction.py \
tests/broker/alpaca/clerk/sqlite/test_exit_reducing_shape.py \
tests/broker/alpaca/clerk/sqlite/test_safe_flatten_execution.py \
tests/broker/alpaca/clerk/sqlite/test_exit_lost_submit_incident.py \
tests/broker/alpaca/clerk/sqlite/test_claimed_broker_io.py -q --tb=short
```

Result: **100 passed, 1.97 s**.

```sh
--basetemp=../tmp/reduction-additional-controls \
tests/broker/alpaca/clerk/sqlite/test_reconcile.py \
tests/broker/alpaca/clerk/sqlite/test_exit.py \
-k 'watchdog or redrive or partial_fill_during_cancel or lost_cancel or overlapping_exit or late_and_duplicate' \
-q --tb=short
```

Result: **20 passed, 135 deselected, 1.20 s**. Passing selected tests establish the stated controls for those scenarios, not absence of every race.

## Follow-up boundary and owner question

1. **Owner decision needed for R1:** when a program's first reducing submission crosses into another session, should the system release it for current-session recovery or intentionally queue it for the next regular open? Recommended: explicit expiry plus the existing authorized recovery policy. Preserve exact status proof before replacing uncertain orders.
2. **Bounded implementation validation after that decision:** run a clock-controlled matrix across original decision, delayed cancellation proof, first submission, ambiguous submit and restart; vary all session boundaries and early close. Assert either one eligible reduction or one actionable refusal, never competing identities. This is a remediation follow-up, not another open-ended research pass.
3. **Still unexamined here:** execution corrections/busts and their effect on lot quantities, fee reconciliation and cost completeness; cross-process failure during persistence; transport/proxy/UI preservation of every recovery reason. These remain scoped questions, not findings. No A1–A3 duplicate, new provider recommendation or runtime observation is implied.
