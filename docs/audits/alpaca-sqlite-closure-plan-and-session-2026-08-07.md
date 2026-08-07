# Alpaca SQLite issue-closure plan and supervised session — 2026-08-07

## Executive decision

There are two open issues whose title or acceptance criteria directly concern the
Alpaca SQLite Clerk:

| Issue | Closure decision | Remaining gate |
| --- | --- | --- |
| [#1403](https://github.com/tim1016/learn-ai/issues/1403) — file-backed catalog scans after SQLite activation | Close after one focused implementation PR is green and merged. The roster-authority foundation is already merged in PR #1405; the remaining patch is implemented and locally verified. | Merge the non-blocking action refresh, bounded quarantine ceremony, and activated-account latency regressions recorded below. |
| [#1383](https://github.com/tim1016/learn-ai/issues/1383) — human paper cutover, soak, qualification, and ADR acceptance | **Do not close today.** Phase 1 is complete, but the first 2026-08-07 market-session canary failed closed when the real-time bar feed stopped after one 5-second bar. No fill/race scenario was exercised. | Diagnose the feed stall, repeat the canary, complete the required scenarios across multiple supervised market sessions, publish the soak package, then accept ADR 0035. |

Closing #1383 today would contradict its explicit multi-session acceptance criteria and
would represent a failed feed as a passed custody qualification. Today can close #1403
and publish a precise, safe partial result for #1383.

## Market-open work performed first

Immediately before deployment, the Alpaca paper account was proven to have zero
positions and zero open orders. Deploy admission and Clerk/channel gates were healthy.

One one-share `SPY` paper canary was deployed:

- Strategy instance: `sqlite-market-qual-0807`
- Strategy: `deployment_validation`
- SQLite authority: generation 1, control revision 293 at admission
- Deploy receipt:
  `alpaca-paper-deploy:PA3KWXU1C4C3:sqlite-market-qual-0807:1786109996307`
- Run ID: `9c0b460fcdb54ce3a9e730646a724f07`
- Admission latency: 228.888 ms
- Deploy response latency: 180.441 ms

The IBKR `reqRealTimeBars` subscription delivered one 5-second bar at approximately
13:39:57 UTC and then delivered no further bars. Warnings were recorded after 30 and
60 seconds. The strategy therefore received no closed minute bar, made no trading
decision, and submitted no order.

The operator stopped the canary rather than broadening exposure or bypassing the feed
gate. The durable stop receipt was:

`cmd:PA3KWXU1C4C3:sqlite-market-qual-0807:9c0b460fcdb54ce3a9e730646a724f07:STOP:STOPPED`

Final state was control revision 296, `OFF_DUTY / STOPPED / STOPPED_FLAT`, with zero
positions, zero open orders, zero fills, zero outstanding intents, and no hold or
freeze. This is a successful fail-closed safety result and a failed qualification
result. It does not satisfy #1383's ENTER, fill, cancellation, race, or recovery gates.

## #1403 implementation and operational evidence

PR #1405 merged at `160e5a5f6561c00122aa9029459db38062ffbe14` and makes the
activated SQLite lifecycle roster the canonical live-instance source. The remaining
focused patch completes #1403's acceptance surface:

1. A committed operator action returns its durable result immediately. Live-panel
   refresh is scheduled as a tracked background prompt and is cancelled/drained at
   service shutdown; the normal five-second producer remains the fallback.
2. A plan/apply CLI ceremony inventories only valid file-backed Alpaca bindings absent
   from the activated SQLite registry. It requires exact database identity, a released
   execution lease, a stopped/checkpointed authority, short-lived confirmation, operator
   count/byte bounds, and unchanged tree hashes. It moves artifacts into quarantine and
   never deletes them.
3. Regression coverage proves activated catalog reads never call the legacy scanner,
   including twenty repeated reads with 200 disposable directories (eight times the
   observed production cleanup set) present in the runner tree.

The live quarantine plan selected 25 disposable Alpaca bindings totaling 65,884 bytes,
within declared bounds of 64 entries and 10 MiB. It preserved all three SQLite-registered
canaries, other-broker bindings, and unbound forensic evidence. Apply moved the exact set
to:

`PythonDataService/artifacts/legacy-catalog-quarantine/8d8ed5c8e700edd69039d53d1a6ac8dc7119f2a0c7c20aab098be06ada454c45`

- Plan ID: `8d8ed5c8e700edd69039d53d1a6ac8dc7119f2a0c7c20aab098be06ada454c45`
- Applied manifest SHA-256:
  `6360e9807fcda0477b2040b6c4ead524e80834ce73d4cb00d6c982e32b559d25`
- Recovery property: exact directories were moved, not deleted.

After restart, logs proved `authority=sqlite`, successful boot recovery, and a SQLite
reconciliation sweep. The UI and API exposed exactly the three SQLite-registered bots;
the disposable file catalog was absent. Point measurements were:

- Catalog: 6.237 ms for three rows
- Deploy view: 7.417 ms
- Stopped panel: 234.095 ms, including the broker/Clerk point admission check
- Durable `reconcile_now` action: 307 ms, receipt `reconciliation:297`

Local validation:

- Project-scope Python lint: passed
- Focused SQLite, panel, live-projection, and live-instance suite: 60 passed in 0.85 s
- First repository-wide failure: an inherited test import collision in
  `test_runtime.py`/`test_exit.py` (`tests/scripts/conftest.py` shadows `_clock_at`),
  after 304 tests passed. This matches PR #1405's documented suite-order limitation and
  does not intersect the files in this patch.

## Recovery work completed offline

A verified online backup was published while the SQLite authority was live:

- Bundle:
  `PythonDataService/artifacts/alpaca_clerk/accounts/alpaca/PA3KWXU1C4C3/verified-backups/backup-g1-1786110520924-4dcef1c230c1a647`
- Snapshot SHA-256:
  `4dcef1c230c1a647912be33903f13f5cd2a996d6fbd1372fd3ad70ca1dd89f1a`
- Generation 1; control revision 296; 296 transitions
- Database identity: `03ed49bd38bb1f3a6462f81706e7dec2`

A non-production clone then passed both recovery paths:

- `RESTORE_VERIFIED_BACKUP` at `1786110720413`
- `REBUILD_FROM_MIRROR` at `1786110725232`

Production authority was not replaced by either rehearsal.

## Closure sequence

### Today: close #1403

1. Put the focused #1403 patch on a fresh branch from merged `master` so it does not
   reuse PR #1405's already-merged branch.
2. Run project-scope lint, the 60-test focused suite, and CI. Preserve the quarantine
   receipt and this audit in the PR; exclude the untracked `.cutover/` evidence tree.
3. Merge only after the action-response, no-scan, quarantine, and latency checks pass.
4. Post the merged PR and live measurements to #1403, then close it.

### Next available market session: unblock #1383

1. Reproduce the SPY `reqRealTimeBars` stall with a read-only subscription before any
   bot starts. Capture IB Gateway farm/session state, request identity, raw bar count,
   and disconnect/error callbacks.
2. If the read-only probe stalls again, open a focused market-data defect with the
   reproduction. Fix it with a regression that fails a stream which remains connected
   but stops advancing; do not classify connection-only health as usable market data.
3. Re-run the same one-share canary only after two consecutive closed minute bars and a
   fresh flat/no-open-orders proof. Abort on every #1383 condition.
4. Exercise and record ENTER acknowledgement, partial fill/duplicate evidence,
   lost-submit identity recovery, cancel/fill race, lost cancel, update gap plus REST
   reconciliation, and accepted/unknown restart recovery. Keep each injected failure
   bounded to one known paper order and return the account to a proven flat terminal
   state before the next scenario.
5. In a separate supervised market session, repeat the normal ENTER/EXIT/restart path,
   verify advancing and unchanged SSE revisions, and confirm stable frontend selection,
   lens, and history.
6. Publish the combined soak/incident package. Only then move ADR 0035 from Proposed to
   Accepted for Alpaca paper scope, keep live-money disabled, and close #1383.

## Non-negotiable close conditions

- No issue is closed on an unmerged local patch.
- No stale or non-advancing feed may authorize exposure.
- Every scenario ends with fresh broker position and open-order proof.
- Any duplicate economic intent, mutation without finalized SQLite intent, mixed writer,
  unresolved drift, fabricated terminal state, or backup identity failure stops the
  ceremony and becomes a focused defect.
- ADR 0035 remains Proposed until all #1383 scenarios have multi-session evidence.
