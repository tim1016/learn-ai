# Five-bot cohort acceptance — 2026-07-21

Authoritative execution record for GitHub issue #1142, rev 2. Times are CDT unless noted.

## Intervention log

### 07:53–08:00 — preflight timing archive

- **Symptom and reason codes:** Catalog latency exceeded the PRD #1142 conditional-archive threshold: `GET /api/live-instances/catalog` was 7.638 s. `POST /api/live-instances/roll-call` was 4.261 s. No start refusal or safety-gate reason code occurred.
- **Evidence:** Account `DUM284968` was `CLEAN`, paper execution posture, clerk generation 52 `ATTACHED`/`READY`, and roll call returned exactly five ready offers: `cohort5-aapl`, `cohort5-msft2`, `cohort5-nvda`, `cohort5-qqq`, and `cohort5-spy`. The excluded `cohort5-msft` remained in Sick bay and was not selected.
- **Hypothesis:** Stale, unreferenced completed-run directories add local composition cost on catalog and roll-call reads. This is timing evidence only; no correctness or safety conclusion is claimed.
- **Change and why:** Per the PRD's required conditional archive, moved (never deleted) 12 stale July 17 directories not referenced by any current cohort deployment into `PythonDataService/artifacts/live_runs_archive/2026-07-21-preflight-slow-catalog/`. Current directories for all six deployed cohort bots, `_broker`, `.strategy_instance_locks`, and the emergency-flatten evidence remain in the active root.
- **Verification:** Retimed after the move: catalog 6.712 s; roll call 7.671 s; account remained `CLEAN`; roll call still returned five ready offers. The residual latency is recorded as #1149 evidence and does not block the #1142 attempt.
- **Commit SHA:** `3916bbd9` (`docs(audit): record five-bot cohort preflight`); no code or safety-gate behavior changed.

### 08:00–08:10 — UI-primary preflight and preset verification

- **Symptom and reason codes:** The first Bots-page render showed an Angular `TS2820` overlay for `paper_five_bot_stagger_v3`; the running source and a successful subsequent compiler build both already contained that union member. After a page reload, the overlay cleared with no source change. The first cohort-dialog preflight then reported `account_not_proven` for all five members.
- **Evidence and diagnosis:** The page correctly identified paper account `DUM284968`, five ready / zero on duty / one sick-bay bot. A fresh Account Truth read and account triage showed a current clean reconciliation receipt, flat exposure, a verified observation lease for Clerk generation 52, and no triage blockers. Reopening the dialog returned zero hard blockers for all five eligible members. The initial blocker was a cache warm-up timing condition, not a request to weaken a gate.
- **Change and why:** No code, profile, safety gate, or service restart was changed. Used the normal UI preflight/reload path, then selected the server-owned five-bot preset.
- **Verification:** The dialog now has exactly `cohort5-aapl`, `cohort5-msft2`, `cohort5-nvda`, `cohort5-qqq`, and `cohort5-spy` selected; `cohort5-msft` remains excluded in Sick bay. `Authorize 5 selected bots` is enabled. This selection has not been submitted.
- **Commit SHA:** `5a4fa7c6` (`docs(audit): record cohort UI preflight`); no code or safety-gate behavior changed.

### 08:20–08:42 — staggered launch and evidence failure

- **Launch evidence:** UI-authorized receipt `paper-validation-1784640014429-8a0e511ba315` used the required `paper_five_bot_stagger_v3` profile. The service accepted exactly `cohort5-aapl` (+0), `cohort5-msft2` (+5), `cohort5-nvda` (+10), `cohort5-qqq` (+15), and `cohort5-spy` (+20); the poisoned original `cohort5-msft` was never included. The final acceptance was recorded at 08:40:18 CDT.
- **Failure and reason code:** At 08:41 CDT the authoritative cohort evidence was `failed` with `COHORT_ACCOUNT_PROOF_FAILED` (14 samples, then 25 on the UI refresh), not a member-runtime failure. Healthy overlap remained `0 s`, so no certificate can mint.
- **Member and exposure evidence:** Every accepted member reported `healthy`, `0 / 2000` orders used, and flat/zero positions. The live roster showed five on-duty bots; no member was blocked, skipped, or dropped.
- **Required response:** Per #1142, the failed attempt is complete and must be gracefully stopped without bypassing or weakening the account-proof gate. No service restart, gate change, crash, emergency flatten, or direct actuation has been performed.
- **UI stop-path finding:** After the UI refreshed to the current server state, the selected-cohort actions exposed only `Soft delete`; the individual Operations view exposed `Take off roster` and account-wide `Emergency flatten`. Neither is a graceful stop. The operator manual still says to request `Stop`, while the referenced Bot Control start/stop card has been removed. A normal direct fallback exists (`POST /api/live-instances/runs/{run_id}/stop`) but has not been invoked because the authorized path was UI-primary.

### 08:42–09:22 — UI-only graceful-stop remediation

- **Change and reason:** Restored the missing normal stop control in the Trader view. The `End this bot safely` card explains that graceful stop writes durable `STOPPED` intent, asks a bound process to exit when one can be reached, and does not submit orders or flatten the account. When host liveness is unproven, the same card makes the different guarantee explicit: the stop prevents a future start and process exit/positions must be verified after control-plane proof returns.
- **Safety boundary:** The card reuses the existing single operator-intent path (`setInstanceDesiredState(..., { action: 'stop', reason: 'Stop', updated_by: 'operator' })`). It introduces no new endpoint, direct command, safety-gate bypass, automatic flatten, or account mutation other than the operator's durable stop intent.
- **Verification before use:** `npm run test:guards` passed. The focused `bot-control-page.component.spec.ts` suite passed 14/14 tests, including on-duty graceful stop dispatch and the unproven-host guidance/control state. `git diff --check` passed.
- **UI actions and result:** Used only the new `Stop bot gracefully` control for the five accepted members. The fleet roster reports clean exits at 09:14:06 (`cohort5-aapl`), 09:16:10 (`cohort5-spy`), 09:18:17 (`cohort5-nvda`), 09:21:30 (`cohort5-qqq`), and 09:21:52 (`cohort5-msft2`). Each is now Off duty/Sick bay; the excluded poisoned `cohort5-msft` was not acted on.
- **Final UI evidence:** The cohort receipt remains `Failed` with `COHORT_ACCOUNT_PROOF_FAILED`, healthy overlap `0 s`, and 180 samples. The roster shows AAPL flat and no working orders. It also shows four explained, attended paper holdings with no working orders: MSFT +1 (`cohort5-msft2`), QQQ +1, NVDA +1, and SPY +1.
- **Deliberate non-action:** Emergency flatten was not used. The remaining positions are identified in the UI and are attended; the #1142 runbook permits emergency flatten only for unexplained unattended exposure. An account-wide flatten would be a separate operator decision and requires explicit authorization.

### 11:34–11:36 — authorized UI account recovery and flat-account proof

- **Authorization and UI route:** After explicit operator authorization, used only the UI path for paper account `DUM284968`: Bot Control → Operations → Emergency account flatten → typed `FLATTEN` confirmation. The control clearly declared its account-wide paper scope and market-order consequence before accepting the request.
- **Recovery evidence:** The first roster refresh still showed the four bot-owned one-share holdings while account evidence was stale. Followed the server-declared `Run account reconcile` action in the roster, then opened Accounts → `DUM284968` → Account Desk → Operator. This is the account-scoped UI recovery/verification route; it exposes broker snapshot, reconciliation, and only server-declared recovery actions.
- **Authoritative result:** The Account Desk's attested broker snapshot at 11:36:38 CDT reports `Open positions: 0`, `Positions (0)`, no open positions for the selected account, Reconciliation `Clean`, and Exposure status `Flat`. The account is therefore flat. The member roster's per-bot exposure rows are a lagging projection and are not used as the final broker truth.
- **Safety and scope:** No direct HTTP mutation, CLI flatten, service restart, or safety-gate bypass was used. The excluded poisoned `cohort5-msft` remained untouched. The account-level UI confirmation and the subsequent Account Desk proof are the execution and verification record.

### 11:52–11:55 — account-observation lease renewal fix

- **Symptom and evidence:** The initial five-bot run failed only with `COHORT_ACCOUNT_PROOF_FAILED`, although every member stayed healthy with zero orders. The runner logs and account event stream repeatedly showed `ACCOUNT_OBSERVATION_LEASE_EXPIRED` beside fresh `ACCOUNT_TRUTH_CLEAN` projections. After UI recovery, the lease renewed at 11:36:44 CDT but expired at 11:37:44 with no subsequent durable renewal.
- **Root cause:** Each durable child runner started `AccountTruthRefreshLoop`, which kept collecting Account Truth, but it did not supply the existing `AccountReconciliationService` success/failure observers. Successful in-run refreshes therefore updated only process-local readiness evidence and never renewed the durable, Clerk-fenced account-observation lease.
- **Change and safety rationale:** The child loop now uses the existing reconciliation success and failure observers. This renews the durable lease only after a clean, current Account Truth projection and stable accepting Clerk generation; failures still revoke proof. No gate threshold, bypass, order path, or operator authorization behavior changed.
- **Regression coverage and verification:** Extended `test_cmd_start_runs_account_truth_refresh_loop_for_durable_submit_child` to require both observer bindings. The focused runner test passed, followed by the relevant reconciliation, Account Truth refresh, and runner suites: `169 passed`; Ruff and `git diff --check` passed.
- **Commit SHA:** `3e994594` (`fix(live): renew account proof from runner refresh`). The fix remains local; no push was attempted.

### 12:07–12:13 — bounded admission-timeout correction

- **Safe failed retry:** The UI preflight cleared all hard blockers for exactly `cohort5-aapl`, `cohort5-msft2`, `cohort5-nvda`, `cohort5-qqq`, and `cohort5-spy`; the Sick-bay `cohort5-msft` was excluded. Receipt `paper-validation-1784653635663-fc46c59312a1` recorded AAPL `Blocked — Cohort Start Rejected — connect_timeout` at 12:07:32 CDT. The remaining four members were safely `Skipped — Cohort Prior Member Blocked`, so no member was ambiguously started.
- **Root cause:** Host logs showed both the account-clerk ensure and run-start requests using the generic two-second action deadline while the host reconciled broker state. The host returned the safe `connect_timeout`; this was not evidence of an uncertain start or a reason to weaken admission checks.
- **Change and verification:** Start and clerk-ensure admission calls now use the already-established bounded 10-second timeout; probe reads retain their own timeout policy. Regression coverage pins both action paths. Focused client, probe-timeout, and cohort-evidence suites passed (`56 passed`); Ruff and `git diff --check` passed. The data service was restarted to load the change.
- **Commit SHA:** `ea096e39` (`fix(live): bound cohort admission actions`). The fix is local; no push was attempted.

### 12:15 onward — current UI-authorized staggered retry (in progress)

- **Authorization and first outcome:** The UI again reported zero hard blockers across the same five eligible bots, applied its five-bot stagger preset, and recorded durable receipt `paper-validation-1784654129416-51dcf1fd55d4` at 12:15:29 CDT. AAPL was accepted at 12:15:48 CDT. MSFT2, NVDA, QQQ, and SPY remain deliberately pending at five-minute stagger intervals; no duplicate start request was sent.
- **Current evidence:** The receipt has no runtime samples or healthy overlap yet, so its evidence verdict remains `Unknown` and no certificate can mint. Account Desk's broker-attested snapshot at 12:17:17 CDT reports `Positions (0)`, zero initial margin, and zero maintenance margin. The Account-roster label saying three bots are on duty conflicts with the receipt and is not treated as broker truth.
- **Status:** This is a partial admission success, not a successful five-bot validation. Continue through the UI cohort monitor and declare success only after all members have server-recorded outcomes and the required healthy-overlap evidence is present.
