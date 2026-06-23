# PRD: Operator Notice Contract and Trader-Readable Failures

**Status**: Draft — ready for implementation
**Owner**: Inkant
**Created**: 2026-06-23
**Closes**: #656 (watchdog silent exit), #657 (raw freshness enum strings in cockpit), #658 (Activity tab stuck loading)
**Architectural anchor**: ADR-0015 (Operator Notice Contract) — ships in PR 1 of this initiative
**Related ADRs**: ADR-0013 (operator surface: judgment vs evidence), ADR-0014 (broker-authored operator view: backend-rendered narratives)

---

## 1. Problem

Three live-cockpit failure modes ship operational enum strings to traders, or worse, ship nothing while the bot silently does the wrong thing.

- **#657** — `OperatorSurfaceRuntimeFreshness.stale_reason_codes: list[str]` is rendered raw in the cockpit. The trader sees `BAR_LOOP_HEARTBEAT_STALE` and cannot tell whether the bot is safe, degraded, or unsafe.
- **#656** — When the host watchdog detects lease loss it exits the engine without a confirmed flatten. The cockpit sees a clean exit; broker exposure persists; no incident artifact survives the restart.
- **#658** — The Activity tab can render "Loading history…" forever when the broker activity publisher is missing, mis-registered, or blind to the bot's own order events (cross-API-client visibility gap).

The repair is not three independent copy fixes. The repair is **one contract** for backend-authored, typed operator notices that the cockpit renders verbatim, plus three application sites.

ADR-0014 already established the principle: broker-activity narratives are backend-authored. ADR-0015 generalizes the principle to every operator-facing failure surface — runtime freshness, watchdog incidents, activity health — and pins the schema, the persistence model, the tier policy, and the exhaustiveness gate.

## 2. Goals

- Every operator-facing failure ships as a typed `OperatorNotice` composed by the backend; the cockpit renders `title`/`message`/`action` verbatim and never composes safety copy.
- Operational enums (`RuntimeFreshnessReasonCode`, watchdog outcomes, activity health states) remain for code, logs, tests, and forensics. They never reach the cockpit string surface.
- Watchdog-driven halt produces a typed two-phase shutdown and a durable incident artifact. Restart cannot trade until reconciliation confirms broker state.
- Activity tab has explicit `ready | starting | degraded | unavailable` states; "loading forever" is unrepresentable.
- Notices fan out into ephemeral projections (recomputed each poll) or incident artifacts (persisted) — never both, never neither.
- Every enum reaching the cockpit surface is exhaustiveness-tested. Adding a new value without a notice fails CI.

## 3. Non-goals

- Backfilling notices onto historical UI surfaces that already render fine (PnL, equity curve, account balance).
- Rewriting `OperatorSurfaceRuntimeFreshness` consumers other than the cockpit shell.
- Internationalization. All copy ships English; the schema does not pretend otherwise.
- Replacing the existing operator-surface poll transport. Notices ride the same 4 s status poll.
- Replacing the SSE channel for broker-activity rows. Health rides the status poll; rows still SSE.

## 4. Core contract

### 4.1 `OperatorNotice`

```python
OperatorNoticeTier = Literal["info", "warning", "critical"]

OperatorNoticeCode = Literal[
    # PR 1 — runtime freshness, implemented
    "runtime.market_closed",
    "runtime.market_session_halted",
    "runtime.market_data_stale",
    "runtime.market_data_feed_stalled",
    "runtime.broker_probe_stale",
    "runtime.broker_probe_missing",
    "runtime.command_loop_unresponsive",
    "runtime.engine_runtime_incompatible",
    "runtime.control_plane_lease_stale",
    "runtime.control_plane_boot_id_mismatch",
    # PR 2 — watchdog, reserved
    "watchdog.flatten_completed",
    "watchdog.flatten_not_needed",
    "watchdog.flatten_timed_out",
    "watchdog.flatten_failed",
    "watchdog.broker_disconnected_before_flatten",
    # PR 5 — activity health, reserved
    "activity.publisher_starting",
    "activity.publisher_not_running",
    "activity.publisher_degraded",
    "activity.source_blind_to_bot_orders",
    "activity.dropped_paused_intent",
    # PR 6 — reconciliation, reserved
    "reconciliation.required_after_uncertain_flatten",
    "reconciliation.discovered_execution_not_in_engine_state",
]

class OperatorNoticeAction(BaseModel):
    kind: Literal[
        "none",
        "wait",
        "open_runbook",
        "focus_cockpit_action",
        "external_manual_check",
        "redeploy",
    ]
    label: str | None = None
    target: str | None = None

class OperatorNotice(BaseModel):
    code: OperatorNoticeCode
    tier: OperatorNoticeTier
    title: str
    message: str
    source_codes: list[str] = Field(default_factory=list)
    facts: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
    action: OperatorNoticeAction
    runbook_slug: str | None = None
    occurred_at_ms: int | None = None
```

**Invariants**:

- `title` and `message` are finished English. The frontend never interpolates copy.
- `facts` is typed-but-generic; cockpit renders it in an expandable details panel using key/value formatting.
- `source_codes` references operational enum strings for forensics; never displayed as primary copy.
- `code` is namespaced (`runtime.*`, `watchdog.*`, `activity.*`, `reconciliation.*`); PR 1 declares every planned slot so frontend type generation is stable across PRs 1–6.
- `runbook_slug`, when set, must reference a file that ships in the same PR. No aspirational links.

### 4.2 Tier policy

| Tier | Trader interpretation | Trader action |
|---|---|---|
| `info` | Expected non-trading state (market closed). | None. |
| `warning` | Degraded; bot is protecting itself. | Monitor; inspect if it persists. |
| `critical` | Safety or control failure. | Verify/reconcile before trusting the bot. |

A tier exists only if it triggers a different trader response. `advisory` was dropped because it could not be distinguished from `warning` in trader action terms.

### 4.3 Action semantics

`OperatorNoticeAction.kind` separates **affordance** from **navigation**:

- Clickable in cockpit: `focus_cockpit_action`, `open_runbook`, `redeploy`.
- Non-clickable explicit non-automation: `external_manual_check`. This matters — "Check positions in IBKR" must not look like the cockpit performed reconciliation.
- `redeploy` routes to the Configuration tab and pre-focuses the existing redeploy/start flow. It never triggers a redeploy silently.
- `none` / `wait` carry no affordance.

### 4.4 Persistence model

Two categories, mutually exclusive:

**Ephemeral projection notices** — runtime freshness, activity health.
- Recomputed from current artifacts on each operator-surface poll.
- Not individually persisted.

**Incident notices** — watchdog halt, flatten timeout/failure, activity publisher lifecycle failure that affects auditability.
- Persisted as `OperatorIncident` artifacts (typed), not as rendered notice strings.
- Per-run locality:

  ```
  artifacts/live_runs/<run_id>/operator_incidents/<incident_id>.json
  ```

- Schema:

  ```python
  class OperatorIncident(BaseModel):
      schema_version: int = 1
      incident_id: str
      category: Literal["watchdog", "activity", "reconciliation"]
      notice: OperatorNotice
      started_at_ms: int
      resolved_at_ms: int | None = None
      evidence: dict[str, object] = Field(default_factory=dict)
  ```

- The operator surface aggregates the most recent unresolved incident across runs for a strategy instance.

`OperatorIncident` schema lands in PR 1 (declared). The store and first writer land in PR 2 (watchdog).

### 4.5 Exhaustiveness gate

Every closed enum that reaches the cockpit through a notice is exhaustiveness-tested:

```python
@pytest.mark.parametrize("code", get_literal_args(RuntimeFreshnessReasonCode))
def test_runtime_freshness_code_has_notice_rule(code): ...

@pytest.mark.parametrize("status", get_literal_args(FlattenOutcomeStatus))
def test_flatten_outcome_has_notice(status): ...

@pytest.mark.parametrize("state", get_literal_args(BrokerActivityHealthState))
def test_activity_health_state_has_notice_policy(state): ...
```

A snapshot test pins the allowed `OperatorNoticeCode` literal so frontend types cannot drift silently. The generic helper `get_literal_args` lands in PR 1 under `tests/operator/_helpers.py` and is reused by PRs 2–5.

## 5. PR plan

Each PR is independently mergeable on top of PR 1.

| # | Scope | Closes |
|---|---|---|
| 1 | `OperatorNotice` contract, ADR-0015, runtime freshness composer, cockpit renderer | #657 |
| 2 | Watchdog two-phase halt, `OperatorIncident` store, post-halt reconciliation gate | #656 |
| 3 | `INTENT_DROPPED_BEFORE_SUBMIT` WAL event + fold support | (part of #658) |
| 4 | Publisher structured-concurrency lifecycle (TaskGroup, registry ownership) | (part of #658) |
| 5 | Broker activity health surface; cockpit consumes typed states | #658 |
| 6 | Cross-client IBKR fallback: live stream + bounded `reqExecutions` sweep + reconciliation notice | (follow-on) |

PRs 2–6 each reuse the contract from PR 1; PR 1 declares all `OperatorNoticeCode` slots to stop type churn.

## 6. PR 1 — detailed scope

### 6.1 Behaviors

1. Tighten `OperatorSurfaceRuntimeFreshness`:
   - `stale_reason_codes` typing moves from `list[str]` to `list[RuntimeFreshnessReasonCode]`.
   - Add `stale_reasons: list[OperatorNotice]` and `headline: OperatorNotice | None`.
   - Existing consumers continue to read `stale_reason_codes` (forensics-grade).

2. Implement runtime-freshness rule composer:
   - Static rules table mapping `frozenset[RuntimeFreshnessReasonCode]` (mode `exact` or `subset`) → `OperatorNoticeCode` with priority.
   - Composer collects active codes, evaluates rules, emits notices, picks the highest-priority notice as `headline`.
   - `BAR_LOOP_SESSION_CLOSED` is `suppress_banner=True` — recorded in evidence, not rendered in the freshness banner. Cockpit shows it in the trading-session status card as `info`.

3. Backend pipeline:
   - New module `app/operator/notices/` owns schema + runtime-freshness rules + composer.
   - `app/routers/live_instances.py` calls the composer when building the operator-surface response; no business logic lives in the router. (No `app/research/...` involvement — that path was a mis-call in the planning conversation.)
   - GraphQL DTOs in `Backend/` expose `headline` and `stale_reasons` alongside the existing `stale_reason_codes` field. Snake-case JSON deserialization rules already established (see `.claude/rules/dotnet.md`).

4. Frontend pipeline:
   - New `OperatorNoticeComponent` renders `title`, `message`, `action`, optional `runbook_slug`, optional facts panel. Standalone, OnPush, no copy composition.
   - `RuntimeBanner` consumes `headline` + `stale_reasons` and renders via `OperatorNoticeComponent`. The raw `stale_reason_codes` field is hidden from the trader (kept available for forensic-debug UI only).
   - `operator-notice-code.ts` mirrors `OperatorNoticeCode` literal; snapshot test in Python guards drift.

### 6.2 Runtime freshness rules table

| Priority | Source codes | Mode | Notice code | Tier |
|---|---|---|---|---|
| 100 | `CONTROL_PLANE_BOOT_ID_MISMATCH` | subset | `runtime.control_plane_boot_id_mismatch` | critical |
| 95 | `CONTROL_PLANE_LEASE_STALE` | subset | `runtime.control_plane_lease_stale` | critical |
| 90 | `COMMAND_LOOP_STALE` | subset | `runtime.command_loop_unresponsive` | critical |
| 85 | `ENGINE_RUNTIME_INVALID_OR_INCOMPATIBLE` | subset | `runtime.engine_runtime_incompatible` | critical |
| 80 | `BROKER_PROBE_MISSING` | subset | `runtime.broker_probe_missing` | warning |
| 75 | `BROKER_PROBE_STALE` | subset | `runtime.broker_probe_stale` | warning |
| 70 | `{BAR_LOOP_HEARTBEAT_STALE, BAR_LOOP_LATEST_BAR_STALE}` | exact | `runtime.market_data_feed_stalled` | warning |
| 60 | `BAR_LOOP_LATEST_BAR_STALE` | subset | `runtime.market_data_stale` | warning |
| 50 | `BAR_LOOP_HEARTBEAT_STALE` | subset | `runtime.market_data_stale` | warning |
| 20 | `BAR_LOOP_SESSION_HALTED` | subset | `runtime.market_session_halted` | info |
| 10 | `BAR_LOOP_SESSION_CLOSED` | subset (suppress_banner) | `runtime.market_closed` | info |

Final reason-code list will be sourced verbatim from the existing `RuntimeFreshnessReasonCode` definition; the exhaustiveness test enforces coverage.

### 6.3 Trader-facing copy (anchor examples)

- `runtime.market_data_feed_stalled` — title "Market data feed is stalled" — "No fresh IBKR bar has arrived for 92 seconds; the expected window is 30 seconds. New trading decisions are held until fresh data arrives." — action `external_manual_check` ("Check IBKR connection").
- `runtime.command_loop_unresponsive` — title "Bot is not responding to commands" — "Pause, Resume, Stop, or Flatten may not take effect until the bot recovers. If this persists, stop the bot from the host runner and verify positions at IBKR." — action `external_manual_check` ("Check positions in IBKR", target `ibkr_positions`).
- `runtime.market_closed` — title "Market closed" — "The bot is idle until the regular trading session opens. No trading decision is being made." — action `none`. Suppressed from banner; rendered on the session card.

Numeric facts (`age_ms`, `expected_window_ms`, `latest_source_bar_ms`) populate `facts`; cockpit shows them in the details panel.

### 6.4 File layout (PR 1)

```
docs/architecture/adrs/0015-operator-notice-contract.md
docs/architecture/operator-notice-prd.md  (this file)
docs/runbooks/runtime-freshness.md

PythonDataService/app/operator/__init__.py
PythonDataService/app/operator/notices/__init__.py
PythonDataService/app/operator/notices/schema.py             # OperatorNotice, Action, Tier, Code, Incident
PythonDataService/app/operator/notices/runtime_freshness.py  # rules table + composer

PythonDataService/app/schemas/live_runs.py                   # MOD: tighten code typing, add headline + stale_reasons
PythonDataService/app/routers/live_instances.py              # MOD: call composer when building runtime_freshness

PythonDataService/tests/operator/__init__.py
PythonDataService/tests/operator/_helpers.py                 # get_literal_args
PythonDataService/tests/operator/test_notice_schema.py
PythonDataService/tests/operator/test_runtime_freshness_rules.py
PythonDataService/tests/operator/test_notice_codes_snapshot.py

Backend/.../OperatorSurface*.cs                              # MOD: expose headline + stale_reasons
Backend.Tests/.../OperatorSurfaceTests.cs                    # MOD: round-trip notice JSON

Frontend/src/.../operator-notice/operator-notice.component.ts/.html/.scss
Frontend/src/.../operator-notice/operator-notice.component.spec.ts
Frontend/src/.../runtime-banner/...                          # MOD: render headline; details panel for stale_reasons
Frontend/src/.../models/operator-notice-code.ts              # MOD: literal mirrors backend
```

The Backend/Frontend paths will be resolved against the actual file layout when implementation starts; the GraphQL surface change is non-breaking-additive.

## 7. Watchdog two-phase halt (PR 2 — sketch for ADR-0015 completeness)

Timeouts (config-backed, defaults explicit):

```
lease_loss_grace_ms    = 5_000
flatten_timeout_ms     = 20_000
disconnect_timeout_ms  = 3_000
```

Lease loss requires persistence through `lease_loss_grace_ms` (`HEALTHY → SUSPECTED_LOSS → LEASE_LOST_HANDLING → EXITED`); a single bad observation that resolves emits `info`-tier debug telemetry only.

Controller — single async protocol (no thread boundary; same asyncio loop as engine):

```python
class WatchdogShutdownController(Protocol):
    async def block_submissions(self) -> None: ...
    async def persist_paused(self, reason: LeaseLossReason) -> None: ...
    async def flatten_now(self, reason: LeaseLossReason) -> FlattenOutcome: ...
    async def disconnect_broker(self) -> BrokerDisconnectOutcome: ...
    async def request_engine_exit(self) -> None: ...
```

Partial-failure rule: fail closed and continue the sequence. No step propagates exceptions to the engine task. Every terminal outcome writes an `OperatorIncident` under `artifacts/live_runs/<run_id>/operator_incidents/`.

Post-halt restart: any unresolved incident with `category="watchdog"` and `notice.code in {"watchdog.flatten_timed_out", "watchdog.flatten_failed", "watchdog.broker_disconnected_before_flatten"}` forces `reconciliation.required_after_uncertain_flatten` on next start. The bot refuses to trade until reconciliation clears the incident. (This ties to the existing reconciliation work merged in 90016ec5 / 334e8485 / d88f8b37.)

## 8. WAL terminal event for paused drops (PR 3 — sketch)

Single new event type:

```python
IntentEventType.INTENT_DROPPED_BEFORE_SUBMIT = "INTENT_DROPPED_BEFORE_SUBMIT"

drop_reason: Literal[
    "operator_paused",
    "control_plane_lease_lost",
    "submissions_blocked",
    "max_orders_per_day",
    "broker_safety_halt",
]
```

Ordering: `append → fsync → clear in-memory queue`. If WAL append fails, block submissions and halt; never silently clear.

Legacy compat (fold-side only): a `SIZING_RESOLVED`-only intent with `ts_ms < legacy_sizing_only_cutoff_ms` may be classified `legacy_sizing_only_dropped` for publisher dedup. `legacy_sizing_only_cutoff_ms = engine_started_at_ms` captured at process start. WAL stays byte-immutable; no synthetic appends. Post-cutoff, any `SIZING_RESOLVED`-only record without an explicit terminal event is anomalous and surfaces as `activity.dropped_paused_intent` evidence or an internal warning.

## 9. Publisher lifecycle (PR 4 — sketch)

Replace manual two-task cancellation with structured concurrency. One owning supervisor task per publisher:

```python
async def _run_supervisor(self) -> None:
    async with asyncio.TaskGroup() as tg:
        tg.create_task(self._run_event_consumer())
        tg.create_task(self._pending_intent_loop())
```

`start()` creates only `_supervisor_task`. `stop()` cancels that task. `is_running` checks the supervisor. `BrokerActivityPublisherRegistry.unregister()` is the sole removal path. Tests prove no child task can outlive its publisher.

## 10. Activity health (PR 5 — sketch)

```python
class BrokerActivityHealth(BaseModel):
    state: Literal["ready", "starting", "degraded", "unavailable"]
    headline: OperatorNotice | None
    notices: list[OperatorNotice]
    facts: BrokerActivityHealthFacts  # publisher_registered, publisher_running, latest_row_seq, ...
```

State derivation is backend-only. Booleans are facts, not state primitives the cockpit composes from. Cadence: rides existing 4 s operator-surface poll. `starting` ages to `unavailable` after 30 s. Rows still SSE; only health rides the status poll.

## 11. Cross-client IBKR visibility (PR 6 — sketch)

Engine remains authoritative for trading decisions from its own broker adapter and WAL. Publisher merges live event stream with bounded `reqExecutions` sweep (`sweep_interval_ms=60_000`, `sweep_lookback_ms=900_000`, run immediately on publisher start and reconnect). Deduplicate by `exec_id`, then `perm_id`/`order_ref`.

If sweep finds a fill absent from engine-known executions: emit `reconciliation.discovered_execution_not_in_engine_state` (critical) and require engine reconciliation. The publisher never silently "corrects" cockpit position view.

## 12. Testing strategy

- Exhaustiveness parametrized tests on every closed enum reaching the cockpit (see §4.5).
- Snapshot test on the `OperatorNoticeCode` literal — drift requires intentional update.
- Rule-table tests: every `RuntimeFreshnessReasonCode` covered by at least one rule; combination rules cannot hide higher-priority codes; emitted `source_codes` union equals input codes minus `suppress_banner` entries.
- Composer behavior tests: priority resolution under conflicting codes, `BAR_LOOP_SESSION_CLOSED` suppression, fact-payload correctness.
- Backend Hot Chocolate resolver tests: GraphQL query → typed notice round-trip with snake-case JSON deserialization.
- Frontend component tests (Vitest + Angular Testing Library): renders title/message/action verbatim, no copy composition path, accessibility checks pass.
- Per-PR exhaustiveness fails CI when a new enum value lacks a corresponding notice rule.

## 13. Risks

- **Rule table priority drift.** Adding new `RuntimeFreshnessReasonCode` values without updating priorities silently re-orders headline selection. Mitigation: every new code requires an explicit priority in the rules table; the exhaustiveness test fails otherwise.
- **Backend↔frontend code drift.** Mitigated by the `OperatorNoticeCode` snapshot test plus the mirrored TypeScript literal. CI must run the Python snapshot before the Frontend type compile.
- **Incident write failure during watchdog halt.** If the per-run directory is unwritable mid-halt, the incident is lost. Mitigation: log a structured `CRITICAL` with full incident payload as last-resort persistence; document in the runbook.
- **Cross-client visibility (PR 6) latency.** A 60 s sweep means trader sees executions late. Mitigation: lower cadence later with API-budget evidence; emit `activity.publisher_degraded` so the trader knows rows are delayed.
- **GraphQL field additions.** `OperatorSurface` is consumed by external cockpit polling; the field additions are non-breaking, but Hot Chocolate v15 stripping rules require explicit `[GraphQLName]` on every new resolver — see `.claude/rules/dotnet.md`.

## 14. Out of scope (this initiative)

- Trader-configurable tier thresholds.
- Notice acknowledgement / dismissal flow.
- Push notifications outside the cockpit.
- Internationalization of notice copy.
- Per-trader notice routing or muting.
- Redesigning the existing operator surface poll cadence.

## 15. Open items resolved before implementation

- ADR number: `0015`. Confirmed against `docs/architecture/adrs/`.
- PRD location: `docs/architecture/operator-notice-prd.md`.
- Operator-notice composer module: new `PythonDataService/app/operator/notices/` (not under `research/`).
- Schema field on `OperatorSurfaceRuntimeFreshness` keeps `stale_reason_codes` for forensics; `headline` + `stale_reasons` are the trader-facing additions.
- Incident persistence: per-run, `artifacts/live_runs/<run_id>/operator_incidents/<incident_id>.json`.
- Runbook scope for PR 1: `docs/runbooks/runtime-freshness.md` only. Control-plane and broker-activity runbooks ship with PRs 2 and 5.
- Legacy WAL cutoff: `engine_started_at_ms` captured at process start, no calendar date.
- Exhaustiveness helper: `PythonDataService/tests/operator/_helpers.py`, reused across PRs 1–5.

---

## Appendix A — Code mapping audit (current state, pre-PR 1)

Confirmed against `master @ 90016ec5`:

| Concern | Current location | PR 1 change |
|---|---|---|
| `OperatorSurfaceRuntimeFreshness` schema | `PythonDataService/app/schemas/live_runs.py:1364` | Add `headline`, `stale_reasons`; tighten `stale_reason_codes` typing |
| Runtime freshness composer | `PythonDataService/app/routers/live_instances.py` | Calls new `app/operator/notices/runtime_freshness.py`; no business logic in router |
| `RuntimeFreshnessReasonCode` | Currently typed as raw `str` at line 1361 / 1368 | Promote to `Literal` in `app/operator/notices/schema.py`; the schema imports it |
| `IntentEventType` | `PythonDataService/app/engine/live/intent_ledger.py` | No change in PR 1; consumed by PR 3 |
| Existing runbook neighbours | `docs/runbooks/broker-instance-operator-surface.md`, others | PR 1 adds `runtime-freshness.md`; PR 5 reuses the broker-instance one |

## Appendix B — Notice code reservation (PR 1 declares, later PRs implement)

| PR | Implements | Reserved (declared but unused) |
|---|---|---|
| 1 | `runtime.*` (all 10) | `watchdog.*`, `activity.*`, `reconciliation.*` |
| 2 | `watchdog.*` (5) | — |
| 3 | (no new codes; WAL event only) | — |
| 4 | (no new codes; lifecycle only) | — |
| 5 | `activity.*` (5) | — |
| 6 | `reconciliation.discovered_execution_not_in_engine_state`; `reconciliation.required_after_uncertain_flatten` (consumed; declared PR 2) | — |

The snapshot test in PR 1 asserts the full union exactly. Each later PR updates the snapshot intentionally.
