# Alpaca SQLite Clerk supervised paper qualification — 2026-08-07

**Status:** PARTIAL QUALIFICATION / RECOVERED AND IDLE — Qualification A
produced one valid paper round trip, then the restart/UI drill hit the
corruption abort recorded in
[#1413](https://github.com/tim1016/learn-ai/issues/1413). The operator-approved
mirror rebuild subsequently restored the current finalized head without
changing generation or database identity, and a supervised three-bar run plus
normal stop/reconciliation/restart completed flat and order-free. Earlier
defects are tracked by [#1410](https://github.com/tim1016/learn-ai/issues/1410),
[#1411](https://github.com/tim1016/learn-ai/issues/1411), and
[#1412](https://github.com/tim1016/learn-ai/issues/1412). This remains partial
evidence, not #1409 closure evidence.

**Scope:** Alpaca paper account `PA3KWXU1C4C3`, SQLite authority generation 1.
Live-money execution remained disabled. This report carries the remaining
qualification and governance work from issue
[#1409](https://github.com/tim1016/learn-ai/issues/1409).

## Closure rule

Issue #1409 and ADR 0035 may close only after every required live scenario below
has passed across multiple operator-supervised market sessions, the final broker
state is freshly proven flat and order-free, and the report records any abort or
incident (including `none`). Deterministic tests and the first canary's safe
failure are supporting evidence; they do not substitute for the live soak.

## Evidence already complete

- Phase 1 cutover and initial flat/order-free proof:
  `docs/audits/alpaca-sqlite-clerk-phase-1-cutover-2026-08-06.md`.
- First canary and feed-stall incident:
  `docs/audits/alpaca-sqlite-closure-plan-and-session-2026-08-07.md`.
- Adversarial, 1M-row, and latency qualification:
  `docs/audits/alpaca-sqlite-clerk-qualification-full.{json,md}`.
- Backup, non-production restore, and mirror-rebuild rehearsal: recorded in the
  first-canary audit above.
- Invariant traceability:
  `docs/references/alpaca-sqlite-clerk-invariant-traceability.md`.
- Trader/Operator truth language and recovery actions:
  `docs/references/alpaca-sqlite-clerk-recovery-language.md`.
- Recovery and cutover runbook:
  `docs/runbooks/alpaca-sqlite-clerk-recovery-and-cutover.md`.

The dedicated truth-language deliverable already exists on merged `master`; the
unchecked #1409 checklist item saying that no dedicated document was found is
stale.

## Adversarial-review disposition

An independent Claude Opus review returned **REQUEST CHANGES** and correctly rejected
closure of #1409 and #1410–#1413. The accepted P1/P2 findings have been remediated in
the local working tree:

- feed liveness, admission, Clerk submission gates, market pulse, and panel channel
  health are symbol-scoped; a two-symbol regression proves that an advancing sibling
  cannot make a never-advancing symbol healthy;
- the decision journal now records the strategy's `enter_intent` / `exit_intent`
  before Clerk execution, with bar-ref idempotency. It no longer translates pending,
  uncertain, or unprovable custody states into confirmed `entered` / `exited` signal
  claims, and a journal failure prevents the broker mutation;
- an exhaustive repository/projection parity test covers every effect-operation state,
  ENTER/EXIT kind, and absent/new/filled order combination. The projection predicate
  is fail-closed for newly introduced states;
- restore, mirror rebuild, and reset enforce the WAL filesystem guard before a
  read-write connection. An unreadable lease requires fresh independent,
  account-bound process-stop proof instead of being treated as no lease;
- the evidence-station regression explicitly asserts that opening raw evidence does
  not dispatch a lifecycle action; and
- the epsilon predicate, duplicated subscription-stall block, `.cutover/` ignore rule,
  filesystem denylist, and suite-order-dependent test import were hardened.

At capture time, this disposition was code/test evidence only. It had not been
committed, reviewed in a PR, passed CI, or exercised in a new supervised market
session. The remediation was subsequently published as commit `f3436c38` in PR
[#1414](https://github.com/tim1016/learn-ai/pull/1414), where automated review is in
progress and CI remains a required merge gate. The exact historical #1413
evidence-click-to-Resume sequence remains unreproduced, so the regression narrows
the risk but does not close that incident.

## Session register

| Session | Market session | Operator present | Result | Evidence |
| --- | --- | --- | --- | --- |
| Phase 1 | 2026-08-06 | yes | PASS — generation 1 activated; safe START/STOP canary ended flat | Phase 1 cutover audit |
| Initial canary | 2026-08-07 | yes | SAFE FAILURE — one 5-second IBKR print, no closed minute, no decision/order; operator stopped flat | First-canary/feed-stall audit |
| Qualification A | 2026-08-07 | yes | ABORT — Resume-control defect fixed locally, then both IBKR consumers repeated the one-print stall while Broker V2 still presented Live/Healthy; stopped and reconciled flat | This report; #1410; #1411 |
| Qualification A, attempt 2 | 2026-08-07 | yes | PASS for one normal ENTER/EXIT round trip; ABORT for the subsequent truth/restart drill | This report; #1412; #1413 |
| Recovery and Qualification A, attempt 3 | 2026-08-07 | yes | PASS — current mirror rebuilt the corrupt authority, three advancing closed bars produced durable no-action decisions, Stop/Reconcile/restart remained flat and idle | This report; #1413 |
| Qualification B | Later market session | pending | PENDING | This report |

## Qualification A preflight

Read-only inspection at approximately 2026-08-07 11:34 CDT observed:

- authority generation: `1`;
- database identity: `03ed49bd38bb1f3a6462f81706e7dec2`;
- control revision: `297`;
- authority health: `healthy`;
- positions: none;
- working orders: none in the operator panel;
- active holds / uncertainties: `0` / `0`;
- registered bots: `3`, all `OFF_DUTY / STOPPED / STOPPED_FLAT`;
- qualification bot: `sqlite-market-qual-0807`, one-share `SPY`, paper,
  `Resume ready`;
- Alpaca market-data and execution channels: healthy;
- IBKR paper data-plane session: connected;
- fault-injection seam: paper-only permission active, no fault armed;
- initial reconciliation receipt: `reconciliation:297`.

This is readiness evidence, not start authorization. Before resuming, the
operator must capture a fresh reconciliation and broker position/open-order
proof, then prove the feed is advancing. After Resume, the strategy may enter
only after two consecutive green closed minute bars. The panel showed `Last
bar: None` during preflight.

The operator ran `reconciliation:298` at `1786120676084` ms UTC and again
observed a clean, flat, order-free account. The first service restart correctly
failed closed while the previous process's 30-second execution lease was still
live. That lease expired at `1786121043010` ms UTC; the retry acquired the lease
at `1786121058693` ms UTC with generation `1` and DB identity
`03ed49bd38bb1f3a6462f81706e7dec2` unchanged. No second writer was installed
during the wait.

The restarted panel then exposed a separate UI defect: the health projection
said `Flat Resume ready`, but the SQLite adapter had replaced the full action
catalog and omitted Resume. Qualification paused before any broker effect.
Issue [#1410](https://github.com/tim1016/learn-ai/issues/1410) records the defect.
A local regression-first fix preserved stopped-bot Resume as a lifecycle action
while retaining SQLite recovery actions; all 188 Broker V2 panel tests and the
project-wide Python lint check passed. Because that fix was not yet merged, the
resulting live attempt is discovery evidence, not closure evidence.

The repeated live stall promoted H1/H3 from deferred characterization to
release blockers. The local #1411 fix generation-safely invalidates an
expected-session subscription after 60 seconds without distinct timestamp
advancement, transparently resubscribes the decision feed, marks an active feed
without its first closed minute stale immediately, and uses distinct raw
5-second timestamps as a 30-second liveness watermark between closed minutes.
Its 55 focused IBKR/feed/admission tests pass. A broader 567-test run passed
561; six unrelated order-router cases were intercepted by the configured
data-plane control-secret guard (403 before their expected handler response).
Live requalification is still required before #1411 can close.

## Required live scenarios

Every row must link the operation-first timeline and record the authority
generation, control revision, command/effect/order/receipt identities, source /
observation / durable-record clocks, broker position/open-order proof, expected
versus observed state, operator action, and warning/error-log reference.

| Scenario | Session | Status | Durable evidence / observation |
| --- | --- | --- | --- |
| Read-only IBKR feed reproduction with farm/session, request, bar-count, disconnect, and error callbacks | A | ABORT | Farms `usfarm`, `ushmds`, and `secdefil` reported healthy. The chart consumer attached and received one raw 5-second print at about `16:44:31Z`, then warned at `16:45:01Z` and `16:46:01Z`. The decision consumer attached at `16:45:06.724Z`, received one print at `16:45:10.705Z`, then warned at `16:45:36.821Z` and `16:46:36.909Z`. No disconnect callback explained the stall. #1411. |
| Normal process restart and execution-lease failover | A | PASS after recovery; accepted/unknown restart still pending | The first failover refused the live prior lease and acquired it after expiry. The later corrupt authority was rebuilt from the finalized mirror through sequence 323, then normal stopped-state restarts completed healthy and remained idle. #1413. |
| Baseline Resume with two-green closed-minute entry gate | A | ABORT | START succeeded as lifecycle run `558d6e5f2dda4772a96a79f32c8ce851`; no durable last bar, decision, effect operation, order, or fill followed because the decision consumer stalled after one print. |
| ENTER acknowledgement and terminal fill | A | PASS (baseline only) | ENTER `effect:sqlite-market-qual-0807:encoded-MTc4NjEyMjYwMDAwMDpFTlRFUg`; broker order `c09fb7ed-dd7b-4d49-b892-5f62c0130a3a`; BUY 1 SPY filled at about 772.30; source event `1786122606676`, durable record `1786122606786`. |
| Partial fill plus duplicate/redelivered broker evidence | A | PENDING | — |
| Lost submit response resolved by exact client identity | A | PENDING | — |
| Cancel-first EXIT with a fill racing cancellation | A | PENDING | — |
| Lost cancel response retains Clerk custody | A | PENDING | — |
| Trade-update disconnect/gap; REST reconciliation completes before admission reopens | A | PENDING | — |
| BOT uncertainty isolates only the affected bot | A | PENDING | — |
| Unknown/unclassified uncertainty becomes `ACCOUNT_CLERK` and fails closed | A | PENDING | — |
| Stop decision, prepare-safe-flatten evaluation, reconciliation, and operation-first timeline | A | PASS for baseline round trip; later UI truth ABORT | EXIT `effect:sqlite-market-qual-0807:encoded-MTc4NjEyMjc4MDAwMDpFWElU` filled SELL 1 and reached `EXIT_ATTRIBUTED_FLAT`; STOP sequence 320 and `reconciliation:321` left zero exposure/orders. The panel then misstated stop/evidence/intent health (#1412). |
| Service restart with accepted/unknown work | A | PENDING | — |
| Normal ENTER/EXIT/restart path repeated in a later market session | B | PENDING | — |
| Unchanged and advancing SSE revisions keep selection, lens, and history stable | A/B | PARTIAL PASS after recovery | During the original post-restart Operator drill, the evidence-station interaction coincided with an unintended durable Resume action and the projection later failed on DB corruption. After recovery, opening Broker ack and its raw evidence loaded ten custody events without a lifecycle action; 25 focused Angular tests passed. Multi-session selection/history stability remains pending. #1413. |
| Verified online backup after soak; identity/hash verification | B | PENDING | — |

The non-production restore and mirror-rebuild rehearsal is already complete. It
must be referenced in the final verdict but need not mutate production again.

## Qualification A attempt 1 receipt

```text
Scenario: Baseline Resume, live-feed observation, safety stop, and reconciliation
Started at (ms UTC): 1786121106666
Ended at (ms UTC): 1786121303876
Authority generation / DB identity / control revision: 1 / 03ed49bd38bb1f3a6462f81706e7dec2 / 298 before START, 300 after STOP, 301 after reconciliation
Strategy instance / lifecycle run: sqlite-market-qual-0807 / 558d6e5f2dda4772a96a79f32c8ce851
Command / effect operation / order ref / broker order / receipt: START cmd:PA3KWXU1C4C3:sqlite-market-qual-0807:558d6e5f2dda4772a96a79f32c8ce851:START:ACTIVE; STOP cmd:PA3KWXU1C4C3:sqlite-market-qual-0807:558d6e5f2dda4772a96a79f32c8ce851:STOP:STOPPED; no effect operation or order; reconciliation:301
Broker source timestamp (ms UTC): first decision-consumer callback logged 1786121110705; no later decision-consumer print arrived
Clerk observation timestamp: START 1786121106666; STOP 1786121283392; reconciliation attempted 1786121303876
Durable record timestamp: START 1786121106666; STOP 1786121283392; reconciliation 1786121303876
Expected state: continuously advancing IBKR bars; entry only after two consecutive green closed minutes; Clerk-owned ENTER then bounded EXIT
Observed state: each IBKR consumer received one print and stalled; Broker V2 presented Live/Healthy while Bot health remained Last bar None; no decision or broker effect
Position proof: final Operator panel Flat; SQLite positions empty; clean reconciliation
Open-order proof: final Operator panel 0 working / 0 unresolved; SQLite orders empty; clean reconciliation
Operator action: Stop bot decisions, inspect stopped-flat receipt, Reconcile now
Warning/error log reference: 16:45:01, 16:45:36, 16:46:01, and 16:46:36 UTC reqRealTimeBars non-delivery warnings
Verdict: ABORT
```

## Qualification A attempt 2 receipt and corruption abort

```text
Scenario: Normal one-share ENTER/EXIT, Stop, reconciliation, truth inspection, and restart
Started at (ms UTC): 1786122492521
Ended at (ms UTC): 1786123725672
Authority generation / DB identity / control revision: 1 / 03ed49bd38bb1f3a6462f81706e7dec2 / 302 before round trip, 321 after reconciliation, finalized mirror sequence 323 after abort shutdown
Strategy instance / lifecycle run: sqlite-market-qual-0807 / 206199d1a0b1405a85cdc8f97db34d5d; unintended later run e271d757f9a24746a92364348221ef46
Command / effect operation / order ref / broker order / receipt: ENTER effect `effect:sqlite-market-qual-0807:encoded-MTc4NjEyMjYwMDAwMDpFTlRFUg`, order ref `learn-ai/sqlite-market-qual-0807/v1:5AMrAr-1SsqXbLyaodnO9g`, broker order `c09fb7ed-dd7b-4d49-b892-5f62c0130a3a`; EXIT effect `effect:sqlite-market-qual-0807:encoded-MTc4NjEyMjc4MDAwMDpFWElU`, order ref `learn-ai/sqlite-market-qual-0807/v1:-dzPQRYbyg7CsMKN49ecng`, broker order `e7dd5d2f-669e-44c7-b008-a9e3e7644b87`; `reconciliation:321`; unintended Resume receipt `8045ab0e-22f7-4ef9-a846-eda169af4c0a`
Broker source timestamp: ENTER 1786122606676; EXIT 1786122787082
Clerk observation / durable record: ENTER fill 1786122606786; EXIT fill 1786122787217; attributed flat 1786122789036; normal STOP 1786122826701
Expected state: normal round trip, stopped-flat truthful panel, evidence drawer read-only, restart preserves authority and remains stopped
Observed state: round trip and flat reconciliation passed; panel truth defects appeared; later evidence interaction recorded Resume; SQLite reads then reported disk I/O error / database disk image malformed
Position proof: independent Alpaca REST after service shutdown returned positions []
Open-order proof: independent Alpaca REST after service shutdown returned open orders []
Operator action: stop data service; do not repair in place; verify finalized mirror read-only
Warning/error log reference: 17:28:15Z projection disk I/O error; later database disk image malformed; POST Stop returned 500
Recovery evidence: mirror identity matches generation 1 and DB identity; contiguous finalized hash chain through sequence 323 / hash 666d23091fd83cdde0e54459d45dd6c99e152b1fbdc8a8795f6407388be5966e / RUN_STOPPED
Verdict: PASS for baseline ENTER/EXIT; ABORT for truth/restart qualification
```

## Corruption mechanism and storage hardening

The incident database lived beneath the compose bind mount
`./PythonDataService/artifacts:/app/artifacts`. Inside the Podman VM that mount
reported `virtiofs`; the container writer used SQLite 3.46.1 while host-side
diagnostic readers used SQLite 3.51.0 from macOS. This placed one WAL authority
across two host/shared-memory and filesystem-locking domains. SQLite's official
[WAL documentation](https://www.sqlite.org/wal.html) requires every process to
run on the same host and states that WAL does not work over a network
filesystem. Its official
[corruption guide](https://www.sqlite.org/howtocorrupt.html#filesystems_with_broken_or_missing_lock_implementations)
also identifies broken or missing filesystem locks with concurrent access as a
database-corruption mechanism. The observed `runs` btree/index damage followed
an immediate container restart plus cross-boundary diagnostic access, matching
that prohibited topology.

The corrective layout masks `/app/artifacts/alpaca_clerk` with the persistent
VM-local `alpaca-clerk-data` named volume while leaving non-SQLite runtime
artifacts on the host bind mount. The migrated Clerk root reports `xfs` inside
the service. Repository initialization and open now refuse known FUSE/remote
filesystem types before creating or opening a WAL authority; focused tests pin
both the new-authority and existing-authority refusal. Recovery tooling is
copied/mounted into the image and the runbook requires default-compose
operations to use a one-shot `python-service` container against the same named
volume. Host and container SQLite clients must never alternate against one WAL
authority.

The sequence-326 database/mirror/decision-journal tree was copied into the
named volume only after a clean service stop. In-volume verification reproduced
`integrity_check=ok`, generation 1, the same database identity, revision 326,
and the same database/mirror head hash. The complete former FUSE-backed source
tree was moved, not deleted, to
`artifacts/sqlite-storage-migration-preserved/alpaca_clerk-fuse-20260807T1249CDT`.
The recreated service opened on XFS, completed boot recovery, remained stopped,
and independent Alpaca REST again returned no positions and no open orders.
A subsequent full Podman VM restart preserved the named volume; a fresh service
container again opened generation 1 / database identity
`03ed49bd38bb1f3a6462f81706e7dec2` at revision 326 with
`integrity_check=ok`, zero exposure, zero working orders, zero active holds,
and zero active uncertainties. Its panel projection remained
`OFF_DUTY / STOPPED / STOPPED_FLAT` with the three durable bar decisions intact.

## Operator-approved recovery and Qualification A attempt 3

```text
Scenario: Mirror rebuild, stopped-state restart, three advancing closed bars, Stop, reconciliation, integrity verification, and restart
Started at (ms UTC): 1786124232857
Ended at (ms UTC): 1786124632009
Authority generation / DB identity / control revision: 1 / 03ed49bd38bb1f3a6462f81706e7dec2 / 323 after rebuild, 326 after final reconciliation
Strategy instance / lifecycle run: sqlite-market-qual-0807 / 4ac1286daea244ed8f9493b77bb4d353
Command / effect operation / order ref / broker order / receipt: Resume receipt d24c7c6f-f2c1-4c3a-96a9-0d62dc7cfe73; RUN_STARTED sequence 324; no effect operation or order; STOP command cmd:PA3KWXU1C4C3:sqlite-market-qual-0807:4ac1286daea244ed8f9493b77bb4d353:STOP:STOPPED / sequence 325; reconciliation:326
Broker source timestamp: advancing minute bars ended at 1786124460000, 1786124520000, and 1786124580000
Clerk observation timestamp: decision receipts 1786124466164, 1786124525722, and 1786124585427; STOP 1786124612829; reconciliation 1786124632009
Durable record timestamp: same decision-journal and custody-transition timestamps; every write completed before the next observation
Expected state: recover only the finalized mirror head; remain stopped after boot; append one decision receipt per closed bar; stop flat; reconcile clean; remain stopped after another restart
Observed state: rebuild preserved the corrupt DB/WAL/SHM and restored all 323 finalized transitions; UI evidence interaction remained read-only; three advancing bars appended decision sequences 2-4 with no_action; stop and reconciliation advanced SQLite to 326; restart remained OFF_DUTY / STOPPED / runtime idle
Position proof: independent Alpaca REST before Resume, during the run, before shutdown, and after Stop returned positions []
Open-order proof: independent Alpaca REST at the same boundaries returned open orders []; operator panel showed 0 working orders
Operator action: approve documented rebuild; inspect recovered UI; Resume; observe three bars; Stop bot decisions; Reconcile now; graceful service stop; offline integrity/mirror verification; restart
Warning/error log reference: none for Alpaca SQLite Clerk recovery/run; separate pre-existing IBKR host-daemon/account-safety observer warnings did not affect the Alpaca authority
Recovery evidence: corrupt files preserved under recovery-preserved/pre-rebuild-1786124232857-4e619722; receipt 1786124232878-rebuild_from_mirror.json; offline PRAGMA integrity_check ok; mirror 326 rows / head hash 259b11dfb514a4f0ec4b1198500dbbe91c1c85e018ef552a43157ee07314ecd0 / file SHA-256 e93f1fefc2d5a071807e92e9def5dbc288fdd1f1204ca2c54ea660963f658693; authority migrated from virtiofs to the XFS-backed learn-ai-alpaca-clerk-data volume with the source tree preserved
Verdict: PASS for recovery, advancing-bar persistence, normal stop/reconciliation, and stopped-state restart; broader multi-session/race qualification remains pending
```

## Scenario receipt template

Copy one block per attempt. Never overwrite a failed attempt with a retry.

```text
Scenario:
Started at (ms UTC):
Ended at (ms UTC):
Authority generation / DB identity / control revision:
Strategy instance / lifecycle run:
Command / effect operation / order ref / broker order / receipt:
Broker source timestamp:
Clerk observation timestamp:
Durable record timestamp:
Expected state:
Observed state:
Position proof:
Open-order proof:
Operator action:
Warning/error log reference:
Verdict: PASS | SAFE FAILURE | ABORT
```

## Abort and incident record

| Time (ms UTC) | Phase/date label (prose) | Condition | Scope | Action | Final broker proof | Defect issue |
| --- | --- | --- | --- | --- | --- | --- |
| — | Initial canary — 2026-08-07 | IBKR feed stopped after one 5-second print; the never-advanced liveness blind window was characterized as unbounded | Feed admission | Operator stopped the bot; no decision or order occurred | Flat; zero open orders; durable stopped-flat receipt | Existing incident record in the first-canary audit; no new defect per the recorded operator decision |
| — | Before Qualification A broker contact | SQLite panel health said `Flat Resume ready`, but the presented action list omitted Resume | Broker V2 UI/action policy | Ceremony paused before broker contact; regression-first local fix; service restart and UI verification | Flat; zero working orders; `reconciliation:298` | [#1410](https://github.com/tim1016/learn-ai/issues/1410) |
| 1786121106666–1786121303876 | Qualification A attempt 1 | Both live IBKR consumers delivered one print and stalled; Trader/Operator continued to present Live/Healthy while durable Bot health had no last bar or decision | Feed delivery and UI truth | Stop bot decisions at `1786121283392`; Reconcile now at `1786121303876`; do not resume | Flat; zero working/unresolved orders; clean `reconciliation:301` | [#1411](https://github.com/tim1016/learn-ai/issues/1411) |
| 1786122826701–1786122842087 | Qualification A attempt 2 | After a valid round trip the panel reported flatten-required, one uncertain intent, empty selected-effect evidence, and no bar/decision | Broker V2 truth projection | Regression-first local fixes; no broadened exposure | Flat; zero open/working orders; clean `reconciliation:321` | [#1412](https://github.com/tim1016/learn-ai/issues/1412) |
| 1786123676085–1786123725672 | Qualification A corruption abort | Evidence-station interaction recorded an unintended Resume; projection then reported a malformed SQLite database | UI action routing and SQLite authority | Stop data service; preserve corrupt DB/WAL/SHM; verify current finalized mirror; await human confirmation | Independent Alpaca REST: paper account, no positions, no open orders; mirror ends in `RUN_STOPPED` | [#1413](https://github.com/tim1016/learn-ai/issues/1413) |
| 1786124232857–1786124632009 | Recovery and Qualification A attempt 3 | Operator-approved recovery of the preceding abort | SQLite authority and supervised paper runtime | Documented `REBUILD_FROM_MIRROR`; preserve corrupt files; verify identity/head; three-bar run; Stop; Reconcile; offline integrity/mirror check; restart idle | Independent Alpaca REST: no positions and no open orders; SQLite revision 326; clean reconciliation; mirror and database heads agree | [#1413](https://github.com/tim1016/learn-ai/issues/1413) |

Any duplicate economic intent, broker mutation without finalized SQLite intent,
mixed custody/lifecycle authority writer, unresolved account drift, regressed custody,
fabricated terminal result, stale evidence authorizing exposure, substituted
authority, UI/execution-policy mismatch, or failed recovery identity/hash check
ends the ceremony immediately. Stop all governed bots, preserve evidence, and
open a focused defect issue.

The activated path does retain a JSONL **signal-decision journal** for read-only panel
evidence. It is not a custody, order, lifecycle, or reconciliation authority; the
earlier blanket phrase "no mixed JSONL/SQLite writer" was imprecise.

## Local validation after adversarial remediation

- The affected Python regression set passed `728` tests, including the new
  multi-symbol, decision-intent/idempotency, recovery-proof/WAL-guard, epsilon-boundary,
  and effect-predicate parity cases.
- The two focused Angular panel files passed `25` tests; the evidence-station case
  asserts that interaction never calls the action dispatcher.
- The OpenAPI contract and generated Angular types were regenerated from the service
  schema. Ruff over `app/` and `tests/`, OpenAPI drift check, compose validation, and
  `git diff --check` passed.
- The normalized repository run completed with `8613` passed, `65` skipped, and `5`
  expected-failure tests unexpectedly passing. Its only two failures were outside this
  change: the opt-in 100k slow performance test exceeded its 60-second wall-clock budget
  under the full local run, and the live Polygon freshness canary received the test
  placeholder API key. The separate local LEAN replay gate was excluded because its
  gitignored cache is missing 66 of 501 required sessions. These are not recorded as a
  clean project-wide pass.
- A thermo-nuclear maintainability review found one immediate-replay-only decision
  deduplication gap; it was expanded to all historical bar references and regression
  tested before this report was finalized.

Residual non-blocking hardening risks remain documented rather than silently claimed
away: bounded source-stall detection depends on IBKR's documented
`reqRealTimeBars` one-bar-per-five-seconds contract and has no independent
per-request heartbeat if the vendor violates that contract; mirror rebuild reconstructs
control revision and does not preserve prior reset provenance; and filesystem-type
detection remains a denylist that cannot enforce the Linux mount check on non-Linux
hosts. Production remains constrained to the Linux/XFS named-volume topology.

## Final governance (do not complete early)

- [ ] All required live scenarios passed across multiple market sessions.
- [ ] Final account proof is fresh, flat, and order-free.
- [ ] Abort/incident record is complete, including `none` where applicable.
- [ ] Recovery runbook reviewed against observed operations and marked final.
- [x] Trader/Operator truth-language specification is published.
- [ ] ADR 0035 status changed from Proposed to Accepted for Alpaca paper only.
- [ ] ADR 0035's precise ADR 0001/0008/0030/0033 supersession text retained.
- [ ] Live-money trading remains disabled.

## Final verdict

**RECOVERED / PARTIALLY QUALIFIED / REQUEST CHANGES / STILL BLOCKED FROM
CLOSURE.** Issue #1409
must remain open. The human-approved mirror rebuild preserved the corrupt
authority files, restored the verified sequence-323 head, and the subsequent
three-bar run, Stop, reconciliation, offline integrity check, mirror check, and
restart advanced the healthy authority to sequence 326 while remaining flat
and order-free. The data service was healthy and the bot stopped at the end of
that recorded session. The local fixes and adversarial remediations must be
committed, independently reviewed, merged, and live-requalified; #1413's historical
trigger remains unresolved; and the remaining multi-session race/fault scenarios and
ADR acceptance must complete before the final governance checklist can be satisfied.
