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
