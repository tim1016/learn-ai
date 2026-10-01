# Kill list — Frontend brokers, fleet, shell and lab specs (#2733)

Part of the lean-and-mean map (#2700). Plan, don't cut.

- **Read at:** `6a4d7d396108ef16471d8df888b9ded74d3c2892` (`origin/master` on 2026-09-30). The map was charted at `87b8e261`; the blocking kill lists (#2708, #2709) were read there. Every line number below is at `6a4d7d39`.
- **Area:** every `*.spec.ts` under `Frontend/src/app/components/brokers/`, `fleet/`, `shell/`, `components/research-lab/`, `components/data-lab/` and `components/strategy-lab/`. That is 117 files, about 23,900 lines and 1,099 `it` cases. `components/broker/` (singular) belongs to #2732.
- **Skipped as already owned by a dead-code list.** From #2708: `alpaca-desk/alpaca-account-activation.component.spec.ts` (whole file) and the `resource-target.spec.ts:57,59` line edits. From #2709: `data-lab/data-lab.auto-bar-timeframe.spec.ts` and `data-lab.auto-chunk-readout.spec.ts` (whole files, C19), the `consumeStale` / `serialize` cases in `data-lab-workspace-store.spec.ts` (C20), and the `formatChange` / `formatVolume` cases in `past-chain-inspector.component.spec.ts` (C21). None of the six routed-but-unlinked pages from the #2709 addendum has a spec in this area.

## How the evidence was gathered

All tools were throwaway scripts in my scratch folder. Nothing was added to the repo.

1. **Structure.** A TypeScript compiler-API script (it used the main checkout's `Frontend/node_modules/typescript`, read-only) listed every `describe` / `it` with its line range and every `expect(...)` chain: its subject, its matcher and its argument.
2. **Assertion shape.** Each `expect` was tagged as a mock-call check, a "component exists" check, a text check (a prose string or regex passed to `toContain` / `toBe` / `getByText` and similar), or a value/DOM/request check. `href`, input `.value` and router URL checks count as values. A test was flagged when all its assertions were text, all were mock calls, it only checked creation, or it asserted nothing.
3. **Whose words.** Each text literal was searched for in non-spec Frontend code. If it was found there, it is Frontend-authored copy. If it appears only in the spec, it is fixture data, usually backend-authored prose the component must show unchanged.
4. **Duplicates.** I compared test names across files, both exact and near matches. I then read pairs one layer apart: the settings page and its `configuration-*` children, the live banner and `fleet/account-workspace.ts`, the indicator-catalog failure hosts and `shared/indicator-picker`, and the research-lab section and detail pages.
5. **Reading.** I read the bodies of every flagged test and every duplicate pair before deciding.

### Where I drew the copy line

The ruling is that a spec pinning exact wording goes. A spec that proves a control exists, is enabled or disabled, or fires the right request stays. Many specs here prove a **state branch** whose only visible sign is a short label. Examples: "Attention unknown" rather than "All clear", "Mode unknown — assume real money" rather than a Paper badge, "Reading saved profiles…" rather than "No profiles". I kept those, because the outcome (unknown never reads as zero, a failure never reads as success, a fail-closed money warning) is what the operator sees. I cut tests whose subject is the copy itself: explanations, guidance, empty-state sentences, static headings, plural wording, and checks that retired wording stays absent. The same applies to backend-authored prose. A test proving that the backend's sentence reaches the screen in place of a client fallback is a data-binding test, and it stays.

## Kill list

Paths are relative to `Frontend/src/app/`. The line is where the `it` starts.

### Kind 2: copy and doc pinning

| # | Spec › describe › it | Kind | Evidence |
|---|---|---|---|
| 1 | `components/brokers/alpaca-desk/configuration/alpaca-settings-page.component.spec.ts` › AlpacaSettingsPageComponent › "says what an unavailable credential slot means and who can fix it" (`:743`) | 2 | It asserts only two Frontend sentences (`/No credential pair i…/`, `/this page cannot make it/`). No control or request is checked. Row 3 pins the same two sentences in the child. |
| 2 | same file › Settings sections (PRD #2560) › "says so when the profile in use saved no defaults" (`:1063`) | 2 | It asserts only the empty-state sentence "No defaults are set, …". |
| 3 | `…/configuration/configuration-revision-form.component.spec.ts` › "says an unavailable slot is a host deployment change, not a page action" (`:61`) | 2 | It makes the same two sentence assertions as row 1. |
| 4 | `…/configuration/configuration-status-panel.component.spec.ts` › "says no broker is bound when nothing has been applied" (`:61`) | 2 | It asserts only "No revision has been applied…" and "Nothing is staged.". |
| 5 | same file › "keeps profile Apply separate from Deploy" (`:99`) | 2 | It asserts one explanatory sentence (`/Applying a profile does not…/`). Nothing about Apply or Deploy behavior is checked. |
| 6 | `…/configuration/configuration-lifecycle-tracker.component.spec.ts` › "says Apply is recorded when the adopted selection says so" (`:101`) | 2 | It asserts only the sentences `/Apply is recorded/` and `/never from this browser/`. The tracker's state branches are proven by `:72`, `:84` and `:92`, which stay. |
| 7 | `…/configuration/configuration-account-evidence.component.spec.ts` › "says the broker reported nothing rather than rendering an empty list" (`:113`) | 2 | It asserts only the empty-state sentence "The broker reported no acco…". |
| 8 | `…/configuration/configuration-profile-list.component.spec.ts` › "says the list is empty rather than rendering an empty frame" (`:42`) | 2 | It asserts only the empty-state sentence `/No configuration profiles a…/`. |
| 9 | `…/configuration/configuration-switch-guide.component.spec.ts` › "shows the complete account-switch workflow and safety boundary" (`:8`) | 2 | It pins a heading, two safety sentences, a list length and the absence of a retired eyebrow label ("Every account switch", #2183). The same steps and boundary sentence are also asserted by `alpaca-settings-page.component.spec.ts` › "keeps the switch guide and its safety boundary when the desk state cannot be read" (`:314`), which stays because it proves a branch: the guide survives a failed desk read. |
| 10 | `components/brokers/alpaca-desk/alpaca-account-list-page.component.spec.ts` › "describes no lane mechanics and offers no surface chooser" (`:140`) | 2 | It makes four `queryByText(...).toBeNull()` checks for retired copy ("Trader view for holdings", "Binding generation", "Endpoint mode", …). No non-spec Frontend file contains those strings, so the test cannot fail unless someone types that exact copy back in. |
| 11 | `components/brokers/alpaca-desk/alpaca-account-card.component.spec.ts` › "renders account figures and a paper badge when loaded" (`:44`) | 2 | It asserts static field labels ("Equity", "Cash", "Buying power", "Updated (local)", "Portfolio value") and the "Paper" badge. It checks no figure. The Paper/Live branch is proven by "tags a live account as Live with danger severity, never a hardcoded Paper" (`:71`), which stays. |
| 12 | `components/brokers/alpaca-workspace/alpaca-account-workspace.component.spec.ts` › "carries no sync indicator — an out-of-sync account is a Home attention line" (`:359`) | 2 | It asserts only that `/^Sync/` and "Clean" are absent. That pins retired wording. |
| 13 | `shell/alpaca-live-banner.component.spec.ts` › "puts the one-thing wording in the singular" (`:659`) | 2 | It asserts one `aria-label` sentence, "1 thing on this account needs you.", which is a grammar pin. The count itself is proven by the banner's other attention cases. |
| 14 | `components/research-lab/strategy-runs/run-detail-page/walk-forward-section/walk-forward-section.component.spec.ts` › "shows empty-state copy when no walk-forwards exist" (`:153`) | 2 | Its name says "copy", and it asserts only "No walk-forwards yet". |
| 15 | `…/run-detail-page/baselines-section/baselines-section.component.spec.ts` › "shows empty-state copy when no baselines exist" (`:136`) | 2 | It asserts only "No baselines yet". |
| 16 | `…/run-detail-page/monte-carlo-section/monte-carlo-section.component.spec.ts` › "shows empty-state copy when no MCs exist" (`:141`) | 2 | It asserts only "No Monte Carlos yet". |
| 17 | `components/research-lab/news/news-page.component.spec.ts` › "says so plainly when nothing matched" (`:136`) | 2 | It asserts only the empty-state sentence "No articles matched this…". |
| 18 | `components/strategy-lab/run-stats/strategy-lab-run-stats.component.spec.ts` › "stacks the grade, headline metrics, supplementary statistics and evidence menu" (`:19`) | 2 | It asserts four static section headings ("Backtest Evidence Grade", "Returns", "More statistics", "Validation atlas"). It checks no value and no order. |
| 19 | `fleet/fleet-refusal-copy.spec.ts` › "returns the fallback copy for a known code" (`:84`) | 2 | It pins the full message and next-step sentences for one code (`clerk_unreachable`). The closed-map contract tests in the same file stay: every snapshot code has an entry, there are no orphans, no prose is blank, and outcomes are derived. |

### Kind 3: duplicates

| # | Spec › describe › it | Kind | Stronger spec that survives |
|---|---|---|---|
| 20 | `components/brokers/alpaca-desk/alpaca-account-card.component.spec.ts` › "renders the account status through the receiptLabel pipe" (`:64`) | 3 | It is a per-component copy of a receipt-label mapping ("Active"). It survives as `shared/pipes/receipt-label.pipe.spec.ts` › formatReceiptLabel › "formats underscore, dot, dash, and uppercase receipt identifiers as title case". |
| 21 | `components/brokers/alpaca-workspace/alpaca-surface-not-ready-tab.component.spec.ts` › "explains a lane without the Home capability, through receiptLabel" (`:63`) | 3 | It asserts a receipt label (`/Bot Panel Read/`) plus one fragment of the explanation (`/capability,/`). The label survives in the same pipe spec as row 20. The tab's state branches stay: `:40` (no confirmed binding) and `:84` (a servable lane links to Home). |
| 22 | `components/brokers/alpaca-desk/configuration/configuration-account-evidence.component.spec.ts` › "approves only an account the broker was observed to reach" (`:39`) | 3 | The child only emits `pinRequested('PA3ZK9QWERTY')`. It survives as `alpaca-settings-page.component.spec.ts` › AlpacaSettingsPageComponent › "approves only an account the broker was observed to reach" (`:902`). That test drives verify → approve through the real child and asserts the exact `pinAccount(clerk, 'profile-1', 1, 'PA3ZK9QWERTY')` request. The "only observed" half survives in the child's own "offers no way to type an account id" (`:28`). |

### Kind 1: trivial

| # | Spec › describe › it | Kind | Evidence |
|---|---|---|---|
| 23 | `components/research-lab/feature-runner/feature-runner.component.spec.ts` › "should create" (`:101`) | 1 | `expect(component).toBeTruthy()` and nothing else. Every other case in the file renders the component. |
| 24 | `components/research-lab/research-lab.routes.spec.ts` (whole file: "registers the Recency Chart page in the Backtests navigation", "registers the Ticker News page in the Market navigation") | 1 | It restates the static `researchLabRoutes` / `RESEARCH_LAB_NAV` literals: the path, `data.title`, the nav label, and the `loadComponent` target. That is route configuration, which `.claude/rules/testing.md` lists under "What NOT to test". The AOT build already proves the lazy import resolves. |
| 25 | `shell/app-menu.spec.ts` › "omits the retired Indicator Report and Design Lab surfaces" (`:158`) | 1 | It makes three `not.toContain` checks on the static `APP_MENU` constant for labels and routes that no code produces. The menu's real logic (active group and item, page titles, deploy-intent and workspace highlighting) is proven by the other 14 cases. |
| 26 | `fleet/operation-url.spec.ts` › "no service reaches an unscoped broker path that the catalog does not declare" (`:97`) | 1 | It reads `services/brokers.service.ts` from disk and asserts that the string `'order-groups'` is absent. Its one caller was deleted in #2103. The guard cannot fail unless that literal is typed back in, and the catalog-resolution case (`:73`) is the real contract. |

**Kind 4 (mock theater): none found.** All 38 mock-call-only tests assert the request a user action fires, with its arguments: the binding generation and routing epoch, `parent_run_id` filters, the cursor and filters, router navigation targets, and "no read while unknown". That is the behavior the ruling keeps.

**Kind 5 (retired features):** owned by #2708 and #2709, and skipped as listed above.

## What the cuts orphan

- **No fixture, mock or test helper.** Every helper the cut tests use is also used by surviving tests in the same file: `FakeConfigurationService`, `renderPage`, `deskState`, `fleet-directory-testing.ts` and the `*-section` builders. Test-only helpers follow their tests.
- **Imports, which lint will catch:**
  - Row 26 leaves `readFileSync` (`node:fs`) and `join` (`node:path`) unused in `fleet/operation-url.spec.ts:1-2`.
  - Row 22 may leave `vi` / `userEvent` unused in `configuration-account-evidence.component.spec.ts`.
  - Row 24 deletes a whole file, together with its imports of `RESEARCH_LAB_NAV`, `NewsPageComponent` and `RecencyChartPageComponent`. Those stay live through the routes.
- **`fleet/node-builtins.d.ts` stays.** After row 26 its only consumer is `fleet/lane-fence-freeze.contract.spec.ts`, which is kept (see below). #2708's pointer said the same.

## Hazards the cutting PR must carry

- **H1. Delete whole `it` blocks only.** Several surviving tests mix wording lines with behavior assertions. Examples: `alpaca-settings-page` `:314`, `alpaca-lane-card` "keeps a failed money read to itself and names the next step" (`:185`), `custody-divergence` `:22` (receipt labels plus backend prose), and `news-page` "labels sentiment as vendor-asserted rather than derived" (`:81`). The map rules out rewriting surviving tests, so these stay whole.
- **H2. Lint and the full suite.** Run `npx eslint Frontend/src/ --max-warnings 0` for the orphaned imports above. Then run the full `ng test`, which applies the AOT template compiler, as #2708's H3 asks.
- **H3. The copy line is a reading.** Tests that prove a state branch through a short label were kept (see "Where I drew the copy line"). Examples are the lane card's unknown counts, the workspace's fail-closed mode wording, the banner's `is-undetermined` cases, `strategy-lab-run-stats` "Not charged", and the three `alpaca-deploy-tab` "explains in place" gates. If the map reads kind 2 more strictly, they are the next candidates. The deploy-tab three are kept partly under "on the money path, when unsure, keep": they are the only proof that each of three conditions refuses Deploy.
- **H4. `/jobs-demo` in a surviving menu test.** `shell/app-menu.spec.ts` › "highlights nothing for a route outside the menu" (`:115`) uses `'/jobs-demo'` as an arbitrary URL that is not in the menu. #2709 cuts that route (B3). The test stays valid, because `activeMenuNodeFor` takes any string. A cutting PR that greps for `jobs-demo` should not treat this hit as a caller.
- **H5. Money path.** No row removes a test that proves an order, custody, flatten, budget, kill-switch, lease or fencing outcome. The money-path specs in this area keep every request, fence and refusal test: the order-entry and SQLite custody generation/epoch fencing, `open-lane-fence`, `lane-fence`, `lane-fence-freeze.contract`, the budget-authority confirm flow, and live graduation. Row 22 drops only a child-level emit that the page-level pin request already covers.
- **H6. Re-check at the cutting SHA.** The account-workspace slices (#2560) are still landing in `components/brokers/`. Re-run the line numbers before cutting.

## Considered and kept

- **`fleet/fleet-directory-testing.spec.ts`.** It tests a test double, not app code. I kept it because its header explains that the double must be able to rebind, or "every freeze-at-open test [is] unfalsifiable". It guards the evidence for the money-path fence tests.
- **The live banner's six workspace-routing cases against `fleet/account-workspace.spec.ts`** (for example banner `:553` and function `:233`, "lands on the chosen account's Home from a bot's page"). The banner cases prove that the banner parses its own URL and renders the `href`. The function cases cover more branches for a function with more than one caller. Both layers earn their place.
- **The indicator-catalog failure cases in four hosts**: `explore` `:427`, `strategy-lab-chart` `:300`, `feature-runner` `:185` and `signal-runner` `:80`, plus their "no failure" twins. These are not duplicates of `shared/indicator-picker/indicator-picker.component.spec.ts:197`. The picker only shows what its `loadFailed` input says, so each host test proves that host wires its own catalog failure into it.
- **The seven `"surfaces service errors"` cases** across the research-lab detail and section pages, and the paired `signal-history` / `experiment-history` cases. Each tests its own component's error path or picker wiring.
- **The `configuration-switch-guide` cases `:22` and `:40`, against `alpaca-settings-page` `:304` and `:329`.** The child asserts the `<code>` element and the copy button. The page asserts the desk-to-guide wiring. Each covers something the other does not.
- **`data-lab-ingress.spec.ts`.** Its text-looking assertions are redirect targets (`'/data-lab/validate'`), so they are behavior.
- **Every axe case** (`"has no detectable accessibility violations…"`). AXE is a CLAUDE.md requirement, and these are its only gate.
- **Backend-prose rendering tests**: the SQLite custody refusal and recovery copy, the `custody-divergence` explanation and evidence refs, the settings restart command and guidance, and the status-panel refusal reason. They prove that the backend's words reach the screen in place of a client fallback, which is a CLAUDE.md rule.

## Pointers outside this area

- **#2732 (`components/broker/**` specs) and #2734 (the rest of the Frontend specs).** Per-component receipt-label copies (rows 20–21's pattern) and empty-state sentence pins (rows 14–17's pattern) are likely to recur there. `shared/indicator-picker`, `shared/trading-chart` and `shared/pipes` specs are #2734's.
- **#2716 (CI checks).** Nothing here. The specs in this area run in the ordinary `ng test` gate.

## Not reviewed

- **Unflagged tests, about 810 of the 1,099.** These assert values, DOM state, controls or requests. I screened them by assertion shape, by name patterns (`calls`, `delegates`, `emits`, `should`, `creates` and similar) and for single-existence checks, but did not read each one. Within-file duplicates among them were not systematically searched.
- **Mixed tests** (mostly wording plus some behavior, 71 cases). They are kept whole under the no-rewrite rule and were not read one by one.
- **Layer pairs not compared:**
  - `alpaca-home.component.spec.ts` against `home-*` children
  - `strategy-lab.component.spec.ts` against `strategy-lab-runner.service.spec.ts`, `strategy-lab-config-rail` and `strategy-lab-run-report.service.spec.ts`
  - `data-lab.component.spec.ts` against `data-lab-workspace-store.spec.ts` and `data-lab-request-mapper.spec.ts`
  - `alpaca-activity-page` against `alpaca-trader-activity-table`
  - `alpaca-history-page` against the `bot-history.service` spec, which is outside this area
- **Fixture-data text tests** (58 cases whose literals appear only in the spec). I treated them as data binding. I did not check each one for a receipt-label value hiding among the fixture strings.
