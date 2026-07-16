# Reconciliation Spine — Account Observation Lease (rev 3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make account reconciliation the spine of the bot lifecycle by promoting the existing 15-second Account Truth observation into a named, durable, account-scoped **Account Observation Lease** — renewable, fail-closed, revoked immediately on explicit bad evidence — while start-time (run-scoped) reconciliation stays automatic and usually invisible.

**Architecture:** Almost all machinery already exists: `AccountTruthRefreshLoop` sweeps the broker every 15 s with an observer hook (`main.py:194`), `assess_account_truth()` is the observation-grade verdict (fresh + sources present + fully attributed; **owned positions are clean**), the submit gate already chains freeze → registry → truth (`live_portfolio.py:~992`), and run-scoped cold-start/runtime reconciliation already gates the bar loop. The delta is small: persist the observation verdict as one durable account-scoped lease artifact, renew it on every clean quiet sweep, revoke it immediately on any material change, make Start and submit consume it through one atomic authority selector, and surface it. Every lease consumer must also prove its expected account ID equals the lease account ID. Schema v2 is fenced by the account Clerk generation; v1 owner-keyed leases fail closed and cannot contribute cutover evidence. **No new scheduler. No per-instance lease files. No new verdict math. No new tuning constants** (lease validity = the existing 60 s readiness TTL, whose `interval×2 < TTL` margin is already enforced by `validate_account_truth_refresh_cadence`).

## Rev 3 implementation boundary

- The lease schema is v2 and carries `clerk_generation`; the refresh observer requires a matching, unexpired `RUNNING` Clerk lease and captures the accepting Clerk generation before and after broker observation. It revokes with `ACCOUNT_CLERK_GENERATION_CHANGED` when that authority is absent, expired, draining, or changes across the sweep. A durable generation file alone never authorizes renewal because it can outlive a crashed Clerk.
- Shadow-comparison rows are versioned. Legacy owner-keyed rows are reported separately and excluded from promotion rather than silently reused. The three-session parity clock therefore restarts for Clerk-keyed evidence.
- `IBKR_ACCOUNT_GATE_AUTHORITY` is one process-wide selector for both Start and submit. It defaults to `account_truth`; `observation_lease` is implemented but must not be enabled until the versioned parity gate and Clerk-restart HITL smoke pass.
- Start and submit both fail closed on an absent, malformed, expired, revoked, or generation-mismatched lease after that switch. There is no per-bot override and therefore no mixed authority inside one deployment.
- The zero-production-caller `advance_account_owner_generation` writer and `AccountOwner.record_accepting_generation` entry point are deleted. Still-live legacy owner-generation readers remain until their Clerk replacements are separately characterized; this document does not relabel active safety code as dead.

**Tech Stack:** Python 3.11 / FastAPI / Pydantic v2, existing OperatorSurface SSE contract, Angular 21 signals, pytest + Vitest.

## Rev 2 changelog — what the review verification changed

Codex review claims were verified against code before revising. Seven confirmed, one rejected, one confirmed-with-different-conclusion:

| # | Claim | Verdict | Evidence |
|---|---|---|---|
| 1 | Receipt `CLEAN` requires flat, so renewing from it blocks intraday trading | **Confirmed — was a blocking flaw** | `compose_receipt` requires `exposure_resolved` (`account_reconciliation.py:250-254`); `_exposure_resolution` marks any non-zero position `unresolved` (`:745-752`) |
| 2 | Lease must be account-scoped, not per-instance | **Confirmed** | Authority doc §"Account-Level Reconciliation Receipt": "not a second account-clean engine"; per-instance fan-out invites partial-write states; run binding already exists (registry + run receipt) |
| 3 | Cold-start reconciliation is run-scoped and cannot mint an account lease | **Confirmed** — rev 1's D2/D3 contradicted each other | `reconciliation_orchestrator.py` folds WAL/sidecar/namespaces; produces no account-wide receipt |
| 4 | 180 s lease is weaker than the current gate; parity proof false | **Confirmed** | `assess_account_truth` blocks immediately on failed refresh (`account_truth_snapshot.py:180`) and at 60 s staleness (`:194`) |
| 5 | A third scheduler duplicates existing loops | **Confirmed** | `DEFAULT_ACCOUNT_TRUTH_REFRESH_INTERVAL_MS = 15_000` (`account_truth_refresh.py:31`); loop started with observer in `main.py:194-198`; child loops exist per authority doc |
| 6 | Rev 1 Slice 1 not independently shippable (gate without renewal) | **Confirmed** | Enforcement now ships only after shadow-mode renewal is proven (Slice 3) |
| 7 | ON_DUTY persisted too early; backend must reorder | **Rejected — see Decision D6** | `set_phase(ON_DUTY, reason="start_accepted")` (`live_instances.py:~3016`) is presence-honest per ADR-0026 §2 ("phase is presence"); trading permission is independently gated (child refuses the bar loop until run reconciliation passes; submits chain freeze/registry/truth) |
| 8 | Crashed-sibling trust leak already fixed; rev 1 Slice 3.1 built a second fix | **Confirmed — Slice 3.1 deleted** | Boot retire: `retire_unmanaged_active_bindings_on_daemon_boot` (`account_registry.py:261`); post-boot reaper retires exited bindings (`host_daemon.py:~1370`); projection computes dead → OFF_DUTY with drift flag (`bot_daily_lifecycle.py:72-77`) |
| 9 | Assumed behaviors don't exist (auto-freeze on NOT_PROVEN; shared CleanExitReceipt; probe-fail = wait; instance reconcile endpoint = account cure) | **All confirmed** | `write_account_freeze` callers: watchdog + restart-intensity only; `CleanExitReceipt` zero hits in `app/`; probe failure poisons (`reconciliation_orchestrator.py:212-224`, deliberate); `/live-instances/{sid}/reconcile` is run-scoped RECONCILE command, 409 without live binding (`live_instances.py:5335`) |

## Global Constraints

- Every temporal value is `int64 ms UTC`; display via `timestampDisplay` pipe / `fmtDurationRemaining`.
- Single-writer evaluator: lease writes are **evidence**, never phase (ADR-0026 §4).
- Closed enums stay closed: lease problems map to existing `evidence_stale` (cure `reconcile_now`) and the existing Account Truth reason codes.
- Frontend renders backend-authored verdicts verbatim (ADR-0013); "lease" never appears in trader copy — "Account verified / Account check overdue".
- Account Truth remains **the sole owner of clean vs not-proven** (authority doc). The lease persists that verdict; it never re-derives cleanliness.
- Structured logging only; project-scope lint + tests before push; thermo review before first push; `podman restart polygon-data-service` before trusting live behavior; OperatorSurface changes update `status_payload_parity.json` in the same commit.

---

## Design record

### The three-proof ladder

| Proof | Scope | Question | Status |
|---|---|---|---|
| Daemon lease (`control_plane.py`, 1 Hz / 5 s) | Control process | Is the control-plane daemon alive? | Exists — unchanged |
| **Account Observation Lease** (this plan) | Broker account | Is current account state fresh, account-matched, fully attributed, and generation-consistent? **Owned exposure allowed.** | New artifact over existing verdict |
| Run reconciliation receipt (`reconciliation_orchestrator.py`) | Bot run | Does this run's WAL/projection agree with broker-owned activity? | Exists — unchanged |

### Two verdict meanings — already structurally present, now named

- **Observation proof** = `assess_account_truth(...) == pass`: fresh (60 s TTL), refresh succeeded, account-matched, critical sources fresh, `final_verdict == "clean"` (attribution + invariants; non-zero positions owned by an active bot are allowed, and terminal facts from retired bots remain attributable). A current position or working order whose only owner is retired is instead the distinct blocking condition `retired_owner_live_exposure`: known rather than foreign, but unmanaged. This is what the submit gate consumes today and what the lease persists.
- **Recovery proof** = `AccountReconciliationReceipt.state == "CLEAN"`: observation proof **plus flat/accepted-override**. Stays exactly where it is: freeze clearing and flat-start requirements (ADR-0026 §5 same-day restarts start flat). It never gates intraday renewal.

### D1 — The lease is the existing verdict made durable, account-scoped, and named

`accounts/<account_id>/account_observation_lease.json`, written by the **existing** refresh-loop observer (`AccountReconciliationService.observe_account_truth`, already invoked every sweep). A passing assessment renews (`status=VERIFIED`, `valid_until_ms = observed_at_ms + DEFAULT_ACCOUNT_TRUTH_READINESS_TTL_MS`); a blocking assessment **revokes immediately** with the assessment's own reason code. Expiry (`now ≥ valid_until_ms` with no revocation) covers exactly one failure mode: the observer itself silently died. Never held past bad evidence merely because a timer hasn't expired.

### D2 — Immediate revocation triggers and Clerk-generation fencing (supersedes rev 1's TTL-decay-only model)

The refresh loop must notify the lease writer on **both** a completed observation and every unavailable/error path; its success-only observer is not sufficient. Revoke on the sweep that observes any of: refresh failure / broker disconnect (`AccountTruthUnavailable`), connected-account mismatch, critical source staleness, `final_verdict != "clean"` (foreign/unattributed activity — this covers new foreign executions, foreign orders, unexplained position deltas, because Account Truth's verdict already folds them), active account freeze, or a Clerk-generation transition. All reason codes come from the existing `assess_account_truth` taxonomy plus `ACCOUNT_CLERK_GENERATION_CHANGED` and `ACCOUNT_FROZEN`.

Renewal is a fence, not a field comparison: require a matching, unexpired `RUNNING` Clerk lease and read the Account Clerk generation and accepting phase as `g0`, collect and assess the broker evidence, then require the same active authority as `g1`; renew only when `g0 == g1`. An absent, expired, draining, changed, or non-accepting Clerk authority revokes the lease. A later clean sweep for the new, stable accepting generation may renew it. Consumers repeat the active-Clerk check, preventing a clean observation from one Clerk authority—or a generation file left by a dead Clerk—authorizing another.

### D3 — No new scheduler; no per-instance files

The lease writer is a ~40-line extension of `observe_account_truth()`. Rev 1's `SessionLeaseTicker` and `live_state/<sid>/session_lease.json` are **not built**. Per-bot facts stay where they already live: registry binding (ACTIVE/RETIRED), run reconciliation receipt, run status.

### D4 — Start consumes the lease; run reconciliation stays; neither substitutes for the other

Start requires: account lease VERIFIED-and-fresh (new gate row in deploy preflight + `_assert_start_allowed`) **and** the existing run-scoped cold-start reconciliation passing inside the child before the bar loop. Where ADR-0026 requires a flat start, Start must additionally require a fresh `CLEAN` recovery receipt (or run the existing account recovery sweep automatically to mint one); observation proof alone intentionally permits owned exposure. Probe failure at cold start keeps poisoning (verified deliberate: a failed probe cannot be distinguished from hidden divergence).

### D5 — Submit gate: strengthen, never weaken; retire only after sequence parity

The existing chain (freeze → registry → truth → session authority) stays. The lease enters **shadow mode** first: assessed and logged next to the live gate, divergences counted. Cutover adds the lease as one more provider only when shadow shows it is at least as strict in every observed sequence (event-sequence parity tests — recorded sweep sequences replayed against both gates — not Cartesian state tables). The raw in-process truth check is retired only then, and only because the lease is the same verdict persisted (plus generation pinning). The submit chain retains a lightweight **child-local broker-connection gate**: the account lease is observed through the main process's IBKR client and cannot prove the child submitter's client is connected. The child-run refresh loops (`run.py:~2381`) are a **later** consolidation candidate only after the account lease, current owner-generation fence, run receipt, and child-local connection gate are all enforced.

### D6 — Pushback: ON_DUTY stays presence-based; the ADR gets a clarifying amendment

ADR-0026 §2's own principle is "phase is presence, health is derived", and the implementation follows it (`_observed_phase`: running → ON_DUTY). The table's ON_DUTY entry ("fresh lease → new run_id") reads as permission-based — a genuine internal tension in the ADR, surfaced here per the authority-conflict rule. Recommendation: keep `set_phase(ON_DUTY, reason="start_accepted")`; the verification window is already trade-safe (no bar loop, no submit path before the run receipt passes; a poisoned child exits → evaluator observes dead run → OFF_DUTY + condition — presence stayed honest throughout). Amend ADR-0026 to state: ON_DUTY = presence; **trading permission = account lease + run receipt + submit chain**, orthogonal to phase. Reordering the backend persist point would add async machinery for zero safety gain. If the operator prefers the table's literal reading instead, that is a one-decision change to Slice 3 — flagged, not silently picked.

### D7 — History on transitions, not heartbeats

`account_events.jsonl` records lease **transitions** (VERIFIED↔REVOKED, distinct revocation reasons, expiry) — never one event per successful renewal. A healthy day appends zero lease events.

### UX contract (unchanged in spirit from rev 1, corrected in plumbing)

| Surface | Healthy (invisible) | Unhealthy (one way out) |
|---|---|---|
| Verdict card | One proof line: "Account verified · 12s ago" (+ client-formatted countdown from `valid_until_ms`; cosmetic only — state words arrive only via SSE) | REVOKED/expired while ON_DUTY → critical banner from the lease's backend-authored reason, one button **Reconcile now** → `POST /api/accounts/{id}/reconciliation` (the **account** endpoint; the instance `/reconcile` endpoint is run-scoped and is not repurposed) |
| Bots page | Nothing new | `evidence_stale` flows through the existing sick-bay pipeline |
| Start flow | "Verifying account" phase in the existing startup-automation progress; flows through silently | Blocked phase renders the OperatorBlocker: lease revoked → `fix_here` (Reconcile now); cold-start poison → existing poison handling (terminal-flavored, Retire & Replace) — matching actual orchestrator behavior, not rev 1's fictional "wait" |
| Account monitor | Already shows last-reconciled + countdown — repoint at the lease | Observation history strip renders **transitions** from `account_events.jsonl`, not per-minute dots |

### Machinery disposition (the fluff-cutting ledger)

**Keep (not duplicates):** daemon lease + child watchdog; Account Truth composition + 15 s refresh loop + backoff/jitter; readiness cache + `assess_account_truth`; recovery receipt + freeze/clear guard (flatness home); run cold-start + runtime reconciliation; boot retire + post-boot reaper + drift-flagged projection (trust leak: already healed — pin with a characterization test, build nothing).

**Build:** the lease artifact + writer hook + assessment/gate + start-gate row + surface projection + UX strip. That is the whole build.

**Do not build (rev 1 deletions):** `SessionLeaseTicker`; per-instance `session_lease.json`; lease-based trust-leak fix; clean-exit lease release (no shared clean-exit procedure exists yet — exit-time lease semantics belong to PRD #974's clean-exit slice on `codex/add-account-freeze-clear`, referenced, not duplicated); the false receipt-TTL parity argument.

**Retire (in this plan, after their replacement is proven):** the in-process raw truth check at the submit gate (Slice 3, post-parity — the lease is the same verdict, durable, plus generation pinning); the 5-minute receipt-expiry-while-sweeps-are-clean oddity (subsumed: observation freshness lives in the lease; the receipt TTL now only bounds *recovery* proofs, its actual job).

**Candidates to retire later (flagged, separate decisions):** auto-reconciliation policy toggle (continuous lease renewal may make the opt-in auto-receipt-replacement redundant — decide after Slice 2 ships); child-run private Account Truth sweeps (D5).

---

# Slice 1 — Semantics and characterization (no behavior change)

Deliverable: the two verdicts are named in code and tests pin today's actual behavior, including the scenarios the design must not regress.

### Task 1.1: Characterization tests for the observation verdict

**Files:**
- Test: `PythonDataService/tests/services/test_account_truth_snapshot.py` (extend)

Pin with tests: build the owned and foreign position cases through `compose_account_truth` using real binding/order-or-execution evidence — do **not** hand-author `final_verdict="clean"`; owned non-zero position + clean verdict → `assess_account_truth` **passes** (the intraday-exposure case rev 1 would have broken); foreign/unattributed activity → blocks with `ACCOUNT_TRUTH_NOT_PROVEN`; `AccountTruthUnavailable` → immediate block; 61 s-old snapshot → `ACCOUNT_TRUTH_STALE`; connected-account mismatch inside the truth payload → blocks.

- [x] Write the tests; local `.venv/bin/python -m pytest tests/services/test_account_truth_snapshot.py -q` passes (32 passed) with zero behavior edits. The prescribed `podman exec polygon-data-service ...` command is presently blocked because that service image has no `pytest`; this is an environment defect, not a test failure.
- [x] Commit: `test(account-truth): characterize observation-grade verdict incl. owned-exposure pass`

### Task 1.2: Characterization test for the healed trust leak

**Files:**
- Test: `PythonDataService/tests/engine/live/test_host_daemon_boot_reconcile.py` + `tests/services/test_bot_daily_lifecycle.py` (extend)

One end-to-end-shaped test per mechanism: prior-boot ACTIVE binding + not in `managed_run_ids` → boot reconcile retires it; dead process + persisted ON_DUTY → projection computes OFF_DUTY with `drift_detected=True`. If a residual stale-phase path exists, this task finds it and it becomes a bug fix with its own regression test — not lease work.

- [x] Existing characterization covers both cases: boot retires an unmanaged `ACTIVE` binding and the post-boot reaper demotes an exited managed child without a status read (`test_host_daemon_boot_reconcile.py`). No replacement machinery is needed.
- [x] Commit: `test(lifecycle): pin boot-retire and dead-process demotion (trust-leak characterization)`

### Task 1.3: Name the recovery/observation split in code

**Files:**
- Modify: `PythonDataService/app/services/account_reconciliation.py` (docstrings on `compose_receipt`/`write_receipt`: "recovery proof — observation proof plus flatness; never used for intraday renewal") and `account_truth_snapshot.py` (`assess_account_truth` docstring: "observation proof — owned exposure is clean")
- Modify: `docs/bot-lifecycle-account-owner-authority.md` (one paragraph naming the split; same-PR update rule)

- [x] Edit and run `ruff check` on the changed Python modules and test; all pass.
- [x] Commit: `docs: name observation-proof vs recovery-proof split at their definitions`

---

# Slice 2 — The lease, in shadow mode

Deliverable: the lease artifact exists, renews and revokes from the existing loop, is surfaced read-only, and changes **no** gate behavior.

### Task 2.1: `AccountObservationLease` schema + repo + assessment

**Files:**
- Create: `PythonDataService/app/engine/live/account_observation_lease.py`
- Test: `PythonDataService/tests/engine/live/test_account_observation_lease.py`

**Interfaces (produces):**

```python
class AccountObservationLease(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[2] = 2
    account_id: str
    status: Literal["VERIFIED", "REVOKED"]
    observed_at_ms: int              # backing AccountTruthResponse.generated_at_ms
    renewed_at_ms: int
    valid_until_ms: int              # observed_at_ms + DEFAULT_ACCOUNT_TRUTH_READINESS_TTL_MS
    clerk_generation: int | None = None
    truth_watermark: str                     # f"account_truth:{generated_at_ms}"
    revoked_reason_code: str | None = None   # assess_account_truth reason codes + ACCOUNT_CLERK_GENERATION_CHANGED + ACCOUNT_FROZEN
    revoked_detail: str | None = None


class AccountObservationLeaseAssessment(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    state: Literal["VERIFIED", "REVOKED", "EXPIRED", "ABSENT"]
    lease: AccountObservationLease | None = None
    reason_code: str
    reason: str                      # backend-authored trader copy


class AccountObservationLeaseRepo:               # accounts/<id>/account_observation_lease.json
    def read(self, account_id: str) -> AccountObservationLease | None: ...
    def renew(self, *, account_id: str, observed_at_ms: int, now_ms: int,
              clerk_generation: int | None) -> AccountObservationLease: ...
    def revoke(self, *, account_id: str, reason_code: str, detail: str, now_ms: int) -> AccountObservationLease: ...


def assess_account_observation_lease(root: Path, account_id: str, *, now_ms: int) -> AccountObservationLeaseAssessment: ...
def account_observation_lease_gate_result(assessment: AccountObservationLeaseAssessment) -> GateResult: ...  # gate_id="account.observation_lease"
```

Reader is fail-closed (missing/malformed/expired → not verified); writes use the existing `atomic_write_pydantic_artifact` from `account_artifacts.py`. Renewal validity reuses `DEFAULT_ACCOUNT_TRUTH_READINESS_TTL_MS` — zero new constants. `renew` on a REVOKED lease is legal (a clean sweep cures a revocation); the transition is what gets journaled (Task 2.2).

- [x] Failing tests implemented and passing locally: absent, renew/60 s TTL, immediate revoke, expiry boundary, malformed artifact, current Clerk-generation mismatch, legacy-v1 rejection, and gate mapping.
- [x] Commit: `feat(live): account observation lease — durable, fail-closed, immediately revocable`

### Task 2.2: Wire renewal/revocation into the existing observer

**Files:**
- Modify: `PythonDataService/app/services/account_reconciliation.py` (`observe_account_truth`, line ~141 — the hook the refresh loop already calls every sweep) and the refresh-failure path (`AccountTruthSnapshotProvider.mark_refresh_failed` caller in `account_truth_refresh.py`)
- Test: `tests/services/test_account_reconciliation.py` (extend)

Behavior per sweep: a refresh-loop outcome callback drives the writer on success **and** unavailable/error paths. For a success, require an active Clerk lease and read accepting Clerk generation `g0`, run `assess_account_truth` on the fresh evidence, then require active generation `g1`; pass plus `g0 == g1` → `renew`; missing/expired/draining/changed Clerk → `revoke(ACCOUNT_CLERK_GENERATION_CHANGED)`; block → `revoke` with the assessment's `primary_reason_code`. Refresh failure → `revoke(ACCOUNT_TRUTH_REFRESH_FAILED)`. Active freeze → `revoke(ACCOUNT_FROZEN)`. Journal only status transitions to `account_events.jsonl` (`account_observation_lease_verified` / `account_observation_lease_revoked` with reason). Quiet clean sweeps write the file, not the journal.

- [x] Implemented and locally covered: stable clean renewals, owner change during the sweep, transition-only journal behavior, foreign/unattributed revocation, clean recovery, and broker-unavailable callback revocation. The callback is wired for broker-error and unexpected-error paths as well.
- [x] Focused service and router coverage passes locally (42 tests).
- [x] Commit: `feat(live): refresh-loop observer renews/revokes the observation lease on every sweep`

### Task 2.3: Shadow comparison at the submit gate

**Files:**
- Modify: `PythonDataService/app/engine/live/live_portfolio.py` (beside the existing `account_truth_gate_provider` check, ~line 992: assess the lease, compare outcomes, `logger.warning` + counter on divergence — **decision still comes from the existing gate**)
- Test: live-portfolio submit-gate tests (extend: divergence logs, never blocks)

- [x] Shadow comparison implemented: every paired submit-boundary outcome is durably appended to the existing account event journal with run/instance identity; divergences also log and increment a process-local counter. The existing Account Truth gate remains the sole submit decision; focused test passes.
- [x] Commit: `feat(live): observation-lease shadow comparison at the submit gate`

### Task 2.4: Surface projection + cockpit proof line + account monitor repoint

**Files:**
- Modify: `PythonDataService/app/schemas/live_runs.py` (`OperatorSurfaceAccountObservation`, frozen: `state`, `reason_line`, `observed_at_ms`, `valid_until_ms`), `app/services/operator_surface.py` (assemble; HELD-equivalent adds proof line "Account verified"), `tests/surface_hub/status_payload_parity.json`
- Modify: `Frontend/.../verdict-card/verdict-card.component.ts` + spec (proof line + cosmetic countdown via `fmtDurationRemaining`; state words only from SSE)
- Modify: `Frontend/.../broker-account-monitor/broker-account-monitor.component.ts` + spec (existing account-monitor component reads the lease projection; observation-history strip renders journaled transitions with `@for` and `track event.recorded_at_ms`)
- Test: Vitest specs; `npx eslint Frontend/src/ --max-warnings 0`

- [x] Surface projection, verdict proof line, and transition-only Account Monitor history are implemented. Python safety/surface coverage (495 tests) and focused Angular lint/specs (42 tests) pass locally.
- [x] Commit: `feat(surface+frontend): account observation lease projection, proof line, transition history`

---

# Slice 3 — Enforcement cutover (atomic)

Deliverable: Start consumes the lease; the submit chain consumes the lease; the redundant raw check retires — all in one PR so production/enforcement never ship split.

### Task 3.1: Sequence parity evidence from shadow mode

- [x] The durable replay verifier is implemented in `app/services/observation_lease_parity.py` with `tests/services/test_observation_lease_parity.py`. It reads the canonical account journal, requires three distinct canonical-NYSE session dates, rejects malformed evidence and every truth=`block` / lease=`pass` pair, and lists lease-stricter pairs without failing the gate.
- [ ] Operational gate: collect version-2, Clerk-keyed submit-boundary rows from ≥ 3 paper-trading sessions with zero lease-weaker divergences, then replay the captured account journal and archive the resulting report before enabling enforcement. **Current replay (2026-07-16):** `comparison_count=4`, `legacy_comparison_count=69`, `observed_session_dates=["2026-07-16"]`, `invalid_comparisons=[]`, `lease_weaker_comparisons=[]`, and `cutover_ready=false`. The first qualifying v2 session is archived in `docs/audits/observation-lease-paper-session-2026-07-16.md`; two distinct NYSE sessions remain. This is sequence parity, not a Cartesian table.
- [x] Clerk-restart HITL smoke: generation mismatch returned `REVOKED / ACCOUNT_CLERK_GENERATION_CHANGED`, then the next clean sweep renewed a verified lease for the replacement generation. No bot or order was started. Evidence is in the 2026-07-16 audit.
- [x] Commit: `test(live): sequence-parity replay for observation lease vs raw truth gate`

### Task 3.2: Start consumes the lease

**Files:**
- Modify: `PythonDataService/app/services/deploy_preflight.py` (new gate row from `account_observation_lease_gate_result`) and the router-side `_assert_start_allowed` path in `live_instances.py`
- Test: deploy-preflight tests + `tests/routers/test_live_instances.py` start-gate cases

Start ordering stays: lease VERIFIED (account proof) → daemon accepts start → child runs run-scoped reconciliation → bar loop. Per D6, `set_phase(ON_DUTY, "start_accepted")` is unchanged; ADR amendment in Task 3.4 records the semantics.

- [x] Dormant cutover path implemented behind the default-off process-wide selector: absent/revoked lease blocks with `RECONCILE_NOW`; verified Clerk-keyed lease permits Start. The existing independent fleet, freeze, crash-recovery, session, and child run-reconciliation gates remain. **Current selector:** `IBKR_ACCOUNT_GATE_AUTHORITY=account_truth`; do not enable `observation_lease` while Task 3.1 remains unmet.

### Task 3.3: Submit cutover + retire the raw check

**Files:**
- Modify: `live_portfolio.py` (the `account_truth_gate_provider` slot now supplies the lease gate; shadow scaffold removed), `live_engine.py` (~914, provider wiring)
- Test: submit-gate tests updated to the lease gate, including that the child-local broker connection remains an independent hard block; parity replay suite from 3.1 stays green

- [x] Dormant lease-authority submit path implemented and fail-closed behind the same selector as Start; Account Truth remains the default authority and shadow comparison remains active. **Current selector:** `IBKR_ACCOUNT_GATE_AUTHORITY=account_truth`; no cutover action is authorized by the legacy rows.
- [ ] After the three-session v2 parity gate and Clerk-restart HITL smoke pass: set `IBKR_ACCOUNT_GATE_AUTHORITY=observation_lease`, restart the process, verify one paper session, then remove the raw truth fallback and selector in a separate rollback-safe cleanup.

### Task 3.4: ADR + authority updates

- [x] ADR-0026 amendment: ON_DUTY = presence; trading permission = observation lease + run receipt + submit chain; the "coarse background tick that reconciles drift" is realized by the existing 15 s observer writing the lease. ADR-0030 records that a v2 lease requires an accepting Clerk generation and matching active `RUNNING` Clerk lease. Auto-reconciliation-policy retirement remains an explicit later decision.
- [x] Commit: `docs: ADR-0026 amendment + authority update for the observation lease`

---

# Slice 4 — Start-flow UX polish

Deliverable: the invisible-when-healthy start experience.

### Task 4.1: "Verifying account" startup-automation phase

**Files:** `Frontend/.../bot-control-page.component.ts` + spec (phase label between roll-call prep and runtime proof, sourced from the preflight lease gate row; blocked state renders the row's backend-authored reason + Reconcile now routing to the **account** reconciliation endpoint via the account monitor cure path in `condition-cure-actions.ts`)

- [x] Start capability projects the selected observation-lease gate; healthy Start shows `Verifying account`, while a revoked lease renders the backend reason and one `Reconcile now` action routed to `POST /api/accounts/{id}/reconciliation` rather than the run-scoped reconcile endpoint. Backend and Vitest regressions cover both paths.

### Task 4.2: End-to-end verification pass

- [ ] `podman restart polygon-data-service`; deploy + start a validation bot; observe: proof line renders; `podman logs` shows lease renewals (file mtime advancing, journal silent); disconnect IBKR → lease REVOKED on next sweep (≤ 15 s), banner + Reconcile now, submits blocked; reconnect → self-heals on next clean sweep with one journaled transition pair. Open a position with the bot → lease stays VERIFIED (the rev 1 regression case).
- [ ] Project-scope gates (ruff, pytest sibling-container, eslint, ng test), baseline inherited failures, thermo review, push.

---

## Self-review notes

- The spine claim now matches reality: the "ping" was already a real account observation every 15 s; this plan makes its verdict durable, named, immediately revocable, and consumed by start + submit — rather than inventing a parallel one.
- Rev 1 defects closed: flat-CLEAN renewal (Slice 1.1 pins the fix's premise), per-instance scope, scheduler duplication, false parity, non-shippable enforcement, fictional freeze-raising, nonexistent clean-exit hook, misrouted cure endpoint.
- Open decisions surfaced, not silently picked: D6 (ON_DUTY presence semantics — recommendation given), auto-reconciliation-policy retirement, child-loop consolidation (D5).
