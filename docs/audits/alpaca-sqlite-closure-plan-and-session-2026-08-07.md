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

## Feed-stall regression and incident note — 2026-08-07 addendum

This addendum records the synthetic (non-live) half of the "Diagnose the feed
stall" step above. The live read-only reproduction against IB Gateway
(farm/session state, request identity, disconnect/error callbacks) is **not**
performed here — it requires a host-process data plane with a live IBKR
connection during market hours (the containerized data plane cannot drive
live IBKR; see `PythonDataService/CLAUDE.md`). That capture remains an
operator hand-off for the next available market session.

**Root-cause characterization.** `IbkrMarketDataFeed.health()`
(`app/marketdata/ibkr_feed.py`) already computes `stale` from bar-advancement
age, not from IBKR's `subscriptions_stale` flag — the 2026-08-07 canary's
socket never raised that flag, so a connection-only health check would have
misread the stall as healthy. The advancement-based check exists and, once it
fires, correctly refuses Start (`MARKET_DATA_STALE`) and refuses submission
(`STREAM_HEALTH_HOLD`) through the dual-health gate. The gap is not "no
staleness check" — it is that the check has two known blind windows:

- **H1 — grace window.** `stale` only flips `True` once `age_ms` exceeds the
  3-minute default threshold (`_STALE_THRESHOLD_MS`,
  `app/marketdata/ibkr_feed.py`). A feed that dies immediately after its last
  bar reads as healthy for up to 3 minutes — roughly 36 missed bars for a
  5-second-bar strategy like the 2026-08-07 canary.
- **H2 — idle / no active subscription.** The admission translation
  (`market_data_admission_fact`, `app/services/bot_start_admission.py`) only
  treats a stale feed as blocking when `active_subscription_count > 0`. A
  stale feed with no attached consumer reads as idle/`AVAILABLE` rather than
  `STALE`, so a feed that went stale *between* runs offers no positive proof
  of liveness before the next Start.

**Operator decision (2026-08-07):** pin the current (correct, advancement-based)
behavior with a regression and record H1/H2 as tracked, deferred limitations
rather than changing the threshold or the idle-subscription rule today. No
new GitHub defect issue was opened for the stall; this note is the durable
record per that decision. A bar-interval-aware stale threshold remains a
proposed follow-up.

**Regression added:** `PythonDataService/tests/services/test_bot_start_admission.py`
— the previously-untested `FeedHealth -> MarketDataAdmissionFact` translation
seam (`market_data_admission_fact`). Neither `tests/services/test_run_admission.py`
(injects the fact directly) nor `tests/broker/v2panel/test_market_pulse.py`
(monkeypatches the function) exercised this seam before.

- `test_connected_nonadvancing_feed_during_rth_is_stale_and_blocks_start` —
  headline regression: a `FeedHealth` shaped like the 2026-08-07 stall
  (connected, stale past threshold, active subscription, RTH) resolves to
  `STALE` and, chained through `evaluate_run_admission`, blocks Start with
  `MARKET_DATA_STALE`. Verified to fail if the admission translation is
  reverted to always return `AVAILABLE`.
- `test_market_data_admission_fact_unavailable_when_disconnected`,
  `test_market_data_admission_fact_unknown_when_health_probe_raises` (fails
  closed to `UNKNOWN`, chained to `MARKET_DATA_UNKNOWN`),
  `test_market_data_admission_fact_available_when_advancing` — the remaining
  state matrix.
- `test_grace_window_is_a_known_limitation` (H1) and
  `test_stale_feed_with_no_active_subscription_is_idle_available` (H2) —
  characterization tests that pin today's behavior deliberately, so a future
  threshold or idle-subscription change trips them on purpose rather than
  silently.

This closes the synthetic half of "Diagnose the feed stall" in the Closure
sequence below. The live read-only reproduction, the repeated canary, and the
full Phase 2 scenario set remain outstanding and require an operator-present
market session.

## No legacy writer in the activated scope — proof

Phase 3 of #1383 requires confirming "no legacy JSONL control writer, file
idempotency ledger, or Clerk Postgres projection" remains in the production
cutover scope. This is a **scoping** proof — the legacy code still exists in
the tree for the (unactivated) legacy-authority path and for one-time cutover
inventory — not a deletion claim.

- **JSONL journal writer** (`app/broker/alpaca/clerk/journal.py`,
  `order_inbox.jsonl` / `order_journal.jsonl`). Constructed only inside the
  legacy `AlpacaClerk` path. `app/main.py` gates it explicitly:
  `if alpaca_clerk_runtime.authority_kind == "legacy":` before touching the
  legacy clerk or its journal (`app/main.py:210-221`). An activated SQLite
  account resolves `authority_kind == "sqlite"` and never enters this branch.
- **File idempotency ledger** (`CustodyResolutionStore`,
  `app/broker/alpaca/clerk/custody_resolution.py:191`). Constructed lazily
  only by the legacy `AlpacaClerk._resolution_store`, which is only reachable
  through the legacy clerk instance — never constructed by the SQLite runtime
  (`app/broker/alpaca/clerk/sqlite/`).
- **Clerk Postgres projector** (`app/services/clerk_transaction_projection.py`,
  `project_alpaca_journal_best_effort`). Gated the same way at
  `app/main.py:211-221`, and additionally defaults off via
  `CLERK_TRANSACTION_PROJECTION_ENABLED: bool = False`
  (`app/config.py:105`).
- **One legitimate reference from the SQLite path**: `sqlite/cutover.py`
  imports `INBOX_FILENAME`/`JOURNAL_FILENAME` from `journal.py` and references
  the literal `custody_resolution_receipts.json` filename — but only as
  known legacy-artifact names to *identify and quarantine* during the one-time
  cutover ceremony. It never constructs `OrderJournal` or
  `CustodyResolutionStore`, and never writes through either.

Net: an activated SQLite account constructs and drives none of the three
named legacy components at runtime. The legacy components remain in the tree
because the legacy-authority path (accounts without a valid activation fence)
still depends on them, by design, per ADR 0035's cutover note.

## Adversarial / 1M-row / performance evidence review

Reviewed `docs/audits/alpaca-sqlite-clerk-qualification-full.{json,md}` against
the PRD §14 budgets (`docs/prds/alpaca-account-clerk-sqlite-control-plane.md:773-797`).

**Adversarial campaign.** The markdown's "2 selected path(s)/node(s)" is 2
collection roots (`tests/broker/alpaca/clerk/sqlite`,
`tests/broker/alpaca/test_trade_updates.py`), which the JSON shows expand to
**341 tests, all passed**, `return_code: 0`, broker dependency `NONE`
(deterministic fake broker). This covers the named atomicity/idempotency,
corrupt-DB/WAL, tampered-mirror, disk-full, restart, and cutover-refusal
scenarios enumerated in `app/broker/alpaca/clerk/sqlite/qualification.py`.

**Performance budgets.** `PERFORMANCE_BUDGETS_MS` in `qualification.py` maps
exactly to the three PRD §14 latency budgets:

| PRD budget | Measured field | 1M-row / 100-bot p95 | Margin | Verdict |
|---|---|---:|---:|---|
| warm catalog server p95 < 100 ms | `account_snapshot` | 0.213 ms | ~470x | PASS |
| warm panel server p95 < 75 ms | `bot_snapshot` | 0.155 ms | ~484x | PASS |
| bounded custody page p95 < 100 ms | `timeline_page` | 49.323 ms | ~2x | PASS — tightest margin of the three |

`performance_budget.status` is `PASSED` with zero recorded violations.

Capture-before-contact write latency (`synchronous=FULL` — confirmed as
SQLite pragma `synchronous=2` in every scale's recorded pragmas) is measured,
not traded away, per PRD requirement:

| Scale | p50 ms | p95 ms | max ms | n |
|---|---:|---:|---:|---:|
| 1 bot / 10k rows | 0.428 | 0.428 | 0.428 | 1 |
| 10 bots / 100k rows | 0.2165 | 0.316 | 0.316 | 10 |
| 100 bots / 1M rows | 0.22 | 0.3947 | 0.663 | 100 |

**Database/WAL/mirror growth** at the 1M-row fixture: DB 372.6 MiB, WAL 0
bytes (checkpointed), mirror 1013.6 MiB. The PRD requires growth "remain
bounded" without stating a numeric ceiling; growth looks roughly proportional
to bot/row count across the three scales, but there is no explicit MiB or
bytes-per-row budget to grade against. Reported as measured evidence per the
PRD's "do not encode unmeasured claims" instruction — **needs operator
confirmation** if an implicit ceiling exists outside this PRD section.

**Query plans**, recorded at every scale, show the hot reads
(`account_commands`, `bot_operations`, `bot_timeline`) all resolve through
covering indexes (`ix_commands_updated_at`,
`ix_effect_operations_strategy_updated_at`,
`ix_custody_transitions_strategy_sequence`) with no full-table scans,
consistent with the "zero full history replay on an unchanged warm read"
budget.

**Overall: both the adversarial and performance budgets PASS at the 1M-row /
100-bot scale.** The bounded-custody-page (`timeline_page`) budget carries
the least headroom (~2x) of the three latency budgets — worth watching if
per-bot transition counts grow materially beyond this fixture in production.

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
2. The advancement-based regression proving a connected-but-nonadvancing stream cannot
   authorize exposure is already in place (see the addendum above,
   `tests/services/test_bot_start_admission.py`). If the read-only probe reproduces the
   stall again, record the farm/session state and callbacks in this incident note (per
   the 2026-08-07 operator decision, no new GitHub defect issue unless a genuinely new
   failure mode appears); do not reclassify connection-only health as usable market data.
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
