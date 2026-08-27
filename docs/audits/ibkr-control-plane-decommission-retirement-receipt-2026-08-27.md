# IBKR control-plane decommission — retirement receipt

**Issue:** #1813 (umbrella) · **PRD:** #1817 · **Date:** 2026-08-27
**Plan:** [`docs/superpowers/plans/2026-08-26-ibkr-decommission-closeout.md`](../superpowers/plans/2026-08-26-ibkr-decommission-closeout.md)
**Source inventory:** [`docs/audits/ibkr-control-plane-decommission-inventory-2026-08-26.md`](ibkr-control-plane-decommission-inventory-2026-08-26.md)
**Slice-0 handoff:** [`docs/audits/ibkr-decommission-post-slice-0-handoff-2026-08-26.md`](ibkr-decommission-post-slice-0-handoff-2026-08-26.md)

This is the close-out record for the IBKR control-plane decommission: what was
removed, which pull request removed it, how it was proved dead before removal,
and where anything that was preserved now lives. It is the acceptance artifact
PRD #1817 requires (user stories 23–24).

After this document, **nobody is coming back to this work.** So the receipt
follows the standard PR-B's thermo round set for the whole project: every
orphan ends in exactly one of two states — **deleted**, or **recorded
specifically enough that a reader can tell "deliberately retained" from
"missed."** Where something was found and deliberately not fixed, it is named
in [§12](#12-found-and-deliberately-not-fixed) rather than left out.

---

## 1. Result in one paragraph

The IBKR integration is now a **market-data feed and nothing else**. Every
IBKR account-authority, order-actuation, P&L, session-mirror, broker-activity,
host-daemon and live-run control surface is gone from the repo — 264 files
deleted, 50 HTTP routes retired, 138 OpenAPI schema components removed, and
the executable control plane replaced by nothing rather than by a shim. The
project's single hardest acceptance criterion — the structural feed-boundary
test carrying **zero** `_ALLOWED_EXCEPTIONS` — is met, and met the only way
the repo's own rule allows: every tracked exception was closed by **retiring
its blocking consumer**, never by widening the allow-list.

Alpaca is untouched and remains the sole live execution path. Activated SQLite
remains Alpaca's sole position, attribution and FIFO authority.

### Scoreboard

| | Slice-0 master `03ce52b6` | after PR-A | after PR-B | after PR-C `9b491b1a` |
|---|---|---|---|---|
| `_ALLOWED_EXCEPTIONS` | 3 | 3 | **0** | **0** |
| OpenAPI routes | 261 | 239 | 211 | 211 |
| OpenAPI schema components | 730 | 659 | 592 | 592 |
| Control-surface manifest prefixes | 13 | 13 | 9 | 9 |
| `BANNED_PREFIXES` in the boundary test | 29 | 29 | 29 | 26 |
| `RETIRED_MODULES` guard | — | — | — | **100** |
| Python suite (passed) | 8545 | 8165 | 7297 | **7294** |
| Frontend suite (passed) | 1899 | 1899 | 1855 | **1854** |

---

## 2. Commit ranges and merge record

The combined-PR plan was **cancelled** by user instruction on 2026-08-27. All
three pull requests merged into `master` individually, in order, as **regular
merges** — never squashes. The branches are stacked, so a squash of a parent
would have made each child re-present its parent's changes as new.

| PR | Branch | Range | Commits | Merged as |
|---|---|---|---|---|
| **#1818** — PR-A, account authority | `decommission/pr-a-account-authority` | `03ce52b6..540af359` | 12 | master `428ff558` |
| **#1819** — PR-B, session & execution | `decommission/pr-b-session-execution` | `540af359..ac908f28` | 26 | master `dcf456b4`, CI green |
| **PR-C** — consolidation & close-out | `decommission/pr-c-consolidation` | `ac908f28..9b491b1a` | 16 | this PR |

PR-C against master: **54 files changed, 840 insertions, 2 743 deletions.**
Across all three: 267 file removals at full-range resolution (264 deletions
plus 3 renames), and the machine-generated OpenAPI contract and
`broker.types.ts` regenerated from them.

Ancestry was verified after each merge — `git merge-base --is-ancestor`
confirms both `540af359` and `ac908f28` are in master's ancestry, so PR-B's
merge contributed only its own 26 commits.

---

## 3. How to read the retirement ledger

For a **whole-module deletion**, the module is the unit and the ledger names
the file; every symbol it defined went with it, and listing them individually
would obscure rather than reveal. For a **sub-file removal** — a symbol cut
out of a file that survives — the ledger names the symbol, because that is the
only resolution at which the removal is visible.

That distinction is not cosmetic. It is the single lesson this project paid
for four times over; see [§14](#14-the-lesson-worth-recording).

The **evidence** column summarises how each group was proved to have no live
consumer. Full derivations are in the task reports and independent reviews
under `.superpowers/sdd/2026-08-26-ibkr-decommission-closeout/`.

---

## 4. Retirement ledger — PR-A (#1818): account authority

Retires the IBKR Account Clerk, Account Truth, account reconciliation,
account safety, and the account-facing operator surfaces.

### 4.1 Backend modules deleted (whole file)

| Module | Evidence |
|---|---|
| `app/broker/ibkr/account.py`, `account_recovery.py`, `account_truth.py`, `account_truth_freshness.py` | Their HTTP surface (`/api/broker/account`, `/api/broker/account-truth`, `/api/broker/positions`) was retired in the same commit; AST import sweep across `app/` + `tests/` returned zero surviving importers. |
| `app/engine/live/account_observation_lease.py`, `account_safety.py`, `account_session_policy.py`, `journal_recovery_state.py` | `account_safety.py` (916 LOC — the `AccountSafetyAuthority` itself) was **missed by the implementer's first sweep** and found by the independent review's own AST sweep. Zero production callers confirmed at symbol level. |
| `app/routers/account_reconciliation.py`, `broker_account_truth.py` | Routers deleted with their routes; the OpenAPI contract regenerated in the same PR proves the surface is gone, and the contract regen is CI-gated. |
| `app/schemas/account_cockpit.py`, `account_directory.py`, `account_events.py`, `account_safety_snapshot.py`, `account_truth.py`, `journal_recovery.py`, `presented_operator_action.py` | Deleted whole. `app/schemas/account_reconciliation.py` was **gutted here, not deleted here** (−366 / +13): 25 of its 27 models were dead, but `LegacyStaleClaimRetirementReceipt` was verified **load-bearing** — the backward-compat on-disk schema for `legacy_stale_claim_retirements.json` — and kept. PR-B deleted the remaining 36 lines once its last reader went. |
| `app/services/account_cockpit.py`, `account_desk_guidance.py`, `account_directory.py`, `account_event_journal.py`, `account_gate_policy.py`, `account_gate_promotion.py`, `account_reconciliation.py`, `account_safety_access.py`, `account_safety_snapshot.py`, `account_truth_refresh.py`, `account_truth_snapshot.py`, `presented_account_actions.py`, `journal_recovery.py`, `clerk_custody_timeline.py`, `clerk_transaction_projection_ibkr.py`, `clerk_transaction_projection_store.py`, `observation_lease_parity.py` | `LegacyStaleClaimRetirementService` was orphaned **by this PR's own deletions** and missed by both implementers' import-graph sweeps; found by the thermo round. Its file was **gutted here** (−390 / +15) rather than deleted, because `fleet_contamination.py:290` still called `retired_legacy_claim_keys()`; PR-B deleted the remaining 150 lines. Symptom worth recording: a hand-built fake `AccountTruthResponse` fixture had been *added* to keep the dead service's tests green rather than deleting them. |
| `app/services/fleet_contamination.py` — two functions in PR-A, whole file in PR-B | PR-A's fix round removed the last production caller into `app/engine/live/fleet.py`'s exposure fold. Both functions confirmed zero production callers tree-wide by the independent reviewer before removal. |
| `scripts/archive_observation_lease_parity.py` | Script for a parity artifact whose producer was deleted in the same PR. |

### 4.2 Frontend surfaces deleted (PR-A)

`account-desk-directory-store.service.ts`, `account-freeze-banner/`,
`account-roster/`, `account-safety/` (`account-safety-snapshot.store.ts`,
`account-truth-spine.component.ts`, `presented-account-action.component.ts`),
`account-truth-board/` (board + execution-history), `broker-operation-result/`,
`shared/operator-blocker-list/`, `lib/account-posture-tag-severity.ts`,
`testing/account-triage-fixtures.ts`, `src/testing/account-safety-snapshot-fixtures.ts`,
and the mirror type files `api/account-cockpit.types.ts`,
`account-directory.types.ts`, `account-events.types.ts`,
`account-reconciliation.types.ts`.

**Evidence:** every deletion verified three independent ways — grep, a
base-vs-HEAD orphan-reachability sweep, and a full AOT build with
`strictTemplates`. The `broker.types.ts` regen was reproduced byte-for-byte.

### 4.3 Sub-file symbol removals (PR-A)

**`Frontend/src/app/services/broker.service.ts` — 20 methods removed**
(36 → 16 at PR-A's tip): `acceptExposureOverride`, `account`, `accountCockpit`,
`accountEvents`, `accountSafetySnapshot`, `accountServiceStatus`,
`accountTriage`, `accounts`, `clearAccountFreeze`,
`executePresentedReconcileNow`, `latestAccountReconciliation`,
`legacyStaleClaimCandidates`, `positions`, `presentLifecycleAction`,
`reconcileAccount`, `recoverAccountJournal`, `repairAccountEventSequence`,
`retireLegacyStaleClaim`, `traderAccountEvents`,
`updateAccountReconciliationAutomation`.

**`ClerkTransactionFilters.broker`** — first narrowed to Alpaca-only, then
deleted entirely in the thermo fix round, along with the unreachable
conditional and the three specs that existed only to exercise it. A field
narrowed rather than deleted leaves an unreachable-in-production branch pinned
by tests; that is not a retirement.

**`SecType`** (`Frontend/src/app/api/broker-models.ts`) — orphaned here by
`cdad955f`, deleted later in PR-B. (Recorded because PR-B's report initially
mis-attributed the orphaning to itself.)

### 4.4 Routes retired by PR-A — 22

`/api/accounts` · `/api/accounts/{account_id}/clerk` · `/cockpit` · `/events` ·
`/events/repair-sequence` · `/events/trader` · `/freeze/accept-exposure-override` ·
`/freeze/clear` · `/journal-recovery/quarantine` · `/journal-recovery/rebaseline` ·
`/presented-actions/reconcile-now` · `/reconciliation` · `/reconciliation/automation` ·
`/reconciliation/latest` · `/safety-snapshot` · `/session-policy` · `/triage` ·
`/api/broker/account` · `/api/broker/account-truth` · `/api/broker/orders/completed` ·
`/api/broker/orders/what-if` · `/api/broker/positions`

OpenAPI schema components: **730 → 659** (71 removed).

### 4.5 Test families retired (PR-A)

20 backend test files deleted plus ~25 trimmed test functions; suite
**8545 → 8165**. Three deleted tests were mixed Alpaca+IBKR — the independent
review caught that deleting them for the IBKR half also dropped Alpaca-path
coverage, and the `projection_unavailable` contract on a live endpoint was
re-covered before the PR closed.

---

## 5. Retirement ledger — PR-B (#1819): session, execution, host bridge

Retires the broker session mirror, broker activity, bot-event projections,
live-run and live-instance control surfaces, IBKR order/P&L/persistence/search,
and the host-daemon bridge. **This is the PR that closed all three tracked
structural-boundary exceptions.**

### 5.1 Backend modules deleted (whole file)

| Group | Modules | Evidence |
|---|---|---|
| Exposure fold (step 0) | `app/engine/live/fleet.py`, `app/services/fleet_contamination.py` | Answered the question PR-A's reviews routed here. PR-A had already retired their last production caller; PR-B re-confirmed zero production callers via a full AST import sweep before deleting. Cascade-orphans enumerated by name in both registry docs. |
| Cascade orphans of the fold | `app/services/legacy_stale_claim_retirement.py`, `account_journal_authority.py`, `app/schemas/account_reconciliation.py` (the remnants PR-A kept — see §4.1), and `app/engine/live/account_identity.py` | The first three each had **exactly one** production caller — the module PR-B had just deleted — confirmed via the same AST sweep. `account_identity.py` (122 lines) was **missed by that module-level sweep** and found by the thermo round: it lost its only production importer when this PR deleted `fleet_contamination.py` — the very deletion whose cascade-orphans the registry rows enumerate by name. Its `account_identity_*` grep hits in three Alpaca modules are unrelated snake_case **field names**, a false positive worth naming. |
| Host bridge | `app/engine/live/host_daemon.py`, `host_daemon_client.py`, `host_runner_policy.py`, `daemon_auth.py`, `daemon_transport.py`, `command_channel.py`, `control_plane.py`, `broker_socket_probe.py`, `broker_callbacks.py`, `intent_wal.py`, `run_lookup.py`, `app/services/host_capability.py` | The host daemon was never in the bar path. Booted-app route enumeration (227 routes) confirmed nothing served them. |
| Session mirror | `app/services/broker_session_mirror.py`, `broker_session_events.py`, `broker_session_history.py`, `broker_session_reconciler.py`, `app/routers/broker_session.py`, `app/operator/notices/broker_session.py`, `app/schemas/broker_session.py`, `app/services/durable_event_channel.py`, `durable_event_stream.py`, `sse_keepalive.py` | Closed structural exception; consumers deleted in the same PR. |
| Broker activity | `app/services/broker_activity_publisher.py`, `broker_activity_publisher_registry.py`, `broker_activity_reconciler.py`, `broker_activity_reconstruction.py`, `broker_activity_templates.py`, `broker_activity_wal.py`, `activity_evidence_matching.py`, `activity_repair_projection.py`, `app/routers/broker_activity.py`, `app/schemas/broker_activity.py`, `app/operator/notices/broker_activity_health.py` | Registry rows in `engine-authority-map.md` and `math-sources-of-truth.md` updated in the same PR — the "never silently drop a registered canonical path" rule. |
| Bot events | `app/services/bot_event_incidents.py`, `bot_event_projection.py`, `bot_event_rejection_bridge.py`, `bot_event_replacement_map.py`, `bot_event_stream_service.py`, `bot_event_wal.py`, `app/routers/bot_events.py`, `app/schemas/bot_events.py` | Routes retired with them; AST sweep clean. |
| Live-run projections | `app/routers/live_runs.py`, `live_instances.py`, `app/services/live_run_state.py`, `live_log_parser.py`, `live_log_failures.py` | |
| IBKR order / P&L / persistence / search | `app/broker/ibkr/orders.py`, `order_history.py`, `order_previews.py`, `order_projection.py`, `order_evidence.py`, `order_error_stream.py`, `pnl.py`, `persistence.py`, `symbol_search.py`, `diagnostics.py`, `app/broker/safety_verdict.py` | Removing `symbol_search` also removed it from `RETAINED_FEED_MODULES`, which is what closed the last boundary exception. |
| Installers | `PythonDataService/scripts/install-host-daemon-systemd-user.sh`, `install-host-daemon-service.ps1`, root `start-live-daemon.sh`, root `bootstrap-host-daemon.sh` (rewritten as `bootstrap-host-venv.sh`) | See [§5.5](#55-the-installers--a-deletion-pr-that-made-a-footgun-reachable). |

`app/routers/broker.py` was **split**, not gutted: 1 000 → 614 lines, keeping
only the retained feed surface.

### 5.2 Sub-file symbol removals (PR-B)

Found by a **symbol-level** re-sweep in the thermo fix round, after the
module-level sweep confirmed itself clean:

| Symbol | File | Evidence |
|---|---|---|
| `IBKR_CODE_MEANINGS` cluster | `app/broker/ibkr/event_codes.py` (131 → 19 lines) | Exactly one occurrence in the tree — its own definition. All five surviving frozensets intact. |
| `IbkrAccountSummary`, `IbkrOrderSpec` | `app/broker/ibkr/models.py` | `IbkrOrderSpec` had no typed use anywhere. Its **description** was preserved: `intent_ledger.py:57` documents durable ledger rows still on disk, so the docstring was rewritten to enumerate the stored dict shape directly. Deleting a type is fine; deleting the description of data still on disk is not. |
| `confined_wal_path` | `app/services/jsonl_wal.py` | One occurrence — its own definition. |
| `BrokerService.diagnose()`, `searchSymbols()` | `Frontend/src/app/services/broker.service.ts` | Their routes were retired in the same PR. |
| `IbkrObjectSnapshot` | `Frontend/src/app/api/broker-models.ts` | **Orphaned by this PR and left standing** by a file-level sweep that could not see it. Found by a symbol-level orphan diff over all 1 476 exported symbols in `Frontend/src`, base vs tip: exactly one symbol crossed from 12 references to zero. |
| 18 model types + `operator-severity.ts` → `operator-observability.types.ts` chain | `Frontend/src/app/api/` | Two-level cascade orphan; import-graph scan run against **both** base and final tree, no-importer set unchanged at 20 files. |

### 5.3 Frontend surfaces deleted (PR-B)

`broker-session-mirror/` (component, events panel, fixtures),
`broker-orders/broker-order-feed-status.component.*`,
`shared/broker-instrument-card/`, `services/broker-session-mirror.service.ts`,
`services/broker-connectivity.service.ts`, `api/broker-session-mirror.types.ts`,
`api/operator-observability.types.ts`, `components/broker/operator-severity.ts`.

### 5.4 Routes retired by PR-B — 28

`/api/broker/diagnose` · `/orders/open` · `/orders/stream` · `/pnl/stream` ·
`/pnl/positions/stream` · `/session-mirror` (+ `/events`, `/events/purge`,
`/events/stream`, `/history`, `/history/purge`, `/stream`) · `/symbols/search` ·
`/api/live-instances/daemon-health` (+ `/renew-lease`) ·
`/api/live-instances/{id}/broker-activity` (+ `/stream`) · `/api/live-runs` ·
`/api/live-runs/{run_id}/` × `bot-events`, `bot-events/stream`, `commands`,
`desired-state`, `executions`, `failures`, `incidents`, `log-tail`, `status`,
`trades`

OpenAPI schema components: **659 → 592** (67 removed).

### 5.5 The installers — a deletion PR that made a footgun *reachable*

Commit `4e531e01` is titled "Retire the IBKR host bridge **and its installer
wiring**" and touched neither installer. `install-host-daemon-systemd-user.sh`
survived, committed, with `ExecStart=… -m app.engine.live.host_daemon` — a
module the same PR deleted. Its Windows sibling installed the same command as
an auto-start NSSM service.

What made it a blocker rather than residue: its only precondition is that
`PythonDataService/.venv` exists, and this PR's **own new**
`bootstrap-host-venv.sh` teaches every operator to create it. The guard would
have started passing on exactly the machines the PR provisions, and the script
then runs `systemctl --user enable --now` on a unit with `Restart=on-failure`
/ `RestartSec=10` — an unbounded 10-second crash loop on `ModuleNotFoundError`.
Both installers deleted; zero repo references remain. **The commit message
that claimed otherwise is disclosed rather than corrected in history.**

### 5.6 Config and installer fields removed

| Field | Where | Note |
|---|---|---|
| `persist_ticks`, `persist_account`, `persist_pnl`, `persist_dir` | `app/broker/ibkr/config.py` | Parquet archive flags for the deleted `persistence.py`. |
| `broker_session_event_retention_count` | `app/broker/ibkr/config.py` | Rolling window for the deleted session mirror. |
| `live_runner_daemon_url` | `app/broker/ibkr/config.py` | Host-daemon URL (ADR 0004); the daemon is gone. |
| `IBKR_PERSIST_TICKS` | `compose.yaml` | Env passthrough for `persist_ticks`. |
| `--with-host-daemon`, `WITH_HOST_DAEMON` | `setup-macos.sh` | Section 7 (host live-engine daemon) removed; the closing banner now points at `bootstrap-host-venv.sh` and the host-venv pytest gate. |

`IBKR_READONLY` and `IBKR_BROKER_ENABLED` are **retained** — their comments
were corrected, because both described a control plane that no longer exists.

### 5.7 Test families retired (PR-B)

Suite **8165 → 7297**. Reconciled by diffing `pytest --collect-only` node-id
lists against a `git archive` of the PR-A tip: 864 node ids removed, 4 added,
each attributed to a named deleted file or trimmed function. 17 retirement-
contract and data-plane-security tests whose subject this PR deleted were
retired, each judged individually — a security guard test dropped for a route
that still exists would have been a real regression, and none were.

### 5.8 One inherited bug fixed here rather than by reopening PR-A

`GET /api/market-data-feed/health` carried the always-on control-secret guard
but appeared in **neither** `control_prefixes` nor `protected_read_prefixes`,
so it would 403 from the browser once the secret is configured. The manifest
test missed it because it only checked unsafe-method routes. Fixed on PR-B's
branch: `/api/market-data-feed` declared in `protected_read_prefixes`, and the
test extended with a companion assertion requiring **every** always-guarded
safe-method route to sit under a declared prefix (44 routes, all covered).
Mutation-tested: pulling the entry reds that assertion and only that one.

---

## 6. Retirement ledger — PR-C: consolidation and close-out

### 6.1 Modules deleted

| Module | Evidence |
|---|---|
| `app/engine/live/journal_exposure.py` | The chartered disposition decision — see [§7](#7-the-journal_exposurepy-disposition-the-decision-two-prs-deferred). |
| `app/services/activity_projection_contract.py` + its two DTOs | Orphaned *before* PR-B and correctly disclosed there rather than swept; a close-out PR is the right place for it. |
| `app/broker/runtime_snapshot.py` | Only caller was its own test. Disclosed by PR-B as a pre-existing orphan and left; taken here because it lives under `app/broker/` — an IBKR module, squarely #1813's residue. |
| `Frontend/src/app/components/broker/ibkr-portal.ts` (+ spec) | Only importer was its own spec. |
| `Frontend/src/app/api/live-runs.types.ts`, `live-runs-controls.types.ts`, `live-instances.types.ts` | Hand-written mirrors of Python models PR-B and C-1 deleted; see [§9](#9-frontend-mirror-types--the-rule-that-settled-it). |

### 6.2 `app/schemas/live_runs.py` — 44 unreachable symbols DELETED, not recorded

**915 → 180 lines.** 48 module-level names removed, 9 retained.

> **Amended by PR-C's thermo fix round.** The narrative below records the
> measurement as it stood when it was written: 46 removed, 11 retained. The
> review then found that two of the 11 — `ExitReason` and `RunStatusSidecar` —
> had no production consumer at all. Their only importers were
> `routers/live_runs.py` and `services/live_run_state.py`, both retired by
> *this* decommission, which left one test file as their sole referrer
> repo-wide. Re-measured by AST at symbol level over `app/`: this file exports
> exactly **four** names that `app/` imports — `GateResult`,
> `BotDutyOutcomeView`, `ReconciliationReceipt`, `MutationRungReceipt` — across
> **five** production modules. Both dead names were deleted along with the
> model-only half of `tests/engine/live/test_run_status.py`; that file's three
> `_atomic_write_json` tests remain, because `app.engine.live.run_status` has
> two live importers. The headline totals above are post-amendment; the "6
> externally imported symbols" figure and the "Retained" list below are
> annotated where they stand.

PR-B's thermo round had cleared this file's orphaned DTOs as "genuinely
PR-C-sized, not a deletion cascade" — correctly, at the time: the 915-line
file still served live importers. Task C-1 then measured 39 transitively-dead
symbols and **recorded rather than deleted** them, because its brief scoped the
file to a docstring fix.

The independent review re-ran that method and got a different answer: **6**
externally imported symbols (by **5** production files, not 6), 11 reachable,
therefore **44 unreachable, not 39** — the five intra-file names had been
double-counted. And the row listed no names, so nothing was actually pinned. A
follow-up driven by that row would have deleted the wrong set.

**That 6 was itself two too many** (thermo round 1, M-1). It counted
`ExitReason` and `RunStatusSidecar`, whose only referrer was a test file. The
externally imported figure, re-measured by AST over `app/`, is **4** — by the
same 5 production files.

**Ruled: delete the 44, keep the 11, replace the residue row with a short
accurate retirement statement.** Reasoning:

- It is **#1813's own cascade**, not inherited debt — and this project's rule
  is that a PR deletes what it orphans.
- **PR-C is the close-out.** "Recorded for later" has no later.
- **A recording that came out numerically wrong on its first attempt is itself
  the argument against recording.** Deleting removes the need for anyone to
  trust a count.

The fix-round implementer **re-derived the set rather than inheriting it** (55
classified symbols / 6 external / 5 production importers / 11 reachable / 44
unreachable — set-identical to the review's) and deleted all 44 with no
exceptions kept. Contract neutrality was **re-proved, not inherited**: the
OpenAPI contract came out byte-identical, `export_openapi_contract.py --check`
exit 0 twice.

The find that justifies insisting on AST over grep: **five same-name production
hits turned out to be separate definitions** — `ChartOverlayNotice`
(`app/services/live_chart_window.py`), `BrokerConnectionState`
(`app/broker/ibkr/models.py`), `HydratePolicy`
(`app/engine/live/indicator_state.py`), `TradingSessionPhase`
(`app/services/session_authority.py`) and `OpenRunbookAction`
(`app/schemas/operator_blocker.py`) — the exact case where grep says "live" and
AST says "dead". The contract's `OpenRunbookAction` is `operator_blocker.py`'s
model, not `live_runs.py`'s. (This sentence said "six" against a list of five
until PR-C's thermo fix round re-counted it: an AST census of the 46 removed
names against every top-level definition under `app/` returns exactly these
five. The count in the prose was wrong; the list was right.)

**Removed (46):** `RunState`, `LiveRunSummary`, `DecisionsSummary`,
`ExecutionsSummary`, `TradesSummary`, `FlagsSummary`, `ArtifactFile`,
`ArtifactsSummary`, `ReconcileSummary`, `LiveRunStatus`, `LogLine`,
`FailureRecord`, `IncidentRecord`, `HydratePolicy`,
`DEFAULT_MAX_ORDERS_PER_DAY`, `HostRunnerProcessState`,
`HostRunnerProcessStatus`, `AccountClerkHealth`, `HostRunnerHealth`,
`EmergencyFlattenRequest`, `AccountEmergencyFlattenDispatchRequest`,
`AccountEmergencyFlattenAuthorizationRequest`,
`AccountEmergencyFlattenResponse`, `_validate_bare_ibkr_host`,
`HostRunnerClerkEnsureRequest`, `MutationOutcomeUnknownResponse`,
`DesiredStatePathStatus`, `DesiredStateValue`, `DesiredStateView`,
`CommandSummary`, `CommandTimelineEntry`, `CommandsTimeline`, `ReadinessGate`,
`ReadinessVector`, `InstanceBrokerView`, `BrokerConnectionState`,
`TradingSessionPhase`, `OpenRunbookAction`, `BrokerActivityHealthFacts`,
`BrokerActivityHealth`, `ChartOverlayNotice`, `ActivityEvidenceRef`,
`ActivityBrokerEventRow`, `FleetExplainedBucket`, `FleetContamination`,
`FleetAccountSummary`.

**Retained (9):** `MutationBlockageStageId`, `MutationRungReceiptCode`,
`MutationRungReceipt`, `GateResultStatus`, `GateResult`, `ReceiptStatus`,
`ReceiptOutcome`, `ReconciliationReceipt`, `BotDutyOutcomeView`.

**Removed by the thermo fix round (2 more, bringing the total to 48):**
`ExitReason`, `RunStatusSidecar`. Both were listed as retained above until
thermo round 1 (M-1) showed they had no production consumer: `git grep` at
slice-0 base `03ce52b6` puts their importers in `routers/live_runs.py` and
`services/live_run_state.py`, two modules this PR's own `RETIRED_MODULES` list
names as deleted. That makes them #1813's own cascade, not inherited debt, so
the same rule that deleted the 46 deletes them. `RunStatusSidecar.exit_reason`
was `ExitReason`'s only user, so the two go together. The production reader of
the artifact they described,
`app/engine/live/exit_taxonomy.py::read_run_exit_evidence`, hand-parses
`run_status.json` with a bare `str` and never touched either model.

Two of the nine retained are read only by modules that were themselves already
importer-less at `03ce52b6` — `app/engine/live/reconciliation_receipt.py`
(`ReconciliationReceipt`) and `app/services/mutation_rung_receipts.py`
(`MutationRungReceipt`). Verified by AST module-importer scan over `app/` and
`tests/` at that base and at HEAD: zero either side. They are pre-existing debt
this decommission did not create, and the rule stated in [§9](#9-frontend-mirror-types--the-rule-that-settled-it)
keeps them out of its scope.

> **Reconciling 44 with 46.** The classification counted **55** top-level
> schema symbols; the file actually declares **56** public names plus one
> private function. The two names removed beyond the classified 44 are the
> module constant `DEFAULT_MAX_ORDERS_PER_DAY` and the private validator
> `_validate_bare_ibkr_host`, which existed only for the deleted
> `HostRunnerClerkEnsureRequest`. Both return zero references across
> `PythonDataService/`, `Frontend/src` and `contracts/`. The counts are stated
> here in full because on this project **counts in prose have been the least
> reliable claims, on every side** — three of four findings in one review round
> were themselves "the count is one short."

### 6.3 Narrowed unions

The two IBKR API-name unions were narrowed to the calls that can still happen —
9 requests and 8 callbacks. The narrowing reaches the browser through the
OpenAPI contract, so it was the review's second hunt. An independent AST scan
over `app/`, handling positional **and** keyword call forms, reproduced exactly
those 9 and 8 with **zero non-literal call sites**, so no name can arrive
dynamically. On `error` specifically: an IBKR error is recorded through
`IbkrApiEvidenceEvent.error`, a field entirely separate from the callback
union. `reqCurrentTimeAsync` was a fourth cut beyond the thermo-sanctioned
list, disclosed after the review found the disclosure one name short.

Contract impact across the whole of PR-C: **1 insertion / 23 deletions** —
exactly the two narrowed enums. Schema component count unchanged at 592.

### 6.4 Structural retirement guard added

`test_ibkr_feed_boundary.py` gained `RETIRED_MODULES` — **100 dotted module
paths** that must no longer resolve — plus two guards:
`test_retired_modules_no_longer_resolve` and
`test_no_surviving_module_references_a_retired_module`, each with an explicit
non-vacuity assertion. `BANNED_PREFIXES` dropped from 29 to 26: three entries
(`app.broker.ibkr.account_recovery`, `account_truth`, `account_truth_freshness`)
became redundant once `RETIRED_MODULES` made their absence a pinned fact.

Mutation-tested four ways by the independent review. The negative proof worth
recording: a **nested** import of a deleted module left
`test_live_chart_window.py` fully green, and the new guard caught it.

### 6.5 Code-judo consolidations (not deletions)

- `BarSessionPhase` collapsed to one definition in the neutral `feed.py`.
- `JsonlWal`'s **four** inline copies of the CodeQL path sanitizer
  (`realpath` + `startswith(root_prefix)`) collapsed into one property. The
  8 new WAL tests were run against the **pre-refactor** code first, to prove
  behaviour preservation rather than assume it.
- `tests/routers/test_broker_search_endpoints.py` →
  `test_broker_option_contracts_endpoints.py` — the file was named for a
  surface that no longer exists.

---

## 7. The `journal_exposure.py` disposition — the decision two PRs deferred

**Outcome: RETIRED by PR-C.** `app/engine/live/journal_exposure.py` deleted,
together with `tests/engine/live/test_journal_exposure.py` (9 tests) and the
`tests/fixtures/golden/journal-exposure-projection/` golden fixture.

This is the one decision the whole project deliberately deferred: PR-A deferred
it to PR-B; PR-B deferred it to PR-C or a human and said so in both
registry-doc rows (commit `6cdc20f8`); PR-C was chartered to settle it. It is
also the highest-stakes deletion in the project, because
`docs/math-sources-of-truth.md` registered the fold as **canonical math**.

**Evidence, all re-derived independently by the review against git history
rather than the current tree:**

1. **Zero production callers, confirmed at symbol level** — all eight exported
   names swept individually, not just the module path. Its sole prior
   production importer, `fleet_contamination.py`, was deleted by PR-B.
2. **The surviving-Alpaca-consumer hypothesis was tested and disproved, not
   assumed.** The vendor adapter that fed this fold,
   `app/broker/alpaca/clerk/exposure.py`, **and** the parity test the module's
   docstring cited, `test_alpaca_projection_uses_canonical_execution_fold`,
   were **both deleted by `e3e302b6` (#1679, "retire legacy broker control") on
   2026-08-19** — over a week before this decommission opened. The docstring
   citation was therefore never evidence of a live consumer; it had been
   dangling for eight days.
3. **The dependency ran the other way.** `journal_exposure.py` imported
   `position_quantity_is_nonzero` **from** the Alpaca SQLite folds, at line 60
   inside `fold_execution_exposure`. Alpaca never depended on this module; this
   module depended on Alpaca's canonical flatness primitive.
4. **The golden fixture was never in the manifest**, so nothing was orphaned by
   deleting it. It was deleted **with the code it proved** — never regenerated
   to make anything pass, which `numerical-rigor.md` names as an anti-pattern.

**Both registry rows now state the outcome, not the deferral.**
[`docs/math-sources-of-truth.md`](../math-sources-of-truth.md) and
[`docs/architecture/engine-authority-map.md`](../architecture/engine-authority-map.md)
each say **retired 2026-08-27** with the evidence above. A repo-wide grep for
deferral language returns exactly one hit — the historical sentence at
`engine-authority-map.md:142` that then settles it.

**No concept is left unowned.** The surviving flatness primitive is
`app/broker/alpaca/clerk/sqlite/folds.py::position_quantity_is_nonzero`,
registered at `docs/math-sources-of-truth.md:105` (Alpaca Clerk attributed
position row). Cascade-deleted with the fold:
`app/engine/live/account_clerk_journal.py::normalize_broker_event`, a six-line
validator whose only external consumer was this fold.

**ADR 0036 consequence 3** named this fold as work that "must conform" and is
`Status: Accepted`. It now carries a dated *"Superseded 2026-08-27 by PR-C of
#1813"* paragraph stating that the consequence is no longer satisfiable as
written and that the conformance obligation now attaches to
`position_quantity_is_nonzero`. The original text is left standing — ADRs stay
historical, per PRD #1817's Out of Scope list.

---

## 8. `BrokerService` → `MarketDataFeedService`, and the surface split

`BrokerService` straddled **two unrelated surfaces**: `/api/broker` (the IBKR
feed) and `/api/accounts/{id}/transactions` (the **Alpaca** Clerk transaction
history, which the source inventory explicitly lists as active and *not* IBKR).
Renaming it to a feed name would have made the name false in a new way — the
same defect PR-B's review caught in this class's *docstring*, described around
rather than fixed.

So PR-C **moved the three Clerk methods verbatim into the existing
`BrokersService`** — `accountTransactions`, `accountTransaction`,
`acknowledgeExternalOrder` — and then renamed the remainder to
[`MarketDataFeedService`](../../Frontend/src/app/services/market-data-feed.service.ts)
(11 methods: `capability`, `connect`, `dataPlaneHealth`, `disconnect`,
`expirations`, `health`, `ibkrApiEvidence`, `probeCapability`, `reconnect`,
`searchOptionContracts`, `strikes`).

**Why this is not scope creep.** The plan said "rename, not rewrite", so it
went to the reviewer as the hardest hunt, and was **upheld**: the plan forbids
splitting into *new* services and none was created; the source inventory
sanctions this exact split at
[`ibkr-control-plane-decommission-inventory-2026-08-26.md:496`](ibkr-control-plane-decommission-inventory-2026-08-26.md);
all three methods diff **byte-identical** between old and new home; and
`account-desk-transaction-history.component.ts` injected **both** services at
base and one at head.

The class went 36 methods at Slice 0 → 11 today. No Alpaca route moved: the
`/api/accounts/{account_id}/transactions` surface is intact.

---

## 9. Frontend mirror types — the rule that settled it

PR-C deleted hand-written mirrors of Python models the backend no longer has.
Four of them — `ExitReason`, `MutationRungReceipt`, `MutationRungReceiptCode`,
`MutationRungReceiptStageId` — were deleted, then **restored** into a new file,
then **deleted again** by the thermo fix round. The restore was wrong; this
section records why, because the reasoning is the useful part.

**The rule, unchanged: this PR deletes what #1813 orphaned — including what its
own deletions orphaned — and does not delete what was already dead before it.**

The restore rested on a premise that turned out to be false: that C-1 had
deliberately kept the four types' Python models as *live*. Two of them,
`ExitReason` and `RunStatusSidecar`, had in fact been orphaned by **this PR's
own kills** (`routers/live_runs.py`, `services/live_run_state.py`) and are now
deleted on the Python side too — see [§6.2](#62-appschemaslive_runspy--44-unreachable-symbols-deleted-not-recorded).
The other premise fails on a different point: once PR-C had deleted the host
files, "leave it alone" was no longer available. Re-authoring four unreferenced
types into a **new** file is an addition, not a retention, and this repo's rules
point the other way ("don't create new files when editing an existing one
works"). Measured at the time of the fix round: zero type references anywhere in
`Frontend/`, `Backend/` or `contracts/` outside the file's own definitions —
`models/operator-notice.ts` re-exports eight names and none of these four — and
zero occurrences in `contracts/openapi/python-data-service.openapi.json`.

The restored copy was also **drifted from its cited source on the commit that
created it**: TS `ExitReason` listed 8 members where the Python enum had 9,
missing `poisoned`. No gate could have caught it — `codegen:check` watches
nothing under `Frontend/src/app/api/` (see [§12](#12-found-and-deliberately-not-fixed)).
A hand-written mirror with no consumer and no gate is a liability with no
counterweight.

What survives in
[`Frontend/src/app/api/operator-notice.types.ts`](../../Frontend/src/app/api/operator-notice.types.ts)
is the `OperatorNotice*` / `OperatorIncident` half, which **is** consumed —
`models/operator-notice.ts` re-exports it and
`api/operator-notice-codes.snapshot.spec.ts` pins `OperatorNoticeCode` against
the Python source. `OperatorIncident` remains the retained case the four were
argued to resemble, and the resemblance is what failed: it has consumers.

The apparent counter-precedent was examined and rejected: `runtime_snapshot.py`
was also a pre-existing orphan, but it lives under `app/**broker**/` — an IBKR
module, squarely #1813's own residue. Different act, different rule.

One casualty worth recording: the naming divergence the restore comment
documented — **Python names the union `MutationBlockageStageId`; the frontend
mirror called it `MutationRungReceiptStageId`** — no longer has a frontend side.
It is preserved here rather than in code, because there is no code left to
carry it.

---

## 10. Cross-PR facts the plan does not record

### 10.1 Files that moved between PRs

The plan
([`2026-08-26-ibkr-decommission-closeout.md:159`](../superpowers/plans/2026-08-26-ibkr-decommission-closeout.md))
assigns **both** `components/broker/ibkr-portal.ts` and `operator-severity.ts`
to PR-C. That is not what shipped:

| File | Plan says | Actually shipped in | Why |
|---|---|---|---|
| `Frontend/src/app/components/broker/operator-severity.ts` | PR-C | **PR-B** | PR-B's deletion of the session-mirror component is what orphaned it, and this project's rule is that **a PR deletes what it orphans**. |
| `Frontend/src/app/api/operator-observability.types.ts` | not assigned | **PR-B** | Second level of the same cascade chain (`operator-severity.ts` → `operator-observability.types.ts`). |
| `Frontend/src/app/components/broker/ibkr-portal.ts` | PR-C | **PR-C** | As planned. |

Recorded explicitly because an undisclosed cross-PR move reads as scope creep
to whoever audits the plan against the diff. A sweep of the task reports found
no other instance of a *planned* file moving PRs. Three related reassignments
that were rulings rather than moves, recorded for completeness: PR-A's review
routed the `fleet.py` / `fleet_contamination.py` full-retirement decision to
PR-B (taken there, step 0); PR-B's thermo round routed three minors to PR-C
(dead `Literal` values leaking into the contract, plus two others) rather than
force an OpenAPI + TS regen on a backend-only fix round; and PR-B disclosed
`runtime_snapshot.py` and `activity_projection_contract.py` as pre-existing
orphans, both taken by PR-C.

### 10.2 Standing hazard: the shared-contract manifest is a cross-stack seam

[`contracts/data-plane-control-surfaces.json`](../../contracts/data-plane-control-surfaces.json)
went **13 → 9 prefixes** in PR-B (`control_prefixes` 7 → 5,
`protected_read_prefixes` 6 → 4 with `/api/market-data-feed` added). It has
consumers in **both stacks**, and editing it from the backend moves the
frontend. This one file produced **three separate findings in one PR**, two of
them CI-red.

**Its three consumer classes — know all three before amending a prefix:**

1. **Generated tests.** `security/data-plane-control-intent.interceptor.spec.ts`
   generates **one Angular test per declared prefix**. PR-B's backend task
   therefore moved the Frontend test count by **4** (1899 → 1895) without
   touching a single file under `Frontend/`. The implementer's report
   attributed that delta to deleted spec files instead — the right number by
   the wrong road, and the wrong road erases the fact.
2. **A hand-written guard script.**
   [`Frontend/scripts/verify-proxy-control-guard.cjs`](../../Frontend/scripts/verify-proxy-control-guard.cjs)
   reads the JSON **by a different path and hardcoded retired literals**. CI
   job `frontend-test` runs `test:guards` unconditionally on every PR. It needs
   no `node_modules`, so it was one command from being caught. The report had
   enumerated the manifest's consumers by grepping for the **two symbols the
   interceptor exports** — a method that structurally cannot find a consumer
   that reads the JSON another way. The lesson drawn and applied: **run every
   consumer, don't list them.**
3. **OpenAPI → TypeScript codegen.** `codegen:check` regenerates
   `broker.types.ts` from the contract, so a contract edit without a regen reds
   `git diff --exit-code` in job `frontend-typecheck`, also unconditional.

The guard script's fix removed the root cause rather than the symptom: the
hardcoded path was replaced with a loop over `PROTECTED_READ_PREFIXES` gated by
an `isProtectedControlRead` precondition. Its comment now states the **true
weaker claim** — *"It does not detect a prefix retiring: that just removes an
iteration"* — after the original claim that it "cannot go vacuous again" was
traced and found false. A tautological assertion introduced alongside it was
deleted, and the deletion proved safe by mutation rather than asserted: a PR
whose thesis is closing vacuous assertions (PR-B closed 17) may not introduce
one.

---

## 11. What was retained, and why — so "kept" stays distinguishable from "missed"

### 11.1 The retained IBKR surface

17 routes survive, all read-only market data or feed-session lifecycle:

`/api/broker/bars/snapshot` · `/bars-5s/snapshot` · `/capability` ·
`/capability/probe` · `/connect` · `/disconnect` · `/reconnect` ·
`/data-plane/health` · `/health` · `/ibkr/evidence` · `/ibkr/evidence/stream` ·
`/option-chain/{symbol}` · `/option-contracts/{symbol}` ·
`/option-surface/{symbol}` · `/expirations/{symbol}` · `/strikes/{symbol}` ·
`/api/market-data-feed/health`

`RETAINED_FEED_MODULES` in the boundary test now has 16 entries and carries an
explicit note that **every module it names has been deleted** — the list is now
entirely forward-looking. It does not describe code that is here and must stay
out of the feed's reach; it describes code that is gone and must not come back
through the feed. A prefix guard costs nothing and is the cheapest way to make
a resurrection fail loudly.

### 11.2 Symbol groups deliberately retained, with the decision recorded

Both groups are recorded in the "#1813 close-out residue" section of
[`docs/architecture/engine-authority-map.md`](../architecture/engine-authority-map.md).
**PR-C decided: retained**, in both cases because the symbols read **durable
artifacts still on disk**.

- **`app/engine/live/live_artifact_io.py` (276 lines)** — reduced to
  `artifact_sha256` in production; sole production importer is
  `app/engine/live/reconcile.py:102`. `artifact_mtime_signature`,
  `read_parquet_rows`, `read_parquet_tail` and `list_run_artifacts` became
  test-only when `routers/live_runs.py`'s three-layer cache was deleted;
  `artifact_exists`, `parquet_row_count`, `artifact_size_bytes`,
  `artifact_mtime_ms`, `LiveArtifactMetadata` and `LiveArtifactReadError`
  survive as their internal helpers.
- **Six files whose production callers PR-B retired**, keeping their symbols for
  durable-artifact reads and their tests: `account_artifacts.py`
  (`read_account_freeze`, `write_account_freeze`, `read_legacy_account_events`,
  `AccountFreezeEvidence`), `intent_ledger.py` (`LedgerProjection`,
  `projection_from_envelope`), `live_state_sidecar.py`
  (`LiveStateSidecarCorruptError`), `account_clerk_journal.py`
  (`read_account_clerk_journal`), `app/schemas/artifact_io.py`
  (`read_pydantic_artifact`), `indicator_state.py` (`IndicatorStateRepo`).

### 11.3 Artifact trees explicitly NOT touched

**No path under `artifacts/` was modified, moved or deleted by any of the three
PRs** — verified: `git diff --name-status 03ce52b6..9b491b1a -- artifacts/
PythonDataService/artifacts/` is empty.

- **`artifacts/live_runs/_broker/`** — this is a **live Alpaca session-capability
  store**, not a historical IBKR artifact. The `_broker` name is misleading and
  has caused confusion before. Touching it would affect running Alpaca bots.
  Out of scope, deliberately.
- **`artifacts/live_state/`** — durable run state read by retained code (see
  §11.2). Deleting the readers would have been a deletion cascade into live
  data; deleting the data is not a code change at all.

Artifacts housekeeping is named as out of scope by PRD #1817 and stays that
way. This receipt records the trees so a future reader does not mistake their
survival for an oversight.

### 11.4 Preserved descriptions of deleted things

Two cases where the *type* went but the *information* had to outlive it:

- `intent_ledger.py:57` — `IbkrOrderSpec` deleted, and its docstring rewritten
  to enumerate the durable ledger row's real fields: which keys always carry a
  value, which are null when they don't apply, and which are read back at Phase
  5E to reconstruct fill evidence when only the broker's `perm_id` survives a
  restart. **Deleting a type is fine; deleting the description of data still on
  disk is not.**
- `sqlite_clerk_compat.py::active_sqlite_facade` cited `HostRunnerHealth.clerks`;
  its docstring now records that as retired.

---

## 12. Found and deliberately not fixed

Recorded so they are not lost. **None is #1813 residue**; each was found while
sweeping and each was left alone on purpose.

| Item | Status | Detail |
|---|---|---|
| **ADR 0030's stale test citation** | Pre-existing; ADR left untouched | [`0030-account-clerk-account-rooted-journal.md:463`](../architecture/adrs/0030-account-clerk-account-rooted-journal.md) cites `test_journal_exposure_survives_bot_crash_and_deduplicates_execution`. **Correction to an earlier note in this project's ledger: the test did exist.** It lived in `PythonDataService/tests/engine/live/test_account_clerk.py` from `d8d4311c` (#1033, 2026-07-14) and was deleted by **`e3e302b6` (#1679, 2026-08-19)** — the *same commit* that deleted the Alpaca exposure adapter and the parity test in §7. One commit on 2026-08-19 orphaned three citations at once. Still pre-existing, still not #1813's doing; ADRs stay historical per PRD #1817. |
| **`broker-options-chain.component.html`** | Aspirational TODO, not residue | An HTML comment describes flipping a caption "until the Python service exposes `/api/broker/market-data-tier`". That route **has never existed in `PythonDataService/`** — `git log --all -S` over the backend returns nothing, and the only occurrence anywhere in history is the comment itself, added by `1fd40fe0`. (An earlier note said `git log -S` returned empty; it returns the comment's own commit. The conclusion is unchanged.) |
| **`codegen:check` watches no hand-written mirror types** | Known gap; separate follow-up | It diffs only `graphql/generated` + `broker.types.ts`. The hand-written mirrors in `Frontend/src/app/api/` are watched by nothing — which is exactly how `broker-models.ts` drifted, and why the orphaned mirrors in §9 were findable only by a manual symbol-level sweep. Deliberately **not** widened into PR-C. |
| **`.claude/rules/python.md:52` names a deleted file** | Found by this receipt; **fixed in this PR** | The "Live-control router freeze" rule reads *"While `app/routers/live_instances.py` exceeds this threshold, a PR may not increase its net physical line count."* PR-B deleted that router. The rule is now unsatisfiable as written and its 1 000-line freeze has no subject. It is a **binding instruction-layer rule**. **Settled by the controller in this PR:** the section now records that the freeze's subject is retired, keeps the principle for the next live-control router, and states explicitly that the three routers which *do* exceed 1,000 lines today — `lean_sidecar.py` (~1.9k), `engine.py` (~1.8k), `jobs.py` (~1.5k) — are **not** live-control routers and are **not** frozen by it. Extending a size freeze to them is a separate decision #1813 had no standing to make. (Broker-adjacent routers are well under: `broker.py` 614 lines, `brokers.py` 655.) `AGENTS.md:11` **was** updated by PR-B and correctly states the deletion. |
| **25 broken relative links elsewhere in `docs/`** | Pre-existing | Found by broadening the documentation-contract checker's link validation to every `docs/**/*.md` (it normally validates only canonical / protected-canonical / in-flight docs and `docs/runbooks/`). All 25 sit in options-research, archive, ML-authority and superseded-plan documents; **none** targets a path removed by #1813, verified against the removal list. |

---

## 13. Deferred follow-ups and Out of Scope

Restated so a future reader does not wonder why they were not done.

- **Deploy-page symbol picker** — tracked as a separate issue, **not filed by
  this task.**
- **Artifacts housekeeping** — out of scope; see §11.3 for what was left and
  why.
- **`codegen:check` coverage of hand-written mirror types** — raised as its own
  follow-up (§12), deliberately not widened into PR-C.
- **Historical ADRs are not rewritten.** PRD #1817 lists this explicitly. ADR
  0036 carries a *dated superseding paragraph* because PR-C made one of its
  consequences unsatisfiable; that is an addition, not a rewrite, and the
  original text stands above it. ADR 0030's stale citation (§12) was left
  alone for the same reason.
- **`.claude/rules/python.md`'s router freeze** — see §12; a policy decision,
  not a doc fix.

---

## 14. The lesson worth recording

**One root cause produced findings in four separate reviews across this
project: a sweep that was exhaustive at the wrong resolution.**

| Where | Sweep resolution | What it could not see |
|---|---|---|
| PR-B backend | module-level | a module (`account_identity.py`) plus 63 symbols orphaned by its own deletions |
| PR-B frontend | file-level | `IbkrObjectSnapshot`, orphaned inside a file that still had importers |
| PR-B manifest | grep for the 2 exported symbols | a third consumer reading the same JSON by another path, hardcoding retired literals |
| PR-A backend | module-level | ~1 300 lines orphaned by the PR's own deletions, including `LegacyStaleClaimRetirementService` |

Every one of these sweeps was **exhaustive at the resolution it operated at**,
and every one would confirm itself clean on a re-run. That is what makes the
failure mode dangerous: re-running the same method produces the same green.

**Symbol-level AST sweeps found what all of them missed** — including six
same-name production hits that turned out to be *separate definitions*
(§6.2), the exact case where grep says "live" and AST says "dead". They also
caught a *nested* import that a green test suite did not (§6.4).

Two corollaries, both earned the hard way on this project:

- **Run every consumer; don't list them.** Enumerating consumers by grepping
  for the symbols one of them exports cannot find a consumer that reads the
  same data another way.
- **Counts in prose have been the least reliable claims, on every side.** A
  residue row that came out numerically wrong on its first attempt (39 vs 44)
  is itself the argument for deleting rather than recording — deletion removes
  the need for anyone to trust a count.

**The next decommission should start at symbol-level AST, not at grep or
module-level imports.**

---

## 15. Verification of record

Every gate below was run on the branch tip and controller-verified
first-hand rather than accepted from a report.

| Gate | Result |
|---|---|
| Python full suite (host venv) | **7294 passed / 44 skipped / 5 xpassed / 0 failed** |
| `ruff check PythonDataService/app/ PythonDataService/tests/` | clean |
| Frontend `ng test` | **224 files / 1854 passed / 0 failed** |
| `eslint --max-warnings 0`, `tsc --noEmit`, `ng build` | clean |
| `codegen:check` | exit 0, reproducible (identical md5 twice) |
| `test:guards` (`verify-proxy-control-guard.cjs`) | `proxy control guard ok`, exit 0 |
| `export_openapi_contract.py --check` | exit 0 twice (idempotent), `git status -- contracts/` empty |
| Feed-boundary structural test | 4 passed; `_ALLOWED_EXCEPTIONS` literally `set()` |
| `scripts/check_documentation_contract.py` | exit 0 (before and after this receipt) |

**Test-count trajectory, reconciled by name at every step:**

| | Python | Frontend |
|---|---|---|
| Slice 0 baseline (`03ce52b6`) | 8545 / 52 skipped / 5 xpassed / 0 failed | 1899 |
| PR-A (#1818) | 8165 / 45 / 5 / 0 | 1899 |
| PR-B (#1819) | 7297 / 44 / 5 / 0 | 1855 |
| **PR-C (`9b491b1a`)** | **7294 / 44 / 5 / 0** | **1854** |

Notes on the reconciliations, because two of them look like false greens:

- PR-B's −868 arrived in three measured steps, not one: the main deletion
  batch took 8165 → 7306, reconciled by diffing `pytest --collect-only` node-id
  lists against a `git archive` of the PR-A tip (864 node ids removed, 4 added,
  each attributed to a named deleted file or trimmed function, plus one skip);
  the review fix round took it to **7307**, the **+1** being
  `test_always_guarded_reads_are_declared_in_shared_manifest`, the new invariant
  added with the §5.8 manifest fix; and the thermo fix round took it to 7297,
  the **−10** being every case in `test_account_identity.py`, named individually.
- Within PR-B, the Frontend went **1899 → 1895 before any `Frontend/` file was
  touched**, because the backend's manifest edit changes the number of
  generated interceptor tests (§10.2). The remaining 1895 → 1855 is five named
  deleted spec files (17+8+5+5+5 = 40).
- PR-C's `live_runs.py` deletion (664 deletions across the 5 files of that fix
  round; the schema file itself went 915 → 219 lines) left the suite
  **unchanged at 7294** — the shape of a false green. Affirmative evidence was
  supplied instead: nothing could import the 44 symbols, so there was no test
  to keep alive and **no fabricated test double was needed**; the file went
  915 → 219 lines; and the OpenAPI contract came out byte-identical. The
  by-name reconciliation was re-derived at **both** revisions, showing renames
  on both sides rather than netting them to zero (−16 / +13, net −3).

**The acceptance criterion is live on `origin/master`:**
`_ALLOWED_EXCEPTIONS: set[tuple[str, str]] = set()` at
[`PythonDataService/tests/structural/test_ibkr_feed_boundary.py:89`](../../PythonDataService/tests/structural/test_ibkr_feed_boundary.py).

---

## 16. Draft closing note for issue #1813

> *Not yet posted. Posting is the controller's action once PR-C merges.*

---

**The IBKR control plane is retired. All six slices are complete.**

IBKR is now a market-data feed and nothing else. Every account-authority,
order-actuation, P&L, session-mirror, broker-activity, host-daemon and
live-run control surface has been removed from the repo — 264 files deleted,
50 HTTP routes retired, 138 OpenAPI schema components removed. Alpaca is
untouched and remains the sole live execution path.

**The acceptance criterion is met and live on `master`:**
`PythonDataService/tests/structural/test_ibkr_feed_boundary.py` carries
`_ALLOWED_EXCEPTIONS: set[tuple[str, str]] = set()`. All three Slice-0 tracked
exceptions were closed by **retiring their blocking consumer** — never by
widening the allow-list, which is the only way this repo's rule permits an
exception to close. PR-C adds a `RETIRED_MODULES` guard over 100 module paths
so the retirement is pinned as a fact rather than left implied.

**Three pull requests, merged individually into `master` in order, as regular
merges:**

| PR | Scope | Range | Merged as |
|---|---|---|---|
| **#1818** | PR-A — account authority, Account Truth, reconciliation, account safety | `03ce52b6..540af359` (12 commits) | `428ff558` |
| **#1819** | PR-B — session mirror, broker activity, bot events, live runs, IBKR orders/P&L/persistence/search, host bridge | `540af359..ac908f28` (26 commits) | `dcf456b4` |
| **PR-C** | Consolidation and close-out — `journal_exposure.py` disposition, `live_runs.py` residue, mirror types, `MarketDataFeedService` rename, structural retirement guard | `ac908f28..9b491b1a` (16 commits) | *this PR* |

**Suites:** Python 8545 → **7294** passed (0 failed); Frontend 1899 → **1854**
passed (0 failed). Every delta reconciled by name.

**Full retirement receipt** — every removed symbol, route, config field, test
family and contract entry, with its no-live-consumer evidence and the
disposition of everything deliberately retained:
`docs/audits/ibkr-control-plane-decommission-retirement-receipt-2026-08-27.md`

Three decisions in there are worth reading even if you read nothing else:

1. **`journal_exposure.py` is retired.** Two PRs deferred this because it was
   registered as canonical math. The evidence that settled it: the Alpaca
   adapter *and* the parity test its docstring cited were both deleted by
   #1679 on 2026-08-19, so that citation was never evidence of a live consumer
   — and the dependency ran the *other* way. Both registry rows now say
   "retired", and ADR 0036's affected consequence carries a dated superseding
   line.
2. **`app/schemas/live_runs.py`: 44 unreachable symbols deleted, not
   recorded** (915 → 219 lines), with the 11 reachable survivors kept and
   contract-neutrality re-proved byte-for-byte. The residue row that would
   have driven a follow-up was numerically wrong on its first attempt — which
   is itself the argument for deleting rather than recording.
3. **The rule that governed every mirror-type call:** this work deletes what
   #1813 orphaned, and does not delete what was already dead before it.

Deferred by design and **not** done here: the deploy-page symbol picker
(separate issue), artifacts housekeeping (out of scope — `artifacts/live_runs/_broker/`
holds a **live Alpaca** session-capability store, not historical IBKR data),
and widening `codegen:check` to watch hand-written mirror types (raised as its
own follow-up). Historical ADRs were not rewritten.

Closing #1813.
