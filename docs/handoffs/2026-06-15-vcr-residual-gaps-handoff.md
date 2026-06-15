# VCR residual-gaps handoff (2026-06-15, post-session)

**Master HEAD at session close:** `8ed5b889` (PR #550 — VCR-0003 follow-up reframed)

**Session shipped 20 PRs** (#530–#550). Final VCR status snapshot:

| Status | Count |
|---|---|
| `remediated` | 16 of 19 |
| `phase_5c_structural_complete_operator_gated` | 1 (VCR-0002) |
| `partially_remediated` | 2 (VCR-0003, VCR-P3-rollup) |

This handoff is the close-out runbook for the three residual gaps. Each section is self-contained: what to do, exactly where the code lives, the test plan, and the known risks.

---

## Gap 1 — VCR-0002 operator activation: flip `durable_submit_enabled=True`

**What's already shipped (structural prerequisites complete):**

| PR | What landed |
|---|---|
| #497 (Phase 5A) | intent_id / order_ref minted; PENDING_INTENT/SUBMITTED/ACK_FAILED_UNCERTAIN WAL events |
| `aae1cf2c` (Phase 5B) | ColdStartReconciler gate; require IntentWal + order_ref for real IBKR submits |
| #533 (Phase 5D) | submit_state_machine wired into `submit_pending_orders`; RETRY_CAP=1; NOT_PROVABLE→HALT |
| #535 (Phase 5D resume guard) | `cmd_resume` refuses on unresolved ACK_FAILED_UNCERTAIN, `--force` override |
| #536 (Phase 5E) | `_convert_ibkr_fill` cross-restart classifier via folded intent WAL keyed by perm_id |
| #539 (Phase 5C ownership query) | `IbkrBrokerOwnershipQuery` subclass; passes `require_durable_submit_activation` structural gate |
| #543 (Phase 5C activation flag) | `LiveConfig.durable_submit_enabled` wired into `LiveEngine.__init__` |
| #545 (Phase 5C cancel-confirm in `_flatten`) | `CANCEL_CONFIRM_TIMEOUT_S=5.0`, halt.flag + CancelConfirmTimeoutHaltError on timeout |
| #546 (Phase 5C cancel-confirm in `_recovery_flatten` + `cmd_emergency_flatten`) | recovery halts on timeout; emergency proceeds + writes `EMERGENCY_FLATTEN_WITH_UNCONFIRMED_CANCELS` audit row |
| #548 (Phase 7B Resume guard #1) | `cmd_resume` reads `verdict_snapshot.json`; refuses non-paper-only without `--force` |

**What's left (operator-controlled, NOT AFK-doable):**

The activation flip is gated by Acceptance Gate #2 — a *behavioral receipt* from a paper deployment proving the IBKR Gateway returns prior-run open orders and executions carrying `orderRef` across reconnect. Until that's observed empirically against the real Gateway, flipping the flag in production is unsafe.

**Runbook to close the gap:**

1. **Deploy `deployment_validation` from current master** (`8ed5b889`) per the existing handoff `docs/handoffs/2026-06-15-vcr-remediation-deploy-validation-handoff.md` §3-§5. The deploy uses `durable_submit_enabled=False` by default, so behaviour is identical to the pre-existing single-shot path.

2. **Observe the IBKR Gateway behaviour across a deliberate restart:**
   - Place a small paper order via the bot.
   - Verify `intent_events.jsonl` carries `SUBMITTED` with the `order_ref = "<namespace>:<intent_id>"` token.
   - Stop the engine (`live stop <instance>`).
   - Read the order back from the Gateway: open `IB.openTrades()` + `IB.fills()` and confirm the `orderRef` field on each trade/execution matches the WAL.
   - Start a fresh engine session (`live start <instance>`). The cold-start reconciler should recognize the prior order via `orderRef` lookup, NOT via the heuristic name-based fallback.
   - Confirm `live_state.json` carries the recovered order with `recovered_via_order_ref=true` (or whatever the reconciler stamps — check `_convert_ibkr_fill` and `cold_start_reconciler.py`).

3. **Flip the activation flag** by passing `durable_submit_enabled=True` in the ledger's `live_config` at deploy time. Mechanically this is a field on `LiveConfig` (see `PythonDataService/app/engine/live/config.py`). The deploy form already has a `live_config` editor — add the field there if the operator doesn't want to hand-edit the JSON.

4. **Verify the activation gate fires:**
   - On the next start, `LiveEngine.__init__` calls `require_durable_submit_activation(enabled=True, ownership_query=IbkrBrokerOwnershipQuery(client), verified_order_ref_cap=VERIFIED_ORDER_REF_CAP)`.
   - If the IBKR client is not the real `IbkrClient`, the subclass-allowlist gate refuses (`DurableSubmitNotActivatable("ownership query unverified")`). This is the safety net.
   - If activation succeeds, the engine logs `[STEP 5C] durable submit activation: enabled, ownership_query=IbkrBrokerOwnershipQuery, cap=...`.

5. **OWNERSHIP_QUERY_UNAVAILABLE_HALT** is the post-activation halt taxonomy entry — fires when activation is on AND a managed flatten path cannot consult the ownership query. Today the code raises through the existing exception path; the explicit halt event is deferred until the flag is on, since the taxonomy doesn't fire without the gated query active. **Code location to extend:** `PythonDataService/app/engine/live/live_engine.py::_flatten` (around lines 1542-1567 where the cancel-confirm timeout is) and `_recovery_flatten` in `run.py`. The pattern is identical to the cancel-confirm path: try the ownership query; on failure write halt.flag + raise.

**Rollback path:** flip `durable_submit_enabled=False` in the ledger. The engine then ignores the ownership query entirely. No data migration required — the WAL events are forward-compatible.

**Risk:** if step 2's empirical observation FAILS (Gateway loses `orderRef` across reconnect — possible if IBKR contractually doesn't promise it), the entire Phase 5C/5D/5E cascade's safety story collapses. Document the observation in `docs/audits/vibe-coded-app-research/findings/VCR-0002-...md` regardless of outcome.

---

## Gap 2 — VCR-0003 Sizing-card data-source cutover

**Current state:** SIZING_RESOLVED events ARE being written to `intent_events.jsonl` (per `live_portfolio.py:621`, shipped in #530). SIZING_SKIP rows ARE being written to `sizing_skip.jsonl` (per `live_portfolio.py:1002`, shipped in #544). The Sizing card endpoint at `PythonDataService/app/routers/live_instances.py::_sizing_audit_rows` still reads from the in-memory `envelope.sizing_resolutions` sidecar list — the durable WAL is the source of truth, but the UI reads the projection.

**Work split into two atomic PRs:**

### PR A — backend fold helper (additive, no behaviour change)

1. **Add `_fold_wal_sizing_audit(run_dir: Path) -> list[dict]`** in `PythonDataService/app/routers/live_instances.py` (or a new helper module). Behaviour:
   - Open `<run_dir>/intent_events.jsonl` via `IntentWal(...).read_tail()`.
   - Filter for `event.event_type is IntentEventType.SIZING_RESOLVED`.
   - Map each event to a dict matching the `SizingAuditRow` schema (`PythonDataService/app/schemas/live_runs.py:745`):
     ```python
     {
       "ts_ms": event.ts_ms,
       "symbol": event.payload.get("symbol", ""),  # check actual shape
       "policy_kind": event.policy_kind,
       "policy_value": event.policy_value,
       "intended_qty": event.intended_qty,
       "reference_price": event.reference_price,
       "sized_via": event.sized_via,
     }
     ```
   - Open `<run_dir>/sizing_skip.jsonl` (one JSON per line). Map each row to the same shape, with `sized_via="policy_set_holdings_skip"` (or whatever convention the existing in-memory list uses — verify by reading `_append_sizing_skip` in `live_portfolio.py:960-1006`).
   - Sort the merged list by `ts_ms`, reverse for newest-first, return the most recent 50.
2. **Verify the canonical row shape** by reading the existing `live_portfolio.py:568-578` append and the `_append_sizing_skip_line` writer (`live_portfolio.py:81`). The fold MUST produce identical fields so the frontend doesn't care which source the endpoint used.
3. **Tests** (`PythonDataService/tests/routers/test_live_instances_sizing_audit.py` — likely already exists, extend):
   - Seed a `tmp_path` run dir with one SIZING_RESOLVED + one sizing_skip line. Assert the fold returns both rows in the merged shape.
   - Edge case: empty WAL + empty skip log → returns `[]`.
   - Edge case: WAL with non-SIZING_RESOLVED events mixed in → only SIZING_RESOLVED rows are returned.
   - Edge case: corrupt WAL → caller's choice (probably fail-open like the existing sidecar reader at `live_instances.py:446-451`).

### PR B — wire the fold into the endpoint + frontend smoke test

1. **Modify `_sizing_audit_rows(strategy_instance_id)`** to:
   - Find the latest `<run_dir>` for the instance via `_latest_run_dir_for_instance` (already exists in `run.py:1769`; either import or reimplement).
   - Call `_fold_wal_sizing_audit(run_dir)`. If non-empty, return that.
   - Fall back to the existing sidecar projection ONLY if the WAL fold is empty (preserves backward-compat for legacy runs).
2. **Frontend smoke test** (`Frontend/src/app/components/broker/sizing-card/**`):
   - Verify the Sizing card still renders correctly. The shape is unchanged, so this should be a no-op.
   - Manual check: deploy a paper run, place a couple of orders + a couple of skips, confirm the per-trade audit table renders both with correct timestamps, policy_kind, intended_qty.
3. **Update VCR-0003 status** from `partially_remediated` to `remediated` once both PRs land and the smoke test passes.

**Risk:** the WAL fold can drift from the sidecar projection if the schemas diverge silently. Mitigation: add a contract test that constructs the same SIZING_RESOLVED event two ways (append to WAL + append to in-memory list) and asserts the two row shapes match byte-for-byte.

**Optional follow-up:** once WAL is authoritative, the in-memory `sizing_resolutions` list on `LivePortfolio` becomes dead code. Remove in a separate cleanup PR. Don't bundle with the cutover.

---

## Gap 3 — VCR-P3-rollup polish items

These are explicitly low-impact and tracked "fix on touch". None gates trading safety. Listed here so a future session can knock them out in batches.

### P3-D — ADR 0009 References-section line numbers drifted
- **Fix:** open `docs/architecture/adrs/0009-live-sizing-authority-and-provenance.md`. The "References" section pins file:line ranges that have shifted by 4-30 lines since the PR1-7 catch-up. Re-anchor on the next ADR 0009 touch.
- **Alternative:** add a one-line note at the top of the References section: *"Line numbers reflect the state at ADR commit time; current code preserves the cited semantics."*

### P3-G — Shadow invariant not asserted at runtime
- **Where:** `PythonDataService/app/engine/live/no_submit_broker_adapter.py:73` defines `NoSubmitBrokerAdapter`. The shadow mode is a parameter on `ColdStartReconciler` (`cold_start_reconciler.py:58`), not a top-level env var.
- **Fix:** Add a startup assertion in `run.py` (around `cmd_start`): if the engine is constructed with `NoSubmitBrokerAdapter`, log `[STEP 0] shadow mode confirmed (no real broker submissions)`. If the cold-start reconciler is invoked with `shadow_mode=True` but the broker is NOT `NoSubmitBrokerAdapter`, raise.
- **Test:** add a smoke test that drives `set_holdings` through the shadow path and asserts no `IB.placeOrder` mock is invoked.

### P3-H — `_order_belongs_to_account` defense-in-depth weak for multi-client gateways
- **Where:** search `_order_belongs_to_account` in `PythonDataService/app/engine/live/` and `PythonDataService/app/broker/ibkr/`.
- **Fix:** tighten the check from `account_id` to `account_id AND client_id`. Update the existing tests to seed both IDs.
- **Risk:** if the broker adapter doesn't currently propagate `client_id` on the order metadata, this requires a wider plumbing change. Verify the field is available before assuming it's a small fix.

### P3-I — Chart-snapshot endpoint UTC vs America/New_York day boundaries
- **Where:** find the chart-snapshot router (`grep -rn "chart-snapshot\|chart_snapshot" PythonDataService/app/routers/`).
- **Fix:** convert the day boundary to `America/New_York` before computing the window. Use `zoneinfo.ZoneInfo("America/New_York")`. See the canonical timestamp policy in `.claude/rules/numerical-rigor.md` → "Timestamp rigor".

### P3-J — Sizing card per-trade audit `ts_ms` rendered without timezone
- **Where:** `Frontend/src/app/components/broker/sizing-card/**` — find the `DatePipe` usage on `ts_ms`.
- **Fix:** replace `{{ row.ts_ms | date:... }}` with `{{ fmtTimestampNy(row.ts_ms) }}` (existing helper, used in other broker UI surfaces).
- **Test:** Vitest snapshot — render the card with a row carrying `ts_ms = <some known instant>` and assert the rendered text contains the expected `MMM d, h:mm a ET` format.

### P3-K — `FailureRecord`/`FailureRow.ts_ms` misleadingly named
- **Issue:** the field is named `ts_ms` (implying int64 ms UTC) but is actually a host-local-TZ string parsed *as if* UTC at ingestion.
- **Fix:** either rename to `ts_local` and convert at ingestion to a true `ts_ms_utc`, OR convert at the source so the name matches. Per `.claude/rules/numerical-rigor.md`, **prefer the canonical `int64 ms UTC`** form — convert at ingestion.
- **Where:** search `FailureRecord` and `FailureRow` in `PythonDataService/app/` and `Frontend/src/app/`.
- **Risk:** wire-shape rename. Coordinate frontend + backend in one PR.

### P3-L — `executions.parquet ts_ms` uses wall-clock observation time, not broker `exec_time_ms`
- **Where:** the executions writer (`PythonDataService/app/engine/live/artifacts.py`? — verify).
- **Fix:** persist BOTH `received_at_ms` (current `ts_ms`) and `exec_time_ms` (from the broker fill event). Update reconciliation joins to use `exec_time_ms`. Don't drop the receipt time — it's useful for latency analysis.
- **Risk:** parquet schema change. Old runs' parquet files won't have the new column. Document the migration in `docs/references/reconciliations/` and add a column-existence guard in the readers.

### P3-M — `HostRunnerDeployRequest.live_config` open dict
- **Issue:** operator can inject unknown keys via `live_config` that hash into `run_id`, but the start-gate refuses with "unknown live_config keys". The deploy succeeds and writes a ledger; the runner exits 2. The operator has to re-deploy under a fresh `run_id`.
- **Fix:** either tighten `_validate_sizing` to symmetrically reject unknown sibling keys at the deploy boundary, or surface the known_fields allow-list in the deploy schema's docstring so operators see the contract.
- **Where:** `PythonDataService/app/schemas/host_runner.py` (verify), `PythonDataService/app/engine/live/config.py::_live_config_from_ledger`.

### P3-N — Stub components without tracker link
- **Where:** grep for `// TODO` and `# TODO` without an issue link. The rollup mentions these are stubs that should at least carry a tracker reference.
- **Fix:** add a tracker link or convert each stub to a real implementation. Bookkeeping only.

### P3-O — Dead-code rollup (rolled into VCR-0017)
- Already remediated as VCR-0017. Nothing to do here.

---

## Quick-start commands for the next session

```bash
# Pull master to current state:
git fetch origin master && git checkout master && git pull

# Verify the current VCR status snapshot:
grep -E '^status:' docs/audits/vibe-coded-app-research/findings/VCR-*.md | sort

# Run the full test suite to baseline (Python only — Frontend independent):
podman exec polygon-data-service python -m pytest /app/tests/engine/live/

# Verify the activation gate test for Gap 1:
podman exec polygon-data-service python -m pytest /app/tests/engine/live/test_live_engine_durable_submit_activation.py -v

# Verify the Resume guard #1 test for Gap 1:
podman exec polygon-data-service python -m pytest /app/tests/engine/live/test_run_cmd_resume_verdict_guard.py -v

# Verify the order-surface guard test for VCR-P3-F (also in Gap 1's cascade):
podman exec polygon-data-service python -m pytest /app/tests/engine/live/test_live_portfolio.py -k "surface_mismatch or p3_f" -v
```

## Decision points the operator owns

These are decisions only the user can make — none of them are AFK-doable:

1. **When to flip `durable_submit_enabled=True`.** Decision input: did step 2 of Gap 1's runbook succeed? If yes, flip. If no, document the failure mode and keep the flag off.
2. **Whether the Sizing card cutover (Gap 2) is worth a frontend re-test cycle.** Functionally the sidecar projection works today; the WAL cutover is a *durability* upgrade, not a feature upgrade. Low priority unless a post-restart forensics gap actually bites.
3. **Which P3 items to batch.** Suggested grouping for one PR: P3-D + P3-J + P3-K (all timestamp/timezone rigor, single review pass).

---

## Where to read for context

- **VCR finding files:** `docs/audits/vibe-coded-app-research/findings/VCR-*.md` — every finding's `remediation_progress` and `follow_up_required` lists are the source of truth.
- **Phase glossary:** `docs/audits/vibe-coded-app-remediation-prd.md` — defines Phase 5A through Phase 12 and their acceptance gates.
- **ADR 0008 (durable submit protocol):** `docs/architecture/adrs/0008-durable-submit-protocol-order-identity-recovery.md` — the contract behind Gap 1.
- **ADR 0009 (live sizing authority):** `docs/architecture/adrs/0009-live-sizing-authority-and-provenance.md` — the contract behind Gap 2.
- **Prior handoff (deploy-validation focus):** `docs/handoffs/2026-06-15-vcr-remediation-deploy-validation-handoff.md` — still accurate for §3-§5 (deploy procedure), §6 (cockpit checks), §7 (rollback). Updated this session to point at current master.

---

*Session close: 20 PRs merged (#530–#550); 16 of 19 VCR findings fully remediated; 1 operator-gated; 2 partial with deferred frontend / polish work. Master at `8ed5b889`.*
