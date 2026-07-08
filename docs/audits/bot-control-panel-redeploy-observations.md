# Bot Control Panel Redeploy Observations

## 2026-07-07T16:12:10Z - dep_val_smoke_002

- Bot: `dep_val_smoke_002`
- Candidate reason: Different from `Bars-July-6`; fleet list showed `BLOCKED`, flat exposure, no live runtime.
- Action taken: Opened `/broker/bots`, selected `dep_val_smoke_002`, used `Fresh run`, verified IBKR paper account `DUM284968`, `paper_orders`, `safe_canary`, `Start trading immediately`, and added required action plan `leg_1 long SPY x 1` with `CLOSE leg_1`.
- Run id: None created during this attempt.
- Outcome category: `blocked-by-safety`
- Outcome: `Deploy & start` was blocked before creating a new run. No order/fill observed.
- Exact UI error: `Deploy — blocked dep_val_smoke_002 is durably STOPPED. Resume the bot to clear the stop latch before starting or using Deploy & start. Use Resume to set desired_state=RUNNING, then start the bot. Use Deploy only when you want to stage a new run without starting it.`
- Related UI state: On the bot detail page, `Resume trading` was disabled with `Broker safety is unknown. The backend cannot prove the broker is paper-only. Resume is held until broker safety is proven.`
- UX recommendation: Avoid sending the operator into a dead end. If `Deploy & start` requires clearing a durable stop latch, the deploy form should surface that before submit and provide a single guided recovery path. Either enable a safe `Resume + deploy/start` path once the global IBKR paper session is proven, or explain why per-bot broker proof is missing and what action will create it. Also keep blocking empty `deployment_validation` action plans before submit; this form still allowed a ready deploy state while the action plan was initially empty.

## 2026-07-07T16:42:10Z - DIagVal6

- Bot: `DIagVal6`
- Candidate reason: Different from `dep_val_smoke_002` and `Bars-July-6`; fleet list showed `BLOCKED`, flat exposure, `Fresh run only`, no live runtime, and 3 errors.
- Action taken: Opened `/broker/bots`, selected `DIagVal6`, used the detail-page `Fresh run` action, verified IBKR paper account `DUM284968`, `paper_orders`, `safe_canary`, `Start trading immediately`, then added required action plan `leg_1 long SPY x 1` with `CLOSE leg_1`.
- Run id: None created during this attempt.
- Outcome category: `blocked-by-safety`
- Outcome: `Deploy & start` was blocked before creating a new run. No order/fill observed.
- Exact UI error: `Deploy — blocked DIagVal6 is durably STOPPED. Resume the bot to clear the stop latch before starting or using Deploy & start. Use Resume to set desired_state=RUNNING, then start the bot. Use Deploy only when you want to stage a new run without starting it.`
- Related UI state: The bot table and detail controls now clearly showed `Fresh run only`. On the detail page, `Start bot process`, `Resume trading`, `Pause trading`, `Flatten and pause`, `Stop bot`, and `Mark poisoned` were disabled; `Fresh run` was enabled. `Resume trading` still explained `Broker safety is unknown. The backend cannot prove the broker is paper-only. Resume is held until broker safety is proven.`
- UX recommendation: The new table/detail `Fresh run only` flag is a real improvement, but the deploy flow still sends the operator into a submit-time dead end. When a Fresh run inherits a durably STOPPED instance and `Start trading immediately` is checked, the deploy form should disable `Deploy & start` or convert the call-to-action to `Deploy only` with copy explaining that Resume must clear the latch first. It should also keep treating an empty deployment-validation action plan as incomplete; before manual leg entry, this form still showed `Ready to deploy` while `ON ENTER` and `ON EXIT` were empty.

## 2026-07-07T17:12:10Z - deiagAPPL6

- Bot: `deiagAPPL6`
- Candidate reason: Different from `DIagVal6`, `dep_val_smoke_002`, and `Bars-July-6`; fleet list showed `BLOCKED`, flat exposure, `Fresh run only`, and last run `Exited with error`.
- Action taken: Opened `/broker/bots`, selected `deiagAPPL6`, used the detail-page `Fresh run` action, verified IBKR paper account `DUM284968`, `paper_orders`, `safe_canary`, `Start trading immediately`, then added required action plan `leg_1 long AAPL x 1` with `CLOSE leg_1`.
- Run id: None created during this attempt.
- Outcome category: `blocked-by-safety`
- Outcome: `Deploy & start` was blocked before creating a new run. No order/fill observed.
- Exact UI error: `Deploy — blockeddeiagAPPL6 is durably STOPPED. Resume the bot to clear the stop latch before starting or using Deploy & start. Use Resume to set desired_state=RUNNING, then start the bot. Use Deploy only when you want to stage a new run without starting it.`
- Related UI state: The bot table and detail controls showed `Fresh run only`; on the detail page, all live controls were disabled and `Fresh run` was enabled. The deploy form again showed `Ready to deploy` while the action plan was initially empty, then accepted the manually added AAPL entry/exit plan before surfacing the durable STOPPED block at submit time.
- UX recommendation: This confirms the durable STOPPED dead end is not bot-specific or symbol-specific. The deploy form should read the inherited instance latch before submit and either disable `Deploy & start`, switch the primary CTA to `Deploy only`, or offer a safe guided `Resume then deploy/start` recovery when paper broker proof is available. Also fix the alert spacing/copy (`blockeddeiagAPPL6`) and make empty action plans fail the Legs step before the form says `Ready to deploy`.

## 2026-07-07T17:42:10Z - Valgate-Jul6

- Bot: `Valgate-Jul6`
- Candidate reason: Different from prior attempts; fleet list showed flat SPY exposure, `DEGRADED`, not `Fresh run only`, and prior status `Clean · 2026-07-06 11:21:28`.
- Action taken: Opened `/broker/bots`, selected `Valgate-Jul6`, used the detail-page `Fresh run` action, verified IBKR paper account `DUM284968`, `paper_orders`, `safe_canary`, and `Start trading immediately`, then added required action plan `leg_1 long SPY x 1` with `CLOSE leg_1`.
- Run id: `31e2dbffc771907ad1658349721752ac742d7c21f363eb078ceaa3cde648d028`
- Outcome category: `died-after-start`
- Outcome: Deploy created a run and start was accepted, but the bot quickly returned to `Bot Off` with no live runtime. No order/fill observed; broker tail only showed account summary, positions, open orders, diagnostics, and live bars refreshes.
- Exact UI error: `Deployment created. Your strategy instance is ready. Start accepted: Host runner process is active.` Then on the bot detail page: `Current lifecycle focus Recovery lane · Poisoned. Meaning: Previous run halted for safety and requires recovery review. The previous run left a safety halt trigger. This comes from the last-exit poison flag; the prior-run field alone is only a coarse classification. Prior Halt Trigger is Cold Start Divergence. Source: Last Exit Poisoned Flag. Evidence time: 2026-07-07 13:46:51 ET.`
- Related UI state: The fleet row changed to `BLOCKED Fresh run only` with `Safety halt · 2026-07-07 12:46:51`. Detail controls showed `Only Fresh run is available`; Start/Resume/Pause/Flatten/Stop/Mark poisoned were disabled and Fresh run remained enabled. `SUBMIT` still read `Broker state unproven`; exposure stayed flat. The incidents panel said `No recent incidents` even though the lifecycle and node receipt showed a poisoned safety halt. The deploy form also continued to show `Ready to deploy` while the action plan was initially empty.
- UX recommendation: This is the first post-start death in the rotation. The UI should treat `Cold Start Divergence` as a first-class incident, not only a lifecycle receipt: put it in Recent Incidents, show the exact poisoned-flag forensic details and recovery path, and link to the relevant audit/log artifact if available. The fleet/table `Fresh run only` chip is helpful after the death, but the detail page should also explain why a bot that just accepted start became poison-blocked. The deploy form still needs pre-submit validation for empty action plans.

## 2026-07-07T18:12:10Z - Validation-Jul6

- Bot: `Validation-Jul6`
- Candidate reason: Different from prior attempts; fleet list showed flat SPY exposure, `DEGRADED`, not `Fresh run only`, and prior status `Clean · 2026-07-06 10:02:31`.
- Action taken: Opened `/broker/bots`, selected `Validation-Jul6`, used the detail-page `Fresh run` action, verified the form showed IBKR paper account `DUM284968`, `paper_orders`, `safe_canary`, and `Start trading immediately`, then added required action plan `leg_1 long SPY x 1` with `CLOSE leg_1`. Did not submit because the UI did not prove account safety.
- Run id: None created during this attempt.
- Outcome category: `blocked-by-safety`
- Outcome: `Deploy & start` stayed disabled before submit. No order/fill observed.
- Exact UI error: `AccountNot proven Account truth is degraded and needs review before calling the account clean. Deploy command Deploy & start Deployment Validation Deploy & start Account NOT_PROVEN. Reconcile account before starting, or turn off "Start trading immediately" to deploy only.`
- Related UI state: The form still showed broker linked as `Data-plane paper session connected`, launch mode `PAPER ORDERS ENABLED`, `safe_canary`, and Start trading immediately checked. After the SPY action plan was added, the primary button remained disabled because account proof was `NOT_PROVEN`. The Legs step still appeared `Complete` while the action plan was initially empty.
- UX recommendation: This safety block is much better than the durable STOPPED cases because it disables the submit button before any run is created. Make it more actionable by deep-linking `Reconcile account` to the exact account-monitor/reconciliation view, naming the missing account-truth evidence, and explaining why a visible paper broker session is not enough to call the bot account-clean. The deploy form still needs to keep Legs incomplete until an entry and matching close leg exist.

## 2026-07-07T18:42:10Z - DEPVALJUL1

- Bot: `DEPVALJUL1`
- Candidate reason: Different from prior attempts; fleet list showed flat MU exposure, `DEGRADED`, not `Fresh run only`, and prior status `Clean · 2026-07-04 22:59:05`.
- Action taken: Opened `/broker/bots`, selected `DEPVALJUL1`, and used the detail-page `Fresh run` action. Did not add an action plan or submit because the deploy form contradicted the bot identity/signal symbol.
- Run id: None created during this attempt.
- Outcome category: `unclear`
- Outcome: Stopped before deploy/start. No order/fill observed.
- Exact UI error: None. The UI showed `Ready to deploy` with `Deploy & start` enabled, but the bot row/detail identified `DEPVALJUL1` as MU while the deploy form prefilled `signal_stream=SPY` and the signal stream field value `SPY`.
- Related UI state: Detail page showed `DEPVALJUL1`, `MU`, flat exposure, `Paper Only`, `SUBMIT Broker state unproven`, no live runtime, and latest signal `ENTER`. Fresh run form showed broker linked to the paper account, account clean, `paper_orders`, `safe_canary`, Start trading immediately checked, and the primary `Deploy & start` button enabled even though the action plan was empty.
- UX recommendation: Treat strategy instance symbol, signal stream, and action plan symbol as a deploy-time safety invariant. If an MU bot opens a Fresh run form with SPY signal stream, disable `Deploy & start` and explain whether the new run will intentionally become SPY or whether lineage failed. The form should carry symbol provenance from the prior ledger, show the exact inherited source, and keep Legs incomplete until a matching entry/exit plan exists.

## 2026-07-07T19:12:40Z - PrajiDemo

- Bot: `PrajiDemo`
- Candidate reason: Different from prior attempts; fleet list showed AAPL, flat exposure, `DEGRADED`, and prior status `Exited with error · 2026-07-03 08:57:35`.
- Action taken: Opened `/broker/bots`, selected `PrajiDemo`, and used the detail-page `Fresh run` action. Did not add an action plan or submit because the detail page and deploy form contradicted each other on exposure/symbol safety.
- Run id: None created during this attempt.
- Outcome category: `unclear`
- Outcome: Stopped before deploy/start. No order/fill observed.
- Exact UI error: None. The detail page showed `ExposureUnknown` for AAPL, while the Fresh run form showed `Ready to deploy` with `Deploy & start` enabled, `signal_stream=SPY`, and no entry/exit legs declared.
- Related UI state: The fleet table said `PrajiDemo` was flat, but the detail page said `ExposureUnknown`, `Paper Only`, `SUBMIT Broker state unproven`, no live runtime, and AAPL context. The Fresh run form showed broker/account clean, `paper_orders`, `safe_canary`, Start trading immediately checked, `signal_stream=SPY`, and `LegsComplete` despite empty action plan.
- UX recommendation: Block Fresh run start when table/detail exposure disagree or exposure is unknown. The deploy form should inherit the bot's visible symbol context, or explicitly label the run as a new SPY deployment and require operator confirmation before Start trading immediately can remain enabled. Empty action plans should make the Legs step incomplete and keep `Deploy & start` disabled.

## 2026-07-07T19:42:40Z - DVS-SPY-SPY-0701

- Bot: `DVS-SPY-SPY-0701`
- Candidate reason: Different from prior attempts; fleet list showed SPY, flat exposure, `DEGRADED`, and prior status `Exited with error · 2026-07-01 08:54:05`.
- Action taken: Opened `/broker/bots`, selected `DVS-SPY-SPY-0701`, used the detail-page `Fresh run` action, verified the form showed paper account `DUM284968`, `paper_orders`, `safe_canary`, Start trading immediately, account clean, and SPY signal context, then added required action plan `leg_1 long SPY x 1` with `CLOSE leg_1` and submitted `Deploy & start`.
- Run id: `c2fed91109d741fd4983944e3fd323e6050a95053a2a73a52ff53e09eb53b9f8`
- Outcome category: `died-after-start`
- Outcome: Deploy created a run and start was accepted, but the bot quickly returned to `Bot Off` with no live runtime. No order/fill observed; broker tail only showed account summary, positions, open orders, diagnostics, live bars, and symbol search refreshes.
- Exact UI error: `Deployment created. Your strategy instance is ready. Start accepted: Host runner process is active.` Then on the bot detail page: `Current lifecycle focus Recovery lane · Poisoned. Meaning: Previous run halted for safety and requires recovery review. The previous run left a safety halt trigger. This comes from the last-exit poison flag; the prior-run field alone is only a coarse classification. Prior Halt Trigger is Cold Start Divergence. Source: Last Exit Poisoned Flag. Evidence time: 2026-07-07 15:44:36 ET.`
- Related UI state: Detail controls changed to Fresh-run-only recovery posture: Start/Resume/Pause/Flatten/Stop/Mark poisoned disabled, Fresh run enabled. `SUBMIT` still read `Broker state unproven`, exposure stayed flat, and Recent Incidents again said `No recent incidents` despite the poisoned lifecycle receipt. Before submit, the deploy form also said `Ready to deploy` while the action plan was initially empty.
- UX recommendation: This reproduces the Valgate post-start death on another SPY deployment. The UI needs a safety-halt incident bridge so `Cold Start Divergence` appears in Recent Incidents with halt trigger, source flag, evidence time, run id, and artifact/log path. Also block or warn pre-submit when the prior lifecycle already says cold-start receipt is blocked, and keep empty Legs from appearing complete.

## 2026-07-07T20:12:40Z - June23

- Bot: `June23`
- Candidate reason: Different from prior attempts; fleet list showed SPY, flat exposure, `DEGRADED`, and prior status `Exited with error · 2026-06-25 09:01:15`.
- Action taken: Opened `/broker/bots`, selected `June23`, used the detail-page `Fresh run` action, verified the form showed paper account `DUM284968`, `paper_orders`, `safe_canary`, Start trading immediately, account clean, fleet clear, and SPY signal context, then opened the Legs step, added/select-confirmed `leg_1 long SPY x 1`, verified the auto-added `CLOSE leg_1`, and submitted `Deploy & start`.
- Run id: `d2356dafc816254ab6e854286eee00c1d2ecc515f519ec1882173e8dfa1cab02`
- Outcome category: `died-after-start`
- Outcome: Deploy created a run and start was accepted, but the bot quickly returned to `Bot Off` with no live runtime and Recovery lane poisoned. No order/fill observed; broker tail only showed account summary, positions, open orders, diagnostics, and position refreshes.
- Exact UI error: `Deployment created. Your strategy instance is ready. Start accepted: Host runner process is active. Run id d2356dafc816254ab6e854286eee00c1d2ecc515f519ec1882173e8dfa1cab02`. The same deploy form then also showed `Deploy & start "June23" is already running. Stop it first, or turn off "Start trading immediately" to deploy without starting.` On the bot detail page: `Current lifecycle focus Recovery lane · Poisoned. Meaning: Previous run halted for safety and requires recovery review. Operator action is required for this lifecycle step. The previous run left a safety halt trigger. This comes from the last-exit poison flag; the prior-run field alone is only a coarse classification. Prior Halt Trigger is Cold Start Divergence. Source: Last Exit Poisoned Flag. Evidence time: 2026-07-07 16:16:27 ET.`
- Related UI state: Detail page showed `Paper Only`, `SUBMIT Broker state unproven`, `ExposureFlat`, `SPY · No live bot runtime is bound`, `BotOff`, and `Pre-flight gates Poison sentinel`. Recent Incidents settled to `No recent incidents No warnings or errors for this run` despite the poisoned lifecycle receipt. Control buttons had disabled-looking titles such as `Start bot process Off. Redeploy required` and `Stop bot Off. Redeploy required`, but the actual button elements were still enabled in the DOM. The deploy form also showed `LegsComplete` and `Ready to deploy` while the action plan was initially empty; the Add stock controls were present but hidden until the Legs step was selected.
- UX recommendation: This is the third observed SPY post-start death with the same `Cold Start Divergence` poisoned flag. Recent Incidents needs to ingest lifecycle safety-halt receipts and show halt trigger, source flag, evidence time, run id, and artifact/log path. The deploy form should not show a success message next to an `already running` warning after a successful start; it should move the operator to the live run or explain that start is in progress. Also make the control disabled state semantic, keep empty Legs incomplete, and make the Legs step visibly required/editable before submit.

## 2026-07-08T00:46:57Z - dep_val_smoke_002

- Bot: `dep_val_smoke_002`
- Candidate reason: Different from the prior `JUN26TSLA` check; fleet list showed SPY, flat exposure, zero open positions, `BLOCKED`, `Fresh run only`, and last run `No result yet · 2026-07-07 19:44:02`.
- Action taken: Opened `/broker/bots`, selected `dep_val_smoke_002`, verified the detail-page live controls, opened `Fresh run`, added the required action plan `leg_1 long SPY x 1` with `CLOSE leg_1`, and submitted the UI's safe `Deploy only` command. Did not attempt Resume/start because the bot page kept Resume disabled for unknown broker safety.
- Run id: A deployment was created, but the success panel showed the `Run id` label with no visible value before `View deployment`.
- Outcome category: `failed-before-start`
- Outcome: Deploy-only succeeded, but no bot process started and no trade occurred. The bot remained `Bot Off`, `SUBMIT Broker state unproven`, `ExposureFlat`, `SPY · No live bot runtime is bound`, lifecycle `Deploy or start · Blocked`; broker tail showed account summary, broker positions, open orders, diagnostics, and symbol search refreshes only.
- Exact UI error: Before the action plan was added, the form said `Deployment Validation requires an action plan; ON ENTER and ON EXIT are both empty.` After adding the leg, it said `Durable STOPPED latch is set. This submit will deploy only; use Resume on the bot page to clear the latch before starting.` On return to detail, `Resume trading` was disabled with `Resume trading Off. Broker safety is unknown. The backend cannot prove the broker is paper-only. Resume is held until broker safety is proven.`
- Related UI state: This is an improvement over earlier durable-STOPPED attempts. The detail page now semantically disabled Start/Resume/Pause/Flatten/Stop/Mark poisoned and left only Fresh run enabled. The deploy form also kept Legs as `Needs input` until a stock entry and matching close action existed. However, the deploy form still showed `Start trading immediately` checked while the command was downgraded to `Deploy only`, and the daemon was stale by 65 commits.
- UX recommendation: Keep this disabled-control behavior; it answers the "only Fresh run possible" case well. Polish the remaining operator path by showing the generated run id, explaining why `Start trading immediately` is ignored/overridden when durable STOPPED forces `Deploy only`, deep-linking the disabled Resume control to broker-safety proof/reconciliation, and escalating stale-daemon state before deploy if it may change control-plane behavior.

## 2026-07-08T01:16:41Z - DIagVal6

- Bot: `DIagVal6`
- Candidate reason: Different from the prior `dep_val_smoke_002` check; fleet list showed SPY, flat exposure, zero open positions, `BLOCKED`, `Fresh run only`, and last run `No result yet · 2026-07-07 20:13:41`.
- Action taken: Opened `/broker/bots`, selected `DIagVal6`, inspected the critical launch-failure state, opened the detail-page `Redeploy bot` flow, added the required action plan `leg_1 long SPY x 1` with `CLOSE leg_1`, and submitted the UI's safe `Deploy only` command. No start/resume was attempted.
- Run id: None created during this attempt.
- Outcome category: `failed-before-start`
- Outcome: Deploy-only was blocked before run creation by the working-tree safety gate. The bot remained `Bot Off`, `Paper Only`, `SUBMIT Broker state unproven`, `ExposureFlat`, `SPY · No live bot runtime is bound`, lifecycle `Recovery lane · Blocked`; broker tail showed only account summary, broker positions, open orders, and diagnostics refreshes. No order/fill observed.
- Exact UI error: Detail page showed `Bot launch failed` and `The bot failed before it could start trading: process exited with code 1`. The run-history excerpt said `[START] HALT — desired_state=STOPPED for DIagVal6 (PythonDataService/artifacts/live_state/DIagVal6/desired_state.json). § 16.4 Resolution 7: clear it with 'run.py resume' before the bot will start.` After adding the SPY action plan, the deploy form said `Durable STOPPED latch is set. This submit will deploy only; use Resume on the bot page to clear the latch before starting.` Submit then showed `Deploy — blocked Working tree is dirty; commit or stash before deploying. working tree has 9 uncommitted change(s) within scope ['PythonDataService', 'references/qc-shadow']: [' M PythonDataService/app/engine/live/config.py', ' M PythonDataService/app/engine/live/deploy.py', ' M PythonDataService/app/engine/live/run.py', ' M PythonDataService/app/engine/strategy/spec/fixtures/deployment_validation.spec.json', ' M PythonDataService/app/routers/engine.py'] A run with these inputs already exists, or the working tree is dirty. Commit or stash the listed paths, then deploy again.`
- Related UI state: Detail controls are improved and were semantically disabled: Start/Resume/Pause/Flatten/Stop/Mark poisoned disabled, Fresh run enabled. The critical notice also exposed a `Redeploy bot` button while the page headline said `Only Fresh run is available`, which is functionally useful but copy-inconsistent. The form correctly kept Legs incomplete until the stock entry and close action existed. `Start trading immediately` still appeared checked while the command was `Deploy only`, and the page reported the daemon was stale by 65 commits.
- UX recommendation: Keep the pre-submit action-plan and STOPPED-latch gates. Add the critical `Bot launch failed` event to Recent Incidents instead of showing `No recent incidents`, align the `Fresh run`/`Redeploy bot` naming, show all 9 dirty scoped changes or say the list is truncated, and make `Start trading immediately` visually disabled/ignored whenever the command has been downgraded to `Deploy only`.

## 2026-07-08T01:44:53Z - JUNE-22

- Bot: `JUNE-22`
- Candidate reason: Different from the prior `DIagVal6` check; fleet list showed SPY, flat exposure, zero open positions, `BLOCKED`, `Fresh run only`, and prior status `Safety halt · 2026-07-07 16:45:41`.
- Action taken: Opened `/broker/bots`, selected `JUNE-22`, inspected the poisoned recovery state, opened the detail-page `Fresh run` form, and stopped before adding legs or submitting because the form had no selectable validated strategy.
- Run id: None created during this attempt.
- Outcome category: `failed-before-start`
- Outcome: Blocked in the deploy form before run creation. The bot remained `Bot Off`, `Paper Only`, `SUBMIT Broker state unproven`, `ExposureFlat`, `SPY · No live bot runtime is bound`, lifecycle `Recovery lane · Poisoned`; broker tail showed account summary, broker positions, open orders, diagnostics, contract details, and live bars refreshes only. No order/fill observed.
- Exact UI error: Detail page showed lifecycle `Bot lifecycle overview · Recovery lane · Poisoned` and current focus `recovery · Poisoned` with `Meaning: Previous run halted for safety and requires recovery review.` Recent Incidents still said `No recent incidents`. The Fresh run form showed `Deploy command Deploy & start`, `Strategy Needs input`, `Legs Needs input`, disabled `Deploy & start`, and the blocking copy `Select a validated strategy before deploying. Strategy must be validated before deployment. Open Strategy Validation to promote it.` The strategy selector contained only disabled `Select a strategy…` while the URL and text fields carried `strategy_key=deployment_validation`, the deployment validation spec path, QC backtest id, and audit-copy path.
- Related UI state: Detail controls were correctly semantically disabled: Start/Resume/Pause/Flatten/Stop/Mark poisoned disabled and Fresh run enabled. The form still showed `paper_orders`, safe canary, and `Start trading immediately` checked, even though no validated strategy could be selected and the button was disabled. The daemon was stale by 65 commits.
- UX recommendation: Treat missing validated-strategy hydration as a first-class redeploy blocker: either preselect the inherited `deployment_validation` strategy from the query params or show a clear "validation receipt unavailable/stale" recovery path with a link to the exact Strategy Validation record. Also carry the poisoned safety-halt receipt into Recent Incidents, and disable or visually mute `Start trading immediately` when the strategy gate prevents deploy/start.

## 2026-07-08T02:18:02Z - Bars-July-6

- Bot: `Bars-July-6`
- Candidate reason: Different from the prior `JUNE-22` check; fleet list showed SPY, flat exposure, zero open positions, `BLOCKED`, `Fresh run only`, and prior status `Safety halt · 2026-07-07 17:19:47`.
- Action taken: Opened `/broker/bots`, selected `Bars-July-6`, inspected the poisoned recovery state, opened the detail-page `Fresh run` flow, verified the form hydrated `deployment_validation` for SPY with paper launch mode, added the required action plan `leg_1 long SPY x 1` with `CLOSE leg_1`, and submitted `Deploy & start`.
- Run id: `f9af53a747539c53b0b1e8576d45484f207032ec86016401cbfddffc7a364ddb`
- Outcome category: `died-after-start`
- Outcome: Deploy created a run and start was accepted, but the bot quickly returned to `Bot Off` with no live runtime and lifecycle `Recovery lane · Blocked`. No order/fill rows were visible after the start; broker tail showed account summary, broker positions, open orders, and broker diagnostics only.
- Exact UI error: The deploy form showed `Deployment created. Your strategy instance is ready. Start accepted: Host runner process is active.` and `Start accepted for run f9af53a747539c53b0b1e8576d45484f207032ec86016401cbfddffc7a364ddb. View deployment to monitor the live run.` On the detail page, lifecycle showed `Recovery lane`, `recovery · Blocked`, and `Meaning: This run wrote poisoned.flag for cold_start_divergence. Reason: unknown_namespace. Review the halt evidence before starting a fresh run.` Recent Incidents showed `APP BLOCKING 2026-07-07 21:15:02 2026-07-08 02:15:02.888 Cold-start divergence — bot halted`, `On startup the bot couldn't reconcile its own records against the broker. It refused to resume on stale state.`, and `Recommended: Reconcile the broker account and re-deploy a fresh run_id. The same run cannot resume.`
- Related UI state: Detail controls were semantically disabled: Start/Resume/Pause/Flatten/Stop/Mark poisoned disabled and Fresh run enabled. The page showed `Paper Only`, `SUBMIT Broker state unproven`, `ExposureFlat`, `SPY · No live bot runtime is bound`, `BotOff`, and `Only Fresh run is available`. The deploy form correctly kept Legs incomplete until the SPY entry/close plan existed, but the top success panel still showed a blank `Run id` label while the lower success copy contained the run id. The form warned that engine code was stale by 67 commits before submit.
- UX recommendation: This is another SPY post-start cold-start halt, but the incident bridge is improved because Recent Incidents now explains the stale-state refusal and recommended recovery. Next polish: expose the raw halt evidence path from `View raw log` or make it copy a forensic breadcrumb, populate the success panel's `Run id` field, and escalate stale-daemon/stale-engine-code state before `Deploy & start` when it may immediately produce a cold-start safety halt.
