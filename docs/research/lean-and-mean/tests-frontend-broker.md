# Kill list — Frontend broker specs (#2732)

Part of the lean-and-mean map (#2700). Plan, don't cut.

- **Read at:** `6a4d7d396108ef16471d8df888b9ded74d3c2892` (`origin/master`, 2026-09-30). `git diff 87b8e261 6a4d7d39 -- Frontend/src/app/components/broker` is empty, so every line number here also holds at the map's charting SHA and matches #2708's.
- **Area:** `Frontend/src/app/components/broker/**/*.spec.ts`. That is 65 files, 18,125 lines, 667 `it`/`test` blocks (30 of them `.each`) and 1,927 `expect` calls.
- **Blocked on:** #2708 (`dead-frontend-broker`). Everything it lists is skipped here, its owner addendum included (see "Owned by #2708").

## How the evidence was gathered

Throwaway Node scripts were kept in `$TMPDIR/tests-frontend-broker/`. Nothing was added to the repo.

1. **Inventory.** For every `it`, the script recorded its describe path and its `expect` lines, and tagged each `expect` as one of four kinds: request/mock call (R), control/role/attribute (C), rendered text (T) or plain value (V). Each test's tag profile was then read against its name.
2. **Copy pins.** For the 118 tests whose every assertion is on rendered text, and for every text-heavy test with no request assertion, I read the `expect` lines. I then sorted each test three ways:
   - **Component-authored wording.** A label, sentence, or empty, loading or unknown message that the template writes. These are copy pins.
   - **Data passing through.** A backend or fixture value: an amount, a server sentence, a hash or id.
   - **A contrast.** The decisive assertion is that a false claim is absent, for example that unavailable is not shown as zero.
3. **Negative assertions that cannot fire.** Every literal in `queryByText(...).toBeNull()` and `not.toContain(...)` was searched for across non-spec `Frontend/src` and `PythonDataService/app`. Regex negatives were checked by hand with `grep`.
4. **Duplicates.** Each component test was compared with the layer above (`bot-panel-shell`, `alpaca-deploy-workflow`) and the layer below (pure `lib/` functions, the shared `receiptLabel` pipe, the Python vocabulary test). I read the stronger test's assertions before naming it.
5. **Mock theater and call order.** I listed every test whose only assertions are bare mock-call counts, and every `invocationCallOrder` use.

## Bar as applied

- **Kind 2 (copy pin)** means every assertion of the test is on component-authored wording. Rewording the template breaks the test. A data, control, request or state change does not.
- **These stay:**
  - tests asserting a backend value is shown, which is data flow;
  - tests asserting a control exists, is enabled, takes focus or sends a request;
  - contrast tests, such as "unavailable is not zero", "unknown is not failure" or "no order was sent", even when they observe through text.

  Wording lines inside a surviving test are not rows, because surviving tests are not rewritten.
- **Receipt labels.** `Frontend/src/app/shared/pipes/receipt-label.pipe.spec.ts` is the one place their wording is proven. A component test whose only point is "this code renders through the pipe" is a duplicate of it.
- **Money path.** The flatten, cohort-flatten, extended-hours ticket, Deploy key, budget, bot-end and arm/stop tests all stay, except the wording or duplicate rows below. Each of those rows names a stronger test that keeps the outcome.

## Kill list

Paths are relative to `Frontend/src/app/components/broker/`. `L` is the `it` line.

### Copy pinning (kind 2)

| # | Spec › test | Kind | Evidence |
|---|---|---|---|
| 1 | `v2-panel/instrument-quote/instrument-quote.component.spec.ts` › L27 "keeps the source label when the price is unavailable" | 2 | It asserts only the symbol text and the fixed label `'IBKR · last bar'`. The label with a price is already proven by L7 in the same file. |
| 2 | `v2-panel/bot-page/recent-decisions-list/recent-decisions-list.component.spec.ts` › L35 "renders a mode-neutral empty state when no decision is recorded yet" | 2 | It asserts the sentence `'No decisions recorded yet.'`, the absence of the word "simulated", and that no table is drawn for an empty list. |
| 3 | `shared/next-attempt/next-attempt.component.spec.ts` › L35 "shows original exposure age separately from the latest check" | 2 | It asserts only two label regexes, `/Position still open since/` and `/Last checked/`. |
| 4 | same file › L44 "shows an unreadable record as unknown" | 2 | It asserts one sentence, `/Recovery status is unknown/`. |
| 5 | `v2-panel/operator-lens/health-card.component.spec.ts` › L39 "names the interval history did not return when the startup join was refused" | 2 | It asserts the label `'History missing'` plus the no-countdown line. The no-countdown line is already proven by L32 in the same file. |
| 6 | `fee-attribution/fee-attribution.component.spec.ts` › L86 "says a period had no fees only when the fee record is known" | 2 | It asserts one empty-state sentence, `'No fees were charged in this period.'`. The contrast, that unavailable is not "no fees", stays in L80. |
| 7 | `v2-panel/bot-banner/bot-banner-run-timing.component.spec.ts` › L92 "names a refresh failure distinctly from an initial load failure" | 2 | It asserts one sentence, `/Run timing could not be refreshed\./`. The retry behavior stays in L77. |
| 8 | `v2-panel/bot-page/trades-today-list.component.spec.ts` › L25 "shows no-trades message when fills are empty" | 2 | It asserts the sentence `'No fills today.'` and that no table is drawn for an empty list. The contrast stays in L38. See hazard H2. |
| 9 | same file › L53 "explains when known fills are outside the chart window" | 2 | It asserts one sentence, `'Fill details are outside the current chart window.'`. |
| 10 | `v2-panel/operator-lens/operator-readiness.component.spec.ts` › L57 "says so when the backend reports no checks" | 2 | It asserts one sentence, `'No checks reported for this bot.'`. |
| 11 | `v2-panel/operator-lens/transaction-rail.component.spec.ts` › L198 "not-applicable station uses — icon and N/A text" | 2 | It asserts only the glyph `'—'` and the label `'N/A'`. Icon-plus-text for accessibility stays in L26 and L219. |
| 12 | same file › L211 "null transaction_ref shows no-transaction message" | 2 | It asserts one regex, `/no active transaction/i`. |
| 13 | `deployment-budget/deployment-budget.component.spec.ts` › L274 "says there is no current price when the open gain or loss is unknown" | 2 | It asserts one sentence, `'Open gain or loss on shares: no current price.'`. |
| 14 | same file › L311 "says it is reading while the money is on its way" | 2 | It asserts one loading sentence, `"Reading this bot's money…"`. |
| 15 | `v2-panel/bot-page/bot-details.component.spec.ts` › L254 "says so when a bot recorded no exit terms" | 2 | It asserts the summary `'Exit terms · none recorded'` and one sentence. |
| 16 | same file › L299 "reports every connection healthy as all connected" | 2 | It asserts one summary string, `'Connections · all connected'`. The per-connection state stays in L274. |
| 17 | same file › L364 "says so when no program was sealed" | 2 | It asserts one sentence, `'No sealed program was recorded for this bot.'`. |
| 18 | `v2-panel/cohort-flatten/cohort-flatten-drawer.component.spec.ts` › L226 "says so plainly when the account has no multi-bot cohort" | 2 | It asserts one regex, `/No cohort on this account/`. The money-path contrast, that a read failure is not "no cohorts", stays in L216. See hazard H2. |
| 19 | `broker-deploy-page/deploy-money-step.component.spec.ts` › L260 "says in plain words what the budget limits, and never in internal terms" | 2 | It asserts `note.textContent).toBe(<exact sentence>)` and `not.toMatch(/admission/i)`. It is a wording rule only. |
| 20 | `broker-deploy-page/alpaca-deploy-workflow.component.spec.ts` › L1002 "never words an unknown outcome in internal terms" | 2 | It asserts only `not.toMatch(/control boundary\|data.plane\|SQLite\|lens/i)` on the alert. The unknown-outcome behavior stays in L823 and L865. |

### Duplicates (kind 3)

| # | Spec › test | Kind | Evidence: the stronger survivor |
|---|---|---|---|
| 21 | `v2-panel/lib/broker-v2-copy-contract.spec.ts` (whole file, 3 tests) | 3 | `PythonDataService/tests/broker/v2panel/test_vocabulary_snapshot.py` › `test_every_emitted_code_has_nontrivial_copy` is stronger: it requires a non-empty label and explanation, and an explanation longer than its code. `test_python_and_frontend_snapshots_are_byte_identical` and CI job `broker-v2-vocabulary-contract` (`.github/workflows/ci.yml:276-313`) prove the Frontend copy equals the Python one. This spec re-reads the same JSON for a weaker property. See hazard H3. |
| 22 | `v2-panel/bot-run-history/operator-run-history.component.spec.ts` (whole file, 1 test) | 3 | `v2-panel/panel-shell/bot-panel-shell.component.spec.ts` › L1705 "shows the current run under Runs and sends earlier runs to History, narrowed to this bot" asserts the same `History` href (`…/history?account=…&bot=…`) through the mounted shell, and also checks the current-run read. |
| 23 | `v2-panel/bot-run-history/bot-current-run.component.spec.ts` › L111 "sends earlier runs to History, narrowed to this bot, instead of paging through them one at a time" | 3 | Same survivor as row 22. It also asserts the `'Previous Runs'` button is absent. |
| 24 | `v2-panel/instrument-quote/panel-instrument-quote.component.spec.ts` (whole file, 1 test) | 3 | `bot-panel-shell.component.spec.ts` › L1675 "prices the tape from the last IBKR bar and never reads a Polygon snapshot (H15)" asserts the price, the `'IBKR · last bar'` label and no Polygon text in the mounted tape. |
| 25 | `v2-panel/panel-shell/bot-panel-shell.component.spec.ts` › the workspace tab this page belongs to › L758 "names the bot in its header" | 3 | `v2-panel/bot-banner/bot-banner.component.spec.ts` › L67 "names the bot by its id, with its strategy, symbol, the way back and its freshness" asserts the same level-2 heading and more. This test also checks that `app-bot-banner` is mounted, which is trivial. |
| 26 | same file › the one view (#2563) › L2373 "marks a Dry Run bot as simulated cash" | 3 | `bot-banner.component.spec.ts` › L114 "marks a Dry Run bot as simulated cash, never with the lane world (H23)" asserts the same chip and also that neither lane world is shown. |
| 27 | same file › L2000 "renders a receiptLabel-formatted reason_code when the backend sends no why prose" | 3 | `v2-panel/lib/panel-action-outcome.spec.ts` › L49 "falls back to a receiptLabel-formatted reason_code when why is absent" proves the fallback. The shell wiring stays proven by L1946 "renders backend-authored remediation for an unknown action outcome". |
| 28 | `v2-panel/panel-shell/bot-panel-shell.history-failure.spec.ts` › L514 "renders the unavailable state for a zero-bar response carrying an unrecognised notice code (coordinator_unavailable, #2204)" | 3 | `v2-panel/lib/chart-history-notice.spec.ts` › L64 "defaults fail-loud for a code it has never seen (coordinator_unavailable, #2204)". The shell wiring stays proven by history-failure L477 (`polygon_api_key_missing` with a working Retry) and L346. |
| 29 | same file › L534 "still renders \"No candles in this window\" for a zero-bar response with no notices" | 3 | `chart-history-notice.spec.ts` › L46 "returns null for a zero-bar response with no notices (genuinely empty)". |
| 30 | same file › L547 "treats polygon_overlay_empty as genuinely empty, not unavailable" | 3 | `chart-history-notice.spec.ts` › L50 "returns null for a zero-bar response whose only notice is polygon_overlay_empty". |
| 31 | `v2-panel/bot-banner/bot-banner.component.spec.ts` › L221 "shows the run timing and the strategy clocks" | 3 | `bot-banner-run-timing.component.spec.ts` › L51 "shows Started/Ended with the last strategy bar and last decision, and a stale flag" asserts the same two clocks and `Last decision`, plus the stale flag. |
| 32 | `v2-panel/operator-lens/transaction-evidence-timeline.component.spec.ts` › L164 "renders a code-like custody_owner through receiptLabel, not raw" | 3 | `Frontend/src/app/shared/pipes/receipt-label.pipe.spec.ts`. The pipe test is the one place wording is proven. |
| 33 | `account-desk/account-desk-transaction-history.component.spec.ts` › L337 "renders transaction origins through the shared receipt label and keeps timestamps as milliseconds" | 3 | `receipt-label.pipe.spec.ts`. Its only assertion is `getByText('Force Flat')`. The "milliseconds" half asserts nothing; that is proven by L345 in the same file. |
| 34 | `v2-panel/bot-page/flatten-sequence.spec.ts` › L150 "names a Dry Run's check by its simulated account, never Alpaca" | 3 | `bot-panel-shell.component.spec.ts` › the one view › L2476 "checks and sells a Dry Run's shares in its simulated account, never at Alpaca" asserts `'Check the position in its simulated account: Done'`, no `Alpaca`, and the commands sent. |
| 35 | `broker-deploy-page/deploy-launch-receipt.component.spec.ts` › L53 "names the bot a Deploy again replaces" | 3 | `alpaca-deploy-workflow.component.spec.ts` › L1341 "names the replaced bot on the receipt" makes the same assertion (`Replaces` → `spy-ema-20260925-1402`) after a real Deploy-again flow. |
| 36 | `shared/typed-halt-confirm/typed-halt-confirm.component.spec.ts` › L57 "renders the dialog when open is true" | 3 | `v2-panel/panel-action-button/panel-action-button.component.spec.ts` › L100 "renders backend-presented confirmation and blockers" opens the confirmation from a real click and asserts the backend copy. Every other test in this file also renders the open dialog. |
| 37 | `v2-panel/bot-page/bot-end-card.component.spec.ts` › L227 "holds Change still while another command on this bot is on its way" | 3 | `bot-panel-shell.component.spec.ts` › the bot's end (#2607) › L2247 "holds Change still while a command is on its way, so a change of end never races it" proves the shell sets the hold during a real command. L2260 proves no command is sent. |

### Trivial (kind 1)

| # | Spec › test | Kind | Evidence |
|---|---|---|---|
| 38 | `v2-panel/operator-lens/transaction-rail.component.spec.ts` › L55 "satisfied station has station--satisfied CSS class" | 1 | Its only assertion is `querySelector('.station--satisfied')` not null. That is exact CSS. |
| 39 | same file › L68 "blocked station has station--blocked CSS class" | 1 | Same, for `.station--blocked`. |
| 40 | `v2-panel/bot-page/trades-today-list.component.spec.ts` › L80 "never claims fees are unreported under the fills (H26)" | 1 | It cannot fail. `/Fees not reported/i` matches nothing in non-spec `Frontend/src`. Its other assertion, the `Fills today` table, repeats L92. |
| 41 | `shared/typed-halt-confirm/typed-halt-confirm.component.spec.ts` › plain confirm mode › L211 "renders the supplied confirm label" | 1 | It is an input pass-through: the button text equals the `confirmLabel` input. |

No kind 4 (mock theater) and no kind 5 rows; kind 5 belongs to #2708. Every test whose only assertions are mock-call counts was checked, among them L264 "polls after a stream error…", L307 "collapses a burst of keystrokes into one scoped fetch", L1761 and L2260. Each asserts a request count or a request that was not sent, which is behavior.

## Owned by #2708 (skipped here)

These are listed so the cutting PR does not cut them twice. The rows are #2708's.

- `gallery-live-store.service.spec.ts`: the three row-4 cases (L126, L284, L406). Per the owner addendum, `bots()` and `status()` stay, and so do the seven transport cases that read through them.
- `candle-renderer.spec.ts`: `describe('barIndexAtX')` and `describe('layoutTag')`, plus L223 and the hover half of L201.
- `fee-attribution.component.spec.ts` L45.
- `format.spec.ts`: `describe('fmtDurationRemaining')`, `describe('fmtElapsedSince')` and `describe('diffBps and toleranceBand…')`.
- `broker-v2-panel.service.spec.ts` L556. It is conditional, row 21 there.
- Line-level edits from #2708 H2 in this area:
  - the `__TEST__` import in `broker-options-surface.component.spec.ts`;
  - `account-desk-transaction-history-store.service.spec.ts:85`;
  - the `getLiveChart` mock key at `bot-panel-shell.component.spec.ts:548`;
  - the `ReadinessCheckView` imports at `bot-details.component.spec.ts:11` and `operator-readiness.component.spec.ts:11`.

## What the cuts orphan

- **`v2-panel/lib/broker-v2-vocabulary.snapshot.json`.** Row 21 removes its only Frontend importer. Today the Frontend copy feeds only that spec and the CI byte-identical comparison. Whether to keep the Frontend copy is a gate question; see the pointers below and hazard H3.
- **`operator-run-history.component.spec.ts` and `panel-instrument-quote.component.spec.ts`.** Rows 22 and 24 delete both files. Their fixtures (`NOT_RECORDED`, local `render` setup) are file-local. The two components stay live: they are rendered by the shell and still covered there.
- **Unused imports.** After the cuts, the cutting PR must drop imports in these specs:
  - `formatTimestampDisplay` in `bot-banner.component.spec.ts`, if row 31 was its last user;
  - `formatReceiptLabel` and similar helpers in the edited specs, wherever they lose their last user.

  `npx eslint Frontend/src/ --max-warnings 0` catches these.
- No shared test helper, fixture file or `*-testing.ts` support file in the area loses its last user. `alpaca-deploy-workflow.fixtures.ts` and `fleet/fleet-directory-testing.ts` keep many surviving users.

## Hazards the cutting PR must carry

- **H1. Run the full `ng test`.** These cuts are spec-only, but rows 21, 22 and 24 delete whole files. A file-scoped run would not notice a sibling spec importing from them. Then run project-scope ESLint.
- **H2. Surviving negatives lose their positive anchor.** Two surviving contrast tests assert that a sentence is absent, and the test that rendered that sentence goes:
  - `trades-today-list` L38 still asserts that `'No fills today.'` is absent, after row 8 goes.
  - `cohort-flatten-drawer` L216 still asserts that `/No cohort on this account/` is absent, after row 18 goes.

  Both still fire if the false claim comes back word for word, but go vacuous on a reword. That is accepted under the copy-pin ruling. Do not delete the contrast tests.
- **H3. Vocabulary snapshot gate.** Row 21 is safe only while CI job `broker-v2-vocabulary-contract` and `test_vocabulary_snapshot.py` stay. If #2716 or the CI-shape ticket (#2738) ever drops that job, re-check this row.
- **H4. Money-path files.** Rows 18, 26, 27, 34, 35 and 37 sit in flatten, Deploy and bot-end specs. Each names a surviving test that keeps the outcome: a flatten sent or refused, a command not sent, a key reused. No order, flatten, arm, budget or fencing outcome loses its only test.
- **H5. Re-check before cutting.** The account-workspace slices (#2560) are still landing in `broker-deploy-page/` and `v2-panel/bot-page/`. Re-verify every row, and each named survivor, at the cutting SHA.

## Considered and kept

- **The H30 call-order check** in `bot-panel-shell` › the one view › L2388 "flattens on one confirmation: reconcile, fresh panel, checked plan, then send" (`invocationCallOrder`, `:2413-2418`). Here the order is the safety outcome: no send before reconcile and a fresh plan. The test also proves the flatten is sent. Partial edits are out of scope, so it stays.
- **Contrast tests observed through text.** These stay because each proves a false claim is absent:
  - unavailable is not zero: `bot-current-run` L140, `trades-today-list` L38, `fee-attribution` L80, `dual-pane-chart.indicator-catalog-failure` L126;
  - unknown is not failure: shell L2039;
  - no order was sent: shell L1515;
  - every estimate and shortfall test in `money-bar.component.spec.ts`, where the arming budget is drawn.
- **Server prose rendered verbatim**, such as `deploy-money-step` L271 and L283 or `bot-end-card` L237 and L259. They prove the UI shows the backend's sentence instead of its own. The parse layer (`operation-error.spec.ts`, `panel-action-outcome.spec.ts`) does not prove that wiring.
- **The precedence tests in `panel-action-outcome.spec.ts`**: the why, then `reason_code`, then `reason` fallbacks. They assert receiptLabel-formatted strings, but what they prove is which field wins. That is logic, not a copy of the pipe.
- **The AXE tests** in the area. Accessibility is a user outcome, and CLAUDE.md requires AXE.
- **"Does not throw" edge cases in `candle-renderer.spec.ts`** (L167, L174, L183). An empty, single-bar or flat series must not crash the Home sparkline.
- **`resource-value-guard.contract.spec.ts`.** It is a source scanner guarding a crash class (#2202), with self-tests proving the scan can fire. It is not a CI gate, so it stays with the tests.
- **`cohort-flatten-confirmation.spec.ts` L103.** It pins an exact blast-radius sentence, but every word in it is a backend fact the operator confirms before a flatten. It is kept on the money path.

## Pointers outside this area

- **#2716 (CI checks) / #2738 (CI shape).** After row 21, `broker-v2-vocabulary.snapshot.json` in the Frontend has no Frontend reader. The CI step comparing it byte-for-byte with the Python copy (`ci.yml:297-313`) then guards a file nothing in the Frontend uses. Either the Frontend copy and that half of the step go, or a Frontend reader is intended. Decide there.
- **#2733 (brokers, fleet, shell, lab specs).** The same copy-pin pattern, an empty, loading or unknown sentence as a test's only assertion, is likely in `components/brokers/**` and `fleet/**`. It was not checked here.

## Not reviewed

- **The four largest files.** These were judged by name and by assertion-kind profile, not line by line: `alpaca-deploy-workflow.component.spec.ts` (75 tests), `bot-panel-shell.component.spec.ts` (55), `dual-pane-chart.component.spec.ts` (30) and `cohort-flatten-drawer.component.spec.ts` (27). That covers the tests mixing request or control assertions with text. Every test in them whose assertions are all text was read. A duplicate hidden between two mixed tests in one of those files could be missed.
- **Cross-file duplicates within one layer**, such as two `alpaca-deploy-workflow.*.spec.ts` files proving the same Deploy-key reuse, were only spot-checked.
- **Python-side duplicates** of Frontend contract tests, beyond the vocabulary snapshot (row 21). Example: `operation-url` against the fleet operation catalog test.
