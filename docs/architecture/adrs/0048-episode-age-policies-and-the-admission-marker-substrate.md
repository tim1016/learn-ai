# ADR 0048 — Episodes declare their own age policy; holds are uncertainties; admission markers are a separate substrate whose home is still open

**Status:** Proposed 2026-08-26
**Provenance:** Decision ticket [#1780](https://github.com/tim1016/learn-ai/issues/1780) (WP5), child of the Two-Tap fleet-freeze umbrella [#1773](https://github.com/tim1016/learn-ai/issues/1773). Findings report: `docs/audits/bot-fleet-stress-2026-08-25.md` (S4 at line 201, remediation §8 lines 335-336). Every claim below was verified against source on `origin/master` at `1ee4305b` before this ADR was written; the three claims marked **measured** were reproduced by executing the shipped code against a copy of the live `DUM284968` artifact tree.
**Decision drivers:** #1780 asks three questions — how long episode-store records live, whether account holds should become uncertainty episodes, and where admission markers belong. Two are answerable from the code. The third is not, and this ADR says so rather than inventing a resolution.
**Related:** ADR 0035 and ADR 0037 (SQLite is the sole Alpaca custody authority — the store this ADR consolidates *into*), ADR 0045 (stuck-EXIT watchdog and the redrive/escalate clock this ADR generalises), ADR 0047 (authority recovery is an offline ceremony — the sibling finding that a structurally unarmable mechanism must be removed, not documented), ADR 0033 (account custody clocks and safety contract — the `AccountSafetyVerdict` vocabulary the admission markers guard), ADR 0027 (operator blocker disposition taxonomy), ADR 0022 (temporal authority — every clock named here is `int64 ms UTC`), ADR 0039 (ADR status is decision standing, not code conformance).
**Vocabulary:** `CONTEXT.md` § "Episode store" — **Episode**, **Age policy**, **Terminal disposition**, **Admission marker**. Owed on acceptance: `CONTEXT.md` today has no entry for *hold*, *uncertainty*, or *episode*, and Decision 2 makes one of those three words disappear. Per ADR 0040 Decision 4.

## Context

### What the episode store actually is

The Alpaca SQLite clerk holds two tables with the same shape. `uncertainties` (`PythonDataService/app/broker/alpaca/clerk/sqlite/schema.py:368-399`) and `holds` (`:344-362`) each carry a `scope`, a `reason_code`, an opened/observed timestamp, a nullable resolved timestamp, and an `evidence_refs_json`. Each has a partial unique index enforcing one active episode per cause — `ux_uncertainties_one_active_cause` (`:390-392`) and `ux_holds_one_active_cause` (`:357-359`). Both are folds of the same hash-chained custody-transition log; neither is an independent authority.

`uncertainties` is the stronger primitive. Seven reason codes are registered in `_REASON_POLICIES` (`uncertainty.py:110-153`), each declaring a `scope`, `blocks_new_exposure`, `allows_reduction`, and a strict `cause_is_valid` decoder (`uncertainty.py:63-69`); the codes themselves are constants in `uncertainty_causes.py:14-20`. An unrecognised code fails closed account-wide (`uncertainty.py:159-164`, `:185-186`). `holds` has none of that: no policy registry, no typed cause, no severity, no operator copy.

`holds` is also far smaller than its DDL suggests. Every production write path hardcodes `scope='ACCOUNT_CLERK'` with a NULL `strategy_instance_id` and a never-set `subject_id` — `_fold_account_hold_raised` (`folds.py:1297-1305`), `_fold_account_hold_refreshed` (`:1312-1316`), `_fold_account_hold_resolved` (`:1321-1324`), and the separate inline INSERT in `external_order_folds.py:88-95`. `observe_account_hold` hardcodes the same scope in its uniqueness check (`repository.py:1287-1289`). Two distinct reason codes are ever written: `UNEXPLAINED_ORDER` (`reconcile.py:71`; the same string under a second name at `trade_evidence.py:34`; a hardcoded literal at `external_order_folds.py:86` and `:91`) and `STREAM_HEALTH_HOLD` (`runtime.py:130`). **The entire holds table is therefore at most two account-scoped rows.** #1780's "2 reason codes implementing the identical raise/refresh/resolve shape" is correct.

### No episode has an age policy today

Neither table has a TTL, a staleness sweep, a retention window, or any comparison of its own timestamp against the clock. `opened_at_ms` and `observed_at_ms` are written and read for display, never for a decision. No code path deletes a row from either table; the only `DELETE FROM` statements anywhere in the clerk target `fills` (`folds.py:999`, `execution_coverage_supersession_fold.py:165`) and `decision_receipts` (`decision_receipts.py:358`).

An episode ends only when a *specific later event* occurs, and that event is produced by exactly one loop:

| Reason code | Ends when | Produced by |
|---|---|---|
| `POSITION_DRIFT`, `BROKER_SNAPSHOT_STALE` | a clean broker reconciliation proves the prerequisite | `resolve_reconciliation_uncertainty` (`uncertainty.py:263-278`), driven by the sweep |
| `RECONCILIATION_INCOMPLETE` | a later pass reaches finalization | `uncertainty.py:281-293` |
| `EXIT_NOT_FLAT` | attributed-flat is proven | `uncertainty.py:296-326` |
| `EXIT_STUCK` | attributed-flat is proven | `uncertainty.py:329-366` |
| `ORDER_OUTCOME_UNKNOWN` | an atomic fold closes it | `uncertainty_folds.py:179-245` |
| `EXECUTION_COVERAGE_CONFLICT` | a supersession fold closes it | `execution_coverage_supersession_fold.py:188`, `folds.py:1023` |
| `UNEXPLAINED_ORDER` (hold) | a reconciliation pass finds no unreviewed foreign order | `_sync_unexplained_order_hold` (`reconcile.py:309-340`), called only from `reconcile.py:828` |
| `STREAM_HEALTH_HOLD` (hold) | an ENTER attempt observes both channels healthy | `_sync_stream_health_hold` (`runtime.py:1138-1187`), called only from the ENTER path at `runtime.py:677-679` |

The last row is the sharpest illustration. `STREAM_HEALTH_HOLD` is raised *and released* inside `EffectPurpose.ENTER` handling. If the fleet is stopped, or every bot is refused for another reason, nothing evaluates the gate and the hold is immortal. That is precisely the `known-gaps.md` S9/S10 entry (lines 304-310): "hold raise/release is still coupled to entry-time evaluation rather than a continuously sampled health authority."

### Three bespoke clocks already do age-policy work, in three different places

- **Submit-absence grace.** `UNCERTAIN_SUBMIT_GRACE_MS = 30_000` (`order_evidence.py:37`). After 30 s with no broker identity, no `ORDER_SUBMIT_ACKED`, and no durable fill, absence becomes a terminal fact (`order_never_reached_broker`, `order_evidence.py:355-379`) and the void is folded with summary code `ORDER_SUBMIT_FAILED_ABSENT` (`:350-352`, applied at `:525-530`). This is an age-gated terminal disposition on the `ORDER_OUTCOME_UNKNOWN` cause, written in the order-evidence module rather than declared on the reason.
- **Stuck-EXIT redrive and escalation.** `EXIT_NOT_FLAT_REDRIVE_AFTER_MS = 120_000` and `EXIT_NOT_FLAT_MAX_REDRIVES = 3` (`exit_watchdog.py:46-47`), applied in `redrive_or_escalate_stale_exits` (`:50-177`): an `EXIT_NOT_FLAT` episode older than the window is re-driven up to three times, then escalated to a durable `EXIT_STUCK` episode (`:102-135`). This is grace + bounded redrive + durable escalation — the exact triple #1780 proposes to generalise. ADR 0045 shipped it for one reason code.
- **Drift-reduction evidence freshness.** `DRIFT_REDUCTION_EVIDENCE_MAX_AGE_MS = 30_000` (`uncertainty.py:39`), applied at `:425-426`. This one is **not** an age policy on the episode: it bounds how old the *observation inside* a `POSITION_DRIFT` episode may be before it can authorise a reduction. The episode itself keeps blocking new exposure regardless of age. Conflating it with the other two would be the WP5-revision-1 error the adversarial review already caught once.

### Capability tokens are a different domain — and there is a third one

#1780 excludes safe-flatten plan expiry (`safe_flatten_execution.py:92-96`, against `plan.expires_at_ms`) and the cutover confirmation TTL (`cutover.py:77-78`, `DEFAULT_CONFIRMATION_TTL_MS = 120_000` / `MAX_CONFIRMATION_TTL_MS = 300_000`, enforced at `:533-534`). Both exclusions are correct: these are consumable authorisations with a single-use lifetime, not incident records, and cutover additionally runs *before* the SQLite authority exists (`cutover_initialization.py`), so folding it into that authority's registry would be a bootstrap cycle.

A **third** token of the same shape exists and #1780 does not name it: the catalog-quarantine confirmation TTL (`catalog_quarantine.py:108`, `:130`, refused at `:172-173`). It belongs in the same excluded class, for the same reason.

Two further clocks sit outside both classes and stay where they are: the SQLite execution lease (`DEFAULT_LEASE_TTL_MS = 30_000`, `repository.py:100`), which is a liveness lease, and the account observation lease (a JSON artifact with its own refresh loop), which #1780 already excludes.

### The admission markers: what is actually on disk, and what actually runs

Admission markers are **not** in the Alpaca SQLite clerk. They are `O_EXCL` files created by `PythonDataService/app/engine/live/account_safety.py:235-256`, the only module in the repo that creates, reads, or removes them. The two stores are not adjacent — they are in disjoint subtrees with disjoint path helpers:

| | Admission markers | Alpaca SQLite clerk |
|---|---|---|
| Path | `<root>/accounts/<ACCOUNT_ID>/.account_safety_admission/…` (`account_safety.py:198-225`) | `<root>/accounts/alpaca/<account_id>/clerk.db` (`writes.py:57-62`, `repository.py:98`) |
| Confinement | `account_artifact_file_path` (`account_artifacts.py:297-310`, root at `:1484-1487`) | `resolve_contained_path` (`broker/alpaca/paths.py:32-44`) |

So #1780's claim that **engine-level accounts have no Alpaca SQLite clerk** is **true** — but the sharper and more useful statement is its converse, which #1780 does not make: **no account that uses admission markers has a SQLite clerk, and no account that has a SQLite clerk uses admission markers.** The sets are disjoint, not overlapping-with-a-gap. On the live artifact tree, `find artifacts -name .account_safety_admission` returns exactly one directory, on `DUM284968`; every Alpaca account directory under `artifacts/accounts/alpaca/` has none. The single production acquisition is `account_reconciliation.py:383`, inside `AccountReconciliationService.observe_account_truth`, which is fed only by IBKR Account Truth (`app/main.py:323-331`, `routers/broker_account_truth.py:30-38`, `routers/broker.py:380-391`). Nothing on the Alpaca deploy, start-admission, or clerk-runtime path takes a marker.

Nothing about the *store* is Alpaca-hostile, incidentally: the account-id grammar is `^[A-Z][A-Z0-9]+$` (`account_artifacts.py:62`), which `PA3KWXU1C4C3` satisfies. The mechanism is broker-neutral by construction and Alpaca-blind in practice.

**The 17 stranded markers are real — and they are not the class that caused the outage.** *(measured)* `artifacts/accounts/DUM284968/.account_safety_admission/participants/` holds exactly 17 files, stamped `clerk_generation` 142 through 189 and `enrolled_at_ms` spanning 2026-08-03 to 2026-08-24. The audit's S4 (`docs/audits/bot-fleet-stress-2026-08-25.md:201`) names a different pair — a stranded `gate` + `writer`, "dated Aug 3", which "broke every account-truth refresh cycle for 3 weeks and inflated deploy latency to 10 s+" and was "removed manually" (§8, lines 335-336). Running the shipped locks against a copy of the live marker tree separates the two classes cleanly:

- with the 17 participant markers present and no `gate`/`writer`: both `account_safety_admission_lock` and `account_safety_entry_admission_lock` acquire in **0.00 s**;
- with a `gate` marker planted: both refuse after **10.0 s** with `account safety admission marker is already held` — `_ACCOUNT_SAFETY_ADMISSION_TIMEOUT_S = 10.0` (`account_safety.py:38`), against a 15 s Account-Truth refresh interval (`account_truth_refresh.py:38`).

So #1780's parenthetical — "the S4 orphan class — 17 stranded participant markers exist on the IBKR dev account right now" — merges two orphan classes with opposite properties. The participant orphans are inert for admission. The `gate`/`writer` orphans are the outage, and they are already gone.

**The repair path is worse than dead code.** *(measured)* `repair_account_safety_admission` (`account_safety.py:530-661`) has zero production call sites — five test call sites in `tests/engine/live/test_account_safety.py` and nothing else — which #1780 states correctly. What #1780 does not state is that it **cannot succeed on the account it was written for, and attempting it makes the account permanently worse.** Two lines cause this:

1. `_admission_participants_remain` (`:664-678`) returns `True` if *any* participant marker exists, at *any* generation. Repair waits on that roster and hard-fails at `:591-597`.
2. `_remove_orphaned_admission_markers` (`:737-766`) removes `gate`, `writer`, and `readers/*` — **never `participants/*`**. The repair therefore cannot clear its own precondition.

Executed against a copy of the live tree with a planted `gate`+`writer`, the shipped function raised `account safety admission participants did not quiesce during maintenance` after 10.0 s, removed nothing, and left `account_safety_admission_maintenance.json` at `{"status":"REPAIRING","generation":1}` — because the fence is written *before* the drain wait (`:575-583`) and only reset on a successful path (`:655-660`). With that file in place, both admission locks then refused **immediately**: `account safety admission is fenced for maintenance` (`:413-417`). Retrying the same operation id failed identically after another 10.0 s. **One attempted repair permanently fences every account-safety admission on that account, and no code path can reopen it** — `repair_account_safety_admission` is the only writer of `status: OPEN`, and it can never reach that line while participants remain.

This is the same shape ADR 0047 found in `rebuild_from_mirror` / `reset_authority`: an advertised remedy that no execution can complete. Here it is one degree worse, because invoking it is destructive rather than merely futile.

**Two more halves of this mechanism are unwired, and one clock is frozen.**

- `account_safety_entry_admission_lock` (`:490-518`) — the *reader* half, the shared entry permit the whole turnstile exists to drain — has **zero production callers** (tests only: `test_account_safety.py:138`, `:185`, `:308`, `:354`). `AccountSafetyAuthority.require_entry_admission` (`:1143`) is likewise test-only. So `readers/` is never populated in production, `wait_for_readers` (`:442-451`) always returns immediately, and the writer turnstile currently excludes only *other writers*.
- Every marker stamps `clerk_generation` from `_current_clerk_generation` (`:383-385`) → `read_account_clerk_generation` (`account_artifacts.py:954-965`), which reads `clerk_generation.json`. **No production writer of that file exists in the tree** — `ACCOUNT_CLERK_GENERATION_FILENAME` appears at `account_artifacts.py:25`, `:958`, `:1640` and `account_epoch.py:22`, `:511`, all reads; the only writes are test helpers. The live file on `DUM284968` records `"source":"host_daemon.clerk_spawn"`, a string that appears nowhere in the current tree. The generation stamp is frozen residue from a removed writer. The codebase already knows this evidence is insufficient — `require_active_account_clerk_generation` says so in its own docstring (`account_artifacts.py:988-992`: "A generation file alone is deliberately insufficient: a crashed or reaped clerk leaves it behind") — but `_current_clerk_generation` uses the bare read, not the guarded one.

No marker carries a PID, host, or heartbeat. No marker's timestamp is ever read for a decision. The only time constants in the module are acquisition deadlines (`:38-39`). The design refuses to infer death deliberately and says so (`:316-319`, `:541-546`), which is defensible — and leaves it with no way to distinguish a live owner from a dead one at all.

## Decision

### 1. Age policy is declared per reason code, on the reason, and every reason must declare one

`ReasonPolicy` (`uncertainty.py:63-69`) gains three fields, and `_REASON_POLICIES` (`:110-153`) becomes the single place an episode's life is specified:

- **`grace_ms: int | None`** — how long the cause may hold before any automatic disposition applies. `None` means "acts immediately".
- **`redrive: Redrive | None`** — an optional bounded automatic retry: an interval and a maximum count. `None` means "no automatic retry".
- **`terminal: TerminalDisposition`** — **required, no default.** Exactly one of `AUTO_VOID` (close the episode with a receipt naming the age rule that closed it) or `ESCALATE(reason_code)` (open a named successor episode). There is no third value and no "leave it open forever".

Requiring `terminal` with no default is the load-bearing part. It is what makes "every *future* uncertainty gets a bounded life by construction" true, rather than true-by-convention: a new reason code cannot be registered without answering the question, because the dataclass will not construct.

The three existing clocks move to declarations and change no behaviour:

- `ORDER_OUTCOME_UNKNOWN` declares `grace_ms=UNCERTAIN_SUBMIT_GRACE_MS` (30 000) and `terminal=AUTO_VOID`, with the receipt keeping summary code `ORDER_SUBMIT_FAILED_ABSENT` (`order_evidence.py:350`). The predicate at `order_evidence.py:379` reads the declared value instead of the module constant.
- `EXIT_NOT_FLAT` declares `redrive=Redrive(after_ms=120_000, max_count=3)` and `terminal=ESCALATE("EXIT_STUCK")`, replacing the constants at `exit_watchdog.py:46-47`. The watchdog keeps its execution logic and loses its policy.
- `EXIT_STUCK` declares `terminal=AUTO_VOID` with **no** grace and **no** redrive — that is, it is a durable escalation that only an attributed-flat proof (`uncertainty.py:329-366`) or an operator closes. Declaring `AUTO_VOID` with `grace_ms=None` is how a policy says "terminal disposition is reached the moment the cause is gone, never on a clock." An escalation target that could itself auto-void on age would silently discard the very episode the escalation exists to preserve.

`DRIFT_REDUCTION_EVIDENCE_MAX_AGE_MS` (`uncertainty.py:39`) **does not move.** It bounds evidence freshness inside a capability decision, not the life of an episode; it stays where it is applied (`:425-426`).

The remaining four codes (`POSITION_DRIFT`, `BROKER_SNAPSHOT_STALE`, `RECONCILIATION_INCOMPLETE`, `EXECUTION_COVERAGE_CONFLICT`) declare `grace_ms=None, redrive=None, terminal=AUTO_VOID`-on-cause-cleared, which is exactly today's behaviour written down. **This ADR does not add a clock to any code that lacks one.** Adding one is a behaviour change per reason and needs its own evidence.

**A declared policy is not a sweep.** Nothing in this decision creates a background timer. The declaration is evaluated where the episode is already evaluated — `decide_capability` (`uncertainty.py:474-640`), the reconciliation sweep, the exit watchdog. Introducing a fourth loop that mutates the custody log on a timer would add a writer to a single-writer authority (ADR 0035, ADR 0037) and is out of scope.

### 2. Holds become uncertainty episodes, in a v11 → v12 migration

Holds are uncertainties with a missing policy registry. They are merged into `uncertainties`; the `holds` table and its three folds are retired.

**Two new reason codes** join `_REASON_POLICIES`, at `scope="ACCOUNT_CLERK"`, `blocks_new_exposure=True`, `allows_reduction=False` — the semantics `decide_capability` already applies to any active hold (`uncertainty.py:514-524`):

- `UNEXPLAINED_ORDER` — `terminal=AUTO_VOID`-on-cause-cleared, `grace_ms=None`. Its cause is a set of unreviewed foreign broker order ids; the evidence-driven resolution at `reconcile.py:326-341` is unchanged.
- `STREAM_HEALTH_HOLD` — `terminal=AUTO_VOID`-on-cause-cleared, `grace_ms=None`.

Both get a typed `cause_is_valid` decoder in `uncertainty_causes.py`, which they do not have today. Neither gets a grace window or a redrive in this migration.

**The migration is small because the data is small.** Every hold row is `scope='ACCOUNT_CLERK'`, `subject_id` NULL, `strategy_instance_id` NULL (`folds.py:1297-1305`, `external_order_folds.py:88-95`), and at most two are ACTIVE at once. The v12 upgrade:

1. Registers `12` in `SCHEMA_MIGRATIONS` (`schema.py:768-841`; v11 is currently the top entry at `:840`) and bumps `SCHEMA_VERSION` (`:36`). The registered path is chain-validated by the existing checker at `schema.py:849-867`.
2. Backfills every `holds` row into `uncertainties` deterministically: `uncertainty_id` derived from `hold_id` so the mapping is reproducible and idempotent; `observed_at_ms ← opened_at_ms`; `resolved_at_ms ← resolved_at_ms`; `severity`, `headline`, `explanation`, `operator_impact`, `next_step` filled from the same operator copy the panel already renders; `facts_schema_version` set to `FACTS_SCHEMA_VERSION`; `facts_json` built from `reason_code` + `evidence_refs_json`. Resolved holds migrate too — they are timeline evidence, and dropping them would silently rewrite `custody_transitions` history.
3. **Collisions cannot occur.** `ux_uncertainties_one_active_cause` keys on `(scope, reason_code, COALESCE(subject_id,''))` (`schema.py:390-392`). Both incoming codes are new strings not among the seven in `uncertainty_causes.py:14-20`, and both arrive at `scope='ACCOUNT_CLERK'` with `subject_id` NULL. The migration still asserts zero collisions rather than assuming them — a v12 that silently dropped a blocking episode would remove an entry fence.
4. Retires `ACCOUNT_HOLD_RAISED` / `_REFRESHED` / `_RESOLVED` as *write* kinds, remapping them onto `UNCERTAINTY_RAISED` / `_REFRESHED` / `_RESOLVED`. The three folds (`folds.py:1365-1367`) and the inline hold SQL in `external_order_folds.py:88-138` are **retained as read-only replay folds**, because they must keep reconstructing history.

**Mirror and rebuild need no new code, and this is load-bearing.** `mirror.py` is a write-only append of the custody-transition stream (`mirror.py:1-8`) and contains no hold-specific or uncertainty-specific logic; `rebuild.py` replays each mirrored row through `fold_registry.apply` (`rebuild.py:105`). Because holds are already a fold of the log rather than an independent authority, a mirror written before v12 replays correctly after v12 **iff** the three hold fold kinds stay registered. That is why step 4 keeps them. Deleting them would make every pre-v12 mirror unreplayable — the exact failure `MirrorChainBroken` (`mirror.py:22-28`) is designed to make loud, and the reason a mirror rebuild must be part of the migration's test surface, not an afterthought. The v8→v9 offline ceremony already compares rebuilt `holds` rows for equivalence (`offline_v9_upgrade.py:438`); v12's equivalence digest reads the migrated `uncertainties` rows instead, and the backup/restore test must cover both directions across the version boundary.

**Compatibility projections keep every existing consumer's wire shape.** `ProjectedHold` (`projection_models.py:89-96`) carries six fields: `hold_id`, `scope`, `strategy_instance_id`, `reason_code`, `opened_at_ms`, `evidence_refs`. `ProjectedUncertainty` (`:99-115`) is a strict superset under two renames. `_holds` (`projections.py:651-673`) becomes a filtered view over the uncertainties read, selecting the two migrated reason codes and renaming `uncertainty_id → hold_id`, `observed_at_ms → opened_at_ms`. Every downstream consumer — `sqlite_clerk_compat.sqlite_clerk_status` (`sqlite_clerk_compat.py:210-218`), `panel_projection_service.build_clerk_card`, `services/run_admission.py:333-338`, `paper_deploy_service.py:352-356`, the `HoldReason` vocabulary (`vocabulary.py:48-51`), the OpenAPI snapshot and the generated Frontend types — is untouched. The panel's `hold_active` / `hold_reason` contract, the `clerk/status` route, and the operator manual's hold section all keep their current shapes.

**One thing the compatibility projection must not preserve.** *(measured)* The SQLite authority stores `"UNEXPLAINED_ORDER"`; the closed panel vocabulary is `{"NO_HOLD", "UNEXPLAINED_ORDER_HOLD", "STREAM_HEALTH_HOLD"}` (`vocabulary.py:48-51`); and `_hold_reason_code` maps anything outside that set to `NO_HOLD` (`panel_projection_service.py:87-95`). Feeding the live constants through the live mapper returns `'UNEXPLAINED_ORDER' -> 'NO_HOLD'` and `'STREAM_HEALTH_HOLD' -> 'STREAM_HEALTH_HOLD'`. **An active, account-wide, entry-blocking unexplained-order hold renders on the operator panel as "No hold" today.** It survives only because no test crosses the DB → `sqlite_clerk_compat` → `build_clerk_card` seam with a value actually read out of the database. This is a pre-existing defect, not one v12 introduces — but "compatibility projections so existing consumers see unchanged wire shapes" would ship it forward verbatim. The migration therefore **normalises the stored code to the wire spelling `UNEXPLAINED_ORDER_HOLD` during backfill**, and lands with the end-to-end test that would have caught it. Fixing a silent-wrong-answer while rewriting the row that carries it is cheaper than migrating it twice.

**Sequencing constraint.** #1777 (WP3+WP4) is concurrently re-homing the hold lifecycle onto an independent 15 s cadence with fresh-observation sample identity and append-on-change-only journal discipline. That work rewrites the *producer*; v12 rewrites the *store*. **#1777 lands first.** Migrating a table while another in-flight child changes who writes to it, at what cadence, and with what append discipline, is how a backfill and a producer disagree about what "one episode" means. This constraint is not in #1780 and is stated here because the ADR cannot honestly omit it.

### 3. Capability tokens stay out — including the one #1780 does not name

Safe-flatten plan expiry (`safe_flatten_execution.py:92-96`), cutover confirmation TTL (`cutover.py:77-78`, `:533-534`), and **catalog-quarantine confirmation TTL** (`catalog_quarantine.py:108`, `:130`, `:172-173`) are consumable authorisations, not incident records. They are excluded from the reason-policy registry. Adding the third to #1780's list of two is a deliberate widening: the exclusion is a *class*, and leaving one member outside it invites a later refactor to fold it in on the grounds that it was never named.

The SQLite execution lease (`repository.py:100`) and the account observation lease also stay put. A lease answers "is the owner alive?"; an episode answers "what is unknown?". They are not the same question and must not share a registry.

### 4. Admission markers are not an episode-store problem — and their substrate is **not decided here**

**What this ADR does settle**, because the receipts are unambiguous:

**4a. The Alpaca SQLite clerk cannot be the marker substrate, and #1780's original plan is unexecutable as stated.** Markers serve `accounts/<ACCOUNT_ID>/` (`account_artifacts.py:1484-1487`); the clerk serves `accounts/alpaca/<account_id>/` (`writes.py:57-62`). The single production marker acquisition is IBKR-fed (`account_reconciliation.py:383`). The sets of accounts are disjoint. "Move the O_EXCL file markers onto the SQLite claim primitive" would move a mechanism onto a store that none of its users has.

**4b. Admission markers do not become uncertainty episodes.** An episode records *what is unknown about custody*; a marker records *who is inside a critical section right now*. Feeding marker orphans into the reason-policy registry would put a liveness question into a store that has no liveness concept, and would give the account-safety authority a dependency on an Alpaca-only database it does not otherwise touch.

**4c. `repair_account_safety_admission` is deleted, not rewired, and deletion is urgent.** Under ADR 0047's precedent a remedy that no execution can complete is removed rather than documented. This one is stronger than that precedent: measured above, invoking it on the account it was written for leaves the account permanently fenced against all admission with no cure in code. It must not be wired to any surface, and it must not survive as dead code that a future agent could reasonably decide to call. The `AccountSafetyAdmissionMaintenanceState` fence (`:129-138`), the `REPAIRING` status, and the repair-receipt ledger go with it — they exist only to serve it.

**4d. The 17 stranded participant markers on `DUM284968` are removed as artifact-tree state, not by code.** They are inert for admission (measured), so no incident is open; they are the only thing blocking any future repair mechanism, so they cannot simply be left. Removal is a one-line operator action on a stopped data plane, in the same class as the S4 `gate`/`writer` removal the audit already records as "state, not code" (`docs/audits/bot-fleet-stress-2026-08-25.md:335-336`).

**4e. #1780's non-negotiable is restated correctly.** "Markers must not be able to outlive their owner silently" is right, but the marker class that caused the S4 outage is `gate`/`writer`, not `participants` (measured: 0.00 s vs 10.0 s acquisition). Any substrate proposal must be evaluated against the `gate`/`writer` class. A design that bounds participant lifetime and leaves the turnstile markers unbounded fixes the class that was harmless and leaves the class that broke the account.

---

**What this ADR does not settle, and what the human must decide.**

The substrate question cannot be answered until one prior question is answered, and it is not a question about storage:

> **Was `account_safety_entry_admission_lock` (`account_safety.py:490-518`) meant to be wired into the entry path and never was — or has it been superseded by the SQLite clerk's own admission gate (`decide_capability`, `uncertainty.py:474-640`) and should be deleted?**

Everything follows mechanically from the answer, because the reader half is the only thing that makes this a multi-party protocol. With no readers, `wait_for_readers` (`:442-451`) always returns immediately, `readers/` is always empty, and the four marker classes (`gate`, `writer`, `readers/*`, `participants/*`) implement a turnstile with participants on one side only.

**If superseded** — the account-safety verdict is a *durable projection* that the SQLite clerk's admission gate already consults through `sqlite_clerk_compat`, and no entry needs to hold a permit across broker I/O — then the mechanism collapses to a single-writer mutual exclusion over one JSON file, and the answer is:

> **Option A (recommended if superseded): one liveness-bound single-writer lease, engine-local.** Replace all four marker classes with one lease record carrying `owner` (a boot-unique token, not a bare PID — the pattern `default_lease_owner` already uses at `writes.py:49-54`), `expires_at_ms`, and `acquired_at_ms`, renewed at TTL/3 by its holder. This is the pattern the SQLite clerk already proved (`repository.py:100`, renewed by the sweep at TTL/3 per ADR 0047). An expired lease is *breakable*, which is what makes an orphan self-curing and makes the repair path unnecessary rather than merely deleted. Cost: ~4 marker classes → 1 file; no broker-neutral store, no shared contract, no cross-authority coupling. Risk: it discards an unbuilt safety property — if the reader side was genuinely needed, this deletes it silently.

**If missing** — the entry permit was a real safety property (a suspension must be exclusive against in-flight entries, and a broker call can outlive any single-process lock) — then the protocol must span two authorities that today share nothing, and the substrate question becomes real:

> **Option B: broker-neutral per-account coordination store.** One new module owning marker/lease state for any account, keyed on `account_id` alone. Cost: a new durable store and its own recovery story; both authorities take a dependency on it. Benefit: one implementation, one set of orphan semantics, works for engine and Alpaca accounts identically. Risk: a third durable authority in a system whose stated direction (ADR 0035, ADR 0037, ADR 0042) is *fewer* authorities.
>
> **Option C: per-authority stores behind a shared contract.** An interface (`acquire_entry_permit` / `acquire_exclusive` / `is_held`) with two implementations: SQLite rows for Alpaca accounts, files for engine accounts. Cost: two implementations to keep in lockstep, and a parity test naming the canonical one (CLAUDE.md guiding-philosophy #5). Benefit: each authority keeps its own storage and recovery story; no new durable store. Risk: the parity test is the only thing preventing drift, and drift here is a safety property.
>
> **Option D: markers as a projection of an existing log.** Derive the participant roster from an existing append-only account log rather than from filesystem entries, so a crashed owner leaves a record with a readable end rather than a file with no end. Cost: requires a log both authorities already write and both can read — which, given the disjoint stores above, does not exist today. Benefit: no new mutual-exclusion primitive at all. Risk: highest, because it changes what "exclusion" means from "a file exists" to "a fold says so", which is a consistency model change, not a storage change.

**Option E — delete admission markers entirely — is not on the table**, and the reason is worth recording so it is not re-proposed. The artifacts root is a host bind mount shared between a host data plane and a container data plane, which is why `O_EXCL` was chosen over `flock` in the first place (`account_safety.py:313-319`, `:496-498`). `observe_account_truth` is reachable from both the refresh loop (`app/main.py:323-331`) and three HTTP routes, so genuinely concurrent writers exist. Whatever replaces the markers must still exclude two writers across a filesystem boundary.

**The recommendation is Option A, conditional on the answer being "superseded"** — it is the only option that removes machinery instead of adding it, and it is the only one that makes orphans self-curing rather than repairable. But that recommendation is worth nothing if the reader side was a real safety property, and this ADR does not have the evidence to decide that. It is a question about what the account-safety authority is *for*, not about where bytes live.

## Considered and rejected

**A generic age policy on the `uncertainties` table rather than per reason code.** A single "episodes expire after N ms" rule, enforced by a sweep. Rejected because the seven existing causes have incompatible correct answers: `BROKER_SNAPSHOT_STALE` should clear the moment a fresh snapshot lands and never on a clock; `EXIT_STUCK` is a durable escalation that must *not* auto-close; `ORDER_OUTCOME_UNKNOWN` genuinely wants a 30 s clock. A single number would either close `EXIT_STUCK` behind an operator's back or leave `ORDER_OUTCOME_UNKNOWN` open forever. Per-reason is not over-engineering here; it is the minimum granularity at which the answers differ.

**Keeping `terminal` optional with a `LEAVE_OPEN` default.** Cheaper to land and compatible with every current code. Rejected because it defeats the entire purpose: the value of Decision 1 is that a future reason code *cannot* be added without a bounded life, and a permissive default restores exactly the drift that produced three clocks in three modules. The compile-time obligation is the deliverable.

**Migrating uncertainties into `holds` instead.** Symmetrical on paper. Rejected on receipts: `holds` has no policy registry, no typed causes, no severity, no operator copy, two writers with divergent fold paths (`folds.py:1297` vs `external_order_folds.py:88-95`), and is at most two account-scoped rows. `uncertainties` has all of the machinery and all of the consumers. Merging the richer table into the poorer one would delete working structure.

**Landing v12 before #1777.** Tempting, since v12 is mechanical and #1777 is behavioural. Rejected: #1777 changes who writes holds, on what cadence, and with what append discipline. A backfill authored against the current entry-time-coupled producer would encode assumptions the new producer breaks — most obviously "a hold's `opened_at_ms` is when an ENTER was attempted", which stops being true the moment the lifecycle moves to a 15 s sampled cadence.

**Preserving the `UNEXPLAINED_ORDER` → `NO_HOLD` mapping verbatim under "unchanged wire shapes".** The strictly conservative migration. Rejected because "unchanged wire shapes" is a promise to *consumers*, not a promise to preserve a wrong answer. The panel's contract is `hold_reason: HoldReason`; nothing in that contract says a live hold must render as `NO_HOLD`. Normalising during backfill costs one `CASE` in the migration and one end-to-end test.

**Rewiring `repair_account_safety_admission` to a CLI ceremony instead of deleting it** — the ADR 0047 pattern, where the offline path was kept because it worked. Rejected because this path does not work: measured, it fails on the only account with orphans, and its failure durably fences that account. Making it reachable would hand an operator a button that bricks admission. If a repair ceremony is wanted later, it is a new function with a new precondition, not this one behind a CLI.

**Answering the substrate question in this ADR.** The strongest temptation, and the one #1780 explicitly warns against. Rejected because the honest input to that decision — whether the entry permit is a missing safety property or a superseded one — is a design intent that lives with the human who specified `AccountSafetyAuthority`, not in the code. An ADR that guessed would produce a `Accepted` record whose central decision rested on an assumption, which is the failure mode ADR 0039 Decision 1 exists to keep out of the corpus.

## Consequences

**Status is `Proposed`, deliberately.** Decisions 1, 2, 3, and 4a–4e are ready to accept as written. Decision 4's substrate choice is open, and under ADR 0039 an `Accepted` ADR is binding intent an agent follows — which a record containing an unresolved central question cannot be. The ADR moves to `Accepted` when the human answers the reader-side question above, at which point 4f (the chosen substrate) is written in and the `Vocabulary:` obligation is discharged. Per ADR 0039 Decision 1, nothing here asserts code conformance either way.

**Positive:**

- Every future uncertainty acquires a bounded life at registration time, enforced by a required dataclass field rather than by review.
- Three clocks in three modules become three declarations in one registry, with no behaviour change and therefore no new risk on the way in.
- The episode store loses a table, three fold kinds as write paths, a second uniqueness index, and a duplicate write path (`external_order_folds.py`'s inline hold SQL), while every consumer's wire shape is preserved.
- Two live defects are fixed as a side effect of work already scoped: the `UNEXPLAINED_ORDER` → `NO_HOLD` panel mismapping, and the account-fencing repair path.
- The admission-marker question is stated in a form that can actually be answered, rather than as a storage question that has no answer until the intent question is settled.

**Negative / accepted:**

- **v12 is a real migration with a real blast radius.** It rewrites rows in the sole custody authority for every Alpaca account. It requires a backup before, a verified rebuild-from-mirror after, and a tested restore across the version boundary. It is not a refactor and must not be reviewed as one — which is the correction #1773 already accepted as review item 6.
- **Pre-v12 mirrors stay replayable only because the hold folds remain registered.** That is a permanent obligation on a fold registry that otherwise only grows. It should carry a comment naming this ADR, or a future cleanup will delete it and make historical mirrors unreplayable.
- **The 17 markers must be cleared by hand.** No code in the tree can do it after Decision 4c, and none should until the substrate is chosen.
- **Deleting `repair_account_safety_admission` deletes five passing tests** (`test_account_safety.py:167`, `:216`, `:269`, `:314`, `:358`). They test a mechanism that cannot succeed in production; keeping them would keep the mechanism.
- **`account_safety_entry_admission_lock` stays in the tree, unwired, until the human answers.** That is uncomfortable — it is the same "structurally unarmable surface" shape ADR 0047 removed — but it is not *surfaced* to an operator, and deleting it would silently answer the question this ADR exists to ask.

**Scope this ADR widened beyond #1780, stated explicitly:**

1. The repair path's failure mode (permanent admission fence), not just its deadness — Decision 4c.
2. The unwired reader half of the admission protocol, which #1780 does not mention and which the substrate choice depends on — Decision 4, open question.
3. The frozen `clerk_generation.json` stamp, which makes marker generation-fencing inert — Context.
4. The catalog-quarantine confirmation TTL, added to #1780's two-member exclusion list — Decision 3.
5. The `UNEXPLAINED_ORDER` / `UNEXPLAINED_ORDER_HOLD` panel mismapping, which #1780's compat-projection clause would otherwise ship forward — Decision 2.
6. The #1777-lands-first sequencing constraint — Decision 2.

**Non-goals, restated from #1780 and unchanged:** this project does not change intake-fence serialization, and does not change act-time re-proof.
