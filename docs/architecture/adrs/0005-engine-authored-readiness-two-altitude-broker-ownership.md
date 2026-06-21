# ADR 0005 — "Can this strategy act on the next bar?" is engine-authored; broker-observed ownership is namespace-keyed and lives at two altitudes

**Status:** Accepted 2026-05-30
**Decision drivers:** The operator console must answer one headline question — *"is this strategy allowed and able to act on the next bar?"* — and must reconcile artifact-derived intended state against broker-observed account reality. Done naively, both invite a second, drifting implementation of logic the engine already owns.
**Related:** ADR 0002 (shadow per-instance namespace + no-submit invariant), ADR 0004 (instance-addressed control plane), `CONTEXT.md`, `docs/ibkr-paper-deployment-plan.md` § 16, `.claude/rules/numerical-rigor.md` (single source of truth).

## Context

The engine's readiness machinery is real but **scattered and unsurfaced**:

- **Pre-flight gates** (`pre_flight.py`, once per session): clean-tree, NTP offset, unexpected-position, run-state-intact — each a `CheckResult(passed, reason)`.
- **Per-bar guards** (in-loop, `live_engine.py`): `readonly`, `max_orders_per_day` cap, force-flat window, session boundary, paused.
- **No consolidated readiness artifact.** `decisions.parquet` records `signal/intended_action/mode` but no structured blocked-reason. Decisively, **`max_orders_used` and the live pause state exist only in the engine's loop** — the backend cannot compute an honest "can act on the next bar" without the engine telling it.

That last fact forces the authorship question. If the status endpoint *recomputes* readiness from artifacts, it becomes a parallel implementation of the engine's guard logic that will eventually disagree with what the bot actually does — the exact failure `numerical-rigor.md` forbids for math, applied to operator state.

The second problem is broker-observed state. IBKR reports **positions at the account level, net across instances**; only orders and executions are namespace-queryable (`cold_start_reconciler.py`: `open_orders_by_namespace`, `executions_for_namespace`). The shipped `check_unexpected_position` (`pre_flight.py:210`) compares a whole-account snapshot against a single strategy's symbol — so two managed instances on one account make each flag the other's positions as false contamination. The naive "expected position" comparison and the naive "is the account clean" comparison are **different questions at different altitudes** and cannot share an authority.

## Decision

### Readiness is engine-authored; the backend transports; the UI renders

- **Live-readiness** is emitted by the engine — the *same runtime path that enforces the gates* — every tick into the engine-authored status surface. The backend transports it verbatim; the UI renders it. The UI never recomputes readiness.
- **Start-readiness** (dead instances, no engine to ask) is **backend-derived** from durable artifacts only (`desired_state`, halt/poison sentinels, hydrate availability, latest reconcile receipt) and is **labeled `start_readiness`, not `live_readiness`**.

Shape — a structured vector, never a boolean:

```
{ kind: "live_readiness" | "start_readiness",
  as_of_ms, source: "engine" | "backend_derived",
  verdict, summary,
  gates: [{ name, status: pass|fail|unknown, severity: hard|soft, detail }],
  live_readiness_available?: false }   # start_readiness only
```

Verdict rules: `READY` (all hard pass, no material soft warnings); `BLOCKED` (≥1 hard fails); `DEGRADED` (hard pass, soft warn/unknown); `UNKNOWN` (no authoritative source).

Gate inputs: `desired_state`, `broker_connection`, namespace-scoped unexpected-position, submission mode, `orders_cap` (used/cap), hydrate result, latest reconcile pass/fail, prior-day halt/poison sentinel, session/force-flat window, and **`data_provenance`** — a *soft* gate that warns (→ DEGRADED) when the latest decision's `bar_source` differs from the spec's expected primary (e.g. expected `ibkr_realtime`, latest used `polygon_backfill`); BLOCKED only if a spec explicitly disallows fallback data.

### Broker ownership is namespace-keyed, reconstructed from the execution trail

Ownership is keyed on **`bot_order_namespace`**. Per-instance owned position is reconstructed from the **namespace-attributed order/execution trail** (the engine's `expected_position_by_symbol` running tally, cross-checkable against `executions_for_namespace`), **not** decomposed from the raw account-position snapshot. The account snapshot is net reality; it is not an ownership ledger.

### Two altitudes, two authors

- **Instance altitude (engine-authored):** the instance console shows the instance's namespace-attributed broker slice — its orders, its owned position, its pending orders, its order cap, its desired/pause state, its artifact-flush state, its **Layer-A execution divergence** — beside artifact-derived intended state. The instance broker gate is **self-consistency only**: *my* expected vs *my* attributed fills. It never reads the whole account.
- **Fleet altitude (backend-authored):** the account overview shows net position, explained-by-instance buckets, the **residual/unattributed bucket** (`residual = broker_account_position − Σ instance_expected_positions`), and the **account-contamination verdict**. This is the *only* readiness signal legitimately authored by the backend, because no single engine can see sibling namespaces. It is not a parallel implementation of engine readiness — it is the sole author of the cross-instance view.

Fleet contamination is surfaced on the instance page as an **inherited banner**, never folded into the engine's readiness vector. It does **not** silently block an executing strategy's own readiness unless an explicit **fleet policy gate** ("dirty account blocks all starts") says so — and that gate stays visibly separate from engine readiness. Example: *"Account residual detected: DEGRADED — SPY +37 shares unattributed outside managed namespaces. Instance readiness remains READY, but account is dirty."*

### Severity matrix

- `live_paper` self-consistency divergence (my expected vs my attributed) → **BLOCKED**.
- `shadow` broker exposure *in its namespace* → **BLOCKED / poisoned** (violates the ADR 0002 no-submit invariant).
- `shadow` / sibling positions outside my namespace → a **fleet** concern, inherited `DEGRADED` / `not_applicable`; never a per-instance self-consistency BLOCK.
- dead-instance start-readiness with unknown broker state → **UNKNOWN/DEGRADED**, unless start would submit orders immediately.

### In-scope correctness fix

`check_unexpected_position` must become **namespace-aware** so sibling managed instances are not flagged as false contamination. Treated as a **P0 correctness bug** in this work, not a separate follow-up: false contamination blocks valid starts and trains operators to bypass a noisy gate.

## Consequences

**Positive:**
- The operator console shows *exactly* what the bot enforces — no parallel truth, no confident-but-wrong red/green.
- The dead-instance case is answered honestly as a distinct, labeled `start_readiness`.
- The two-altitude split resolves the multi-instance position paradox: account net is decomposed as `Σ` instance expecteds `+` residual; no instance sees a sibling's position as contamination; only the residual is contamination.

**Negative:**
- New engine emission plumbing: the scattered pre-flight and per-bar guards must be consolidated into one evaluated readiness vector emitted each tick.
- A backend fleet aggregator must read all instance sidecars `+` one account-position snapshot to compute residual and the contamination verdict.
- `check_unexpected_position` and its `run.py` callers gain a namespace parameter and the set of sibling-managed namespaces.

**Non-consequences:**
- Mixed-source `bar_source` *history* is not built now; latest value `+` degraded-on-mismatch is sufficient. History stays in artifact inspection / reconciliation.
- The console renders no hardcoded indicator names; strategy-state descriptors (source of truth: the spec / `resolve_decision_columns`) are delivered via the status payload. (Mechanism, not a load-bearing decision — recorded in `CONTEXT.md`.)

## References

- `PythonDataService/app/engine/live/live_engine.py` — gains the consolidated readiness emission.
- `PythonDataService/app/engine/live/pre_flight.py:210` — `check_unexpected_position` made namespace-aware (P0).
- `PythonDataService/app/engine/live/live_state_sidecar.py` — `expected_position_by_symbol`, `bot_order_namespace` (ownership inputs).
- `PythonDataService/app/engine/live/cold_start_reconciler.py` — namespace-scoped broker queries (attribution mechanism).
- `PythonDataService/app/routers/live_runs.py` — transports live-readiness; computes start-readiness + fleet contamination.
- `CONTEXT.md` — readiness + broker-ownership glossary.

---

## Amendment 2026-06-20 — PRD #616 operator-surface projection layer

**Status:** Amended 2026-06-20 alongside PRD #616 cockpit-redesign work. The original ADR established the engine readiness sidecar (`ReadinessVector` / `ReadinessGate`) and the two altitudes (instance + fleet). It did not name the *projection layer* the cockpit consumes. This amendment records that layer and the cross-altitude separation of account identity from position contamination.

### B1. `operator_surface` is the Python-authored projection layer

The engine readiness sidecar's contract (gate `{name, status, severity, detail}`) is unchanged. The cockpit consumes a *projection* of that contract on `operator_surface.readiness_gates` — each `OperatorGate` carries the engine fields plus `suggested_action: GateSuggestedAction | None` and `suggested_action_unavailable_reason: str | None`. The projection lives in `app/services/operator_surface.py`; the engine sidecar carries no UI navigation concepts (cockpit tab names, dispatch verbs, button identifiers).

The projection authority rule is asymmetric on purpose:

- The **engine** authors gates against bar-loop runtime conditions; it does not know about cockpit affordances.
- The **operator-surface projection** maps gate evidence to operator-language remediation hints and authors the structured `suggested_action` union the cockpit dispatches.
- The **cockpit** renders the hints, dispatches the actions, and never re-derives the suggestion from `gate.name`.

This separation keeps the engine's structural contract small (the sidecar shape does not change when a new cockpit tab is named) while keeping the cockpit's behaviour data-driven (a server-authored gate → suggested-action mapping change does not require a Frontend change).

### B2. `OperatorGate.suggested_action` is a closed discriminated union

The shape is a closed `kind` union (`invoke_capability` | `focus_action` | `redeploy` | `open_runbook`); see ADR 0013 for the inclusion rationale and PRD #616 for the field definitions. Either `suggested_action` is a valid descriptor, or it is `null` AND `suggested_action_unavailable_reason` is populated with a documented rationale code — `null` alone is never ambiguous. An unknown gate name fails closed visibly (the projection returns `suggested_action=null, suggested_action_unavailable_reason="UNKNOWN_GATE_NAME"`); the cockpit renders the raw gate name and the unavailable reason rather than guessing a remediation.

Destructive actions (Mark Poisoned, Stop, Flatten-and-pause) never appear inline via `invoke_capability` — they reach the operator only through `focus_action`, which navigates to the canonical render site (PRD #617). The shape of the union enforces this rule structurally rather than via prose.

### B3. Account identity is a separate altitude from position contamination

The fleet altitude now ships `FleetAccountSummary { account_id, account_identity, account_identity_reason_codes, contamination: FleetContamination }`. Account identity (`CONSISTENT` / `CONFLICTING` / `UNKNOWN` with reason codes from a closed vocabulary — `ACCOUNT_ID_MISSING`, `INSTANCE_ACCOUNT_MISMATCH`, `BROKER_ACCOUNT_UNAVAILABLE`, `BROKER_ACCOUNT_MISMATCH`) is **separate from position contamination**:

- A `CONFLICTING` account identity does NOT raise the contamination verdict.
- A `contaminated` position verdict does NOT raise the account-identity verdict.
- The cockpit's account-row attention formula reads `account_identity !== 'CONSISTENT' || contamination.verdict !== 'clean' || contamination.policy_blocks_starts`; the formula is stable so that future policy semantics (`policy_blocks_starts`) do not require an Angular change.

This separation closes the original two-altitude split's gap: position contamination is the *engine's* fleet authority (engines see net broker minus their attributed positions); account identity is the *configuration* author's authority (every managed instance must agree on the account, and that account must match the broker session). Conflating the two — as a "verdict" overloaded with identity disagreement — would be a category error.

### B4. `LiveInstanceSummary` carries the readiness verdict

The fleet overview row now carries `readiness_verdict` (`READY` / `BLOCKED` / `DEGRADED` / `UNKNOWN`) and `readiness_as_of_ms`, authored from the same readiness source as the per-instance status. This lets the cockpit render the outer-tab badge (`dep_val_smoke_001 · IDLE · BLOCKED`) without an N+1 fetch of every instance's full status. The new fields are additive; consumers that ignore them see no change.

### B5. References

- `PythonDataService/app/services/operator_surface.py` — owns the `OperatorGate` projection, the reactive `_project_broker`, and the real `_project_trading_session.next_transition_ms` computation.
- `PythonDataService/app/services/operator_capability.py` — shared capability evaluator consuming `ResumeGuardState` (see ADR 0010 §Amendment 2026-06-20).
- `PythonDataService/app/engine/live/fleet.py` — `compute_fleet_account_summary` composes contamination with identity.
- `docs/architecture/adrs/0013-operator-surface-judgment-vs-evidence.md` — the operator-surface inclusion boundary (judgment vs evidence).

