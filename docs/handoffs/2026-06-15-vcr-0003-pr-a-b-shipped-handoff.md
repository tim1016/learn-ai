# VCR-0003 PR A + PR B shipped — close-out handoff (2026-06-15)

**Master HEAD at session close:** `8ed5b889` (unchanged; PRs not yet merged).

**Prior session handoff:** `docs/handoffs/2026-06-15-vcr-residual-gaps-handoff.md` — the residual-gaps runbook this session executed against (Gap 2).

**What this session shipped:**

| PR | Branch | Status |
|---|---|---|
| **#552** | `feat/vcr-0003-sizing-wal-fold-helper` | Open, 2 commits (`f76fe753` + fix-up `6474cbfc`), CI green, no human review, CodeRabbit re-pending |
| **#553** | `feat/vcr-0003-sizing-wal-fold-wire` | Open, stacked on PR A's pre-fixup head |

PR A adds `_fold_wal_sizing_audit(run_dir)` and the optional `IntentEvent.symbol` field. PR B wires the helper into `_sizing_audit_rows` with the sidecar projection as the legacy-run fallback.

This handoff covers everything left to close VCR-0003 plus the residual gaps from the prior handoff that this session did NOT touch.

---

## Section 1 — Merge sequence for PR #552 and #553

**Why the ordering matters:** PR B's base branch is PR A's *pre-fixup* commit (`f76fe753`). After PR A merges into master, PR B will need a one-click "Update branch" rebase before it can merge cleanly.

**Runbook:**

1. **Review PR #552** (https://github.com/tim1016/learn-ai/pull/552).
   - CodeRabbit's auto-review should land within minutes of session close (its prior attempt was rate-limited; the fix-up push retriggered it).
   - The fix-up commit's body lists the 5 hardening fixes with line references.
   - All 13 CI checks were green pre-fixup; re-running after `6474cbfc`.

2. **Merge PR #552 first** (squash or merge — either works; the repo convention based on recent commits is squash-merge into master with `(#NNN)` suffix).

3. **On PR #553, click "Update branch"** to rebase it onto the new master. GitHub will offer this automatically since PR A's commits now appear in PR B's base.

4. **Review PR #553** — only ~23 lines of wiring + 5 wiring tests.

5. **Merge PR #553**.

6. Proceed to Section 2 (smoke test) → Section 3 (status flip).

---

## Section 2 — Frontend smoke test (operator-owned, gates VCR-0003 status flip)

**This is the AFK-undoable step.** The handoff from the prior session named it as the close-out gate; it remains so.

**Runbook:**

1. **Deploy `deployment_validation` from current master post-merge.** Use the procedure in `docs/handoffs/2026-06-15-vcr-remediation-deploy-validation-handoff.md` §3–§5. Default `durable_submit_enabled=False` (Gap 1 is still operator-gated).

2. **Place a couple of orders and a couple of skips:**
   - The strategy's bar handler should call `set_holdings(symbol, target_fraction)` twice with `target_fraction > 0` against a flat account (produces SIZING_RESOLVED + a real submit).
   - Then call `set_holdings(symbol, current_target_fraction)` so `target_quantity == current_qty` (produces SIZING_SKIP with `reason="target_equals_current"`).
   - Alternatively, force `target_fraction = 0` against a flat account (`reason="zero_shares_while_flat"`).

3. **Mid-run sanity check:** Open the Sizing card for the instance. Confirm the per-trade audit table renders:
   - Both resolve rows AND skip rows (the in-memory `sizing_resolutions` projection — this is what the legacy sidecar path returns).
   - Timestamps, `symbol`, `policy_kind`, `policy_value`, `intended_qty`, `reference_price`, `sized_via` all populated.

4. **Restart the engine** (`live stop <sid>` then `live start <sid>`).

5. **Post-restart check:** Re-open the Sizing card. After PR B's merge, the endpoint now reads from `_fold_wal_sizing_audit` first. Confirm:
   - The same resolves + skips render as before the restart (this is the durability win — pre-PR-B, the in-memory list would have been lost on restart).
   - Symbols are populated on every row (this is the `IntentEvent.symbol` win).
   - Timestamps match what was observed pre-restart.

6. **Known surface-level inconsistencies the operator may observe** (deferred to follow-ups per `docs/audits/.../code-review-findings.md` or the JSON output captured in the session transcript; full list in Section 4 below):
   - `sized_via` on a policy-path skip may flip from `policy_set_holdings` (mid-run, in-memory) to `policy_set_holdings_skip` (post-restart, fold). Same domain event.
   - `skipped: true` and `skip_reason` are dropped at the wire boundary by `SizingAuditRow.model_validate` (Pydantic `extra="ignore"` default). The only hint of a skip in the rendered card is the `_skip` suffix on `sized_via`.

   Document both observations as expected-for-now; they're listed below for the next session to fix.

---

## Section 3 — Flip VCR-0003 status from `partially_remediated` to `remediated`

Only after Section 2 passes.

**Files to edit:**
- `docs/audits/vibe-coded-app-research/findings/VCR-0003-*.md` — change `status:` field at the top, append a `remediation_history:` entry naming PRs #552 + #553 and the smoke-test date.

**Quick check:**
```bash
grep -E '^status:' docs/audits/vibe-coded-app-research/findings/VCR-*.md | sort
```
Should show 17/19 `remediated` (was 16/19; VCR-0003 flips), 1 `phase_5c_structural_complete_operator_gated` (VCR-0002), 1 `partially_remediated` (VCR-P3-rollup).

---

## Section 4 — Outstanding code-review findings (deferred from the max-effort review of PR A)

The session ran a 9-angle × 1-vote-verify × sweep review on PR #552 and applied the 5 highest-confidence fixes (#1, #2, #3, #6, #13 in the JSON output). **10 findings remain open.** None are crashes; all are tracked here for the next session.

Severity-ordered:

### F-5 — `SizingResolution` dataclass missing `symbol` field
- **Where:** `PythonDataService/app/engine/live/intent_ledger.py:36-49` (dataclass) and `:122-154` (fold's SIZING_RESOLVED branch).
- **Symptom:** PR A added `symbol` to `IntentEvent` but the canonical fold's `SizingResolution` doesn't read it. Two divergent projections of the same WAL stream.
- **Fix:** add `symbol: str = ""` to `SizingResolution`, read it in the fold branch. Update the existing `test_intent_ledger.py::test_sizing_resolved_*` tests.
- **Effort:** ~5 lines + 1 test.

### F-7 — `SizingAuditRow` silently drops `skipped`/`skip_reason`
- **Where:** `PythonDataService/app/schemas/live_runs.py:745-754`.
- **Symptom:** Pydantic v2 default `extra="ignore"` strips the two keys at `SizingAuditRow.model_validate(row)` in `_sizing` (`live_instances.py:~598`). Pre-existing on master; surfaced by PR A's helper emitting the keys.
- **Fix:** widen `SizingAuditRow` with `skipped: bool = False, skip_reason: str = ""`. Update the Frontend TypeScript interface at `Frontend/src/app/api/live-instances.types.ts` (search for `SizingAuditRow`).
- **Effort:** ~10 lines backend + frontend.

### F-6 — Rollback fragility under `extra="forbid"`
- **Where:** `PythonDataService/app/engine/live/intent_events.py:67`.
- **Symptom:** `IntentEvent.model_dump_json()` now emits `"symbol": null` on every event type. An older binary's `read_tail` raises `ValidationError` → `IntentWalCorruptError` → Poisoned cold-start on every line written by the newer engine.
- **Fix:** either (a) relax to `extra="ignore"` (loses validation strictness), or (b) document a "schema fence" in the deploy procedure — never roll back across this commit while an instance has live state.
- **Effort:** ~1 line + a doc note. Operationally important if blue/green is in play.

### F-8 — Sidecar fallback shows PRIOR-run audit rows
- **Where:** `PythonDataService/app/routers/live_instances.py:_sizing_audit_rows` (PR B's wiring path).
- **Symptom:** `live_state.json` is keyed by INSTANCE, not by run. When the latest run's WAL+skip log are both empty (e.g., Run B just started), the fallback returns Run A's audit rows labeled as Run B's.
- **Fix:** stamp `run_id` into the response shape, OR gate the fallback on "run dir is older than this engine's start time", OR scope `live_state.json` per-run.
- **Effort:** ~20 lines depending on approach; coordinate with the bot control page UI.

### F-9 — Sidecar fallback suppressed by skip-only WAL fold
- **Where:** same as F-8.
- **Symptom:** when WAL is corrupt but skip log has rows, `wal_rows` is truthy and the sidecar fallback is skipped. Operator loses ALL historical SIZING_RESOLVED context the sidecar still has.
- **Fix:** track whether the WAL was readable (separate flag) and trigger fallback when `wal_was_readable=False` regardless of skip-log content. Add a `logger.warning` when the WAL fold catches `IntentWalCorruptError` — silent fail-open hides a Poisoned-class canary.
- **Effort:** ~10 lines + 1 test.

### F-10 — `sized_via` divergence across a restart
- **Where:** `PythonDataService/app/engine/live/live_portfolio.py:576/996` vs `PythonDataService/app/routers/live_instances.py:~536`.
- **Symptom:** In-memory policy-path skip writes `sized_via="policy_set_holdings"`. Fold reads the same skip from `sizing_skip.jsonl` and emits `sized_via="policy_set_holdings_skip"`. Same domain event, different label, observable across restart.
- **Fix:** decide canonical value (probably `policy_set_holdings_skip` for any skip, or split into a `sized_via` + `skipped` pair). Update writer + fold to agree.
- **Effort:** ~5 lines; sequence with F-7 since the wire shape changes.

### F-11 — Stable-sort tie-break inversion at equal `ts_ms`
- **Where:** `PythonDataService/app/routers/live_instances.py:~530` (fold) vs `~457` (sidecar projection).
- **Symptom:** Two events sharing `ts_ms` (same bar): fold puts WAL row first; sidecar's `rows.reverse()` puts the later-appended row first. Observable across restart.
- **Fix:** define and document tie-break. Likely "later-appended wins" matches operator intuition. Use a stable composite sort key in the fold.
- **Effort:** ~3 lines + 1 test.

### F-12 — Disjointness invariant lives only in docstring
- **Where:** `PythonDataService/app/routers/live_instances.py:484` (docstring claim) + `live_portfolio.py:635-639` (the comment that flags the deferred relaxation).
- **Symptom:** Fold has no intent_id dedup. If a future PR relaxes `IntentEvent.intent_id: Field(min_length=1)` to allow SIZING_SKIP on the WAL, the fold double-counts skipped trades.
- **Fix:** two-line `seen_intent_ids: set[str]` guard on the WAL slice (skip rows have no intent_id so they can't self-collide).
- **Effort:** ~2 lines + 1 test.

### F-14 — Duplicate fold (router vs `intent_ledger.fold`)
- **Where:** `PythonDataService/app/routers/live_instances.py:_fold_wal_sizing_audit` reimplements the SIZING_RESOLVED projection that `intent_ledger.fold` already does.
- **Symptom:** Future churn — every new SIZING_RESOLVED field needs hand-syncing across both folds. F-5 is one such drift already.
- **Fix:** move the fold into `intent_ledger.py` as `sizing_audit_view(LedgerView) -> list[dict]` or similar. Router calls it. Sequence with F-5 (add symbol to SizingResolution first).
- **Effort:** ~40 lines (refactor) — defer until a second consumer (replay, CLI) is needed.

### F-15 — Unbounded WAL read on every Sizing-card poll (perf)
- **Where:** `PythonDataService/app/routers/live_instances.py:~482` calls `IntentWal(wal_path).read_tail()` per poll.
- **Symptom:** 30k events × Pydantic v2 validation × poll cadence = visible UI latency on long-running instances.
- **Fix:** (a) cursor cache keyed on `(wal_mtime, wal_size)`; (b) early `event_type` substring check before Pydantic; (c) `_latest_run_dir_for_instance` also does a full `live_runs.iterdir()` per poll — cache that too.
- **Effort:** ~30 lines for a cursor cache. Defer until smoke test produces real latency complaints.

---

## Section 5 — Gap 1 (VCR-0002 operator activation) — unchanged

**Still operator-gated. Nothing this session touched VCR-0002.**

Refer to the prior handoff: `docs/handoffs/2026-06-15-vcr-residual-gaps-handoff.md` § Gap 1. The runbook there is current. Summary:

- All structural prerequisites shipped (PRs #497, #533, #535, #536, #539, #543, #545, #546, #548).
- The activation flip needs Acceptance Gate #2 — empirical observation that the IBKR Gateway returns prior-run open orders and executions carrying `orderRef` across reconnect.
- That observation requires deploying paper, placing an order, stopping the engine, reading `IB.openTrades()` + `IB.fills()` back from the Gateway, then starting a fresh session and watching `cold_start_reconciler` recognize the prior order via `orderRef` lookup.
- Only after the observation passes does the operator flip `LiveConfig.durable_submit_enabled = True`.

The optional Section 2 smoke test for VCR-0003 is a good moment to also do the VCR-0002 observation — they share the deploy step.

---

## Section 6 — Gap 3 (VCR-P3-rollup polish items) — unchanged

**Still tracked "fix on touch". Nothing this session touched these.**

Refer to the prior handoff `docs/handoffs/2026-06-15-vcr-residual-gaps-handoff.md` § Gap 3 for the full list (P3-D, P3-G, P3-H, P3-I, P3-J, P3-K, P3-L, P3-M, P3-N). Suggested grouping for one PR per the prior handoff: P3-D + P3-J + P3-K (all timestamp/timezone rigor, single review pass).

---

## Quick-start commands for the next session

```bash
# Pull master to current state:
git fetch origin master && git checkout master && git pull

# Verify the merge status of PRs #552 and #553:
gh pr list --state open --limit 10

# Verify the current VCR status snapshot:
grep -E '^status:' docs/audits/vibe-coded-app-research/findings/VCR-*.md | sort

# Re-run the PR A fold tests:
podman exec polygon-data-service python -m pytest /app/tests/routers/test_live_instances_sizing_audit.py -v

# Re-run the PR B wiring tests:
podman exec polygon-data-service python -m pytest /app/tests/routers/test_live_instances_sizing_audit_wiring.py -v

# Project-scope lint baseline:
podman exec polygon-data-service ruff check /app/app/
# Expect: "Found 4 errors." — all pre-existing E741 in engine_runner.py; not regressions.
```

---

## Decision points the operator owns

1. **Schema-fence operational policy for the `IntentEvent.symbol` addition.** Pick (a) accept rollback fragility and document a no-rollback window, or (b) relax `extra="forbid"` to `extra="ignore"`. F-6 above.
2. **Whether to ship F-7 + F-10 together** in one PR (they touch the same wire shape — `SizingAuditRow` + `sized_via` convention).
3. **Whether to do the F-14 architectural refactor** (move the fold into `intent_ledger.py`) before a second consumer exists, or defer until needed.

---

## Where to read for context

- **This session's PRs:** #552 (https://github.com/tim1016/learn-ai/pull/552), #553 (https://github.com/tim1016/learn-ai/pull/553).
- **Prior handoff (residual gaps):** `docs/handoffs/2026-06-15-vcr-residual-gaps-handoff.md` — Gaps 1, 2, 3 runbook. This session executed Gap 2.
- **Deploy-validation handoff (for the smoke-test deploy):** `docs/handoffs/2026-06-15-vcr-remediation-deploy-validation-handoff.md` §3-§7.
- **ADR 0009 (live sizing authority):** `docs/architecture/adrs/0009-live-sizing-authority-and-provenance.md`.
- **ADR 0008 (durable submit protocol):** `docs/architecture/adrs/0008-durable-submit-protocol-order-identity-recovery.md` — F-1's "seq is the fold cursor" rule comes from §3, §5.
- **VCR finding files:** `docs/audits/vibe-coded-app-research/findings/VCR-*.md`.

---

*Session close: 2 PRs opened (#552 + #553) with 1 fix-up commit on PR A from a max-effort code review. 32 new tests across PR A + PR B + fix-up regression coverage. VCR-0003 backend work complete pending merge + smoke test. Master at `8ed5b889` (unchanged — neither PR merged this session). 10 deferred code-review findings tracked in Section 4.*
