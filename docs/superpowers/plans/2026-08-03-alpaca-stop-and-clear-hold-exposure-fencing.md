# Alpaca Stop and Clear Hold Exposure Fencing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the Workstream A exit gate from the 2026-08-02 remediation research plan by (a) fencing the Alpaca Clerk so no ENTER decision made before Stop can reach the broker after Stop, (b) making Clear Hold require reason-specific fresh proof instead of a generic channel-health check, and (c) backfilling the one missing P0-1 regression test plus two stale doc lines.

**Architecture:** Reuse existing durable state and injection patterns rather than adding new infrastructure. The Stop fence reads the same `DesiredState` file `_stop_locked` already writes durably before cancellation, injected into the Clerk via a `Callable[[str], DesiredState]` probe (mirroring the existing `bot_running_probe` pattern). The Clear Hold fix reuses the Clerk's existing `reconcile_once()`/`_reconcile_with_proof()` broker round-trip and re-derives `derive.hold_state()` under the intake lock to close the time-of-check/time-of-use gap without holding the lock for the broker call.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic v2, pytest + pytest-asyncio, asyncio.

## Global Constraints

- Every float/timestamp value stays `int64 ms UTC` per `.claude/rules/temporal-rigor.md`; no new datetime types are introduced by this plan.
- `ruff check app/ tests/` (project scope) must pass with zero warnings before any commit that touches `PythonDataService/`.
- No silent exception handlers (`except: pass`) — Task 6 specifically fixes one *missing* handler, not adds a silent one.
- Every new production code path in this plan ships with a regression test in the same task that adds it (TDD: write the failing test first, watch it fail, then implement).
- Do not touch Workstreams B–E or the IBKR Clerk — out of scope per the accepted design doc.
- Follow existing file patterns exactly; do not reformat or restyle code outside the lines each task touches.

---

## Task 1: Backfill the missing P0-1 regression test

**Files:**
- Modify: `PythonDataService/tests/services/test_bot_runner.py`

**Interfaces:**
- Consumes: `BotTaskRegistry.deploy_with_admission` (existing), `BotTaskRegistry.stop` (existing), `evaluate_run_admission` (existing, unchanged) — this task adds no new production code, only a test.

This test proves what Section 1.1 of the design doc found already works but was never pinned: deploying a strategy instance, stopping it, then redeploying the same `strategy_instance_id` with a *different* strategy/symbol must be rejected, and the original binding must survive unchanged.

- [ ] **Step 1: Read the existing `test_deploy_while_running_is_refused` test for its exact fixture shape**

Run: `grep -n "test_deploy_while_running_is_refused" -A 20 PythonDataService/tests/services/test_bot_runner.py`

Use its `registry`/`deploy_with_admission` construction pattern verbatim — do not invent a new fixture shape.

- [ ] **Step 2: Write the failing test**

Add to `test_bot_runner.py`, near `test_deploy_while_running_is_refused`:

```python
async def test_deploy_after_stop_with_changed_configuration_is_refused(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    deployed = await registry.deploy_with_admission(
        broker="alpaca",
        strategy_instance_id=_SID,
        strategy_key="deployment_validation",
        symbol="SPY",
        mode="log_only",
    )
    original_binding_bytes = (
        tmp_path / "live_state" / _SID / "strategy_instance.json"
    ).read_bytes()

    await registry.stop(broker="alpaca", strategy_instance_id=_SID)

    with pytest.raises(RunAdmissionRefusedError) as excinfo:
        await registry.deploy_with_admission(
            broker="alpaca",
            strategy_instance_id=_SID,
            strategy_key="ema_crossover_signal",
            symbol="QQQ",
            mode="log_only",
        )
    assert excinfo.value.decision.reason_code == "STRATEGY_INSTANCE_ALREADY_EXISTS"

    unchanged_binding_bytes = (
        tmp_path / "live_state" / _SID / "strategy_instance.json"
    ).read_bytes()
    assert unchanged_binding_bytes == original_binding_bytes
```

Adjust the exact keyword arguments and helper name (`_registry`, `_SID`) to match whatever `test_deploy_while_running_is_refused` actually uses in Step 1 — that test is the source of truth for the fixture's real call shape, not this snippet.

- [ ] **Step 3: Run it to verify it currently passes (this is a backfill, not a bug fix)**

Run: `podman exec polygon-data-service python -m pytest /app/tests/services/test_bot_runner.py::test_deploy_after_stop_with_changed_configuration_is_refused -v`
Expected: PASS on the first run — Section 1.1 of the design doc already confirmed `evaluate_run_admission` blocks this. If it FAILS, stop and re-open Section 1.1 of the design doc; the assumption that P0-1 is closed was wrong and this plan's scope needs to change before continuing.

- [ ] **Step 4: Commit**

```bash
git add PythonDataService/tests/services/test_bot_runner.py
git commit -m "test(bot-runner): pin redeploy-after-stop rejection (P0-1 backfill)"
```

---

## Task 2: Correct the two stale doc lines

**Files:**
- Modify: `docs/architecture/alpaca-bot-control-remediation-research-plan-2026-08-02.md`

No code. Two textual corrections identified in the design doc Section 1.2 and Section 1.3.

- [ ] **Step 1: Update the Resume-button visibility line**

In `docs/architecture/alpaca-bot-control-remediation-research-plan-2026-08-02.md`, find the A1.1 table row for **Resume** (`Required UI behavior` column currently reads `Hidden until new-run creation, Clerk proof, and command recovery work end to end`). Replace that cell's text with:

```
Resolved 2026-08-03: new-run creation, Clerk proof, and checkpoint
carryover all shipped and are tested (`bot_resume_admission.py`,
`test_resume_existing_creates_new_run_and_preserves_action_plan`).
The panel now renders Resume disabled-with-reason (backend-authored
`RunAdmissionDecision` via `action_policy.py`'s `_guard_resume`)
rather than hidden, which satisfies this row's original intent now
that the precondition is met.
```

Also update §2.1 item 3's sentence *"Until the new-run behavior exists end to end, the V2 panel must not render a Resume button."* by appending: *"(Resolved 2026-08-03 — see the A1.1 table.)"*

- [ ] **Step 2: Commit**

```bash
git add docs/architecture/alpaca-bot-control-remediation-research-plan-2026-08-02.md
git commit -m "docs: mark Resume-button visibility requirement as resolved"
```

---

## Task 3: Amend the design spec with the `prove_stop_outcome` discovery

**Files:**
- Modify: `docs/superpowers/specs/2026-08-03-alpaca-stop-and-clear-hold-exposure-fencing-design.md`

While gathering exact signatures for this plan, a richer existing mechanism was found that the committed spec doc's Section 1.3 doesn't mention: `PythonDataService/app/services/bot_carryover.py`'s `prove_stop_outcome()` already cancels working entry orders (`clerk.cancel_working_entries_for_instance`) and obtains a fresh custody proof (`clerk.prove_instance_custody`) at the end of `_stop_locked`, producing a nuanced outcome (`STOPPED_FLAT` / `STOPPED_WITH_APPROVED_ATTRIBUTED_EXPOSURE` / `STOP_REQUIRES_FLATTEN` / `STOPPED_CUSTODY_UNPROVABLE`). This doesn't change Task 4/5's design (the ENTER fence and the finalize/reap-on-pending fix are independent of it), but the spec should say so accurately. It also surfaces one adjacent, explicitly out-of-scope finding: `BotRunTerminalRecorder.replace_provisional_stop` (`bot_run_terminal.py:79-97`) hardcodes `kind="STOPPED"` even when the reason code is `STOPPED_CUSTODY_UNPROVABLE` — a "terminal words require terminal evidence" gap, but a separate, broader change (touches the shared `BotDutyOutcomeKind` enum and its consumers) that this plan does not take on.

- [ ] **Step 1: Add an addendum section to the spec doc**

Append a new `## 8. Addendum (2026-08-03, during plan-writing)` section:

```markdown
## 8. Addendum (2026-08-03, during plan-writing)

Section 1.3's description of Stop is accurate but incomplete. Current
`_stop_locked` already calls, after the provisional finalize/reap,
`prove_terminal_stop_outcome` → `prove_stop_outcome`
(`app/services/bot_carryover.py:122-171`), which:

1. cancels Clerk-owned working ENTER orders
   (`clerk.cancel_working_entries_for_instance`);
2. obtains a fresh custody proof (`clerk.prove_instance_custody`,
   backed by the same `_reconcile_with_proof` this plan's Clear Hold
   fix also uses);
3. records one of `STOPPED_FLAT`, `STOPPED_WITH_APPROVED_ATTRIBUTED_EXPOSURE`,
   `STOP_REQUIRES_FLATTEN`, or `STOPPED_CUSTODY_UNPROVABLE` as the
   terminal `reason_code`, persisted as a carryover checkpoint.

This is independent of, and unaffected by, Sections 2.1/2.2's design
(the ENTER fence and the finalize/reap-on-pending fix) — it runs after
both, and neither changes its inputs or outputs.

**Flagged, explicitly out of scope:** `BotRunTerminalRecorder
.replace_provisional_stop` (`bot_run_terminal.py:79-97`) hardcodes
`kind="STOPPED"` regardless of which `StopCustodyOutcome` reason code
is passed — so a `STOPPED_CUSTODY_UNPROVABLE` outcome still reports
`kind="STOPPED"`, only distinguishable by reading `reason_code`. This
is a real "terminal words require terminal evidence" gap (decision
principle #4), but fixing it means changing the shared
`BotDutyOutcomeKind` enum's meaning or adding a new value, which
ripples into every other consumer of that enum (receipt labels,
frontend `receiptLabel` mappings, other terminal-state readers). That
is a broader change than this exposure-fencing slice and is not
included here. Tracked as a follow-up, not fixed in this plan.
```

- [ ] **Step 2: Commit**

```bash
git add docs/superpowers/specs/2026-08-03-alpaca-stop-and-clear-hold-exposure-fencing-design.md
git commit -m "docs: amend Stop/Clear-Hold design spec with prove_stop_outcome findings"
```

---

## Task 4: Wire a per-instance desired-state probe into the Alpaca Clerk

**Files:**
- Modify: `PythonDataService/app/services/bot_runner.py` (add public accessor)
- Modify: `PythonDataService/app/broker/alpaca/clerk/clerk.py` (accept the probe)
- Modify: `PythonDataService/app/main.py` (wire the probe at install time)
- Test: `PythonDataService/tests/services/test_bot_runner.py`
- Test: `PythonDataService/tests/broker/alpaca/clerk/test_instance_orders.py`

**Interfaces:**
- Produces: `BotTaskRegistry.desired_state(strategy_instance_id: str) -> DesiredState` — a public method later tasks and the Clerk both rely on.
- Produces: `AlpacaClerk.__init__(..., desired_state_probe: Callable[[str], DesiredState] | None = None)` — stored as `self._desired_state_probe`, consumed by Task 5.

This task only wires the dependency; it does not yet use it for the fence (that's Task 5). It's split out because it's independently testable (the probe returns the right value) before it's load-bearing for admission.

- [ ] **Step 1: Write the failing test for the registry accessor**

Add to `tests/services/test_bot_runner.py`:

```python
async def test_desired_state_reports_durable_intent(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    await registry.deploy_with_admission(
        broker="alpaca",
        strategy_instance_id=_SID,
        strategy_key="deployment_validation",
        symbol="SPY",
        mode="log_only",
    )
    assert registry.desired_state(_SID) is DesiredState.RUNNING

    await registry.stop(broker="alpaca", strategy_instance_id=_SID)
    assert registry.desired_state(_SID) is DesiredState.STOPPED
```

Add `from app.engine.live.desired_state import DesiredState` to the test file's imports if not already present (check first: `grep -n "^from app.engine.live.desired_state" tests/services/test_bot_runner.py`).

- [ ] **Step 2: Run it to verify it fails**

Run: `podman exec polygon-data-service python -m pytest /app/tests/services/test_bot_runner.py::test_desired_state_reports_durable_intent -v`
Expected: FAIL with `AttributeError: 'BotTaskRegistry' object has no attribute 'desired_state'`

- [ ] **Step 3: Add the public accessor to `BotTaskRegistry`**

In `PythonDataService/app/services/bot_runner.py`, immediately after the existing `_desired_repo` method (around line 968-969):

```python
    def desired_state(self, strategy_instance_id: str) -> DesiredState:
        """This instance's durable operator intent (defaults to RUNNING)."""
        return self._desired_repo(strategy_instance_id).read_state()
```

`DesiredState` is already imported in this file (used by `DesiredState.STOPPED`/`.PAUSED`/`.RUNNING` elsewhere) — confirm with `grep -n "^from app.engine.live.desired_state" app/services/bot_runner.py` before adding a duplicate import.

- [ ] **Step 4: Run the test again to verify it passes**

Run: `podman exec polygon-data-service python -m pytest /app/tests/services/test_bot_runner.py::test_desired_state_reports_durable_intent -v`
Expected: PASS

- [ ] **Step 5: Write the failing test for the Clerk accepting the probe**

Add to `tests/broker/alpaca/clerk/test_instance_orders.py`:

```python
async def test_clerk_stores_desired_state_probe() -> None:
    broker = _FakeBroker()
    calls: list[str] = []

    def probe(sid: str) -> DesiredState:
        calls.append(sid)
        return DesiredState.RUNNING

    clerk = AlpacaClerk(read=broker, trade=broker, desired_state_probe=probe)
    assert clerk._desired_state_probe is probe
```

Add `from app.engine.live.desired_state import DesiredState` to this test file's imports.

- [ ] **Step 6: Run it to verify it fails**

Run: `podman exec polygon-data-service python -m pytest /app/tests/broker/alpaca/clerk/test_instance_orders.py::test_clerk_stores_desired_state_probe -v`
Expected: FAIL with `TypeError: __init__() got an unexpected keyword argument 'desired_state_probe'`

- [ ] **Step 7: Add the constructor parameter to `AlpacaClerk`**

In `PythonDataService/app/broker/alpaca/clerk/clerk.py`:

Add to the imports (near the other `app.engine.live` imports, e.g. after `from app.engine.live.order_identity import (...)`):

```python
from app.engine.live.desired_state import DesiredState
```

In `__init__` (around line 112-127), add the parameter after `bot_running_probe`:

```python
    def __init__(
        self,
        *,
        read: BrokerReadPort,
        trade: BrokerTradePort,
        clock: Clock = default_clock,
        stream_health: StreamHealthGate | None = None,
        clerk_generation: str | None = None,
        bot_running_probe: Callable[[], bool] | None = None,
        desired_state_probe: Callable[[str], DesiredState] | None = None,
    ) -> None:
        self._read = read
        self._trade = trade
        self._clock = clock
        self._stream_health = stream_health
        self._clerk_generation = clerk_generation or uuid4().hex
        self._bot_running_probe = bot_running_probe
        self._desired_state_probe = desired_state_probe
```

(Leave every other line of `__init__` exactly as-is — only the two new lines are added.)

- [ ] **Step 8: Run the test again to verify it passes**

Run: `podman exec polygon-data-service python -m pytest /app/tests/broker/alpaca/clerk/test_instance_orders.py::test_clerk_stores_desired_state_probe -v`
Expected: PASS

- [ ] **Step 9: Wire the production probe in `main.py`**

In `PythonDataService/app/main.py`, find the `_alpaca_bot_running` closure (around line 182-186) and add a sibling closure immediately after it, then pass it into the `AlpacaClerk(...)` construction:

```python
        def _alpaca_bot_running() -> bool:
            from app.services.bot_runner import get_bot_task_registry

            registry = get_bot_task_registry()
            return registry is not None and registry.any_running()

        def _alpaca_desired_state(strategy_instance_id: str) -> DesiredState:
            from app.services.bot_runner import get_bot_task_registry

            registry = get_bot_task_registry()
            if registry is None:
                return DesiredState.STOPPED
            return registry.desired_state(strategy_instance_id)

        alpaca_broker = AlpacaBroker()
        alpaca_clerk = AlpacaClerk(
            read=alpaca_broker,
            trade=alpaca_broker,
            stream_health=build_default_stream_health_gate(),
            bot_running_probe=_alpaca_bot_running,
            desired_state_probe=_alpaca_desired_state,
        )
```

Add `from app.engine.live.desired_state import DesiredState` to `main.py`'s imports (check first whether it's already imported there for another reason: `grep -n "desired_state" app/main.py`).

- [ ] **Step 10: Run the full touched-file test suites**

Run: `podman exec polygon-data-service python -m pytest /app/tests/services/test_bot_runner.py /app/tests/broker/alpaca/clerk/test_instance_orders.py -v`
Expected: all PASS, including the two new tests.

- [ ] **Step 11: Lint and commit**

Run: `ruff check PythonDataService/app/ PythonDataService/tests/`
Expected: no warnings.

```bash
git add PythonDataService/app/services/bot_runner.py PythonDataService/app/broker/alpaca/clerk/clerk.py PythonDataService/app/main.py PythonDataService/tests/services/test_bot_runner.py PythonDataService/tests/broker/alpaca/clerk/test_instance_orders.py
git commit -m "feat(alpaca): wire per-instance desired-state probe into the Clerk"
```

---

## Task 5: Add the run-generation fence at the Clerk's ENTER boundary (P0-3, core fix)

**Files:**
- Modify: `PythonDataService/app/broker/alpaca/clerk/effects.py`
- Test: `PythonDataService/tests/broker/alpaca/clerk/test_instance_orders.py`

**Interfaces:**
- Consumes: `self._desired_state_probe: Callable[[str], DesiredState] | None` from Task 4.
- Produces: no new public interface — this closes the choke point every ENTER already flows through.

This is the fix that actually prevents new exposure after Stop: a coroutine that decided to ENTER before Stop but reaches the Clerk after Stop must be rejected here, before `_submit_leg` is ever called.

- [ ] **Step 1: Write the failing test — ENTER refused when desired state is not RUNNING**

Add to `tests/broker/alpaca/clerk/test_instance_orders.py`, near `test_effect_enter_derives_side_and_replay_never_duplicates_broker_work`:

```python
async def test_effect_enter_is_fenced_by_desired_state() -> None:
    from app.broker.alpaca.clerk.models import EffectOperationState, EffectPurpose
    from app.engine.live.desired_state import DesiredState

    broker = _FakeBroker()
    clerk = AlpacaClerk(
        read=broker,
        trade=broker,
        desired_state_probe=lambda sid: DesiredState.STOPPED,
    )
    plan = _effect_plan()

    receipt = await clerk.execute_for_instance(
        strategy_instance_id=_SID_A,
        run_id="run-1",
        decision_id="bar-1700000000000-enter",
        purpose=EffectPurpose.ENTER,
        action_plan=plan,
        quantity=2,
    )

    assert receipt.state is EffectOperationState.REJECTED
    assert broker.submit_calls == []
```

- [ ] **Step 2: Write the failing test — EXIT is NOT fenced (reductions stay allowed)**

Add immediately after it:

```python
async def test_effect_exit_ignores_desired_state_fence() -> None:
    from app.broker.alpaca.clerk.models import EffectOperationState, EffectPurpose

    broker = _FakeBroker()
    clerk = AlpacaClerk(read=broker, trade=broker)
    plan = _effect_plan()
    await _submit_and_fill(clerk, _SID_A, quantity=1.0)

    from app.engine.live.desired_state import DesiredState

    clerk._desired_state_probe = lambda sid: DesiredState.STOPPED
    receipt = await clerk.execute_for_instance(
        strategy_instance_id=_SID_A,
        run_id="run-1",
        decision_id="bar-1700000001000-exit",
        purpose=EffectPurpose.EXIT,
        action_plan=plan,
        quantity=1,
    )

    assert receipt.state is not EffectOperationState.REJECTED
```

- [ ] **Step 3: Run both to verify they fail (or pass vacuously) before the fix**

Run: `podman exec polygon-data-service python -m pytest /app/tests/broker/alpaca/clerk/test_instance_orders.py::test_effect_enter_is_fenced_by_desired_state /app/tests/broker/alpaca/clerk/test_instance_orders.py::test_effect_exit_ignores_desired_state_fence -v`
Expected: `test_effect_enter_is_fenced_by_desired_state` FAILS (the receipt is currently SUBMITTED, not REJECTED, and `broker.submit_calls` is non-empty). `test_effect_exit_ignores_desired_state_fence` PASSES already (there's no fence yet to violate) — that's fine, it's guarding against a *future* regression, not proving today's bug.

- [ ] **Step 4: Add the fence check to `_resolve_enter`**

In `PythonDataService/app/broker/alpaca/clerk/effects.py`, add the import:

```python
from app.engine.live.desired_state import DesiredState
```

Modify `_resolve_enter` (currently lines 233-252) — insert the fence check between the early-return and the existing hold check:

```python
    async def _resolve_enter(
        self,
        operation: AlpacaEffectOperation,
        account_id: str,
        journal: OrderJournal,
        entries: list[OrderJournalEntry],
        latest: EffectOperationReceipt,
    ) -> EffectOperationReceipt:
        if latest.state is not EffectOperationState.ACCEPTED:
            return latest
        if (
            self._desired_state_probe is not None
            and self._desired_state_probe(operation.strategy_instance_id) is not DesiredState.RUNNING
        ):
            receipt = EffectOperationReceipt(
                operation=operation,
                state=EffectOperationState.REJECTED,
                explanation=(
                    "Entry was blocked because this run's Stop intent was recorded "
                    "before this decision reached the Clerk."
                ),
                next_step="This decision predates Stop; no new order will be submitted for this run.",
            )
            await self._append_effect_receipt(journal, account_id, receipt)
            return receipt
        hold = derive.hold_state(entries)
        if hold.active:
```

(The rest of the method — the hold-rejection branch onward — is unchanged; only the new block above `hold = derive.hold_state(entries)` is inserted.)

- [ ] **Step 5: Run the two new tests to verify they pass**

Run: `podman exec polygon-data-service python -m pytest /app/tests/broker/alpaca/clerk/test_instance_orders.py::test_effect_enter_is_fenced_by_desired_state /app/tests/broker/alpaca/clerk/test_instance_orders.py::test_effect_exit_ignores_desired_state_fence -v`
Expected: both PASS.

- [ ] **Step 6: Write the "already accepted effect still resolves" regression test**

This proves the fence doesn't retroactively break an ENTER that reached the Clerk *before* Stop — the "accepted, shielded" case from the design doc's race point 3. Add:

```python
async def test_effect_enter_accepted_before_stop_still_resolves() -> None:
    from app.broker.alpaca.clerk.models import EffectOperationState, EffectPurpose
    from app.engine.live.desired_state import DesiredState

    broker = _FakeBroker()
    desired = {"state": DesiredState.RUNNING}
    clerk = AlpacaClerk(
        read=broker, trade=broker, desired_state_probe=lambda sid: desired["state"]
    )
    plan = _effect_plan()

    accepted = await clerk.execute_for_instance(
        strategy_instance_id=_SID_A,
        run_id="run-1",
        decision_id="bar-1700000000000-enter",
        purpose=EffectPurpose.ENTER,
        action_plan=plan,
        quantity=2,
    )
    assert accepted.state is EffectOperationState.SUBMITTED

    # Stop lands after the first call already resolved (accepted + submitted).
    desired["state"] = DesiredState.STOPPED

    # A second call with the SAME decision_id is a replay of the already-
    # accepted effect, not a new decision — it must return the same receipt,
    # not a fresh REJECTED one.
    replay = await clerk.execute_for_instance(
        strategy_instance_id=_SID_A,
        run_id="run-1",
        decision_id="bar-1700000000000-enter",
        purpose=EffectPurpose.ENTER,
        action_plan=plan,
        quantity=2,
    )
    assert replay == accepted
```

Run: `podman exec polygon-data-service python -m pytest /app/tests/broker/alpaca/clerk/test_instance_orders.py::test_effect_enter_accepted_before_stop_still_resolves -v`
Expected: PASS without any code change — `_resolve_enter`'s `if latest.state is not EffectOperationState.ACCEPTED: return latest` early-return (line 241-242, unchanged) already guarantees a replayed already-resolved decision never re-enters the fence check. This test documents and pins that guarantee explicitly.

**Note on design-doc Section 5 race points not given a dedicated test here:** point 1 (Stop before any decision) is the trivial case — nothing reaches the Clerk, nothing to assert beyond what Step 5 already covers. Point 5 (a lifecycle event or foreign order arriving concurrently with Stop) needs no new test because the fence check added in Step 4 is a synchronous read (`self._desired_state_probe(...)`, a local file read via `DesiredStateRepo.read_state()`, not a coroutine) inserted with no intervening `await` between it and the rest of `_resolve_enter`, and the whole method already runs inside `self._intake_lock` (unchanged) — there is no new window for a concurrent lifecycle event to interleave that didn't already exist, or not exist, before this change. Point 6 (process restart while STOPPING) is covered in Task 6 instead, where it belongs — it's about `_stop_locked`'s behavior, not the fence.

- [ ] **Step 7: Run the full clerk test file and lint**

Run: `podman exec polygon-data-service python -m pytest /app/tests/broker/alpaca/clerk/test_instance_orders.py -v`
Expected: all PASS (no regressions in the other ~30+ existing tests in this file).

Run: `ruff check PythonDataService/app/ PythonDataService/tests/`
Expected: no warnings.

- [ ] **Step 8: Commit**

```bash
git add PythonDataService/app/broker/alpaca/clerk/effects.py PythonDataService/tests/broker/alpaca/clerk/test_instance_orders.py
git commit -m "fix(alpaca-clerk): fence ENTER effects on durable desired-state (P0-3)"
```

---

## Task 6: Fix Stop's unconditional finalize/reap on a task that never terminated (P0-3, honesty fix)

**Files:**
- Modify: `PythonDataService/app/services/bot_runner.py`
- Test: `PythonDataService/tests/services/test_bot_runner.py`

**Interfaces:**
- Consumes: nothing new — uses `asyncio.wait`'s existing return value, currently discarded.

This closes the second half of P0-3: even with Task 5's fence making a survivor harmless to the broker, `_stop_locked` must stop claiming `OPERATOR_STOP` succeeded and reaping the task while it's still alive — that's a separate honesty/supervision defect (per the accepted design, escalation is per-instance only, not an account-wide hold).

- [ ] **Step 1: Add the cancellation-suppressing feed fixture**

In `tests/services/test_bot_runner.py`, add near `_FakeFeed`/`_StaleFeed`:

```python
class _CancellationSuppressingFeed(_FakeFeed):
    """Simulates a strategy coroutine whose task never terminates on cancel.

    ``stream_bars`` swallows exactly one ``CancelledError`` at its
    ``await`` suspension point and keeps looping, so ``task.cancel()``
    never actually finishes the task — reproducing the P0-3 audit
    finding without a real external dependency.
    """

    def __init__(self, bars: list[MarketDataBar]) -> None:
        super().__init__(bars, mode="hold")
        self.cancellation_suppressed = False

    async def stream_bars(self, symbol: str, *, use_rth: bool = True):
        for bar in self._bars:
            self.bars_consumed += 1
            yield bar
        while True:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                if self.cancellation_suppressed:
                    raise
                self.cancellation_suppressed = True
```

- [ ] **Step 2: Write the failing test**

```python
async def test_stop_does_not_finalize_or_reap_a_task_that_survives_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.services.bot_runner._STOP_TIMEOUT_S", 0.05)
    feed = _CancellationSuppressingFeed(bars=[])
    registry = _registry(tmp_path, feed=feed)
    await registry.deploy_with_admission(
        broker="alpaca",
        strategy_instance_id=_SID,
        strategy_key="deployment_validation",
        symbol="SPY",
        mode="log_only",
    )

    status = await registry.stop(broker="alpaca", strategy_instance_id=_SID)

    assert feed.cancellation_suppressed is True
    # The task must still be tracked — not reaped — while it's alive.
    assert _SID in registry._bots
    assert registry._bots[_SID].task.done() is False
    # status() must honestly report the bot as still running, since the
    # task is still alive; desired_state carries the STOPPED intent.
    assert status.running is True
    assert registry.desired_state(_SID) is DesiredState.STOPPED
```

Adjust `_registry(tmp_path, feed=feed)`'s exact call shape to whatever helper the file already uses to inject a custom feed (check `grep -n "def _registry" tests/services/test_bot_runner.py` for its real signature — it may take the feed via a different parameter name or via `registry.set_feed(...)`).

- [ ] **Step 3: Run it to verify it fails**

Run: `podman exec polygon-data-service python -m pytest /app/tests/services/test_bot_runner.py::test_stop_does_not_finalize_or_reap_a_task_that_survives_cancellation -v`
Expected: FAIL — today `_SID` is NOT in `registry._bots` (it was reaped) and/or `status.running` is `False` even though the task never actually finished.

- [ ] **Step 4: Fix `_stop_locked` to check the wait result**

In `PythonDataService/app/services/bot_runner.py`, modify `_stop_locked` (currently lines 618-674). Replace:

```python
        managed.stop_reason_code = PROVISIONAL_STOP_REASON_CODE
        managed.task.cancel()
        await asyncio.wait({managed.task}, timeout=_STOP_TIMEOUT_S)
        # Backstop for a coroutine that never entered supervision (cancelled
        # pre-start): _finalize is idempotent, so this is a no-op whenever the
        # supervisor already recorded the outcome.
        self._terminal.finalize(
            managed.binding,
            kind="STOPPED",
            reason_code=PROVISIONAL_STOP_REASON_CODE,
        )
        self._terminal.reap(strategy_instance_id, managed.binding.run_id)
        outcome = "OPERATOR_STOP"
        if broker == "alpaca" and managed.binding.mode == "trade":
            outcome = await prove_terminal_stop_outcome(
                managed.binding,
                checkpoint_path=self._carryover_checkpoint_path(strategy_instance_id),
                now_ms=self._now_ms,
            )
        self._terminal.replace_provisional_stop(
            managed.binding,
            reason_code=outcome,
        )
        return self.status(broker, strategy_instance_id)
```

with:

```python
        managed.stop_reason_code = PROVISIONAL_STOP_REASON_CODE
        managed.task.cancel()
        _done, pending = await asyncio.wait({managed.task}, timeout=_STOP_TIMEOUT_S)
        if pending:
            logger.warning(
                "Stop cancellation did not terminate within the timeout",
                extra={
                    "action": "stop_cancellation_timeout",
                    "strategy_instance_id": strategy_instance_id,
                    "run_id": managed.binding.run_id,
                    "timeout_s": _STOP_TIMEOUT_S,
                },
            )
            return self.status(broker, strategy_instance_id)
        # Backstop for a coroutine that never entered supervision (cancelled
        # pre-start): _finalize is idempotent, so this is a no-op whenever the
        # supervisor already recorded the outcome.
        self._terminal.finalize(
            managed.binding,
            kind="STOPPED",
            reason_code=PROVISIONAL_STOP_REASON_CODE,
        )
        self._terminal.reap(strategy_instance_id, managed.binding.run_id)
        outcome = "OPERATOR_STOP"
        if broker == "alpaca" and managed.binding.mode == "trade":
            outcome = await prove_terminal_stop_outcome(
                managed.binding,
                checkpoint_path=self._carryover_checkpoint_path(strategy_instance_id),
                now_ms=self._now_ms,
            )
        self._terminal.replace_provisional_stop(
            managed.binding,
            reason_code=outcome,
        )
        return self.status(broker, strategy_instance_id)
```

Note what did NOT change: `DesiredState.STOPPED` is still written durably before `task.cancel()` (unchanged, above this snippet) — the fence in Task 5 already reads that regardless of whether this timeout path triggers. `logger` must already be imported at module level (`import logging` / `logger = logging.getLogger(__name__)`) — confirm with `grep -n "^logger = " app/services/bot_runner.py`; if it's named differently, use that name instead.

- [ ] **Step 5: Run the test again to verify it passes**

Run: `podman exec polygon-data-service python -m pytest /app/tests/services/test_bot_runner.py::test_stop_does_not_finalize_or_reap_a_task_that_survives_cancellation -v`
Expected: PASS

- [ ] **Step 6: Write the integration test proving Task 5's fence catches the survivor's next decision**

This is the point of doing Task 5 and Task 6 together — prove they compose:

```python
async def test_surviving_task_after_stop_timeout_cannot_place_new_orders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.broker.alpaca.clerk.clerk import AlpacaClerk
    from app.broker.alpaca.clerk.models import EffectOperationState, EffectPurpose
    from app.schemas.action_plan import ActionPlan, CloseLegExit, StockEntryLeg, StockInstrument

    monkeypatch.setattr("app.services.bot_runner._STOP_TIMEOUT_S", 0.05)
    feed = _CancellationSuppressingFeed(bars=[])
    registry = _registry(tmp_path, feed=feed)
    await registry.deploy_with_admission(
        broker="alpaca",
        strategy_instance_id=_SID,
        strategy_key="deployment_validation",
        symbol="SPY",
        mode="log_only",
    )
    await registry.stop(broker="alpaca", strategy_instance_id=_SID)
    assert feed.cancellation_suppressed is True

    class _FakeBroker:
        submit_calls: list = []

        async def get_account(self):
            from app.broker.contract.models import BrokerAccountSnapshot

            return BrokerAccountSnapshot(
                broker="alpaca", account_id="paper-account", account_mode="paper",
                account_status="ACTIVE", currency="USD", cash=1000.0, equity=1000.0,
                buying_power=2000.0, portfolio_value=1000.0, long_market_value=0.0,
                short_market_value=0.0, pattern_day_trader=False, trading_blocked=False,
                account_blocked=False, created_at_ms=0, observed_at_ms=0,
            )

    broker = _FakeBroker()
    clerk = AlpacaClerk(
        read=broker,
        trade=broker,
        desired_state_probe=lambda sid: registry.desired_state(sid),
    )
    plan = ActionPlan(
        on_enter=[
            StockEntryLeg(
                leg_id="primary",
                instrument=StockInstrument(kind="stock", underlying="SPY"),
                position="long",
                qty_ratio=1,
            )
        ],
        on_exit=[CloseLegExit(kind="close_leg", entry_leg_id="primary")],
    )

    # The survivor's coroutine "decided" to ENTER after Stop was called.
    receipt = await clerk.execute_for_instance(
        strategy_instance_id=_SID,
        run_id="run-1",
        decision_id="bar-late-enter",
        purpose=EffectPurpose.ENTER,
        action_plan=plan,
        quantity=1,
    )

    assert receipt.state is EffectOperationState.REJECTED
```

Reconcile the `_FakeBroker` shape here with whatever the existing `_FakeBroker` in `test_instance_orders.py` already provides (`read`/`trade` port double) — prefer importing and reusing that existing class over redefining a new one inline, if it's importable without pulling in unrelated fixtures. If it isn't cleanly importable, keep this local minimal double as written.

- [ ] **Step 7: Run it to verify it passes**

Run: `podman exec polygon-data-service python -m pytest /app/tests/services/test_bot_runner.py::test_surviving_task_after_stop_timeout_cannot_place_new_orders -v`
Expected: PASS — this is the plan's single most important assertion: a coroutine that survives a failed Stop cannot place a new order, proven end-to-end across both fixes.

- [ ] **Step 8: Write the process-restart persistence test (design-doc Section 5, race point 6)**

The fence's correctness after a restart depends only on `desired_state.json` surviving on disk and a fresh process reading the same file — prove that directly with a second, independently-constructed registry pointed at the same `tmp_path`, rather than simulating full boot recovery:

```python
async def test_stop_intent_and_fence_survive_process_restart(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    await registry.deploy_with_admission(
        broker="alpaca",
        strategy_instance_id=_SID,
        strategy_key="deployment_validation",
        symbol="SPY",
        mode="log_only",
    )
    await registry.stop(broker="alpaca", strategy_instance_id=_SID)

    # Simulate a process restart: a brand-new registry instance, no shared
    # in-memory state, reading the same on-disk artifacts.
    restarted_registry = _registry(tmp_path)
    assert restarted_registry.desired_state(_SID) is DesiredState.STOPPED

    from app.broker.alpaca.clerk.clerk import AlpacaClerk
    from app.broker.alpaca.clerk.models import EffectOperationState, EffectPurpose
    from app.schemas.action_plan import ActionPlan, CloseLegExit, StockEntryLeg, StockInstrument

    class _FakeBroker:
        async def get_account(self):
            from app.broker.contract.models import BrokerAccountSnapshot

            return BrokerAccountSnapshot(
                broker="alpaca", account_id="paper-account", account_mode="paper",
                account_status="ACTIVE", currency="USD", cash=1000.0, equity=1000.0,
                buying_power=2000.0, portfolio_value=1000.0, long_market_value=0.0,
                short_market_value=0.0, pattern_day_trader=False, trading_blocked=False,
                account_blocked=False, created_at_ms=0, observed_at_ms=0,
            )

    broker = _FakeBroker()
    clerk = AlpacaClerk(
        read=broker,
        trade=broker,
        desired_state_probe=lambda sid: restarted_registry.desired_state(sid),
    )
    plan = ActionPlan(
        on_enter=[
            StockEntryLeg(
                leg_id="primary",
                instrument=StockInstrument(kind="stock", underlying="SPY"),
                position="long",
                qty_ratio=1,
            )
        ],
        on_exit=[CloseLegExit(kind="close_leg", entry_leg_id="primary")],
    )
    receipt = await clerk.execute_for_instance(
        strategy_instance_id=_SID,
        run_id="run-1",
        decision_id="bar-post-restart-enter",
        purpose=EffectPurpose.ENTER,
        action_plan=plan,
        quantity=1,
    )
    assert receipt.state is EffectOperationState.REJECTED
```

Run: `podman exec polygon-data-service python -m pytest /app/tests/services/test_bot_runner.py::test_stop_intent_and_fence_survive_process_restart -v`
Expected: PASS on the first run — `DesiredStateRepo` (Task 4) is already a plain file read with no in-memory caching, so this should require no production code change. If it fails, that's a real bug in the assumption this task's design rests on and must be fixed before continuing, not worked around.

- [ ] **Step 9: Run the full `test_bot_runner.py` suite and lint**

Run: `podman exec polygon-data-service python -m pytest /app/tests/services/test_bot_runner.py -v`
Expected: all PASS (56+ existing tests, no regressions).

Run: `ruff check PythonDataService/app/ PythonDataService/tests/`
Expected: no warnings.

- [ ] **Step 10: Commit**

```bash
git add PythonDataService/app/services/bot_runner.py PythonDataService/tests/services/test_bot_runner.py
git commit -m "fix(bot-runner): do not finalize or reap Stop while the task survives cancellation"
```

---

## Task 7: Fix the missing `clear_hold` router exception handler

**Files:**
- Modify: `PythonDataService/app/routers/brokers.py`
- Test: `PythonDataService/tests/broker/alpaca/clerk/test_clerk_status_endpoint.py` (already has real ASGI-level `/clerk/clear-hold` tests — reuse its pattern, don't create a new file)

**Interfaces:**
- Consumes: `InventoryBaselineRefusedError` (existing, already used elsewhere in this file).

Discovered while researching Task 8: `clear_clerk_hold` (the `/​{broker}/clerk/clear-hold` endpoint) only catches `BrokerError` — it does **not** catch `InventoryBaselineRefusedError`, even though `clerk.clear_hold()` already raises it today for the existing stream-health-not-fresh case. That refusal currently surfaces as an uncaught 500, not a typed 409. Task 8 adds a *second* raise site for the same exception (the reason-specific hold-clear refusal) through this same unguarded endpoint, so this must be fixed first or Task 8's new refusal will also 500.

- [ ] **Step 1: Write the failing test proving today's 500**

Add to `tests/broker/alpaca/clerk/test_clerk_status_endpoint.py`, using the file's existing `set_alpaca_clerk`/`_post`/`ASGITransport` pattern (see `_alpaca_clerk` fixture and `test_clear_hold_restores_submission`, both already in this file) — this test builds its own clerk inline (with a broken `StreamHealthGate`) instead of using the `_alpaca_clerk` fixture, because it needs to trigger the *existing* stream-health refusal path in `clear_hold` without depending on Task 8's not-yet-built reconciliation branch:

An active hold must exist before `clear_hold`'s `_channel_fresh()` check is even reached (an inactive hold short-circuits to a no-op before the channel check runs). Raise one the same way `test_status_reports_hold_after_unexplained_order` does (a foreign order via `reconcile_once()`) — which hold *reason* raised it doesn't matter here, since before Task 8 lands `clear_hold` only ever checks channel freshness regardless of reason:

```python
@responses.activate
async def test_clear_hold_refusal_returns_409_not_500(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.broker.alpaca.clerk.models import ChannelHealth
    from app.broker.alpaca.clerk.stream_health import StreamHealthGate

    monkeypatch.setenv("ALPACA_CLERK_DIR", str(tmp_path))
    journal_module.reset_clerk_settings_for_testing()
    reset_alpaca_settings_for_testing()
    alpaca_settings = AlpacaSettings(api_key_id="k", api_secret_key="s", mode="paper")
    broker = AlpacaBroker(AlpacaTradingClient(settings=alpaca_settings))
    broken_gate = StreamHealthGate(
        market_data=lambda: ChannelHealth(
            stream="market_data", healthy=False, reason="feed down", observed_at_ms=0
        ),
        execution=lambda: ChannelHealth(
            stream="execution", healthy=True, reason="", observed_at_ms=0
        ),
    )
    set_alpaca_clerk(AlpacaClerk(read=broker, trade=broker, stream_health=broken_gate))
    try:
        responses.add(responses.GET, f"{_BASE}/v2/account", body=_ACCOUNT_BODY, status=200)
        responses.add(
            responses.GET, f"{_BASE}/v2/orders", body=_foreign_order_body(), status=200
        )
        responses.add(responses.GET, f"{_BASE}/v2/positions", body="[]", status=200)
        await _raise_hold_via_sweep()

        response = await _post(
            "/api/brokers/alpaca/clerk/clear-hold",
            {"operator": "ops", "reason": "attempting to clear"},
        )
    finally:
        reset_alpaca_clerk_for_testing()
        journal_module.reset_clerk_settings_for_testing()
        reset_alpaca_settings_for_testing()

    assert response.status_code == 409
    assert response.json()["detail"]["message"].startswith("Exposure hold cannot be cleared")
```

`_raise_hold_via_sweep()` (lines 147-152) calls `get_alpaca_clerk()` internally and operates on whatever clerk is currently module-installed — since `set_alpaca_clerk(...)` above already installed this test's clerk (with the broken gate), it operates on this test's clerk unchanged, no modification needed.

- [ ] **Step 2: Run it to verify it fails**

Run: `podman exec polygon-data-service python -m pytest tests/broker/alpaca/clerk/test_clerk_status_endpoint.py::test_clear_hold_refusal_returns_409_not_500 -v`
Expected: FAIL — the response is a 500 (an unhandled `InventoryBaselineRefusedError` propagating out of the ASGI app), not 409.

- [ ] **Step 3: Add the missing exception handler**

In `PythonDataService/app/routers/brokers.py`, modify `clear_clerk_hold` (currently lines 308-320):

```python
@router.post(
    "/{broker}/clerk/clear-hold",
    response_model=ClerkStatus,
    dependencies=[Depends(require_data_plane_control_secret)],
)
async def clear_clerk_hold(broker: str, request: ClearHoldRequest) -> ClerkStatus:
    """Clear the account exposure hold (operator exit); return the updated status.

    A control mutation (the control secret gates it). Transport only: resolve the
    Clerk and delegate. The Clerk journals HOLD_CLEARED (idempotent — a clear
    against no active hold is a benign NO-OP) and returns the post-clear status so
    the desk re-renders in one round-trip.
    """
    clerk = _require_trade_clerk(broker)
    try:
        return await clerk.clear_hold(operator=request.operator, reason=request.reason)
    except InventoryBaselineRefusedError as error:
        raise HTTPException(status_code=409, detail={"message": str(error), "why": error.detail})
    except BrokerError as error:
        _raise_http(error)
```

`InventoryBaselineRefusedError` is already imported at the top of this file (used by `resolve_custody`, line ~29) — no new import needed.

- [ ] **Step 4: Run the test again to verify it passes**

Run: `podman exec polygon-data-service python -m pytest tests/broker/alpaca/clerk/test_clerk_status_endpoint.py::test_clear_hold_refusal_returns_409_not_500 -v`
Expected: PASS

- [ ] **Step 5: Run the full test file and lint**

Run: `podman exec polygon-data-service python -m pytest tests/broker/alpaca/clerk/test_clerk_status_endpoint.py -v`
Expected: all PASS (no regressions in the existing status/submit/cancel/clear-hold tests in this file).

Run: `ruff check PythonDataService/app/ PythonDataService/tests/`
Expected: no warnings.

- [ ] **Step 6: Commit**

```bash
git add PythonDataService/app/routers/brokers.py PythonDataService/tests/broker/alpaca/clerk/test_clerk_status_endpoint.py
git commit -m "fix(brokers-router): return 409 not 500 when clear-hold is refused"
```

---

## Task 8: Reason-specific Clear Hold proof (P0-2, core fix)

**Files:**
- Modify: `PythonDataService/app/broker/alpaca/clerk/clerk.py`
- Test: `PythonDataService/tests/broker/alpaca/clerk/test_clerk_reconciliation.py`

**Interfaces:**
- Consumes: `derive.hold_state(entries) -> HoldState` (existing, unchanged), `self.reconcile_once() -> ReconciliationVerdict` (existing, unchanged), `self._channel_fresh() -> bool` (existing, unchanged), `InventoryBaselineRefusedError` (existing).
- Produces: no new public method — `clear_hold`'s existing signature and return type (`ClerkStatus`) are unchanged; only its internal admission logic changes.

This is the design doc's Section 3: dispatch on hold reason code, run a fresh reconciliation for `UNEXPLAINED_ORDER_HOLD`, and re-check under the lock that nothing new arrived between the broker round-trip and the append.

- [ ] **Step 1: Flip the currently-pinned bug test**

Find `test_hold_is_re_raised_after_clear_when_foreign_order_persists` in `tests/broker/alpaca/clerk/test_clerk_reconciliation.py` (confirmed at lines 537-553 during research; re-locate with `grep -n "def test_hold_is_re_raised_after_clear_when_foreign_order_persists"` since line numbers may have shifted). Today it asserts the clear *succeeds* while the foreign order is still present. Replace its body:

```python
async def test_clear_hold_refuses_while_foreign_order_persists() -> None:
    # A foreign order still present at the broker must refuse the clear —
    # this is the P0-2 fix: reason-specific proof, not a generic channel check.
    broker = _FakeBroker(orders=[_order(client_order_id="foreign")])
    clerk = AlpacaClerk(read=broker, trade=broker, clock=_fixed_clock)
    await clerk.reconcile_once()
    assert clerk.is_on_hold() is True

    with pytest.raises(InventoryBaselineRefusedError):
        await clerk.clear_hold(operator="inkant", reason="reviewed")

    assert clerk.is_on_hold() is True
    kinds = _kinds(clerk)
    assert ClerkEntryKind.HOLD_CLEARED not in kinds
```

Add `InventoryBaselineRefusedError` to this test file's import from `app.broker.alpaca.clerk.clerk` (it already imports `AlpacaClerk` from there — extend that import line).

**Also fix the ASGI-level equivalent of this same pinned bug.** `tests/broker/alpaca/clerk/test_clerk_status_endpoint.py::test_clear_hold_restores_submission` (currently around line 228-277) has the identical shape one layer up: it raises a hold via `_raise_hold_via_sweep()` against a `responses`-mocked `/v2/orders` that always returns the foreign order, then immediately POSTs `/clerk/clear-hold` and asserts `200` + `hold.active is False`. After this task's fix, `clear_hold` will internally call `reconcile_once()` again, which will re-read the *same* mocked `/v2/orders` response (the `responses` library serves one `responses.add()` registration to every matching call unless a second one is queued) — so it will see the foreign order is still "there" and correctly refuse. Fix the test, don't skip it: add a second `responses.add(responses.GET, f"{_BASE}/v2/orders", body="[]", status=200)` registration (the `responses` library serves multiple same-URL registrations in FIFO order) immediately before the `clear-hold` POST, so the internal reconciliation this task adds sees a clean broker read:

```python
@responses.activate
async def test_clear_hold_restores_submission(_alpaca_clerk: None) -> None:
    responses.add(responses.GET, f"{_BASE}/v2/account", body=_ACCOUNT_BODY, status=200)
    responses.add(
        responses.GET, f"{_BASE}/v2/orders", body=_foreign_order_body(), status=200
    )
    responses.add(responses.GET, f"{_BASE}/v2/positions", body="[]", status=200)
    await _raise_hold_via_sweep()

    # The foreign order is now resolved at the broker — this is what
    # clear_hold's own internal reconciliation (this task's fix) will read.
    responses.add(responses.GET, f"{_BASE}/v2/orders", body="[]", status=200)
    responses.add(responses.GET, f"{_BASE}/v2/positions", body="[]", status=200)

    cleared = await _post(
        "/api/brokers/alpaca/clerk/clear-hold",
        {"operator": "ops", "reason": "Verified the account is safe."},
    )
    assert cleared.status_code == 200
    assert cleared.json()["hold"]["active"] is False
```

(The rest of the test — the subsequent-submit-lands assertion after line 243 — is unchanged.) Run this file in isolation after Step 7's implementation lands to confirm the fix; it's listed again in Step 9's full-file run.

- [ ] **Step 2: Write the failing test — clear succeeds once the foreign order is genuinely gone**

```python
async def test_clear_hold_succeeds_once_foreign_order_is_gone() -> None:
    broker = _FakeBroker(orders=[_order(client_order_id="foreign")])
    clerk = AlpacaClerk(read=broker, trade=broker, clock=_fixed_clock)
    await clerk.reconcile_once()
    assert clerk.is_on_hold() is True

    broker.orders = []  # the foreign order is cancelled/resolved at the broker
    status = await clerk.clear_hold(operator="inkant", reason="reviewed")

    assert clerk.is_on_hold() is False
    assert status.hold.active is False
    entries = clerk._journal.read_entries()  # type: ignore[union-attr]
    cleared = [e for e in entries if e.kind is ClerkEntryKind.HOLD_CLEARED]
    assert len(cleared) == 1
    # The proof reference (Section 3.3 of the design doc): the reconciliation
    # verdict that justified the clear rides on the existing `verdict` field
    # already used by RECONCILIATION lines — no new schema field needed.
    assert cleared[0].verdict == "clean"
```

Confirm `_FakeBroker.orders` is a mutable attribute the fake actually reads live on each call (check its `list_orders` implementation around line 131-150) rather than a value captured once at construction — if it's captured at construction, mutate via whatever mechanism the fake actually supports (e.g. re-instantiate the broker or use a setter the fake already exposes).

- [ ] **Step 3: Write the failing test — stream-health-only holds are unaffected**

`StreamHealthGate` (`app/broker/alpaca/clerk/stream_health.py:78-89`) is a frozen dataclass of two `Callable[[], ChannelHealth]` providers — there's no mutation method to call; drive it broken/healthy by swapping the clerk's `_stream_health` attribute between a broken-reporting gate and `None` (or a healthy one). A `STREAM_HEALTH_HOLD` is only journaled when a submit is actually attempted while broken (`clerk.py:211-227`, `_set_hold` with `STREAM_HEALTH_HOLD_CODE`) — so raise it the same way production does, via a refused `submit()` call, rather than hand-journaling a `HOLD_SET`:

```python
async def test_clear_hold_stream_health_reason_still_uses_channel_check() -> None:
    from app.broker.alpaca.clerk.models import ChannelHealth
    from app.broker.alpaca.clerk.stream_health import StreamHealthGate
    from app.broker.contract.errors import BrokerSubmissionHeld
    from app.broker.contract.models import BrokerOrderRequest

    broker = _FakeBroker()
    broken_gate = StreamHealthGate(
        market_data=lambda: ChannelHealth(
            stream="market_data", healthy=False, reason="feed down", observed_at_ms=_FIXED_MS
        ),
        execution=lambda: ChannelHealth(
            stream="execution", healthy=True, reason="", observed_at_ms=_FIXED_MS
        ),
    )
    clerk = AlpacaClerk(read=broker, trade=broker, clock=_fixed_clock, stream_health=broken_gate)

    with pytest.raises(BrokerSubmissionHeld):
        await clerk.submit(_request())
    assert clerk.is_on_hold() is True

    healthy_gate = StreamHealthGate(
        market_data=lambda: ChannelHealth(
            stream="market_data", healthy=True, reason="", observed_at_ms=_FIXED_MS
        ),
        execution=lambda: ChannelHealth(
            stream="execution", healthy=True, reason="", observed_at_ms=_FIXED_MS
        ),
    )
    clerk._stream_health = healthy_gate

    status = await clerk.clear_hold(operator="inkant", reason="channel restored")
    assert status.hold.active is False
```

`_request()` is this test file's existing helper for building a minimal `BrokerOrderRequest` (used by other tests in this file, e.g. `test_bot_namespace_submit_fill_attribution_and_projection`'s sibling submit tests — confirm its exact signature with `grep -n "def _request" tests/broker/alpaca/clerk/test_clerk_reconciliation.py`; if this file doesn't already define one, copy the one from `test_instance_orders.py` rather than inventing a third variant).

- [ ] **Step 4: Write the failing test — unknown hold reason refuses generically**

```python
async def test_clear_hold_refuses_unknown_reason_code() -> None:
    broker = _FakeBroker()
    clerk = AlpacaClerk(read=broker, trade=broker, clock=_fixed_clock)
    account_id, journal = await clerk._ensure_journal()
    await journal.append_async(
        OrderJournalEntry(
            kind=ClerkEntryKind.HOLD_SET,
            account_id=account_id,
            reason_code="SOME_FUTURE_HOLD_REASON",
            reason="a reason this clear-admission registry doesn't know",
            recorded_at_ms=_FIXED_MS,
        )
    )

    with pytest.raises(InventoryBaselineRefusedError):
        await clerk.clear_hold(operator="inkant", reason="attempt")

    assert clerk.is_on_hold() is True
```

- [ ] **Step 5: Write the failing test — the TOCTOU race**

A new foreign order arrives (via a second `reconcile_once()` sweep, simulating a concurrent observation) between this clear attempt's own reconciliation and its lock-protected append. Use an incrementing fake clock so `since_ms` ordering is meaningful (the file's shared `_fixed_clock` returns a constant and cannot distinguish before/after):

```python
async def test_clear_hold_refuses_when_new_foreign_order_arrives_during_reconciliation() -> None:
    ticks = iter(range(_FIXED_MS, _FIXED_MS + 1000, 10))
    incrementing_clock = lambda: next(ticks)  # noqa: E731

    broker = _FakeBroker(orders=[_order(client_order_id="foreign")])
    clerk = AlpacaClerk(read=broker, trade=broker, clock=incrementing_clock)
    await clerk.reconcile_once()
    assert clerk.is_on_hold() is True

    broker.orders = []  # looks clean to the upcoming reconciliation read...

    real_reconcile_once = clerk.reconcile_once

    async def _reconcile_then_inject_new_foreign_order():
        verdict = await real_reconcile_once()
        # ...but a second, independent foreign order is journaled between
        # the reconciliation read and clear_hold's lock-protected re-check.
        account_id, journal = await clerk._ensure_journal()
        await journal.append_async(
            OrderJournalEntry(
                kind=ClerkEntryKind.UNEXPLAINED_ORDER,
                account_id=account_id,
                recorded_at_ms=incrementing_clock(),
            )
        )
        return verdict

    clerk.reconcile_once = _reconcile_then_inject_new_foreign_order

    with pytest.raises(InventoryBaselineRefusedError):
        await clerk.clear_hold(operator="inkant", reason="reviewed")

    assert clerk.is_on_hold() is True
```

Use `ruff`'s exact lambda-assignment suppression convention already present elsewhere in this test file if `# noqa: E731` isn't the one this codebase uses (check `grep -n "noqa: E731\|lambda.*=" tests/broker/alpaca/clerk/*.py` first); if lambdas-assigned-to-names are avoided entirely in this file's existing style, use a small local `def` instead to match convention.

- [ ] **Step 6: Run all five new/modified tests to verify current failures**

Run: `podman exec polygon-data-service python -m pytest /app/tests/broker/alpaca/clerk/test_clerk_reconciliation.py -k "clear_hold_refuses or clear_hold_succeeds or clear_hold_stream_health or clear_hold_refuses_unknown or clear_hold_refuses_when_new_foreign" -v`
Expected: FAIL for `test_clear_hold_refuses_while_foreign_order_persists`, `test_clear_hold_refuses_unknown_reason_code`, `test_clear_hold_refuses_when_new_foreign_order_arrives_during_reconciliation` (clear currently succeeds in all three). `test_clear_hold_succeeds_once_foreign_order_is_gone` and `test_clear_hold_stream_health_reason_still_uses_channel_check` should already PASS (they describe behavior that should remain true) — if either fails, the fixture is wrong, not the production code; fix the fixture, not `clear_hold`.

- [ ] **Step 7: Implement the reason-code dispatch in `clear_hold`**

In `PythonDataService/app/broker/alpaca/clerk/clerk.py`, replace `clear_hold` (currently lines 563-582):

```python
    async def clear_hold(self, *, operator: str, reason: str) -> ClerkStatus:
        """Clear the active hold (operator exit) with reason-specific proof.

        Idempotent: clearing with no active hold is a journal-free NO-OP.
        The required proof depends on why the hold exists — a generic
        channel-health check is not sufficient for an ``UNEXPLAINED_ORDER``
        hold, which requires a fresh reconciliation proving the foreign
        order (and any other unresolved custody work) is actually gone.
        Unregistered reason codes refuse by default (fail closed).
        Returns the updated status for a one-round-trip render.
        """
        account_id, journal = await self._ensure_journal()
        entries = journal.read_entries()
        hold = derive.hold_state(entries)
        if not hold.active:
            return self._status_from(account_id, entries)

        proof_verdict: ReconciliationVerdict | None = None
        if hold.reason_code == STREAM_HEALTH_HOLD_CODE:
            if not self._channel_fresh():
                raise InventoryBaselineRefusedError(
                    "Exposure hold cannot be cleared while submission channels are unhealthy.",
                    detail="Restore both channels and reconcile before clearing the hold.",
                )
            proof_observed_at_ms = self._clock()
        elif hold.reason_code == UNEXPLAINED_ORDER_HOLD_CODE:
            proof_observed_at_ms = self._clock()
            proof_verdict = await self.reconcile_once()
            if proof_verdict != "clean":
                raise InventoryBaselineRefusedError(
                    "Exposure hold cannot be cleared: reconciliation is not clean.",
                    detail=f"The reconciliation verdict is '{proof_verdict}'.",
                )
        else:
            raise InventoryBaselineRefusedError(
                "Exposure hold cannot be cleared: no proof is registered for this hold reason.",
                detail=f"Unrecognized hold reason code '{hold.reason_code}'.",
            )

        async with self._intake_lock:
            entries = journal.read_entries()
            current_hold = derive.hold_state(entries)
            if (
                current_hold.active
                and current_hold.since_ms is not None
                and current_hold.since_ms > proof_observed_at_ms
            ):
                raise InventoryBaselineRefusedError(
                    "Exposure hold cannot be cleared: new evidence arrived after the proof was obtained.",
                    detail="A new hold condition was observed during the reconciliation; retry the clear.",
                )
            entries = await self._clear_hold_locked(
                journal=journal,
                account_id=account_id,
                operator=operator,
                reason=reason,
                verdict=proof_verdict,
            )
            return self._status_from(account_id, entries)
```

`_clear_hold_locked` (currently lines 584-619) needs one small change to accept and journal the proof reference. Replace its signature and body:

```python
    async def _clear_hold_locked(
        self,
        *,
        journal: OrderJournal,
        account_id: str,
        operator: str,
        reason: str,
        verdict: ReconciliationVerdict | None = None,
    ) -> list[OrderJournalEntry]:
        entries = journal.read_entries()
        hold = derive.hold_state(entries)
        if hold.active:
            await journal.append_async(
                OrderJournalEntry(
                    kind=ClerkEntryKind.HOLD_CLEARED,
                    account_id=account_id,
                    operator=operator,
                    reason_code=hold.reason_code or UNEXPLAINED_ORDER_HOLD_CODE,
                    reason=reason,
                    recorded_at_ms=self._clock(),
                    verdict=verdict,
                )
            )
```

(Only the new `verdict: ReconciliationVerdict | None = None` parameter and the `verdict=verdict` line added to the `OrderJournalEntry(...)` call change — the rest of the method, including its logging call and the no-op branch below, is unchanged.) `STREAM_HEALTH_HOLD_CODE`, `UNEXPLAINED_ORDER_HOLD_CODE`, and `ReconciliationVerdict` are already imported (lines 51-52, 65). `derive` is already imported (line 29). "Journal sequence at observation" from the design doc's Section 3.3 is deliberately **not** added here — a persisted monotonic journal sequence doesn't exist in this codebase yet; formalizing one is explicitly Workstream C's concern (out of scope per the design doc's Section 7 non-goals), not this task's.

- [ ] **Step 8: Run all five tests again to verify they pass**

Run: `podman exec polygon-data-service python -m pytest /app/tests/broker/alpaca/clerk/test_clerk_reconciliation.py -k "clear_hold_refuses or clear_hold_succeeds or clear_hold_stream_health or clear_hold_refuses_unknown or clear_hold_refuses_when_new_foreign" -v`
Expected: all 5 PASS.

- [ ] **Step 9: Run the full reconciliation test file**

Run: `podman exec polygon-data-service python -m pytest /app/tests/broker/alpaca/clerk/test_clerk_reconciliation.py -v`
Expected: all PASS. Pay particular attention to any other existing test that calls `clear_hold` on a stream-health-only hold or a no-op clear — those must still pass unchanged.

- [ ] **Step 10: Run the full Clerk test suite and lint**

Run: `podman exec polygon-data-service python -m pytest /app/tests/broker/alpaca/ -v`
Expected: all PASS — this exercises effects, instance orders, and reconciliation together, catching any cross-file interaction between Task 5's and Task 8's changes.

Run: `ruff check PythonDataService/app/ PythonDataService/tests/`
Expected: no warnings.

- [ ] **Step 11: Commit**

```bash
git add PythonDataService/app/broker/alpaca/clerk/clerk.py PythonDataService/tests/broker/alpaca/clerk/test_clerk_reconciliation.py
git commit -m "fix(alpaca-clerk): require reason-specific proof to clear an exposure hold (P0-2)"
```

---

## Task 9: ADR amendment documenting the fence and escalation behavior

**Files:**
- Modify: `docs/architecture/adrs/0033-account-custody-clocks-and-safety-contract.md`

No code. Records the decision per the design doc's Section 6 DoD item.

- [ ] **Step 1: Read the existing ADR 0033 to match its section structure**

Run: `grep -n "^##" docs/architecture/adrs/0033-account-custody-clocks-and-safety-contract.md`

- [ ] **Step 2: Append an amendment section**

Add, following whatever heading pattern the file's existing amendments (if any) use — check first whether 0033 already has an "## Amendment" pattern like ADR 0034 does, and match it exactly:

```markdown
## Amendment: per-run generation fence and Stop-timeout escalation (2026-08-03)

Extends the account-epoch fencing above to bot-run granularity, closing
the P0-3 gap the original ADR's account-level `AccountEpochAuthority`
did not cover: a decision made by one run before Stop, reaching the
Clerk after Stop, must be refused at the same boundary every other
submission goes through.

**Decision.** The Alpaca Clerk's ENTER admission path
(`ClerkEffectOperations._resolve_enter`) consults a durable per-instance
`DesiredState` (`RUNNING | PAUSED | STOPPED`) immediately before
evaluating exposure and submitting, via an injected
`desired_state_probe: Callable[[str], DesiredState]` — the same
dependency-injection shape as the existing `bot_running_probe`. This
reuses the exact durable field `BotTaskRegistry._stop_locked` already
writes before cancelling the task; no new durable state was introduced.
EXIT/reduction operations are exempt — they can only decrease exposure.

**Escalation on cancellation timeout.** When `task.cancel()` does not
terminate the task within `_STOP_TIMEOUT_S`, the bot runner no longer
finalizes a terminal `STOPPED` outcome or reaps the task — it stays
supervised and `status()` honestly reports it as still running (via the
unchanged `managed.task.done()` check), while `desired_state` carries
the STOPPED intent. This is scoped **per-instance only**; a timed-out
Stop does not place an account-wide hold. Rationale: the ENTER fence
above already makes a surviving task harmless to the broker regardless
of escalation scope, so an account-wide hold would stop healthy,
unrelated bots for a narrow single-instance race — a real availability
cost the fence's existence no longer justifies. Account-wide hold
remains available as a manual operator escalation if a timeout pattern
later suggests something systemic.

**Considered and rejected:** escalating to an account-wide hold
automatically on every cancellation timeout, matching this ADR's
original candidate table entry for "safest immediate backstop." Rejected
because the fence closes the actual safety gap; account-wide escalation
would only be justified if the fence itself were in question, which is
a different (and more serious) failure mode than a single slow
coroutine.
```

- [ ] **Step 3: Commit**

```bash
git add docs/architecture/adrs/0033-account-custody-clocks-and-safety-contract.md
git commit -m "docs(adr-0033): amend with per-run generation fence and Stop-timeout escalation"
```

---

## Task 10: Full project-scope verification

**Files:** none modified — verification only.

- [ ] **Step 1: Run the full Python test suite**

Run: `podman exec polygon-data-service python -m pytest /app/tests -v`
Expected: all pass except any pre-existing inherited failure already documented in the 2026-08-03 hardening doc (`test_action_execution.py::test_live_panel_skips_resume_admission_reconciliation`, baselined as pre-existing on `master`). If any *other* test fails, stop and fix it before proceeding — do not treat it as inherited without baselining via `git stash` against `origin/master` per `.claude/rules/testing.md`.

- [ ] **Step 2: Run project-scope ruff**

Run: `ruff check PythonDataService/app/ PythonDataService/tests/`
Expected: zero warnings.

- [ ] **Step 3: Run the `thermo-nuclear-code-quality-review` skill**

Per CLAUDE.md's hard rule, invoke it via the Skill tool before opening a PR. Address every major finding in-branch.

- [ ] **Step 4: Confirm the Workstream A exit gate checklist from the design doc's Section 6**

Manually verify each line:
- Every P0 (P0-1, P0-2, P0-3) has a deterministic test — check Task 1, Task 8 (Steps 1-5), Task 5+6 (combined).
- All new tests pass together in the full suite run (Step 1).
- No bypass exists: confirm (by reading, not just testing) that `submit_for_instance` (dead code, per the design doc Section 2.3) has no callers that could route around the fence — `grep -rn "submit_for_instance" PythonDataService/app/` should show only the definition and test usage, not a second live call site.

- [ ] **Step 5: Summarize for PR description**

Note in the eventual PR description: which P0s this closes, the accepted scope decisions (per-instance timeout escalation, one-step Clear Hold), the flagged-but-out-of-scope `kind` hardcoding finding (Task 3), and the router-fix discovery (Task 7) as an unplanned-but-necessary dependency of Task 8.
