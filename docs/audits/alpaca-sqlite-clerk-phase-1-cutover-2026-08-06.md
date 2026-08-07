# Alpaca SQLite Clerk phase 1 paper cutover evidence

## Scope and disposition

- Ceremony date: 2026-08-06 America/Chicago (2026-08-06/07 UTC).
- Scope: Alpaca paper account `PA3KWXU1C4C3` only.
- Live-money execution remained disabled.
- IBKR bot control was excluded because it is deprecated.
- Result: phase 1 cutover and one safe canary completed. The multi-market-session
  qualification and ADR acceptance owned by #1383 remain pending.

## Build identity

- Merged prerequisite: `139862088a83cd6f86707b891c3eef0dec97698b`
  (PR #1401).
- The ceremony discovered the focused defects recorded in #1402. The corrective
  source-and-test patch exercised by the final canary has SHA-256
  `3609c06406cbe50ed3723e110c340b0f42e2c7918c2d51a23968a7487c4f9b1c` and
  is preserved by the PR containing this report.

## Pre-cutover broker proof

The paper account was initially not flat. With explicit operator authorization,
one SPY share was sold in the paper account at 2026-08-06T23:32:24Z:

- client identity: `manual-cutover-flatten-1786059144`;
- Alpaca paper order: `bd20a61c-d398-4602-9a29-1bc609753bf0`;
- fill: 1 SPY at $768.77.

Fresh broker evidence then proved zero positions and zero open orders. The final
pre-cutover broker proof was captured at 2026-08-06T23:36:54Z. The proof and
signed plan are retained locally under the ignored
`PythonDataService/.cutover/PA3KWXU1C4C3/` evidence directory; credentials and
control secrets are not committed.

## Cutover receipt

- SQLite generation: 1.
- Database identity: `03ed49bd38bb1f3a6462f81706e7dec2`.
- Schema version: 6.
- Initial custody transitions: 0; legacy custody was not imported.
- Verified backup:
  `accounts/alpaca/PA3KWXU1C4C3/verified-backups/backup-g1-1786059383406-476d650437eb5bc9`.
- Backup snapshot SHA-256:
  `476d650437eb5bc9c747e9bd5debf9c29c0d49780e5d5347f503b9b2a5705bea`.
- Activation ID:
  `a30d09aa59e151aef5a6ebfb3bc762dec5de39fc0f3201dcc904c78a6c41adc7`.
- Quarantine manifest SHA-256:
  `36004d45769cd36d80d56dbf587a9354500a4c65bf1ba0721bcdcdbfaad714fe`.
- Cutover receipt:
  `accounts/alpaca/PA3KWXU1C4C3/cutover-evidence/g1-1786059479855-785a48ff1b64/cutover-receipt.json`.

After activation the service restarted with `authority=sqlite`, rejected mixed
legacy custody authority, completed boot recovery, and began the SQLite
reconciliation sweep. A pre-fix process restart correctly observed the execution
lease takeover fence and waited for its bounded expiry instead of overriding it.

## Canary and discovered defects

The first attempt produced no SQLite transition or broker effect because the
idle execution lease had expired. Its three pre-start runner files were moved,
not deleted, to the local ignored cutover evidence directory. A subsequent START
exposed a second defect: SQLite durably committed STOP, but the process registry
attempted to author the same natural-key STOP again before cancelling the task.

Issue #1402 records all defects found during the ceremony and the regression
fixes in this PR:

1. retain the live SQLite writer lease with an independent heartbeat while
   preserving strict fail-closed ownership;
2. publish the actual stream-health gate facts to retained status surfaces;
3. quiesce the runner after an already-durable Clerk STOP without committing a
   second STOP;
4. render SQLite's `stop_bot_decisions` action as the primary danger action.

## Final safe canary

- Strategy instance: `sqlite-canary-stopfix-0806`.
- Deploy receipt:
  `alpaca-paper-deploy:PA3KWXU1C4C3:sqlite-canary-stopfix-0806:1786062107397`.
- Lifecycle run: `70408c32231a4c3198d4e89be916c91a`.
- START committed successfully at 2026-08-07T00:21:47Z.
- The panel showed runtime active, market closed, flat exposure, zero working
  orders, zero unresolved operations, no fills, and the SQLite STOP action.
- The operator invoked that action once. STOP committed at
  2026-08-07T00:23:14Z and the HTTP action completed with `200 OK`.
- Runner terminal proof: desired state `STOPPED`, lifecycle `OFF_DUTY`, no active
  run, outcome `STOPPED` with reason `STOPPED_FLAT`.
- SQLite terminal proof: run `STOPPED`; START and STOP command receipts both
  succeeded; no orders, positions, or uncertainties.
- Final direct Alpaca paper proof: zero positions and zero open orders.

No abort condition in #1383 occurred: there was no duplicate economic intent,
broker mutation without a finalized intent, unexplained exposure, mixed active
writer, fabricated terminal result, or stale evidence authorizing exposure.

## Verification

- Python-focused regression suite: 163 passed.
- Frontend-focused component suite: 22 passed.
- Python lint: passed.
- Frontend lint and repository frontend contract guards: passed.
- Existing 1,000,000-transition qualification artifact:
  `docs/audits/alpaca-sqlite-clerk-qualification-full.md` (performance budget
  passed).

## Remaining qualification

Phase 1 is complete, but #1383 intentionally remains open. Its phase 2 criteria
require supervised evidence across multiple market sessions, including partial
fills, lost broker responses, fill/cancel races, websocket-gap reconciliation,
accepted/unknown restart recovery, backup restore/rebuild rehearsal, and SSE
revision stability. A closed market cannot provide honest evidence for those
scenarios.

The ceremony also confirmed that the old bot-control catalog and refresh path
still scan disposable file-backed runner artifacts after SQLite activation.
That separate performance/retirement defect is tracked by #1403. It does not
invalidate SQLite custody, but it explains the remaining slow bot list, deploy,
panel, and post-action response times.

ADR 0035 therefore remains **Proposed** until the supervised qualification is
published. The account activation recorded here does not qualify live money.
