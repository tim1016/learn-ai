# VCR Remediation + Deployment-Validation Deploy — Handoff (2026-06-15)

**Status as of:** 2026-06-15 ~10:55 ET (updated after #545/#546)
**Master HEAD (post-#546):** `2ce8904f` (Phase 5C cancel-confirm timeout in LiveEngine._flatten + recovery_flatten + cmd_emergency_flatten landed)
**Open PRs in flight:** #547 (chore: VCR-0002/0013 status sync) — doc-only
**Recently merged in this session:** #530 Phase 8 SIZING_SKIP, #531 Phase 12 manual, #532 VCR-0009 emergency-flatten cancel-first, #533 Phase 5D state machine, #534 Phase 5D resume guard #3, #535 Phase 5E fill classifier, #536 Phase 5E cross-restart classifier, #537 Phase 7C constant-time daemon token, #538 Phase 7B order-block, #539 Phase 5C ownership query subclass, #541 Phase 7B mid-session verdict observer, #542 Phase 3 reconnect re-validation, #543 Phase 5C activation flag, #544 Phase 8 SIZING_SKIP audit log, #545 Phase 5C cancel-confirm in LiveEngine._flatten, #546 Phase 5C cancel-confirm in recovery_flatten + cmd_emergency_flatten

**VCR status snapshot:** 15 of 19 remediated, 3 partially_remediated (VCR-0003 sizing card cutover deferred, VCR-0010 Resume guard #1 TODO, VCR-P3 routine cleanup), 1 operator-gated (VCR-0002 — `durable_submit_enabled` flag flip awaits behavioral receipt from the deployment_validation paper deploy).

---

## 1. What you said you want to do next

1. Make the IB Gateway connection and **deploy the `deployment_validation` strategy** as a sanity check that the live runner end-to-end works.
2. **Use an older known-good commit hash** for the live submission run, not whatever HEAD becomes after Phase 5D and the rest of the durable-submit cascade lands.
3. Finish merging the in-flight PRs once they go green and reviewer comments are addressed.
4. Pick up the remaining remediation in a new session — I should leave a clean plan.

The rest of this doc is the handoff for that.

---

## 2. Which commit hash to deploy from

You have three clean, green-on-CI candidates depending on how conservative you want to be. All keep `durable_submit_enabled=False` by default — the activation flag flip is a separate operator decision per VCR-0002 follow-up, NOT a side effect of deploying these hashes.

| Hash | Description | Includes |
|------|-------------|----------|
| `2ce8904f` (current master, **recommended**) | Phase 5C cancel-confirm structurally complete across all three flatten paths; verdict observer (#541) + activation flag (#543) + emergency cancel-confirm (#546). **CI green.** | Phases 1-12 + Phase 5C structural + Phase 7B verdict observer. Durable submit OFF by default. |
| `e9469910` | Phase 12 manual merged on top of Phase 8 / 9 / 10 / 11. Pre-Phase-5C / Pre-5D / Pre-7B observer. **CI green.** | Phases 1-12 except 5C/5D/5E/7B observer; durable submit code path is still single-shot. |
| `a5994e95` (Phase 7D merge) | Pre-Phase-8 — the state before the new SIZING_RESOLVED WAL emit landed. **CI green.** | Phases 1-7D only. No SIZING_RESOLVED emit, no entry-Greek delete, no Phase 12 manual, no Phase 10 doc updates. |

**Recommendation: deploy from `2ce8904f` (current master).** The Phase 5C structural work doesn't activate by default — `LiveConfig.durable_submit_enabled` defaults to False so behavior is identical to e9469910's single-shot path. Deploying current master gives you the option to flip the flag later without re-deploying, plus you get the Phase 7B mid-session verdict observer (one extra safety layer at zero cost).

If `e9469910` feels safer (the prior "deploy from here" recommendation), it's a fine fallback — you lose only the verdict observer and the cancel-confirm timeout protection (the latter only matters if the broker session loses its cancel-confirm callback, which is a corner case).

```bash
# To deploy from current master 2ce8904f:
git checkout 2ce8904f -- .

# Or from e9469910:
git checkout e9469910 -- .

# Or from a5994e95:
git checkout a5994e95 -- .
```

---

## 3. Pre-deploy checklist (paper trading only)

Run this list before the first submission. Each line corresponds to a checked gate in the live runner.

- [ ] IB Gateway / TWS is **on the paper port** (default `7497`).
- [ ] In your deploy environment: `IBKR_MODE=paper`, `IBKR_READONLY=true`. Verify with `env | grep IBKR_`.
- [ ] Paper account ID **starts with `DU`** and matches what the broker reports.
- [ ] Working tree is clean if the strategy requires it (deployment_validation uses `clean_tree_required=false` per its registry entry).
- [ ] No existing `halt.flag` in the run dir.
- [ ] Bot Control broker safety verdict reads `paper-only` (green hero band). If amber `unknown` or red `unsafe`, **stop** and resolve before deploying — Phase 7A surfaces the verdict; Phase 7B enforcement that would block orders on amber/red is still pending (deferred — see §6).

---

## 4. Deploying `deployment_validation`

### 4.1 What the strategy does

`PythonDataService/app/engine/strategy/algorithms/deployment_validation.py::DeploymentValidationConsecutiveGreen`:

- Universe: configurable; default whatever the deploy ledger names.
- Resolution: **1-minute bars** (not 15-minute).
- Detection window: **09:45 ET to 15:45 ET**.
- Entry: after **two consecutive green minute bars** (close > open), submit a long entry intended to fill at the next bar's open via Engine Lab's `next_bar_open` mode.
- Hold: through the 3rd, 4th, and 5th bars after the entry fill, then submit `Liquidate` on the 5th bar.
- Stop: at 15:45 ET, refuse new entries and liquidate any open position.

This is **not an alpha strategy** — it's a deployment-validation primitive that exercises the entry/hold/exit path end-to-end so you can confirm the runner is healthy.

### 4.2 Deploy command

The deploy key is the canonical module name (Phase 2 / VCR-0004 enforced this contract):

```jsonc
{
  "strategy_key": "deployment_validation",
  "live_config": {
    "sizing": { "kind": "FixedShares", "value": 1 }
  }
}
```

`FixedShares(1)` is the Safe canary sizing — it can buy at most 1 share per entry signal, so even if the strategy fires repeatedly the position stays tiny. **Do not** swap this for `SetHoldings` or `FixedNotional` on the first deploy; the point is to validate the runner with the smallest possible position.

Per Phase 1 (VCR-0001 closure), the deploy boundary will refuse `live_config.sizing` omission with HTTP 422, and the runner will refuse to start without an explicit policy. Both are working as designed.

### 4.3 What to watch during the live run

| Indicator | Where to look |
|-----------|---------------|
| Broker safety verdict | Bot Control hero band; should read `paper-only`. |
| Pre-flight gate | Run-dir `pre_flight_report.json` lists each gate's verdict at start. |
| Sizing audit | Bot Control Sizing card; should show 1 row per fired signal with `policy_kind=FixedShares, policy_value=1, intended_qty=1`. |
| Intent WAL | `artifacts/live_runs/<run_id>/intent_events.jsonl`. With `e9469910` you should see `[SIZING_RESOLVED, PENDING_INTENT, SUBMITTED]` per entry submit, and a matching trio per liquidate. With `a5994e95` no `SIZING_RESOLVED` rows since that's Phase 8. |
| Halt flag | Run-dir `halt.flag` should NOT appear during a clean session. If it does, read it and surface in the bot control page failure list. |
| Order ref on broker side | Every IBKR order should carry `orderRef = learn-ai/<run_id>/v1:<intent_id>` (Phase 5A). Verify with the IBKR TWS Account Window → Orders pane. |

### 4.4 Emergency stop procedures

From cleanest to most aggressive:

1. **"Flatten and pause"** (bot control panic button) — writes `desired_state=PAUSED`, then enqueues `FLATTEN_NOW`. Process stays alive; you can resume. Safe.
2. **STOP** — graceful shutdown, optional `--with-flatten`. Returns `still_running_after_2s` if the process doesn't honor SIGTERM (Phase 6B / VCR-0018-B). The runner exits.
3. **CLI emergency flatten** — `python -m app.engine.live.run emergency-flatten --account DU... --confirm`. Now (per PR #532, VCR-0009) cancels owned open orders BEFORE liquidating, so an open SELL limit can no longer race the emergency SELL market.
4. **Hard kill**: `podman exec polygon-data-service pkill -TERM -f "engine.live.run start"` — bypasses WAL ordering. The next start of the same `run_id` will go through `ColdStartReconciler.verify()` (Phase 5B) and halt if it can't classify the divergence.

---

## 5. Open PR status (carry into next session if not merged today)

| PR | Title | Status | Action |
|----|-------|--------|--------|
| #532 | VCR-0009 emergency-flatten cancel-first | **MERGED** 2026-06-15 | done |
| #533 | Phase 5D submit retry policy (VCR-0002) | In-flight; CodeRabbit + Python Lint green, other checks finishing | wait for green, merge if no review changes requested; otherwise address comments first |

If you're cautious, **don't merge #533 before the live `deployment_validation` deploy you're planning**. The Phase 5D state machine wiring touches `submit_pending_orders` — it's well-tested with 4 new tests + 7 existing tests still pass, but the live-trading code path is the single highest-stakes seam in the repo. Deploying from `e9469910` skips this code entirely.

After your deploy succeeds, merge #533 from a position of confidence.

---

## 6. What remains — picking up in a new session

### Already merged this session (Phases 1-12 + the doc/hygiene + Phase 0 re-grounding)

✓ Phase 0 re-grounding for VCR-0009, -0011, -0012, -0018 sub-items
✓ Phase 7C constant-time daemon token compare (VCR-0011) — closed
✓ Phase 11 hard-delete dead code + scratch files (VCR-0017) — closed
✓ Phase 10 architecture doc realignment (VCR-0015 / VCR-0016) — closed
✓ Phase 9 entry-Greek hard-delete + EF migration (VCR-0005) — closed
✓ Phase 8 SIZING_RESOLVED WAL emit (VCR-0003) — **partial**: SIZING_SKIP deferred + Sizing-card WAL-fold cutover deferred
✓ Phase 3 reconnect re-validation scope verdict (VCR-0006) — doc-only; gating decisions captured
✓ Phase 12 canonical operator runbook — `docs/operator-architecture-and-runbook.md`
✓ Phase 1 / 2 / 3 (start-time) / 4 / 5A / 5B / 6A-D / 7A / 7D were already on master before this session
✓ VCR-0009 cmd_emergency_flatten cancel-first (PR #532)

### In flight (PR open, waiting CI / review)

🟡 **Phase 5D — submit_state_machine wired into submit_pending_orders** — PR #533. Submit-side complete (4 new tests); Resume guard (cmd_resume side) deferred to a follow-up.

### Pending — to pick up next session (priority order)

1. **Phase 5D Resume guard** — small follow-up PR. Modify `cmd_resume` to check:
   - `broker_safety.final_verdict == "paper-only"`
   - `ColdStartReconciler.verify()` last result is `clean`
   - No unresolved `ACK_FAILED_UNCERTAIN` in WAL
   - Refuse Resume otherwise; surface the blocking guard in the bot control page.

2. **Phase 5C — ownership-query gates** (VCR-0009 deferred extras). The minimum-viable cancel-first fix shipped in PR #532; the full Phase 5C still owes:
   - `IbkrBrokerOwnershipQuery(VerifiedBrokerOwnershipQuery)` subclass — base class already exists at `app/engine/live/broker_ownership_query.py:44-60`.
   - Wire `require_durable_submit_activation` in `LiveEngine` construction.
   - `OWNERSHIP_QUERY_UNAVAILABLE_HALT` event type (new in `IntentEventType` enum).
   - `CANCEL_CONFIRM_TIMEOUT_HALT` event with `CANCEL_CONFIRM_TIMEOUT_S = 5` default.
   - `EMERGENCY_FLATTEN_WITH_UNCONFIRMED_CANCELS` audit event for the force path.

3. **Phase 5E — fill ownership classifier** (VCR-0012). Wire `classify_ownership` from `order_identity.py:173-204` (already-tested function) into `LiveEngine._convert_ibkr_fill` so cross-restart fills classify correctly. The intent ledger already builds `known_perm_ids` / `known_exec_ids`; just needs the call site replaced.

4. **Phase 7B — verdict enforcement** (VCR-0010 / VCR-0018-A/C/D/E). Phase 7A surfaces the verdict; 7B turns it into enforcement:
   - Engine refuses to submit orders when `final_verdict == "unsafe"`.
   - Engine refuses to start when `final_verdict == "unknown"` outside the named diagnostic path.
   - Mid-session transition `paper-only → not-paper-only` writes `halt.flag` + durable PAUSED + `BROKER_SAFETY_VERDICT_TRANSITION_HALT` WAL event (new event type).
   - Resume guarded by same verdict check (composes with Phase 5D Resume).

5. **Phase 3 reconnect re-validation** (VCR-0006 follow-up). Three architectural gating decisions captured in the finding's `follow_up_required`:
   - Where do broker-lifecycle WAL events live? (intent_wal vs new broker_wal vs sidecar)
   - Halt-trigger taxonomy — extend `PoisonedHaltTrigger` or rename to a `BROKER_LIFECYCLE_HALT` family.
   - Reconnect-observer API — client event hook vs bar-loop polling.

   Decide all three before opening a PR.

6. **VCR-0018 mechanical tail** (A/C/D/E/J/K) — Frontend-only PR per the file:line evidence in the VCR-0018 finding's Phase 0 re-grounding section. Sentinel pill wired to Phase 7A verdict (A), readiness-gate labels expanded (C), deploy-form dialog wording (D), Start card label rename (E), Sizing card NY timestamp formatter (J), failures-table `ts_ms` instead of `raw_ts` (K).

7. **Phase 8 follow-ups** — SIZING_SKIP needs an `IntentEvent` model relaxation (intent_id may be empty for SIZING_SKIP) that ripples through `intent_ledger.py` fold and `ColdStartReconciler`. Phase-0-style review territory. Sizing-card data-source cutover from in-memory `sizing_resolutions` list to a WAL fold is the prerequisite for retiring the in-memory list.

---

## 7. Where the rest of the docs are

- **Remediation PRD** — `docs/audits/vibe-coded-app-remediation-prd.md` (the single source of truth for what Phase N means).
- **Per-finding docs** — `docs/audits/vibe-coded-app-research/findings/VCR-*.md`. Each updated with Phase 0 re-grounding evidence + remediation status. Use `grep -l 'status: open' docs/audits/vibe-coded-app-research/findings/` to see what's still open at a glance.
- **Operator runbook** — `docs/operator-architecture-and-runbook.md` (Phase 12 / this session).
- **Architecture docs** — `docs/architecture/numerical-authority-migration-plan.md` (Phase 5 ADR 0009 section added this session), `docs/architecture/engine-authority-map.md`, `docs/architecture/lean-sidecar-lab.md` (status headers updated this session).

---

## 8. If something goes wrong during the live deploy

| Symptom | Likely cause | First thing to try |
|---------|--------------|---------------------|
| Deploy refused with `live_config.sizing is required` (HTTP 422) | Phase 1 / VCR-0001 closure working as designed | Add `"sizing": { "kind": "FixedShares", "value": 1 }` to the deploy payload |
| Runner refuses to start: "account identity mismatch" | Phase 3 start-time gate fired | Confirm `ledger.account_id` matches IBKR `connected_account`. **Redeploy** — do not hand-edit the ledger; the field is hashed into `run_id`. |
| `Strategy key not found` at start | Phase 2 / VCR-0004 registry mismatch | Confirm `strategy_key` is exactly `deployment_validation` (the module name). |
| Bot Control hero amber `Broker safety unknown` | Phase 7A verdict gate fired pre-start | One of the four gates not positively verifiable: `IBKR_MODE`, `IBKR_READONLY`, port not in `PAPER_PORTS`, account not starting with `DU`. Resolve and redeploy. |
| `halt.flag` from prior run | Phase 6D pre-flight rerun | Inspect halt cause. **Do not** silently `rm halt.flag` for a SUBMIT_UNCERTAIN / cold-start divergence cause. |
| Emergency Flatten produced an over-sell | Pre-#532 master would have had this | Confirm you're on `e9469910` or later; PR #532 fix is in. If you're on `a5994e95`, the cancel-first fix is NOT there — be extra cautious. |

---

## 9. One-pager TL;DR for the new session

- Deploy `deployment_validation` from commit **`e9469910`**, `sizing = FixedShares(1)`, paper account.
- PR #533 (Phase 5D) is in flight — wait for green CI and address any reviewer comments before merging.
- Then pick up Phase 5D Resume guard → Phase 5E → Phase 5C → Phase 7B → Phase 3 reconnect → VCR-0018 Frontend tail → Phase 8 follow-ups, in priority order from §6.
- All evidence is in the finding files; PRD §N answers "what does Phase N mean" with acceptance criteria.

Good luck with the paper run.
