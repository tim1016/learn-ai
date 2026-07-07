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
