# PRD: Remediate Vibe-Coded App Research Findings

**Status:** Draft v1  
**Created:** 2026-06-14  
**Source audit:** `docs/audits/vibe-coded-app-research.md`  
**Finding set:** `docs/audits/vibe-coded-app-research/findings/VCR-0001..VCR-0018.md` + `VCR-P3-rollup.md`  
**Owner:** developer/operator  
**Primary objective:** Remove the dangerous live/paper-trading back doors found by the VCR audit, then reduce operator-trust drift, governance drift, and repo bloat.

## 1. Executive Summary

The VCR audit found a healthy codebase with two serious safety holes:

1. **Sizing safety can be bypassed by an empty `live_config`** (`VCR-0001`, P0). ADR 0009's Safe canary path exists, but the server still accepts deploys with no `sizing` key. Those runs fall back to `SimpleFloorSizing`, allowing `set_holdings(..., 1.0)` to buy the whole account.
2. **ADR 0008 durable-submit/restart safety exists as modules but is not wired into production** (`VCR-0002`, P0). Restart reconciliation, intent WAL, durable order identity, and ownership classification do not gate the live order path.

The rest of the findings cluster around four themes:

- **Deploy integrity:** strategy keys in the dropdown do not match live runner module names; broker account identity is not verified against the ledger.
- **Operator-trust drift:** the UI claims actions or safety states that runtime code does not actually perform, such as runtime `RECONCILE`, FLATTEN semantics, QC approval, and paper-mode banners.
- **Audit/provenance completion:** `SIZING_RESOLVED` is declared but not emitted; governance docs are stale; stale .NET entry Greeks remain exposed.
- **Repo hygiene:** dead scaffold code, stale manual artifacts, and scratch files add noise.

This remediation PRD deliberately sequences work so the safety holes close first, while medium-confidence findings are re-grounded before implementation.

## 2. Goals

1. Make it impossible for a new deploy to run without an explicit sizing policy.
2. Prevent the live engine from starting when ledger account identity disagrees with the broker-connected account.
3. Align strategy registry keys, deploy values, and live runner import values.
4. Add an interim operator-visible warning for ADR 0008 until full durable-submit wiring is complete.
5. Wire ADR 0008 incrementally: durable intent WAL, order identity, cold-start reconciliation, ownership classification, and submit retry policy.
6. Make operator UI claims truthful: RECONCILE, FLATTEN, paper mode, QC provenance, readiness labels, and stop outcomes.
7. Complete ADR 0009 audit trail by emitting durable `SIZING_RESOLVED` events.
8. Remove or archive high-confidence dead/bloated code/docs.
9. Produce one canonical text-only developer/operator manual after behavior is fixed.

## 3. Non-Goals

- Do not enable live-money trading.
- Do not change strategy alpha rules.
- Do not loosen numerical tolerances.
- Do not regenerate golden fixtures.
- Do not rewrite the full broker cockpit UI.
- Do not convert the final manual to HTML/PDF in this remediation wave.
- Do not delete medium-confidence code until owner review or stronger reachability proof exists.

## 4. Severity and Release Policy

### Blocker

`VCR-0001` must be fixed before any further live/paper deploy UX enhancements ship. It is a short-path safety fix and closes the exact failure ADR 0009 was meant to eliminate.

### Release-Gated

`VCR-0002`, `VCR-0006`, `VCR-0004`, and `VCR-0008` must be mitigated before the platform is presented as restart-safe, multi-strategy-safe, or operator-ready.

### Re-ground Before Coding

The audit marked these as medium confidence and they require a direct file/line verification pass before implementation:

- `VCR-0007` FLATTEN aliases STOP.
- `VCR-0009` emergency flatten lacks cancel-first.
- `VCR-0010` paper-mode hero hardcoded.
- `VCR-0011` daemon token comparison.
- `VCR-0012` cross-restart fill drop.
- Portions of `VCR-0018`.

If re-grounding disproves a finding, close it in the research report with evidence rather than coding around it.

## 5. Remediation Plan

### Phase 0 — Re-ground Medium-Confidence Findings

**Purpose:** Avoid implementing against prose-only claims.

**Gate model:** Phase 0 is a **phase-scoped evidence gate, not a global implementation gate.** A remediation PR may proceed if every finding it depends on is `confidence: high` or has been re-grounded. Phases 1-3 may start immediately because they depend only on high-confidence findings. Phases consuming medium-confidence findings MUST promote those findings to `confidence: high` or close them as invalid before opening implementation PRs.

**Scope:**

- Medium-confidence findings: `VCR-0007`, `VCR-0009`, `VCR-0010`, `VCR-0011`, `VCR-0012`, `VCR-0018` items.
- Per-phase dependency:
  - Phase 1: VCR-0001 — already `high`. **No Phase 0 work required.**
  - Phase 2: VCR-0004, VCR-P3-A/B/C — already `high`. **No Phase 0 work required.**
  - Phase 3: VCR-0006 — already `high`. **No Phase 0 work required.**
  - Phase 4: VCR-0002 `high` + VCR-0008 needs re-grounding for the "Fix this" UI affordance specifics.
  - Phase 5: VCR-0002 `high`; VCR-0012 medium → re-ground before fill-classifier work (5E). VCR-0009 medium → re-ground before emergency-flatten cancel-first (also touched in Phase 6).
  - Phase 6: VCR-0007, VCR-0009, VCR-0018-B/F/G, VCR-P3-P/Q need re-grounding.
  - Phase 7: VCR-0010, VCR-0011, VCR-0018-A/C/D/E, VCR-P3-J/K/L/N need re-grounding.

**Tasks:**

1. Capture exact file/line evidence for each medium-confidence claim that gates a phase about to start.
2. Update each finding with `confidence: high` or close as invalid.
3. Add a short table to `docs/audits/vibe-coded-app-research.md` summarizing verification outcome.

**Guardrails:**

1. If re-grounding **invalidates** a finding (the claimed behavior is not what the code does), the dependent phase pauses and rescopes before implementation. Phase 6's "Flatten and pause" contract is contingent on VCR-0007's claim being correct.
2. If re-grounding **strengthens or expands** a finding (more file:lines, additional code paths), the dependent phase absorbs the new evidence before implementation.
3. Every dependent phase still needs a regression test that proves the bug from runtime behavior, not just from file:line evidence.
4. Medium-confidence findings must not be "fixed from memory" — re-ground first, then code.

**Acceptance criteria:**

- Every medium-confidence finding that gates an active phase has direct evidence or is explicitly rejected.
- No production code changes in this phase (verification is doc-only).

### Phase 1 — Sizing Policy Must Be Explicit

**Findings:** `VCR-0001`, related `VCR-P3-M`.

**Problem:** New deploys can omit `live_config.sizing` and fall into legacy `SimpleFloorSizing`.

**Required behavior:**

- New deploy requests must require `live_config.sizing`.
- Empty `live_config` must be rejected at deploy/API boundary for new runs.
- Unknown `live_config` sibling keys must be rejected at deploy/API boundary, not after ledger creation.
- Legacy ledgers with absent sizing may still be readable for audit/manual inspection, but they are **view-only / redeploy-only**. `cmd_start` must refuse to start them. No override flag is allowed.

**Implementation direction:**

1. Replace open `HostRunnerDeployRequest.live_config: dict` with a typed model or stricter validator that requires `sizing`.
2. Preserve legacy ledger read compatibility in `_live_config_from_ledger`.
3. Add `check_sizing_policy_present` to pre-flight/start checks.
4. Enforce legacy behavior:
   - Pre-policy ledgers may be viewed in the cockpit and Sizing card.
   - Starting a pre-policy ledger is refused with a clear error: redeploy with explicit sizing.
   - Do **not** add `--allow-pre-policy-sizing` or an equivalent bypass. Sizing is part of `live_config`, and `live_config` is hashed into `run_id`; start-time overrides would make `run_id` dishonest.
5. Remove or quarantine comments that normalize SimpleFloor fallback for new runs.

**Regression tests:**

- Deploy with `live_config={}` returns 400.
- Deploy with no `live_config` returns 400.
- Deploy with `live_config={"future_field": 1, "sizing": ...}` returns 400.
- Deploy with Safe canary serializes canonical `FixedShares(1)`.
- Legacy ledger with no sizing loads as pre-policy in read-only/cockpit views but `cmd_start` refuses with a redeploy-required error.
- `check_all_in_coexistence` cannot be bypassed by absent policy.

**Acceptance criteria:**

- There is no new-deploy or legacy-start path from API/UI/host-daemon to `LivePortfolio(SimpleFloorSizing)`.
- Sizing card still renders old runs honestly as pre-policy.

### Phase 2 — Deploy Strategy Key Contract

**Findings:** `VCR-0004`, `VCR-0013`, `VCR-P3-A/B/C`.

**Problem:** Dropdown strategy keys are registry names, but the runner imports module names. Seven of ten advertised deploy choices cannot start.

**Decision:** Use module-name strategy keys as the live deploy contract, and add an explicit `class_name` field to `StrategyRegistration` to retire the `<PascalKey>Algorithm` naming convention.

Rationale:

- The live runner already imports by module name.
- Existing spec fixtures mostly use module-name stems.
- Existing live ledgers and tests already use `spy_ema_crossover` / `deployment_validation`.
- It eliminates the `removeprefix("spy_")` workaround.
- Collapsing three identifier spaces (registry key, API value, module path) into one removes the latent shape that allowed VCR-0004 to drift. A separate `module_name` field is rejected as YAGNI — no concrete need to rename a module without changing API identity.
- Explicit `class_name` retires the `<PascalKey>Algorithm` convention and removes the `DeploymentValidationAlgorithm = DeploymentValidationConsecutiveGreen` alias (VCR-P3-A).

**Resolved identifier contract:**

- Canonical strategy key = module name.
- API name, ledger `strategy_key`, runner `--strategy`, and Python module path all use the same string.
- Runner resolves a strategy by:
  - module: `app.engine.strategy.algorithms.{strategy_key}`
  - class: `_STRATEGY_REGISTRY[strategy_key].class_name`
- Future class renames do not affect `run_id` identity unless the module/key changes.

**Implementation direction:**

1. Change `_STRATEGY_REGISTRY` live-deploy-relevant keys to module names:
   - `ema_crossover` → `spy_ema_crossover`
   - `orb` → `spy_orb`
   - `ema_crossover_options` → `spy_ema_crossover_options`
   - `rsi_range_a/b/c` → `spy_strategy_a/b/c`
2. Add `class_name: str` to `StrategyRegistration`. Populate for every entry, including `deployment_validation` → `DeploymentValidationConsecutiveGreen`. Delete the `DeploymentValidationAlgorithm` alias.
3. Replace the runner's `<PascalKey>Algorithm` convention in `run.py` with `getattr(module, registration.class_name)`.
4. Keep display names stable for users.
5. Remove prefix-stripping workarounds in sizing-surface lookups once all callers use module names.
6. Register or explicitly block importable non-deployable modules:
   - `buy_and_hold`
   - `spy_vwap_reversion`
7. Update frontend mocks to match production API values.
8. Update `FALLBACK_STRATEGY`.
9. Fix spec-fixture auto-fill to use the canonical module key.

**Regression tests:**

- For every `/api/engine/strategies` item, runner import resolves.
- For every registered deployable strategy, `getattr(module, registration.class_name)` resolves.
- `DeploymentValidationAlgorithm` alias is gone; `deployment_validation` resolves through `class_name = "DeploymentValidationConsecutiveGreen"`.
- Frontend deploy form mock data mirrors a real fixture generated from backend response.
- Spec fixture auto-fill works for `spy_ema_crossover`.
- Non-deployable modules cannot be launched through `run.py --strategy`.

**Acceptance criteria:**

- Every strategy shown in the broker deploy dropdown can start or is explicitly disabled with a clear reason.
- No deploy path writes a ledger with a strategy key the runner cannot import.

### Phase 3 — Account Identity Verification

**Findings:** `VCR-0006`.

**Problem:** `ledger.account_id` is hashed into identity but not compared against the broker-reported connected account.

**Required behavior:**

- Start fails before strategy initialization if `ledger.account_id` does not match broker `connected_account`.
- Executions/provenance should distinguish ledger account from broker-reported account for forensic clarity.

**Implementation direction:**

**Final contract:** Account identity is **strict and immutable**. `ledger.account_id` is deploy-time identity and part of `run_id`. `broker.connected_account` is runtime proof. They MUST match exactly after normalization at initial connect and at every reconnect. Missing or malformed account identity is view-only / redeploy-only. No auto-population. No DU-prefix fallback (DU-prefix remains part of the paper-safety verdict, not identity).

**A. Normalization rule.**

```
normalize(account_id) := raw.strip().upper()
  must match ^[A-Z][A-Z0-9]+$
```

Comparison: `normalize(ledger.account_id) == normalize(broker.connected_account)`.

- Trim leading/trailing whitespace.
- Uppercase.
- Reject internal whitespace, control characters, non-alphanumeric chars.
- No prefix-match, substring-match, or "starts with DU" shortcut.

**B. Empty / missing / malformed `ledger.account_id`.**

Fail-closed: refuse start; require redeploy. Mirrors Phase 1's "missing sizing = view-only/redeploy-only" policy. `account_id` is hashed into `run_id`, so any in-place mutation would make the identity fingerprint dishonest. Auto-population from broker and DU-prefix fallback are both rejected for the same reason.

**C. Re-check on every reconnect.**

Re-validate account identity every time the IBKR connection is established or re-established. On reconnect mismatch:

- Block new order submission immediately.
- Write `halt.flag` (existing fatal-halt artifact).
- Set durable `desired_state = PAUSED`.
- Emit `RECONNECT_ACCOUNT_MISMATCH_HALT` WAL event:
  ```json
  {
    "event_type": "RECONNECT_ACCOUNT_MISMATCH_HALT",
    "ledger_account_id": "DU1234567",
    "connected_account": "DU7654321",
    "connection_epoch": 2,
    "ts_ms_utc": 1234567890
  }
  ```
- Surface in cockpit failure list.

**Distinct event class** from `BROKER_SAFETY_VERDICT_TRANSITION_HALT`:

- `BROKER_SAFETY_VERDICT_TRANSITION_HALT` → safety gates degraded (mode/port/readonly/prefix).
- `RECONNECT_ACCOUNT_MISMATCH_HALT` → broker account identity changed.

**D. Forensic persistence — per-session metadata + WAL event.**

Per-row `connected_account` in `executions.parquet` is unnecessary because (C) halts before further orders/fills are trusted. Per-session is sufficient and less noisy.

Session metadata artifact (`artifacts/live_runs/<run_id>/session_metadata.json`):

```json
{
  "ledger_account_id": "DU1234567",
  "connected_account": "DU1234567",
  "session_started_ms": 1234567890,
  "session_ended_ms": null,
  "connection_epoch": 1
}
```

WAL events:

```json
{ "event_type": "SESSION_STARTED",   "ledger_account_id": "...", "connected_account": "...", "connection_epoch": 1, "ts_ms_utc": ... }
{ "event_type": "BROKER_RECONNECTED", "ledger_account_id": "...", "connected_account": "...", "connection_epoch": N, "ts_ms_utc": ... }
```

On reconnect: emit `BROKER_RECONNECTED` (on match) or halt with `RECONNECT_ACCOUNT_MISMATCH_HALT` (on mismatch). Increment `connection_epoch` on each successful (re)connect.

**Implementation direction:**

1. Pass `ledger.account_id` into `LiveEngine` constructor. Compare against `client.connected_account` in `_validate_paper_client` (immediately after IBKR connection, before any strategy initialization).
2. Refuse start if `ledger.account_id` is empty/malformed or if normalized comparison fails. Error must include both raw values.
3. Add a reconnect hook (in the IBKR client's reconnect path) that re-runs the same comparison and either emits `BROKER_RECONNECTED` or triggers `RECONNECT_ACCOUNT_MISMATCH_HALT`.
4. Persist `session_metadata.json` on `SESSION_STARTED` and append `connection_epoch` on each reconnect.

**Regression tests:**

- Matching account with case difference passes (normalization).
- Leading/trailing whitespace is normalized; match passes.
- Internal whitespace in `ledger.account_id` fails.
- Non-alphanumeric character in either field fails.
- Empty `ledger.account_id` fails (legacy ledger refused).
- Missing `ledger.account_id` field fails.
- Ledger `DU1234567` vs broker `DU9999999` fails before strategy initializes.
- Reconnect to the same account passes and emits `BROKER_RECONNECTED` with incremented `connection_epoch`.
- Reconnect to different account halts with `RECONNECT_ACCOUNT_MISMATCH_HALT`, sets durable `desired_state=PAUSED`.
- No orders submit after a reconnect-mismatch halt.
- `session_metadata.json` and `SESSION_STARTED` event both carry `ledger_account_id` and `connected_account`.
- Error surfaced in run status sidecar / cockpit failure table on every mismatch path.

**Acceptance criteria:**

- Ledger account identity cannot silently diverge from the broker account receiving orders, at start or at reconnect.
- Legacy ledgers without `account_id` cannot start; redeploy is the only forward path.
- All identity-related halts carry both raw values in the WAL for forensic reconstruction.

### Phase 4 — Immediate Operator-Truth Mitigations for ADR 0008 Gap

**Findings:** `VCR-0002`, `VCR-0008`, `VCR-0012`.

**Problem:** Durable-submit modules are not wired. Full fix is multi-PR, but UI currently implies restart/reconcile safety.

**Resolved contract:** remove the runtime UI affordance now (option B). Promote to a durable "Schedule reconcile on next restart" flag at Phase 5B (option D), when `ColdStartReconciler.verify()` exists to consume it. Rationale: a structured no-op (option C) leaves an operator-facing workflow that did nothing real; a "Mark for re-sync at next restart" label (option A) is a promise the consumer cannot keep until Phase 5B. Option B is honest, then D becomes honest once consumed.

**Required interim behavior (Phase 4):**

- The cockpit MUST NOT imply runtime `RECONCILE` refreshes state.
- The run surface MUST warn that restart safety is not fully wired until ADR 0008 phases complete.
- Any "Fix this" action pointing to runtime `RECONCILE` MUST be removed.

**Implementation direction (Phase 4):**

1. Remove the runtime UI "Re-sync now" / "Fix this → reconcile" affordance entirely.
2. Show a banner instead: *"Runtime reconcile is not wired yet. After crash/restart or suspected broker drift, manually verify broker state before continuing."*
3. Keep the `RECONCILE` verb in `command_channel` for backend compatibility (CLI / panic / older paths), but the runtime handler returns an explicitly non-success structured result:
   ```json
   {
     "result": "accepted_noop",
     "reason": "runtime_reconcile_not_wired",
     "manual_action": "restart_required_no_broker_refresh_occurred"
   }
   ```
4. Do NOT display the backend no-op as a successful reconcile anywhere in the UI.
5. Add a runbook entry in the future manual for manual restart reconciliation.

**Regression tests (Phase 4):**

- Runtime `RECONCILE` returns `accepted_noop`, never `success`.
- UI has no "Re-sync now" button.
- "Fix this" links do not dispatch `RECONCILE`.
- Cockpit renders the ADR 0008 banner for active runs until durable-submit wiring status is true.

**Acceptance criteria (Phase 4):**

- Operator cannot mistake current runtime `RECONCILE` for a completed broker-state reconciliation.

**Phase 5B promotion (option D contract):**

When Phase 5B lands (`ColdStartReconciler.verify()` invoked from `cmd_start`), promote the durable-flag path:

- Add durable sidecar flag (instance-scoped):
  ```json
  {
    "reconcile_pending": true,
    "requested_at_ms": 1234567890,
    "requested_by": "operator"
  }
  ```
- Re-introduce UI button: *"Schedule reconcile on next restart"*
- UI copy: *"This does not refresh the running bot. It requests a full reconcile before the next start."*
- `ColdStartReconciler.verify()` consumes the flag and records outcome ∈ `{clean, divergence_detected, halted}` durably.
- Clear the flag ONLY after a completed reconcile result is durably recorded (idempotent — interrupted reconcile leaves the flag set).

**Regression tests (Phase 5B promotion):**

- Scheduled flag survives restart.
- Reconciler consumes flag and writes outcome before clearing it.
- A crash between flag-consumed and outcome-recorded leaves the flag set for the next start.
- UI wording says "next restart," not "immediate refresh."

### Phase 5 — Durable Submit Wiring

**Findings:** `VCR-0002`, `VCR-0012`, related `VCR-0009`.

**Problem:** Intent WAL, order identity, cold-start reconciler, submit state machine, and ownership classifier are implemented but not part of production order flow.

**Phasing:** This must be several PRs with narrow acceptance gates.

#### Phase 5A — Intent Identity Foundation

**Invariant introduced:** `intent_id ↔ order_ref ↔ attempted broker order`. An `intent_id` means "this will become, or attempted to become, a broker order." Nothing else mints one.

1. Mint `intent_id` only **after** sizing resolution proves an order will be submitted (i.e. `delta != 0`). Sizing skips do **not** mint an intent.
2. Stamp `orderRef = build_order_ref(namespace, intent_id)` on every IBKR order.
3. Append `PENDING_INTENT` to the WAL before broker submit.
4. Append `SUBMITTED` (with `perm_id`, `broker_order_id`) or `ACK_FAILED_UNCERTAIN` (with error context) based on submit result.
5. Submit retry / uncertainty contract as scoped for 5A (full state-machine wiring lands in 5D).
6. Existing in-memory `sizing_resolutions` list stays in place to keep the Sizing card alive; Phase 8 swaps it for a WAL fold.

Tests:

- Broker order spec contains deterministic parseable `orderRef`.
- WAL contains ordered intent events on success.
- Submit exception writes `ACK_FAILED_UNCERTAIN`.
- A `set_holdings` call that resolves to a no-op (target_qty == current_qty) does **not** mint an `intent_id`.
- An `intent_id` minted in this run is unique vs any prior run's WAL.

#### Phase 5B — Cold-Start Reconciler Gate

1. Invoke `ColdStartReconciler.verify()` from `cmd_start` before strategy initialization.
2. Halt on unclassified divergence.
3. Surface divergence details in run status/cockpit.

Tests:

- Clean ledger + broker state starts.
- Open broker order without matching intent blocks start.
- Missing broker order for pending intent blocks start or classifies according to ADR 0008.

#### Phase 5C — Ownership Query Wiring

**Default policy: ownership query unavailable → fatal halt** for every managed runtime cancel/flatten path. The only carve-out is an out-of-process emergency-flatten with an explicit `force=True` flag, which represents operator-confirmed last-resort behavior — never a fall-through that normal code paths land in.

1. Register an IBKR `IbkrBrokerOwnershipQuery(VerifiedBrokerOwnershipQuery)` subclass that calls `IB.reqOpenOrders()` / `IB.reqExecutions()` filtered by `bot_order_namespace`.
2. Wire `require_durable_submit_activation(query, enabled=True)` in `LiveEngine` construction; refuse start if activation fails.
3. Make cancellation/flatten paths consult the ownership query, not in-memory `_owned_order_ids`.
4. Preserve foreign-order isolation: namespace-scoped queries by construction; reject any returned record whose parsed `order_ref` namespace does not exactly equal this instance's `bot_order_namespace`.

**Ownership-query-unavailable contract (managed runtime paths — "Flatten and pause," recovery flatten, cold-start reconciliation cleanup, any managed runtime cancel/flatten):**

- Do **not** assume empty owned-orders list.
- Do **not** liquidate.
- Write `halt.flag`.
- Set durable `desired_state = PAUSED`.
- Emit `OWNERSHIP_QUERY_UNAVAILABLE_HALT` WAL event:
  ```json
  {
    "event_type": "OWNERSHIP_QUERY_UNAVAILABLE_HALT",
    "trigger_path": "flatten_and_pause" | "recovery_flatten" | "cold_start_cleanup" | ...,
    "reason": "ownership_query_timeout" | "ownership_query_unactivated" | ...,
    "ts_ms_utc": 1234567890
  }
  ```

**Emergency-flatten carve-out (out-of-process, explicitly operator-confirmed):**

- A separate code path with `force=True` that is **not** reachable from normal runtime control. Operator confirmation is required at invocation time (CLI flag, cockpit "Emergency Flatten" confirm dialog with explicit acknowledgment).
- Liquidates broker-account-net positions because the alternative — leaving open broker positions during a panic — is worse than acting without ownership proof.
- Logs loudly. Writes to `emergency_flatten_audit.jsonl`:
  ```json
  {
    "event_type": "EMERGENCY_FLATTEN_WITHOUT_OWNERSHIP_PROOF",
    "account_id": "DU1234567",
    "reason": "ownership_query_unavailable",
    "operator_confirmed": true,
    "positions_attempted": [...],
    "ts_ms_utc": 1234567890
  }
  ```
- Cockpit failure / fleet-audit surface renders this event distinctly so it can be traced.

**Cancel-then-liquidate ordering (sequential, no parallel-fire):**

Every managed cancel/flatten path follows this order:

1. Query owned open orders (via the ownership query).
2. Submit cancels for the owned open orders.
3. Wait for cancel confirms (per-order ack from broker).
4. Only after all confirms land, fetch current positions.
5. Submit liquidation orders.

**Cancel-confirm timeout:**

- Named constant `CANCEL_CONFIRM_TIMEOUT_S = 5` (initial value; tune later).
- On timeout: emit `CANCEL_CONFIRM_TIMEOUT_HALT`, write `halt.flag`, set `desired_state = PAUSED`. Do **not** liquidate.
- Emergency-flatten force path may proceed past the timeout with audit event `EMERGENCY_FLATTEN_WITH_UNCONFIRMED_CANCELS`.

Tests:

- Foreign open order is not cancelled (namespace mismatch rejected).
- Owned prior-session order can be cancelled after restart (ownership query finds it from the broker).
- Ownership query unavailable in a "Flatten and pause" path → `OWNERSHIP_QUERY_UNAVAILABLE_HALT`, no liquidation, durable PAUSED.
- Same for recovery flatten path.
- Emergency-flatten with `force=True` proceeds despite unavailable query; emits `EMERGENCY_FLATTEN_WITHOUT_OWNERSHIP_PROOF`.
- Sequential ordering: liquidation order is not constructed until every cancel has been confirmed.
- Cancel-confirm timeout in a managed path → `CANCEL_CONFIRM_TIMEOUT_HALT`, no liquidation.
- Emergency-flatten with `force=True` past cancel-confirm timeout → `EMERGENCY_FLATTEN_WITH_UNCONFIRMED_CANCELS`.

#### Phase 5D — Submit Retry Policy

**Confirm:** Phase 5D wires existing `submit_state_machine.next_action` (with `RETRY_CAP = 1` and `NOT_PROVABLE → HALT`) into the actual submit loop. Do **not** redesign the state machine.

**State-machine contract (already pinned in `submit_state_machine.py`, restated here so we do not silently relax):**

- `PRESENT` → adopt (treat as `SUBMITTED_RECOVERED` per CONTEXT.md).
- `PROVABLY_ABSENT` + `retry_count < RETRY_CAP` → retry, **reusing the same `intent_id` and `order_ref`**.
- `PROVABLY_ABSENT` + `retry_count >= RETRY_CAP` → halt.
- `NOT_PROVABLE` → halt (never guess).

Invariant: **one `intent_id` → one `order_ref` → one logical attempted broker order.** Retry never mints a new `intent_id`.

**`SubmitVerdict.HALT` semantics (resolved):**

Run-halt (not intent-only halt; not poison/redeploy):

- The run's broker-state model is no longer trustworthy — cash, positions, and open orders may be wrong for every subsequent decision.
- Stop the bar loop.
- Block new order submission.
- Write `halt.flag`.
- Set durable `desired_state = PAUSED`.
- Emit `SUBMIT_UNCERTAIN_HALT` WAL event:
  ```json
  {
    "event_type": "SUBMIT_UNCERTAIN_HALT",
    "intent_id": "intent_...",
    "order_ref": "learn-ai/.../v1:intent_...",
    "submit_verdict": "HALT",
    "probe_result": "NOT_PROVABLE",
    "retry_count": 1,
    "reason": "submit_state_not_provable",
    "ts_ms_utc": 1234567890
  }
  ```
- Require operator reconciliation before resume.

**Resume contract for `SUBMIT_UNCERTAIN_HALT`:**

Resume is allowed only if **all three** guards pass:

1. `broker_safety.final_verdict == "paper-only"`.
2. Cold-start / runtime ownership reconciliation is clean (Phase 5B `ColdStartReconciler.verify()` result is `clean`).
3. No unresolved uncertain intent remains in the WAL (every `ACK_FAILED_UNCERTAIN` is resolved to `SUBMITTED_RECOVERED`, `INTENT_NOT_ACCEPTED`, or `SUBMIT_UNCERTAIN_HALTED`).

If any guard fails, Resume is refused and the surfaced reason names which guard blocked it.

**Why not intent-only halt:** a NOT_PROVABLE submit means the engine doesn't know whether the order landed at the broker. Continuing the bar loop runs subsequent intents against a tainted cash/position model. The bug Phase 5 is closing is exactly "engine continues with wrong state after an uncertain submit." (α) intent-halt re-opens it.

**Why not poison/redeploy:** the WAL + cold-start reconciler are designed to recover. STOPPED forces a redeploy (heavy) when PAUSED + reconcile suffices.

Tests:

- `PROVABLY_ABSENT` + `retry_count=0` retries once; second attempt reuses `intent_id`/`order_ref`.
- `PROVABLY_ABSENT` + `retry_count=1` halts (cap reached).
- `NOT_PROVABLE` halts on first occurrence (never retries).
- `PRESENT` is adopted.
- `SUBMIT_UNCERTAIN_HALT` writes `halt.flag`, sets `desired_state=PAUSED`, emits the event, exits the bar loop.
- Resume refused while broker safety verdict is not `paper-only`.
- Resume refused while any unresolved uncertain intent exists in the WAL.
- Resume refused while `ColdStartReconciler.verify()` last result is not `clean`.
- Resume succeeds only when all three guards pass.

#### Phase 5E — Fill Conversion Uses Classifier

1. Replace raw `_order_meta` gate in `_convert_ibkr_fill` with durable ownership classification.
2. Record fills that are bot-owned by `perm_id` or `orderRef`, even after restart.
3. Continue ignoring true foreign fills for portfolio update while still persisting foreign executions for outside-mutation detection.

Tests:

- Fill with missing in-memory order id but owned `perm_id` records into portfolio.
- Foreign fill is persisted but not applied to bot portfolio.

**Acceptance criteria for full Phase 5:**

- A crash between submit and ack produces a provable next action or a halt, never silent retry/ignore.
- A restart cannot lose ownership of prior-session bot orders/fills.
- Runtime UI warning from Phase 4 can be removed only after these tests pass.

### Phase 6 — Operator Command Semantics

**Findings:** `VCR-0007`, `VCR-0008`, `VCR-0009`, `VCR-0018-B/F/G`, `VCR-P3-P/Q`. **Source ADR:** `docs/architecture/adrs/0010-operator-action-contract-flatten-pause-stop.md`.

**Problem:** Command labels and state-machine effects are not always aligned.

**Resolved operator-action contract (v1):**

| UI Action | Durable Intent | One-shot Work | Process |
|---|---|---|---|
| Pause | `PAUSED` | none | alive |
| Resume | `RUNNING` | none | alive |
| Flatten and pause | `PAUSED` | cancel owned opens + liquidate | alive |
| Stop | `STOPPED` | graceful shutdown; optional flatten only if explicitly configured | exits |
| Mark poisoned | unchanged or blocked | write poison flag | depends on policy |

Composition rule: **the endpoint composes; the one-shot stays pure.** `FLATTEN_NOW` MUST NOT mutate durable desired state itself. The "Flatten and pause" UI endpoint performs, in order:

1. write `desired_state = PAUSED`
2. enqueue one-shot `FLATTEN_NOW`

This preserves the desired-state / one-shot orthogonality from CONTEXT.md while giving the operator a single safe button.

**Resume is a guarded write, not a pure desired-state write.** Per Phase 7's verdict-halt contract, Resume MUST consult the current broker safety verdict before promoting `desired_state` to RUNNING. If `final_verdict != "paper-only"`, the durable state stays PAUSED and the API surfaces `broker_safety_not_paper_only`. This means the table row "Resume → durable RUNNING" holds only when the verdict permits; otherwise Resume is a no-op-with-reason.

**Rejected v1 options (record with reasons so we don't re-litigate):**

- True FLATTEN that keeps the bot running with intent unchanged: the next eligible bar can re-enter, which is the opposite of what the panic-button mental model expects.
- `STOP_AND_FLATTEN` composite: convenience-only, reintroduces composite semantics, and makes STOP feel like a normal operator button rather than an instance-retirement path.
- Rename current `FLATTEN = STOP + flatten` to "Stop and close positions": cements the fused semantics in the data model and contradicts the desired-state/one-shot separation.

**Implementation direction after Phase 0 verification:**

1. Wire FLATTEN per the contract above: rename `_shutdown_flatten` → `_flatten`; do **not** terminate the process; bar loop honors `desired_state=PAUSED` and refuses new entries until the operator resumes.
2. Emergency flatten must cancel owned open orders before liquidation (closes VCR-0009).
3. Stop command must distinguish "signal accepted" (200 + `command_id`) from "process exited" (separate ack carrying exit reason / `still_running_after_2s`).
4. Force-flat must be enforced at engine level, not by each strategy remembering to suppress orders.
5. `_fatal_halt` and desired-state persistence failures must not be swallowed as successful state transitions.
6. Add per-instance lock across check + spawn + register in `RunnerProcessManager.start`.
7. Re-run critical pre-flight checks at start, not only deploy: halt flag, clean tree if required, NTP if available, sizing policy present.

**Regression tests:**

- "Flatten and pause" endpoint writes `desired_state=PAUSED` *before* enqueueing `FLATTEN_NOW`. Durable write failure aborts before the one-shot is sent.
- After "Flatten and pause" completes, process is still alive and the next bar refuses to enter.
- `FLATTEN_NOW` alone (e.g. via CLI) does NOT mutate desired state.
- Resume from PAUSED restores normal entry behavior; no replay of suppressed signals.
- Emergency flatten cancels owned open orders before liquidation.
- Stop timeout returns `still_running_after_2s` distinct from `accepted`.
- Force-flat state prevents new orders at engine level.
- Persistence write failure propagates to operator-visible failure.
- Concurrent starts for same instance produce one process.

**Acceptance criteria:**

- Every cockpit command has one documented runtime effect and UI wording matches it.

### Phase 7 — Paper-Mode and Provenance UI Truth

**Findings:** `VCR-0010`, `VCR-0014`, `VCR-0018-A/C/D/E`, `VCR-P3-J/K/L/N`. **Source ADR:** `docs/architecture/adrs/0011-broker-safety-verdict-fail-closed-reactive-halt-on-transition.md`.

**Problem:** Several UI labels imply stronger guarantees than runtime data supports.

**Implementation direction:**

**Resolved broker safety verdict contract:**

```ts
type BrokerSafetyVerdict = {
  configured_mode: "paper" | "live" | "unknown";
  readonly_flag: boolean | null;
  port_class: "paper_port" | "live_port" | "unknown";
  connected_account_prefix: "DU" | "non_DU" | null;
  final_verdict: "paper-only" | "unsafe" | "unknown";
  failing_gates: string[];     // gates that positively indicate live/non-paper
  unknown_gates: string[];     // gates whose state cannot be confirmed
};
```

**Fail-closed derivation:**

- `paper-only` iff **every** required gate is positively verified:
  `configured_mode == "paper" AND readonly_flag == true AND port_class == "paper_port" AND connected_account_prefix == "DU"`
- `unsafe` iff **any** gate positively indicates live/non-paper risk:
  `configured_mode == "live" OR port_class == "live_port" OR connected_account_prefix == "non_DU"`
- `unknown` otherwise (any gate is unknown AND no gate is unsafe).

The hero is a trust anchor; any missing signal degrades to `unknown`, never to `paper-only`.

**Runtime behavior beyond rendering:**

- `unsafe` is **order-blocking** — engine refuses to submit orders while the verdict is unsafe.
- `unknown` is **start-blocking** unless an explicitly documented diagnostic / read-only path needs to inspect state. The diagnostic path must be a separate, named code surface — not an accidental fall-through.
- The verdict feeds the start-readiness gate (CONTEXT.md "Readiness gate"). A start-readiness verdict of READY requires `final_verdict == "paper-only"`.
- The Frontend MUST NOT independently derive `final_verdict` from the raw gate fields. It renders the server-derived verdict; it may optionally show gate details.

**Reactivity:**

- The verdict rides the existing broker status / readiness payload — same polling/SSE cadence. No new transport. No connect-time cache as the source of truth.

**Hero rendering:**

| Verdict | Color | Message |
|---|---|---|
| `paper-only` | green | "Paper trading mode — DU{last4}" |
| `unsafe` | red | "Unsafe broker state: {first failing gate}. Orders blocked." |
| `unknown` | amber | "Broker safety unknown: {first unknown gate}. Verify before continuing." |

**Resolved QC provenance card contract:**

```ts
type QcProvenance = {
  audit_copy_path: string;
  audit_copy_sha256: string;
  audit_copy_sha256_verified: boolean;
  audit_copy_sizing_rule_verdict?: "proven_match" | "proven_mismatch" | "cannot_prove";
  qc_cloud_backtest_id: string;
  qc_cloud_backtest_id_verified: false;  // always false until a QC Cloud API verification path exists
};
```

Card rendering:

- **Audit copy:** ✓ SHA verified against on-disk file / allow-list — OR — ✗ SHA not verified / cannot prove (with allow-list verdict if available).
- **QC Cloud backtest:** `{id}` — *Operator-recorded, not auto-verified.*

Forbidden labels (unless and until a real verification path exists):

- "QC-approved"
- "Byte-identical to backtest"
- "verified backtest"

**Mid-session verdict transition (resolved):**

If a run starts under `final_verdict == "paper-only"` and later observes `final_verdict != "paper-only"`, the engine MUST:

1. Immediately block new order submission.
2. Write `halt.flag` (existing fatal-halt artifact path).
3. Set durable `desired_state = PAUSED` — **not** STOPPED. STOPPED is instance retirement; verdict transitions may be transient (broker disconnect, gateway restart, probe failure). PAUSED is reversible after inspection.
4. Emit a WAL event:
   ```json
   {
     "event_type": "BROKER_SAFETY_VERDICT_TRANSITION_HALT",
     "from": "paper-only",
     "to": "unknown",
     "snapshot": { "configured_mode": "...", "readonly_flag": "...", "port_class": "...", "connected_account_prefix": "..." },
     "failing_gates": [],
     "unknown_gates": ["port_class"],
     "ts_ms_utc": 1234567890
   }
   ```
5. Stop / suspend the active trading loop per existing fatal-halt mechanics.
6. The cockpit failure list renders the event with the offending gate.

**Resume contract (interacts with Phase 6 operator-action table):**

When the operator clicks Resume on a verdict-halted instance:

```
if broker_safety.final_verdict == "paper-only":
    desired_state = RUNNING
    bot resumes
else:
    remain PAUSED
    surface reason: broker_safety_not_paper_only
```

This means the Resume action is no longer a pure desired-state write — it is a *guarded* write that consults the current verdict. The verdict gate stays the trust anchor; the operator cannot trick the bot back to RUNNING under an unsafe/unknown verdict by clicking the button.

**Implementation hooks:**

1. Capture `startup_broker_safety_verdict` at run start.
2. Add a `verdict_transition_observer` in the live loop or status-update path.
3. Wire Resume to consult `broker_safety.final_verdict` before promoting durable state to RUNNING.

**Remaining Phase 7 mechanical fixes:**

1. Expand `READINESS_GATE_LABELS` to cover all server-emitted gates (VCR-P3-N adjacent).
2. Rename "Historical Data Loading" to "Indicator state hydration" where applicable.
3. Sizing card renders timestamps with the same NY formatter as the rest of broker UI (VCR-P3-J).
4. Clarify failure timestamp naming or convert to true `ts_ms_utc` (VCR-P3-K). Per `.claude/rules/numerical-rigor.md` "Timestamp rigor," prefer the conversion to canonical `int64 ms UTC` over the naming workaround.
5. Persist both broker `exec_time_ms` and local `received_at_ms` for executions (VCR-P3-L).

**Regression tests:**

- Hero changes when any gate's state changes, reactively, on the next status payload.
- `final_verdict` derivation is exhaustively tested for every gate-state combination (one parameterized test).
- Engine refuses to submit orders when verdict is `unsafe` (separate from the existing four-layer enforcement — this is a verdict-driven gate).
- Engine refuses to start when verdict is `unknown` outside the diagnostic path.
- Frontend renders the verdict directly; no client-side derivation.
- QC card never renders "QC-approved" unless `qc_cloud_backtest_id_verified == true`.
- All readiness gate enum values have labels.
- Broker timestamp components use consistent NY formatter.
- Mid-session transition `paper-only → unknown` triggers fatal halt + durable PAUSED + `BROKER_SAFETY_VERDICT_TRANSITION_HALT` event.
- Mid-session transition `paper-only → unsafe` same as above.
- No order can submit after a verdict transition halt event.
- Resume while verdict remains unknown/unsafe is refused; durable state stays PAUSED.
- Resume after verdict returns to `paper-only` is allowed and promotes desired state to RUNNING.
- Event/failure table includes old verdict, new verdict, gate details, timestamp.

**Acceptance criteria:**

- UI safety/provenance claims are traceable to server-side facts.
- The broker safety verdict gates order submission (unsafe), run start (unknown), AND continuation of an active run (mid-session transition out of `paper-only`) — not just hero color.
- Resume is a guarded write — never a way around the verdict.

### Phase 8 — Complete ADR 0009 Audit Trail

**Findings:** `VCR-0003`, `VCR-P3-E/F`.

**Problem:** `SIZING_RESOLVED` event exists in types/fold logic but no production writer exists.

**Sequencing:** Phase 8 ships **after** Phase 5A. Reasoning: `SIZING_RESOLVED` carries the same `intent_id` as `PENDING_INTENT`/`SUBMITTED`, so the identity foundation must exist first. Shipping Phase 8 before 5A would force a throwaway sidecar (`intent_id` would have no consumer yet) — exactly the kind of plumbing this PRD is trying to retire.

**Implementation direction:**

1. Phase 5A already mints `intent_id` after sizing resolution proves an order will be submitted (`delta != 0`). Phase 8 inserts `SIZING_RESOLVED` between the mint and `PENDING_INTENT`, using the same `intent_id`.
2. Emit `SIZING_RESOLVED` before `PENDING_INTENT` (which precedes `SUBMITTED` / `ACK_FAILED_UNCERTAIN`).
3. `SIZING_RESOLVED` payload must include:
   - `ts_ms_utc`
   - `intent_id`
   - `policy_kind`
   - `policy_value`
   - `target_qty`
   - `current_qty`
   - `delta_qty`
   - `reference_price`
   - `sizing_provenance_at_resolve_time`
   - `sized_via`
4. Emit `SIZING_SKIP` when sizing resolution decides not to submit (target == current, zero shares while flat, etc.). `SIZING_SKIP` carries **no `intent_id`** — a skip is not an intent.
5. `SIZING_SKIP` payload includes:
   - `ts_ms_utc`
   - `symbol`
   - `policy_kind`, `policy_value`
   - `target_qty`, `current_qty`
   - `reference_price`
   - `reason` (e.g. `target_equals_current`, `zero_shares_while_flat`)
6. Sizing card cuts over from in-memory `sizing_resolutions` list to a WAL fold of `SIZING_RESOLVED` + `SIZING_SKIP` events in this same PR. No bridge period where two sources of truth exist.
7. Add reverse order-surface validation: policy-registered strategy using explicit `market_order` fails fast at the call site (closes VCR-P3-F).

**Regression tests:**

- Every submitted order from `set_holdings` has exactly one `SIZING_RESOLVED` event, with the same `intent_id` as its `PENDING_INTENT`.
- A skip resolution emits `SIZING_SKIP` and no order.
- `SIZING_SKIP` events carry no `intent_id`.
- `executions.parquet` fill joins to `SIZING_RESOLVED` by `intent_id`.
- Sizing card renders the per-trade audit list from WAL fold, not from in-memory `sizing_resolutions`.
- Explicit `market_order` from a policy-registered strategy fails surface validation.

**Acceptance criteria:**

- Sizing audit list is durable, intent-keyed, and recoverable after restart.
- The `intent_id ↔ order_ref ↔ attempted broker order` invariant from Phase 5A is preserved (skips never mint intents).

### Phase 9 — Portfolio Valuation Greek Drift

**Findings:** `VCR-0005`, extends `F-0018`.

**Problem:** `PortfolioValuationService` still aggregates stale entry Greeks and exposes/persists them as current `NetDelta/Gamma/Theta/Vega`.

**Decision:** Hard-delete the stale entry-Greek aggregates from `PortfolioValuation` and `PortfolioSnapshot`. Reject deprecate-null (carries dead schema forever) and rename-to-`EntryNetDelta` (aggregate entry-Greeks are not a useful portfolio concept).

> **This is not a Greek engine removal.** Phase 9 removes only the stale entry-Greek aggregates from valuation/snapshot surfaces. Current Greeks remain owned by Python-backed live-Greek/risk paths (QuantLib pricer, `getDollarDelta`, `getPortfolioVega`).

Rationale:

- The fields are misleading, not merely stale: `NetDelta/NetGamma/NetTheta/NetVega` imply current portfolio Greeks, but the implementation aggregates entry-time Greeks. Keeping them invites future misuse.
- No known in-repo consumer depends on them. Frontend `getValuation` selects them, but no template or derived state uses them. This is the right time to remove the contract.
- Deprecate-null preserves clutter — adds a future cleanup task for no real value.
- Aggregated `EntryNetDelta` is not a useful portfolio-level concept. If entry Greeks are ever needed, they belong as per-position/per-leg metadata, not an aggregate valuation field.
- Historical snapshot data is not worth preserving as-is — it was recorded under a misleading meaning. Dropping the columns is safer than letting future analytics treat them as valid historical portfolio Greeks.

**Scope (in):**

- `PortfolioValuation`
- `PortfolioSnapshot`
- `getPortfolioValuation` GraphQL field
- snapshot persistence
- Frontend `getValuation` query + generated types

**Scope (out — explicitly do not touch):**

- Python live Greeks
- QuantLib / option-pricing Greek responses (`QuantLibStrategyResponse.NetDelta/...`, `QuantLibPriceResponse.Delta/...`)
- `getDollarDelta`
- `getPortfolioVega`
- Current Python-backed risk paths

**Implementation direction:**

1. Remove stale Greek aggregation from `PortfolioValuationService.ComputeValuationInternal` (delete the `EntryDelta/EntryGamma/EntryTheta/EntryVega` summation).
2. Remove `NetDelta/NetGamma/NetTheta/NetVega` from `PortfolioValuation` (interface + DTO).
3. Remove corresponding GraphQL fields from the `PortfolioValuation` object type.
4. Stop selecting `netDelta/netGamma/netTheta/netVega` in Frontend `getValuation`.
5. Remove the fields from Frontend types (`graphql/types.ts`, `graphql/portfolio-types.ts`).
6. Add EF migration dropping the four `PortfolioSnapshot` columns.
7. Update tests expecting those fields.
8. Update docs:
   - `docs/math-sources-of-truth.md`
   - `docs/architecture/numerical-authority-migration-plan.md`
   - `F-0018` closure note or a follow-up note stating the original closure was doc-only/incomplete and Phase 9 finishes the code cleanup.

**Regression tests:**

- `PortfolioValuationService` no longer references `EntryDelta`, `EntryGamma`, `EntryTheta`, or `EntryVega`.
- GraphQL introspection no longer exposes those fields on `PortfolioValuation`.
- `PortfolioSnapshot` no longer has those columns after migration.
- Frontend `getValuation` no longer requests the fields.
- Existing `getDollarDelta` / `getPortfolioVega` / QuantLib live-Greek tests remain unchanged and green.

**Acceptance criteria:**

- No GraphQL field labelled as current portfolio Greek is backed by entry-time Greeks.
- QuantLib live-Greek path is untouched and verifiable.

### Phase 10 — Governance Docs

**Findings:** `VCR-0015`, `VCR-0016`, `VCR-P3-D`.

**Problem:** Architecture docs trail shipped code.

**Implementation direction:**

1. Add ADR 0009 live-sizing migration to `numerical-authority-migration-plan.md`.
2. Correct Phase 2/2.3 scope to say PortfolioRisk migrated but PortfolioValuation needed remediation until Phase 9.
3. Update `engine-authority-map.md` LEAN sidecar row to shipped-through-Phase-5g.3 status.
4. Update `lean-sidecar-lab.md` status header.
5. Re-anchor ADR 0009 reference line numbers or replace fragile line ranges with semantic anchors.

**Acceptance criteria:**

- `docs/math-sources-of-truth.md`, `engine-authority-map.md`, and `numerical-authority-migration-plan.md` agree on live sizing, LEAN sidecar, and portfolio Greeks.

### Phase 11 — Dead/Bloated Code Cleanup

**Findings:** `VCR-0017`, `VCR-P3-O/N`.

**High-confidence delete PR:**

- `Frontend/src/app/components/authors/`
- `Frontend/src/app/components/books/`
- author/book services and GraphQL types
- `Frontend/src/app/components/run-comparison/`
- obsolete compare GraphQL query if only used there
- `Frontend/src/app/services/polygon.service.ts` and its spec

**Default policy:** hard-delete. Git history is the archive. Do **not** move into `docs/archive/` unless there is a concrete active reference that must keep a path discoverable.

**`validation_study.py` PR (pre-delete verification required):**

Run these checks before deletion:

```bash
rg "validation_study|validation_service|trade_comparison" PythonDataService app Backend Frontend docs
rg "include_router\(.*validation" PythonDataService/app
```

- If results show only self-references, tests, and stale docs → **hard delete** `PythonDataService/app/routers/validation_study.py` and its exclusively-owned helpers (confirmed by static reference search per helper).
- If a real workflow exists → **do not delete**. Instead, promote it to a registered FastAPI router with auth, validation, smoke tests, and docs, **or** move it to `scripts/` as an explicit offline tool. "Hidden REPL tool under `app/routers/`" is not a permitted state.
- Root-level scratch/binary files (`crudops.sql`, `order_store.sql`, `247-Critical-feedback.md`, two stale `.docx`, `dependency-audit.xlsx`, `analysis-hardening-gap-report.docx`) follow the same default: hard-delete after a similar reference check.

**Manual migration PR (after Phase 12 manual ships):**

Run this check first:

```bash
rg "broker-user-manual|Broker & Live Trading|broker manual" docs Frontend Backend PythonDataService
```

- Migrate any unique safety/checklist/troubleshooting content from `docs/broker-user-manual.html` and `.pdf` into `docs/operator-architecture-and-runbook.md`.
- Then **hard-delete** the HTML and PDF artifacts. Do not archive by default.
- Update or remove any active link that points to the deleted artifacts (the reference check above identifies them).

**Acceptance criteria:**

- High-confidence deletions have no route/import/test references.
- `validation_study.py` is hard-deleted OR promoted to a registered tool with tests — not left as an unregistered router.
- One canonical manual remains: `docs/operator-architecture-and-runbook.md`.
- No active docs or UI link to deleted manual artifacts.
- No unique safety/operator content is lost; if content matters, it is migrated first.
- Future unregistered routers are detectable by a small static check (`rg "@router\|APIRouter" app/routers/ | rg -v include_router` or equivalent).

### Phase 12 — Canonical Operator Manual

**Finding/PRD source:** original VCR research PRD §6.3; research report Stage G.

**Purpose:** Replace fragmented/stale manual content with one text-only canonical developer/operator guide.

**Create:**

- `docs/operator-architecture-and-runbook.md`

**Content requirements:**

- Paper-trading-only language unless live-money support is independently validated.
- Current fixed behavior after Phases 1-10.
- ADR 0009 sizing as current behavior.
- ADR 0008 restart/reconcile status:
  - if Phase 5 complete: document durable restart safety.
  - if not complete: document manual reconciliation requirement and warning.
- Deployment flow.
- Strategy catalog and exact deploy keys.
- EMA crossover exact behavior: 15-minute bars, EMA(5)/EMA(10), RSI(14) gate, 75-minute hold.
- Broker cockpit states and commands.
- Sizing policies and audit trail.
- Pre-flight checklist.
- Emergency procedures.
- Troubleshooting.
- Evidence appendix.

**Acceptance criteria:**

- Unique content from old broker manual is migrated or explicitly discarded.
- Old manual artifacts are no longer treated as current.
- Manual distinguishes shipped behavior from known gaps.

## 6. Cross-Phase Test Strategy

Each remediation PR must include targeted tests. Do not rely on full-suite green alone.

Minimum test set by area:

- Python live deploy/request tests for schema and ledger identity.
- Python live engine tests for sizing policy, account mismatch, command semantics, and start gates.
- Python broker adapter/fake broker tests for cancel-before-liquidate and durable-submit flows.
- Frontend Vitest tests for deploy strategy keys, provenance wording, paper-mode verdict, readiness labels, and command labels.
- Backend tests for removal/migration of stale Greek fields.
- Static doc consistency checks where cheap.

No test should assert current unsafe behavior unless the test name states it is legacy-only and the tested path is blocked for new runs.

## 7. Rollout Plan

Recommended PR order:

1. Phase 0 re-grounding doc-only.
2. Phase 1 explicit sizing policy.
3. Phase 2 strategy-key contract.
4. Phase 3 account identity check.
5. Phase 4 ADR 0008 UI mitigation.
6. Phase 6 immediate command semantics that do not depend on ADR 0008.
7. Phase 5A-5E durable-submit wiring.
8. Phase 8 sizing WAL event, coordinated with Phase 5 intent identity.
9. Phase 7 UI/provenance truth.
10. Phase 9 portfolio valuation Greek cleanup.
11. Phase 10 governance docs.
12. Phase 11 dead-code cleanup.
13. Phase 12 canonical manual.

If bandwidth is constrained, ship PRs 1-5 first. That closes the highest-risk current operator paths before the larger durable-submit migration.

## 8. Open Decisions

1. **Resolved:** legacy pre-policy ledgers are view-only / redeploy-only. No explicit override flag. Rationale: sizing is hashed into `run_id`; allowing start-time effective-sizing changes without rewriting `live_config` violates ADR 0006 identity and reintroduces the VCR-0001 back door. Cost is bounded because live deployment is young and legacy ledgers are finite.
2. **Resolved:** module-name is the canonical strategy key on every surface (API value, ledger `strategy_key`, runner `--strategy`, Python module path). No separate `module_name` field — that indirection is the bug shape VCR-0004 came from. `StrategyRegistration` gains an explicit `class_name: str`, retiring the `<PascalKey>Algorithm` convention and the `DeploymentValidationAlgorithm` alias. Future class renames do not affect `run_id` identity unless the module/key changes.
3. **Resolved:** "Flatten and pause" is the v1 panic-button contract — write `desired_state=PAUSED`, then enqueue one-shot `FLATTEN_NOW`. `FLATTEN_NOW` stays pure (does not mutate desired state); the UI endpoint composes the two primitives. True FLATTEN-keep-running and `STOP_AND_FLATTEN` rejected. (Phase 6.)
4. **Resolved:** hard-delete `NetDelta/NetGamma/NetTheta/NetVega` from `PortfolioValuation` + `PortfolioSnapshot` (and drop the four DB columns). Deprecate-null rejected (carries dead schema forever); `EntryNetDelta` rename rejected (aggregate entry-Greeks are not a useful portfolio concept). QuantLib live-Greek path is explicitly out of scope. (Phase 9.)
5. **Resolved:** hard-delete `validation_study.py` (and its exclusively-owned helpers) after a static reference check confirms no active consumer. Exception: if a real workflow exists, promote/register it properly (auth, tests, docs) or move to `scripts/`. No `docs/archive/` move; "hidden REPL tool under `app/routers/`" is not a permitted state. (Phase 11.)
6. **Resolved:** hard-delete `docs/broker-user-manual.html` and `.pdf` after Phase 12 ships and after migrating any unique safety/checklist/troubleshooting content. Git history is the archive; no `docs/archive/` move. (Phase 11 / Phase 12.)

## 9. Done Definition

This remediation program is complete when:

- New deploys cannot omit explicit sizing.
- Every broker dropdown strategy either starts or is disabled with a truthful reason.
- Ledger account identity is verified against broker-connected account.
- Runtime UI no longer claims RECONCILE/FLATTEN/paper/QC states that are not backed by code.
- Durable-submit/restart safety either fully gates production order flow or is clearly bannered as not yet wired.
- Sizing audit events are durable and intent-keyed.
- Stale entry Greeks are not exposed as current portfolio Greeks.
- Governance docs agree with code.
- High-confidence dead code is removed or archived.
- One canonical operator manual exists and the old broker manual is no longer current.
