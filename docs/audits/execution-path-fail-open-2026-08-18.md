# Execution-path fail-open sweep — 2026-08-18

> **STATUS: SUPPORTING POINT-IN-TIME EVIDENCE — NOT IMPLEMENTATION AUTHORITY.**
> This is a diagnosis at the pinned commit. ADRs and current code remain the
> authorities; `docs/known-gaps.md` remains the defect register.

**Charter:** [#1644](https://github.com/tim1016/learn-ai/issues/1644)

**Commit read:** [`a7771477f7ff897891e7685c745b84b11b0c5e0e`](https://github.com/tim1016/learn-ai/tree/a7771477f7ff897891e7685c745b84b11b0c5e0e) (merged custody repair [#1651](https://github.com/tim1016/learn-ai/pull/1651))

**Question:** Outside #1592's decision-to-custody walk, can recovery, sweeping,
activity replay, or fence release admit later new exposure while custody evidence
is missing, indeterminate, or rejected?

**Answer:** Yes. One activated-SQLite seam and five retiring legacy-JSONL seams
remain. On SQLite, a broker-position mismatch on a symbol with a captured working
order is explicitly classified `indeterminate`, but the reconciliation verdict is
`clean`, no new uncertainty is authored, startup accepts the pass, and admission
does not inspect ordinary working bot-entry orders. On legacy, (1) `stale`,
`missing_intent`, and in-flight-suppressed position reconciliation are not submit
admission fences, and the selector installs legacy after intent replay without an
account reconciliation; (2) bounded activity replay can durably advance its cursor
over unowned activity without raising a hold, relying on the same
non-authoritative legacy reconciliation to catch up; (3) direct hold clear compares
only millisecond timestamps, so later unexplained evidence recorded in the same
millisecond as the proof can be cleared; (4) the reconciliation order snapshot is
capped at 500 without a completeness check, so a full partial page can be accepted
as clean and clear a hold; and (5) the paper-only developer reset can move aside an
unactivated legacy authority without broker-flat or stopped-runner proof, after
which selection installs a fresh empty legacy writer. The five legacy findings
resolve by deletion under accepted ADR 0037, not by repairs or legacy regression
tests.

No other in-scope writer was confirmed fail-open. In particular, #1651 now makes
unexpected/cancelled SQLite reconciliation failure author a durable
`RECONCILIATION_INCOMPLETE` blocker before releasing its process fence; execution
coverage recovery is CAS-guarded and atomically resolves economics plus
uncertainty; recovery replacement requires stopped authority, identity/integrity
proof, and a subsequent authority-first boot reconciliation; and SQLite activity
replay is deliberately a no-op because full reconciliation is canonical.

## Scope and exclusions

“Fail open” retains #1592's narrow definition: a **later new-exposure decision is
allowed** despite missing, indeterminate, or rejected custody evidence. A wrong
display alone does not qualify. The admission authority checked was
[`sqlite/uncertainty.py:388-509`](https://github.com/tim1016/learn-ai/blob/a7771477f7ff897891e7685c745b84b11b0c5e0e/PythonDataService/app/broker/alpaca/clerk/sqlite/uncertainty.py#L388-L509)
for SQLite and the actual legacy submit/effect gates, not a UI projection.

The five already-confirmed seams [#1614](https://github.com/tim1016/learn-ai/issues/1614)–[#1618](https://github.com/tim1016/learn-ai/issues/1618)
were excluded. The nine already-refuted #1592 candidates were also excluded:
submit-timeout mapping, shielded SQLite effect ownership, undrained legacy effect
tasks, the legacy Postgres projection task, SQLite/mirror non-atomicity, bot-runner
multi-file publication, an absent production stream gate, broad catches around
closed-order/activity recovery, and the historical IBKR call graph. This audit
does not use any of those as a new confirmation.

ADR 0037 is accepted but not implemented: a missing activation still constructs
legacy at this commit
([`active_authority.py:214-237`](https://github.com/tim1016/learn-ai/blob/a7771477f7ff897891e7685c745b84b11b0c5e0e/PythonDataService/app/broker/alpaca/clerk/active_authority.py#L214-L237)).
Legacy-only findings are therefore reachable now, but their prescribed closure is
the ADR's no-authority fallback plus deletion
([ADR 0037](https://github.com/tim1016/learn-ai/blob/a7771477f7ff897891e7685c745b84b11b0c5e0e/docs/architecture/adrs/0037-sqlite-sole-alpaca-custody-authority.md)).

## Writer census and adversarial result

The four fault probes in the table are: **E** unexpected exception, **P** partial
result, **C** crash between writes, and **S** stale-but-clean evidence. “Refuted”
means the named invariant prevents admission from reopening; it does not mean the
path is operationally infallible.

| Writer / relaxation | Authority and retiring reachability | E / P / C / S result | Verdict and admission consequence |
|---|---|---|---|
| SQLite effect recovery and terminalization in [`reconcile.py:187-299`](https://github.com/tim1016/learn-ai/blob/a7771477f7ff897891e7685c745b84b11b0c5e0e/PythonDataService/app/broker/alpaca/clerk/sqlite/reconcile.py#L187-L299), including ENTER exact lookup, EXIT, manual submit, and cancellation | Activated SQLite; live | E bubbles to the account-level incomplete handler. P/malformed ENTER and non-`BrokerError` handling are the closed [#1616](https://github.com/tim1016/learn-ai/issues/1616)/[#1617](https://github.com/tim1016/learn-ai/issues/1617) seams and were not re-audited. C leaves the effect nonterminal for boot recovery. S can terminalize only from exact broker identity or the already-specified post-grace absence contract. | **REFUTED outside exclusions.** Effect/order folds and their admission uncertainty are a single SQLite transition; unresolved work stays selected. |
| SQLite account reconciliation: stale/incomplete resolution, unexplained-order hold release, position-drift resolution, EXIT-flat resolution, and process-fence end in [`reconcile.py:304-479`](https://github.com/tim1016/learn-ai/blob/a7771477f7ff897891e7685c745b84b11b0c5e0e/PythonDataService/app/broker/alpaca/clerk/sqlite/reconcile.py#L304-L479) and [`:606-825`](https://github.com/tim1016/learn-ai/blob/a7771477f7ff897891e7685c745b84b11b0c5e0e/PythonDataService/app/broker/alpaca/clerk/sqlite/reconcile.py#L606-L825) | Activated SQLite; live | E/C now commit `RECONCILIATION_INCOMPLETE` before `end_reconciliation`; if blocker publication fails, end is not called. P at the 500-order boundary is `stale`. Control-revision CAS retries snapshots changed by an interleaving local writer. S with an in-flight mismatch is the exception detailed as C1 below. | **CONFIRMED C1; otherwise refuted.** Existing drift is retained, but a first indeterminate episode is not authored and admission reopens. |
| SQLite stream-health hold resolution in [`runtime.py:803-854`](https://github.com/tim1016/learn-ai/blob/a7771477f7ff897891e7685c745b84b11b0c5e0e/PythonDataService/app/broker/alpaca/clerk/sqlite/runtime.py#L803-L854) | Activated SQLite; live | E/C before the resolution transition prevent the subsequent `accept_enter`; after commit, ENTER admission is rechecked under the same intake lock. P is impossible because the hold check/fold is one transition. S is a current positive health snapshot; rejected evidence now makes the execution channel unhealthy under closed #1615. | **REFUTED.** The optional `gate=None` seam was already refuted by #1592 and was not revisited. |
| SQLite external-order acknowledgement in [`external_orders.py:99-128`](https://github.com/tim1016/learn-ai/blob/a7771477f7ff897891e7685c745b84b11b0c5e0e/PythonDataService/app/broker/alpaca/clerk/sqlite/external_orders.py#L99-L128) and its fold [`external_order_folds.py:108-139`](https://github.com/tim1016/learn-ai/blob/a7771477f7ff897891e7685c745b84b11b0c5e0e/PythonDataService/app/broker/alpaca/clerk/sqlite/external_order_folds.py#L108-L139) | Activated SQLite; live operator recovery | E rejects the command. P/C cannot expose reviewed-with-active-cause or resolved-with-unreviewed-cause because acknowledgement and the exact hold-cause removal share one SQLite transaction. S is not asserted as broker absence: the operator deliberately accepts a retained external observation, and other causes remain held. | **REFUTED.** This is explicit operator disposition, not missing evidence; tests pin narrow cause removal ([`test_reconcile.py:695-740`](https://github.com/tim1016/learn-ai/blob/a7771477f7ff897891e7685c745b84b11b0c5e0e/PythonDataService/tests/broker/alpaca/clerk/sqlite/test_reconcile.py#L695-L740)). |
| Direct and historical execution-coverage resolution in [`repository_execution_coverage_api.py:339-519`](https://github.com/tim1016/learn-ai/blob/a7771477f7ff897891e7685c745b84b11b0c5e0e/PythonDataService/app/broker/alpaca/clerk/sqlite/repository_execution_coverage_api.py#L339-L519) and [`historical_execution_recovery.py:284-487`](https://github.com/tim1016/learn-ai/blob/a7771477f7ff897891e7685c745b84b11b0c5e0e/PythonDataService/app/broker/alpaca/clerk/sqlite/historical_execution_recovery.py#L284-L487) | Activated SQLite; live operator recovery | E leaves the active coverage uncertainty. P is rejected unless exact activity identity/economics and the rendered authority context match. C after quarantine retains a blocking uncertainty and accepts only the exact `revision + 1` replay. S is fenced by signed plan TTL, PAPER/account recheck, generation/token/revision CAS, and exact economics. | **REFUTED.** The recovery transition swaps cumulative/exact economics and resolves its uncertainty atomically; the interruption test proves the intermediate state remains blocked ([`test_historical_execution_recovery.py:509-570`](https://github.com/tim1016/learn-ai/blob/a7771477f7ff897891e7685c745b84b11b0c5e0e/PythonDataService/tests/broker/alpaca/clerk/sqlite/test_historical_execution_recovery.py#L509-L570)). |
| Typed recovery dispatcher (`resolve_execution_coverage`, `stop_bot_decisions`, `reconcile_now`, verified cancellation) in [`recovery_execution.py:77-196`](https://github.com/tim1016/learn-ai/blob/a7771477f7ff897891e7685c745b84b11b0c5e0e/PythonDataService/app/broker/alpaca/clerk/sqlite/recovery_execution.py#L77-L196) | Activated SQLite; live | E propagates. P in a cancel batch leaves uncancelled working evidence present; it does not itself resolve a hold/uncertainty. C after durable STOP leaves the run stopped and replay re-drives only local quiescence. S is rejected by the action's concurrency token and fresh context; reconciliation inherits the row above. | **REFUTED.** No generic “clear” verb exists. |
| SQLite trade-update reconnect activity/history replay in [`trade_evidence.py:359-374`](https://github.com/tim1016/learn-ai/blob/a7771477f7ff897891e7685c745b84b11b0c5e0e/PythonDataService/app/broker/alpaca/clerk/trade_evidence.py#L359-L374) | Activated SQLite; live | Account-activity replay writes nothing; full account reconciliation must complete before reconnect admission. E/P/C/S therefore inherit SQLite reconciliation's blocker behavior and C1, rather than creating a second cursor authority. | **REFUTED as a separate writer.** Returning `0` is intentional authority locality, not silently skipped SQLite custody. |
| SQLite backup restore, mirror rebuild, authority reset, and offline schema upgrade/rollback in [`recovery.py:383-810`](https://github.com/tim1016/learn-ai/blob/a7771477f7ff897891e7685c745b84b11b0c5e0e/PythonDataService/app/broker/alpaca/clerk/sqlite/recovery.py#L383-L810) and [`offline_v9_upgrade.py`](https://github.com/tim1016/learn-ai/blob/a7771477f7ff897891e7685c745b84b11b0c5e0e/PythonDataService/app/broker/alpaca/clerk/sqlite/offline_v9_upgrade.py) | SQLite offline recovery; live tooling but no live writer may coexist | E/C restore the preserved DB where possible or leave registry/activation identity disagreement, which startup refuses. P is rejected by mirror-head, generation/token, file-type, live-lease/process-stop, and projection-parity checks. Authority reset additionally requires a fresh flat/no-open-order broker proof and every governed bot stopped ([`recovery.py:995-1036`](https://github.com/tim1016/learn-ai/blob/a7771477f7ff897891e7685c745b84b11b0c5e0e/PythonDataService/app/broker/alpaca/clerk/sqlite/recovery.py#L995-L1036)). S after reset is caught by mandatory boot reconciliation before writer installation. | **REFUTED.** Recovery receipts may be missing after a post-publication crash, but receipts do not grant order authority. Startup validates identity and reconciles before returning a writer ([`active_authority.py:239-319`](https://github.com/tim1016/learn-ai/blob/a7771477f7ff897891e7685c745b84b11b0c5e0e/PythonDataService/app/broker/alpaca/clerk/active_authority.py#L239-L319)). |
| Paper developer clean-slate reset in [`dev_reset.py:106-278`](https://github.com/tim1016/learn-ai/blob/a7771477f7ff897891e7685c745b84b11b0c5e0e/PythonDataService/app/broker/alpaca/clerk/sqlite/dev_reset.py#L106-L278) | Activated SQLite and unactivated legacy; live developer tooling | Activated SQLite is conservative: stop state is checked and the establishment-backed reset registry forces reactivation. For unactivated legacy, E/C roll moved artifacts back, but the successful operation intentionally omits broker-flat proof and runner roll call, its stop check returns immediately when no SQLite DB exists, and it records no reset fence without an established generation. | **CONFIRMED C6 for unactivated legacy; otherwise refuted.** Selection sees no activation/reset fence and installs a fresh legacy writer after the old JSONL has been quarantined. |
| Legacy-to-SQLite authority cutover in [`cutover.py:429-685`](https://github.com/tim1016/learn-ai/blob/a7771477f7ff897891e7685c745b84b11b0c5e0e/PythonDataService/app/broker/alpaca/clerk/sqlite/cutover.py#L429-L685) | Offline authority replacement under the runbook's all-writers-stopped boundary ([runbook](https://github.com/tim1016/learn-ai/blob/a7771477f7ff897891e7685c745b84b11b0c5e0e/docs/runbooks/alpaca-sqlite-clerk-recovery-and-cutover.md#stop-boundary)) | E/C before activation rolls quarantine back; once activation append starts, evidence stays quarantined so malformed/ambiguous activation fails startup closed. P is rejected by content-addressed database, initialization, runner roster, legacy artifacts, and plan token. S broker evidence must be bounded, PAPER, flat, and order-free at apply, then boot reconciliation rechecks before installing the SQLite writer. | **REFUTED within the documented ceremony.** Durable stopped-bot evidence is rehashed at apply, and no post-cutover broker call bypasses authority-first recovery. |
| Legacy uncertain-submit/startup replay in [`recovery.py:33-233`](https://github.com/tim1016/learn-ai/blob/a7771477f7ff897891e7685c745b84b11b0c5e0e/PythonDataService/app/broker/alpaca/clerk/recovery.py#L33-L233) | Legacy JSONL; retiring (ADR 0037) | E/BrokerError leaves the intent unresolved; P resolves each intent independently; C leaves capture-first journal evidence; S/404 remains uncertain for the specified 30-second visibility grace, then follows the tested absence contract. | **REFUTED as a new seam.** The ordinary timeout mapping was one of #1592's nine refutations and was not reopened. |
| Legacy reconciliation verdict and periodic sweep in [`clerk.py:1108-1233`](https://github.com/tim1016/learn-ai/blob/a7771477f7ff897891e7685c745b84b11b0c5e0e/PythonDataService/app/broker/alpaca/clerk/clerk.py#L1108-L1233) and [`reconcile.py:70-137`](https://github.com/tim1016/learn-ai/blob/a7771477f7ff897891e7685c745b84b11b0c5e0e/PythonDataService/app/broker/alpaca/clerk/reconcile.py#L70-L137) | Legacy JSONL; retiring | E is logged for a later pass. P/C after `UNEXPLAINED_ORDER` still derive a hold even if `HOLD_SET` was not appended. But `stale` and `missing_intent` append only a reconciliation line. S/in-flight evidence can suppress position drift and even return clean with `broker_facts_complete=False`. | **CONFIRMED C2.** The projected freeze/incompleteness is not read by submit/effect admission. |
| Legacy full-page order reconciliation in [`clerk.py:1117-1208`](https://github.com/tim1016/learn-ai/blob/a7771477f7ff897891e7685c745b84b11b0c5e0e/PythonDataService/app/broker/alpaca/clerk/clerk.py#L1117-L1208) | Legacy JSONL; retiring | E becomes stale only for typed `BrokerError`; the excluded broad catch is not used here. P is accepted: `status="all", limit=500` has no full-page completeness check. Since the adapter requests descending order, an older still-working foreign order can be omitted behind 500 newer submissions. C preserves append-only rows, but cannot repair a fact never read. S is the successful truncated snapshot itself. | **CONFIRMED C5.** A false-clean result can release an unexplained-order hold or supply a false working-order proof while the omitted broker order persists. |
| Legacy activity replay/cursor in [`activity_recovery.py:31-73`](https://github.com/tim1016/learn-ai/blob/a7771477f7ff897891e7685c745b84b11b0c5e0e/PythonDataService/app/broker/alpaca/clerk/activity_recovery.py#L31-L73) and [`trade_evidence.py:132-154`](https://github.com/tim1016/learn-ai/blob/a7771477f7ff897891e7685c745b84b11b0c5e0e/PythonDataService/app/broker/alpaca/clerk/trade_evidence.py#L132-L154) | Legacy JSONL; retiring | E propagates from the writer without a cursor row for the failing item; the caller's broad catch is an excluded #1592 candidate and is not used as this confirmation. P/C are replayable by durable activity id and an inclusive millisecond cursor, but a successful bounded page can omit older activity and an unowned retained fill still advances the cursor. S is accepted as an activity receipt without a hold or attribution. | **CONFIRMED C3.** A later bounded closed-order pass may catch the order, but a successful partial page can omit an old-submitted order that filled during the gap; legacy C2 then leaves admission open. The activity writer itself has already accepted the unowned fact. |
| Legacy direct hold clear, custody-resolution orchestration, and inventory baseline in [`clerk.py:543-860`](https://github.com/tim1016/learn-ai/blob/a7771477f7ff897891e7685c745b84b11b0c5e0e/PythonDataService/app/broker/alpaca/clerk/clerk.py#L543-L860) and [`custody_resolution.py:37-344`](https://github.com/tim1016/learn-ai/blob/a7771477f7ff897891e7685c745b84b11b0c5e0e/PythonDataService/app/broker/alpaca/clerk/custody_resolution.py#L37-L344) | Legacy JSONL; retiring operator recovery | The snapshot-guarded orchestration holds intake across mutating plans; E marks the idempotency reservation failed; C leaves append-only evidence for replay. Inventory baseline is an explicit operator adoption, rejects running bots/unresolved intents/working orders, and its durable baseline makes a crash before the clean receipt conservative. Direct `clear_hold`, however, releases intake around proof and compares only `since_ms > proof_observed_at_ms`. | **CONFIRMED C4 for direct clear; otherwise refuted.** A later unexplained row at the same millisecond is followed by `HOLD_CLEARED`, so last-write-wins admission opens. |

## Confirmed seams

### C1 — SQLite turns first in-flight position uncertainty into a clean pass

`plan_account_reconciliation` computes `mismatched_symbols`, moves every mismatch
whose symbol has a nonterminal broker order to `indeterminate_symbols`, and then
sets the verdict to `clean` whenever there is neither a foreign order nor a
non-in-flight drift
([`reconcile.py:121-184`](https://github.com/tim1016/learn-ai/blob/a7771477f7ff897891e7685c745b84b11b0c5e0e/PythonDataService/app/broker/alpaca/clerk/sqlite/reconcile.py#L121-L184)).
The test contract explicitly pins “mismatch + partially filled order” as clean and
indeterminate
([`test_reconcile.py:404-421`](https://github.com/tim1016/learn-ai/blob/a7771477f7ff897891e7685c745b84b11b0c5e0e/PythonDataService/tests/broker/alpaca/clerk/sqlite/test_reconcile.py#L404-L421)).

`_sync_position_drift` returns early for that shape. Its comment correctly says a
working order cannot prove a *previously observed* drift disappeared, but the
early return also means a **first** indeterminate mismatch authors no episode
([`reconcile.py:363-381`](https://github.com/tim1016/learn-ai/blob/a7771477f7ff897891e7685c745b84b11b0c5e0e/PythonDataService/app/broker/alpaca/clerk/sqlite/reconcile.py#L363-L381)).
Finalization suppresses EXIT-flat resolution when indeterminate, but still resolves
`RECONCILIATION_INCOMPLETE` and returns `verdict="clean"`; the public result even
omits `indeterminate_symbols`
([`reconcile.py:777-825`](https://github.com/tim1016/learn-ai/blob/a7771477f7ff897891e7685c745b84b11b0c5e0e/PythonDataService/app/broker/alpaca/clerk/sqlite/reconcile.py#L777-L825)).

The strongest refutation would be an account-wide working-entry fence. There is
none. New-exposure admission checks reconciliation-in-progress, active EXIT,
holds, active uncertainties, and manual orders—not an ordinary bot ENTRY
([`uncertainty.py:404-509`](https://github.com/tim1016/learn-ai/blob/a7771477f7ff897891e7685c745b84b11b0c5e0e/PythonDataService/app/broker/alpaca/clerk/sqlite/uncertainty.py#L404-L509)).
`accept_enter` relies on that decision
([`enter.py:179-205`](https://github.com/tim1016/learn-ai/blob/a7771477f7ff897891e7685c745b84b11b0c5e0e/PythonDataService/app/broker/alpaca/clerk/sqlite/enter.py#L179-L205)).
The order's submission effect can already be `succeeded` once exact broker identity
is known while the order remains working; therefore it supplies no separate
nonterminal-effect fence. Startup also treats any result other than `stale` as
recoverable and installs the writer
([`runtime.py:596-612`](https://github.com/tim1016/learn-ai/blob/a7771477f7ff897891e7685c745b84b11b0c5e0e/PythonDataService/app/broker/alpaca/clerk/sqlite/runtime.py#L596-L612)). An
affected strategy's own `Start` is independently blocked when its custody proof
reports that working order ([`runtime.py:647-679`](https://github.com/tim1016/learn-ai/blob/a7771477f7ff897891e7685c745b84b11b0c5e0e/PythonDataService/app/broker/alpaca/clerk/sqlite/runtime.py#L647-L679),
[`run_admission.py:257-276`](https://github.com/tim1016/learn-ai/blob/a7771477f7ff897891e7685c745b84b11b0c5e0e/PythonDataService/app/services/run_admission.py#L257-L276)); that
instance-local proof is not an account fence.

Concrete surviving sequence: a captured, broker-acknowledged ENTRY is partially
filled; the order response and position response describe different points in the
fill's propagation, so folded attributed quantity and broker position disagree;
the working order makes the mismatch indeterminate; reconciliation returns clean
and writes no blocker; a later ENTER for an already-active run, a manual
new-exposure command, or a different strategy whose own custody proof has no
working order reaches admission. This is exactly
indeterminate custody evidence, not merely an optimistic display. Confidence:
**high on code behavior and admission reachability; moderate on the duration of a
real broker propagation window**.

### C2 — Legacy stale/missing-intent recovery is not submit-authoritative

Legacy reconciliation maps broker-read failure to `stale` and unattributed broker
exposure to `missing_intent`, but only `unexplained_order` asks `_apply_reconcile_plan`
to set a hold
([`clerk/reconcile.py:70-137`](https://github.com/tim1016/learn-ai/blob/a7771477f7ff897891e7685c745b84b11b0c5e0e/PythonDataService/app/broker/alpaca/clerk/reconcile.py#L70-L137),
[`clerk.py:1211-1233`](https://github.com/tim1016/learn-ai/blob/a7771477f7ff897891e7685c745b84b11b0c5e0e/PythonDataService/app/broker/alpaca/clerk/clerk.py#L1211-L1233)).
`derive.account_freeze_state` truthfully projects both verdicts as freezes, but the
actual legacy manual and bot ENTER gates read `hold_state`, not that freeze
([`derive.py:65-102`](https://github.com/tim1016/learn-ai/blob/a7771477f7ff897891e7685c745b84b11b0c5e0e/PythonDataService/app/broker/alpaca/clerk/derive.py#L65-L102),
[`clerk.py:215-245`](https://github.com/tim1016/learn-ai/blob/a7771477f7ff897891e7685c745b84b11b0c5e0e/PythonDataService/app/broker/alpaca/clerk/clerk.py#L215-L245),
[`effects.py:234-301`](https://github.com/tim1016/learn-ai/blob/a7771477f7ff897891e7685c745b84b11b0c5e0e/PythonDataService/app/broker/alpaca/clerk/effects.py#L234-L301)).
The repository test even pins `missing_intent` as observational with no hold
([`test_clerk_reconciliation.py:668-680`](https://github.com/tim1016/learn-ai/blob/a7771477f7ff897891e7685c745b84b11b0c5e0e/PythonDataService/tests/broker/alpaca/clerk/test_clerk_reconciliation.py#L668-L680)).

There is a second successful-read form of the same authority gap. Position drift
on a symbol with a locally in-flight order is deliberately suppressed for that
pass ([`derive.py:288-341`](https://github.com/tim1016/learn-ai/blob/a7771477f7ff897891e7685c745b84b11b0c5e0e/PythonDataService/app/broker/alpaca/clerk/derive.py#L288-L341)). The pass may return
`clean` while separately marking `broker_facts_complete=False`
([`clerk.py:1199-1208`](https://github.com/tim1016/learn-ai/blob/a7771477f7ff897891e7685c745b84b11b0c5e0e/PythonDataService/app/broker/alpaca/clerk/clerk.py#L1199-L1208)). That incompleteness protects a
new `Start` through its custody proof, but neither manual submission nor effects
from an already-active run consult it.

Boot is weaker still: with no activation, selection awaits only unresolved-intent
replay and installs the legacy writer; it does not require an account reconciliation
first. A pre-existing broker position with no local intent can therefore coexist
with a newly order-capable legacy process. The later periodic pass improves the
display freeze but still does not fix admission. Confidence: **high**. Reachability:
**legacy only; delete under ADR 0037**.

### C3 — Legacy activity replay accepts unowned history without fencing admission

The legacy cursor is the maximum `occurred_at_ms` of durable `ACTIVITY_RECOVERY`
rows. `record` deduplicates by activity id, looks up an owner, and appends the row
even when no owner exists; it neither emits `UNEXPLAINED_ORDER` nor sets a hold
([`activity_recovery.py:31-73`](https://github.com/tim1016/learn-ai/blob/a7771477f7ff897891e7685c745b84b11b0c5e0e/PythonDataService/app/broker/alpaca/clerk/activity_recovery.py#L31-L73)).
The regression fixture explicitly records a foreign fill with `owned is False` and
then proves the cursor advanced to its timestamp
([`test_trade_updates.py:1084-1139`](https://github.com/tim1016/learn-ai/blob/a7771477f7ff897891e7685c745b84b11b0c5e0e/PythonDataService/tests/broker/alpaca/test_trade_updates.py#L1084-L1139)).

Ascending writes and the broker adapter's inclusive `>= after_ms` filter refute a
simple crash-between-equal-timestamps loss
([`broker.py:93-132`](https://github.com/tim1016/learn-ai/blob/a7771477f7ff897891e7685c745b84b11b0c5e0e/PythonDataService/app/broker/alpaca/broker.py#L93-L132)).
They do not refute the admission gap: replay is bounded, and the later closed-order
window is independently bounded by a list whose selection is based on order
submission time, not the gap transition time
([`trade_updates.py:641-665`](https://github.com/tim1016/learn-ai/blob/a7771477f7ff897891e7685c745b84b11b0c5e0e/PythonDataService/app/broker/alpaca/trade_updates.py#L641-L665),
[`client.py:224-239`](https://github.com/tim1016/learn-ai/blob/a7771477f7ff897891e7685c745b84b11b0c5e0e/PythonDataService/app/broker/alpaca/client.py#L224-L239)). If the
corresponding old-submitted terminal order is omitted from a successful page, the
durable unowned fill itself creates no hold; periodic legacy reconciliation then
falls into C2's non-authoritative `missing_intent` verdict. Confidence: **high on
writer semantics, moderate on the bounded-window omission**. The broad exception
catches already refuted by #1592 are not part of this confirmation. Reachability:
**legacy only; delete under ADR 0037**.

### C4 — Legacy direct hold clear can erase same-millisecond newer evidence

For an unexplained-order hold, `clear_hold` records `proof_observed_at_ms`, runs a
clean reconciliation without holding intake, reacquires intake, and refuses a clear
only when the current hold's `since_ms` is **greater than** the proof timestamp
([`clerk.py:600-666`](https://github.com/tim1016/learn-ai/blob/a7771477f7ff897891e7685c745b84b11b0c5e0e/PythonDataService/app/broker/alpaca/clerk/clerk.py#L600-L666)).
`hold_state` updates `since_ms` from each `UNEXPLAINED_ORDER`, but both values have
only integer-millisecond resolution
([`derive.py:105-139`](https://github.com/tim1016/learn-ai/blob/a7771477f7ff897891e7685c745b84b11b0c5e0e/PythonDataService/app/broker/alpaca/clerk/derive.py#L105-L139)).

The existing race test uses an artificially strictly-incrementing clock and thus
proves only the `>` case
([`test_clerk_reconciliation.py:628-665`](https://github.com/tim1016/learn-ai/blob/a7771477f7ff897891e7685c745b84b11b0c5e0e/PythonDataService/tests/broker/alpaca/clerk/test_clerk_reconciliation.py#L628-L665)).
With a real same-millisecond interleaving, a later unexplained lifecycle row has
`since_ms == proof_observed_at_ms`; the check passes, `HOLD_CLEARED` is appended
after that row, and last-write-wins `hold_state` opens submission. The
snapshot-guarded `resolve_custody` path is refuted because it holds intake over its
mutating plan; this confirmation is specific to the separate direct `clear_hold`
API, which remains a control-secret-gated legacy route at this commit
([`routers/brokers.py:754-774`](https://github.com/tim1016/learn-ai/blob/a7771477f7ff897891e7685c745b84b11b0c5e0e/PythonDataService/app/routers/brokers.py#L754-L774)).
Confidence: **high on deterministic code behavior, low/moderate on race
frequency**. Reachability: **legacy only; delete under ADR 0037**.

### C5 — Legacy reconciliation accepts a full partial order page as complete

Every fresh legacy reconciliation requests `status="all", limit=500`, then treats
the returned collection as the complete order universe. It has no analogue of
SQLite's full-page stale check
([`clerk.py:1117-1208`](https://github.com/tim1016/learn-ai/blob/a7771477f7ff897891e7685c745b84b11b0c5e0e/PythonDataService/app/broker/alpaca/clerk/clerk.py#L1117-L1208)). The Alpaca client explicitly requests descending order
([`client.py:224-239`](https://github.com/tim1016/learn-ai/blob/a7771477f7ff897891e7685c745b84b11b0c5e0e/PythonDataService/app/broker/alpaca/client.py#L224-L239)). Thus an older, still-working
foreign order can fall outside a successful page containing 500 newer submissions;
if positions are flat and the returned page is otherwise explained, the plan is
clean even though the foreign order persists.

This is independently admission-relevant: `clear_hold` accepts that clean result
as proof and appends `HOLD_CLEARED`
([`clerk.py:632-665`](https://github.com/tim1016/learn-ai/blob/a7771477f7ff897891e7685c745b84b11b0c5e0e/PythonDataService/app/broker/alpaca/clerk/clerk.py#L632-L665)). It can also understate the
working-order fact in a Start proof, while manual submits and effects are already
subject only to the hold gate described in C2. This is a successful partial-result
failure, not the broad exception handling excluded from #1592. Confidence:
**high on deterministic code behavior; moderate on an account accumulating the
required order depth**. Reachability: **legacy only; delete under ADR 0037**.

### C6 — Developer reset can erase unactivated legacy authority and reinstall an empty writer

The paper developer reset advertises that it operates without broker contact and
its manifest explicitly lists `broker_flat_proof` and `runner_roll_call` as
intentional omissions
([`dev_reset.py:106-114`](https://github.com/tim1016/learn-ai/blob/a7771477f7ff897891e7685c745b84b11b0c5e0e/PythonDataService/app/broker/alpaca/clerk/sqlite/dev_reset.py#L106-L114),
[`dev_reset.py:213-236`](https://github.com/tim1016/learn-ai/blob/a7771477f7ff897891e7685c745b84b11b0c5e0e/PythonDataService/app/broker/alpaca/clerk/sqlite/dev_reset.py#L213-L236)). Its stop check proves only SQLite state and returns immediately when no
SQLite database exists
([`dev_reset.py:436-474`](https://github.com/tim1016/learn-ai/blob/a7771477f7ff897891e7685c745b84b11b0c5e0e/PythonDataService/app/broker/alpaca/clerk/sqlite/dev_reset.py#L436-L474)). It moves legacy JSONL and runner artifacts, but records the
startup reset fence only when the established-SQLite registry already contains a
generation
([`dev_reset.py:252-278`](https://github.com/tim1016/learn-ai/blob/a7771477f7ff897891e7685c745b84b11b0c5e0e/PythonDataService/app/broker/alpaca/clerk/sqlite/dev_reset.py#L252-L278)). The CLI regression test expressly allows a legacy journal to be moved
without broker evidence
([`test_cutover_cli.py:279-325`](https://github.com/tim1016/learn-ai/blob/a7771477f7ff897891e7685c745b84b11b0c5e0e/PythonDataService/tests/broker/alpaca/clerk/sqlite/test_cutover_cli.py#L279-L325)).

For an unactivated account, the next selector read still sees `activation is None`
and constructs a legacy Clerk, runs intent recovery against the now-empty journal,
then installs its sweep and evidence sink
([`active_authority.py:214-237`](https://github.com/tim1016/learn-ai/blob/a7771477f7ff897891e7685c745b84b11b0c5e0e/PythonDataService/app/broker/alpaca/clerk/active_authority.py#L214-L237)). Existing broker positions or working orders can therefore coexist with a
fresh order-capable authority that has forgotten the old hold, uncertainty, and
ownership evidence. Activated SQLite reset is refuted: its established generation
causes startup to remain unavailable pending reactivation. Confidence: **high**.
Reachability: **unactivated legacy only; delete under ADR 0037**.

## Recommended tracker items

The following text is ready to file. It deliberately requests no fixes to legacy
modules and no legacy regression tests.

### Filed issue 1 — [#1655](https://github.com/tim1016/learn-ai/issues/1655)

**Title:** Fail closed when SQLite reconciliation has in-flight position uncertainty

**Body:**

> Found by charter #1644 at `a7771477`. `plan_account_reconciliation` classifies a
> broker-vs-attributed position mismatch on a symbol with a nonterminal captured
> order as `indeterminate_symbols`, but returns `verdict="clean"` when there is no
> other drift. `_sync_position_drift` retains an already-active drift episode yet
> authors no blocker for a first indeterminate episode. Finalization resolves
> `RECONCILIATION_INCOMPLETE`, the public result omits the indeterminate symbols,
> startup accepts the pass, and new-exposure admission does not inspect ordinary
> working bot ENTRY orders. An already-active run, manual submit, or another
> strategy whose own proof has no working order can therefore admit new exposure
> while account custody evidence is explicitly indeterminate. The affected
> strategy's own Start remains blocked by its instance working-order proof.
>
> Acceptance criteria:
>
> - A nonempty `indeterminate_symbols` result is never represented as an
>   admission-clean account.
> - Author one durable, typed, account-wide uncertainty (or equivalently strong
>   fence) on the first indeterminate observation; it blocks new exposure and
>   preserves reduction/cancel/reconcile reachability.
> - Do not resolve that episode until a later complete broker snapshot proves
>   attributed equality with no in-flight indeterminacy; blocker resolution and
>   the clean receipt are atomic under intake/control-revision fencing.
> - Surface the indeterminate symbols in the reconciliation result/receipt rather
>   than returning a lossy `clean` result.
> - Regression-test periodic, operator, reconnect, and boot recovery with a known
>   partially filled working order and a broker position ahead of folded custody;
>   prove manual new exposure, an active-run ENTER, and a different strategy's
>   ENTER are blocked. Preserve the instance-local Start work-state fence.
> - Inject an exception/crash after the blocker write and prove restart remains
>   blocked; then prove a later coherent exact snapshot resolves once.

### Filed issue 2 — [#1656](https://github.com/tim1016/learn-ai/issues/1656)

**Title:** Verify ADR 0037 deletion closes legacy incomplete reconciliation admission

**Body:**

> Charter #1644 found an additional fail-open mechanism reachable only through the
> legacy JSONL Alpaca authority at `a7771477`: `stale`/`missing_intent`
> reconciliation appends a display/start freeze but no submit hold, while manual
> and bot ENTER admission read only `hold_state`. Successful reconciliation also
> suppresses position drift on symbols with in-flight local orders and can return
> clean with `broker_facts_complete=False`; that fact blocks a new Start but is not
> read by manual submission or an already-active run's effects. Legacy startup
> installs the writer after intent replay without requiring an account
> reconciliation.
>
> ADR 0037 already decides that missing activation means no authority and that
> legacy JSONL is deleted. Close these findings by that deletion, not by repairing
> or extending legacy code.
>
> Acceptance criteria:
>
> - Implement ADR 0037's migration gate and make a missing activation record return
>   unavailable; no `legacy_factory`, legacy sweep, or legacy evidence sink is
>   installed.
> - Remove the legacy reconciliation admission path and its reachable call sites.
> - Add selector/boot tests proving an unactivated account cannot submit or install
>   a writer, including when legacy JSONL contains a clean verdict.
> - Verify deletion makes a stale/missing-intent legacy verdict unreachable from
>   Alpaca Broker V2, including the in-flight-suppressed position case.
> - Do **not** add regression tests to `clerk/recovery.py`,
>   `clerk/reconcile.py`, or other modules scheduled for deletion.

### Filed issue 3 — [#1657](https://github.com/tim1016/learn-ai/issues/1657)

**Title:** Verify ADR 0037 deletion closes unowned legacy activity replay

**Body:**

> Charter #1644 found an activity-replay fail-open mechanism reachable only
> through the legacy JSONL Alpaca authority at `a7771477`. The durable cursor is
> the maximum recovered activity timestamp. `AlpacaActivityRecovery.record`
> appends and advances that cursor for an unowned fill but neither raises an
> unexplained-order hold nor attributes the execution. A successful bounded
> closed-order recovery can omit an old-submitted order that filled during the
> gap because that list is selected by submission time; periodic legacy
> reconciliation then emits the non-admission-authoritative `missing_intent`
> verdict. Later new exposure is allowed. This confirmation does not depend on
> the broad recovery catches already refuted by #1592.
>
> ADR 0037 already retires legacy JSONL custody. Close by deletion, not by adding
> a hold/cursor protocol to the legacy writer.
>
> Acceptance criteria:
>
> - Implement ADR 0037 so no legacy activity-recovery writer or evidence sink is
>   selected for Alpaca.
> - Verify an unactivated account installs no writer and cannot dispatch a legacy
>   activity recovery or subsequent order effect.
> - Remove the legacy activity-recovery call sites when they become dead.
> - Do **not** add a regression test against `activity_recovery.py` or otherwise
>   extend the retiring cursor protocol.

### Filed issue 4 — [#1658](https://github.com/tim1016/learn-ai/issues/1658)

**Title:** Verify ADR 0037 deletion closes the legacy same-millisecond hold-clear race

**Body:**

> Charter #1644 found a legacy-only direct-clear race at `a7771477`. For an
> unexplained-order hold, `AlpacaClerk.clear_hold` releases intake while obtaining
> a clean reconciliation, then rejects newer evidence only when
> `current_hold.since_ms > proof_observed_at_ms`. A new `UNEXPLAINED_ORDER` recorded
> after the proof but in the same integer millisecond compares equal. The method
> appends `HOLD_CLEARED` after that evidence, and last-write-wins `hold_state`
> permits later new exposure. The existing race test uses a strictly incrementing
> fake clock and does not cover equality.
>
> This path is retired by ADR 0037. Close by deletion, not by changing the
> timestamp comparison or pinning another legacy test.
>
> Acceptance criteria:
>
> - Implement ADR 0037 so the direct legacy hold-clear route and legacy Clerk are
>   unreachable for Alpaca accounts.
> - Verify through selector/router tests that no Broker V2 command can dispatch to
>   legacy `clear_hold` after retirement.
> - Do **not** add a regression test against the legacy timestamp race or otherwise
>   extend the retiring module.

### Filed issue 5 — [#1659](https://github.com/tim1016/learn-ai/issues/1659)

**Title:** Verify ADR 0037 deletion closes legacy incomplete-order-page reconciliation

**Body:**

> Charter #1644 found a legacy-only partial-result fail-open at `a7771477`.
> `AlpacaClerk._reconcile_with_proof` requests `status="all", limit=500` but does
> not reject a full page as incomplete. The client requests descending order, so
> an older still-working foreign order can be omitted behind 500 newer
> submissions. The successful partial snapshot can be labeled clean and used by
> direct hold clear to append `HOLD_CLEARED` while the foreign order remains at
> the broker. It can likewise understate an instance working-order proof. This
> confirmation does not depend on the broad recovery catches excluded from #1592.
>
> ADR 0037 retires the legacy reconciliation authority. Close by deletion, not by
> adding pagination/completeness behavior to legacy.
>
> Acceptance criteria:
>
> - Implement ADR 0037 so no legacy reconciliation sweep or direct hold-clear
>   command is selected for Alpaca.
> - Verify through selector/router tests that an unactivated account cannot obtain
>   a clean legacy proof, clear a legacy hold, or submit new exposure.
> - Remove the legacy full-page reconciliation call sites when they become dead.
> - Do **not** add a 500-order regression test to the retiring legacy Clerk.

### Filed issue 6 — [#1660](https://github.com/tim1016/learn-ai/issues/1660)

**Title:** Verify ADR 0037 deletion closes unactivated legacy developer-reset authority erasure

**Body:**

> Charter #1644 found a legacy-only recovery-tool fail-open at `a7771477`. The
> paper developer clean-slate reset intentionally omits broker-flat proof and a
> runner roll call. Its stop check returns when there is no SQLite database, and
> its durable startup reset fence is written only for an established SQLite
> generation. It can therefore quarantine an unactivated legacy JSONL authority
> and runner catalog without proving broker-flat/stopped state. On next selection,
> activation is still absent, so the selector constructs a fresh empty legacy
> Clerk and installs its sweep/evidence sink. Broker positions or working orders
> can coexist with an order-capable authority that has forgotten prior custody
> evidence.
>
> ADR 0037 makes missing activation unavailable and retires the legacy writer.
> Close by enforcing that decision, not by extending the legacy reset protocol.
>
> Acceptance criteria:
>
> - Implement ADR 0037 so missing activation always yields unavailable after a
>   developer reset; no empty legacy writer, sweep, or evidence sink is installed.
> - Remove or make unreachable developer-reset handling for unactivated legacy
>   authority artifacts as part of legacy deletion.
> - Add selector/reset boundary tests proving a legacy reset cannot restore order
>   authority without the SQLite cutover/activation ceremony.
> - Preserve the existing activated-SQLite behavior: reset remains unavailable
>   pending reactivation.
> - Do **not** add custody behavior to the retiring legacy reset path.

## Registered `docs/known-gaps.md` text

> ### Execution-path fail-open seams (verified 2026-08-18)
>
> Source: `docs/audits/execution-path-fail-open-2026-08-18.md`, read at commit
> `a7771477`. This sweep excluded #1614–#1618 and #1592's nine refuted candidates.
>
> - **Activated SQLite: first in-flight position mismatch is admission-clean.**
>   `sqlite/reconcile.py:121-184,363-381,777-825` classifies a mismatch on a
>   captured working-order symbol as indeterminate, returns `clean`, and authors no
>   blocker unless a prior POSITION_DRIFT already exists. Ordinary working bot
>   ENTRY orders are not an independent new-exposure fence.
>   [#1655](https://github.com/tim1016/learn-ai/issues/1655)
> - **Retiring legacy JSONL: incomplete reconciliation facts do not fence submit
>   admission.** `stale`/`missing_intent` project a freeze but legacy manual and bot
>   ENTER read only holds; in-flight-suppressed position drift can return clean with
>   `broker_facts_complete=False`, which protects Start but not existing effects or
>   manual submission. ADR 0037 resolves this by deletion. Do not add legacy
>   regression tests; verify retirement closes reachability.
>   [#1656](https://github.com/tim1016/learn-ai/issues/1656)
> - **Retiring legacy JSONL: unowned activity replay advances its cursor without a
>   submit fence.** An unowned fill is durably accepted without a hold, while the
>   bounded closed-order pass can omit its old-submitted order. ADR 0037 resolves
>   this by deletion. [#1657](https://github.com/tim1016/learn-ai/issues/1657)
> - **Retiring legacy JSONL: direct hold clear has a same-millisecond evidence
>   race.** `clerk.py:600-666` uses `since_ms > proof_observed_at_ms`; equal-time
>   later unexplained evidence can be followed by `HOLD_CLEARED`. ADR 0037 resolves
>   this by deletion. Do not fix or regression-test the retiring module; verify the
>   route is unreachable. [#1658](https://github.com/tim1016/learn-ai/issues/1658)
> - **Retiring legacy JSONL: reconciliation accepts a full 500-order page as
>   complete.** An older working foreign order can be omitted from the descending
>   page, allowing a false-clean proof to clear a hold while the order persists.
>   ADR 0037 resolves this by deletion; do not add legacy pagination behavior or
>   regression tests. [#1659](https://github.com/tim1016/learn-ai/issues/1659)
> - **Retiring legacy JSONL: developer reset can erase unactivated authority and
>   reinstall an empty writer.** The paper reset intentionally omits broker-flat and
>   runner-roll-call proof, checks no legacy process when SQLite is absent, and
>   records no startup reset fence without an established generation. Selection then
>   reconstructs empty legacy authority. ADR 0037 resolves this through no-authority
>   fallback and deletion. [#1660](https://github.com/tim1016/learn-ai/issues/1660)

## Confidence and limits

Confidence is **high on writer enumeration, code behavior, and admission
composition**, and **moderate on real-world frequency** of the broker propagation
window, bounded-history/order-page omissions, and same-millisecond race. This was
static primary-repository research at the pinned SHA; no paper broker was fault-injected,
no production state was inspected, and no code or tests were changed. “Stale but
clean” was not treated as a defect merely because any distributed observation can
age: it was confirmed only where the repository exposes a concrete contradictory
fact (`indeterminate_symbols`), a durable unowned activity, a provably incomplete
bounded page, an authority-erasing reset, or a later journal row that the release
guard can order incorrectly.
