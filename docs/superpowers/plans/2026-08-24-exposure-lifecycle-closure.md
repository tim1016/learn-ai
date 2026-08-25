# Exposure Lifecycle Closure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the exposure lifecycle for Alpaca/SQLite custody: every attributed position gets a presented, executable, Clerk-custody path to flat (F18); a terminal `EXIT_NOT_FLAT` is age-watched, boundedly re-driven, and durably escalated instead of silently permanent; and transient Clerk refusals on the EXIT path defer to the next decision clock instead of crashing healthy bots (F19).

**Architecture:** Three wiring moves, no new HTTP surface. (1) **F19** — a transient-vs-terminal refusal taxonomy lives beside `AdmissionBlockedError` in `uncertainty.py`; `resolve_accepted_exit` (exit.py) converts transient refusals into an honest "still accepted, sweep re-drives" snapshot, and the runner EXIT call site in `bot_trade_strategy.py` honors the same taxonomy as defense-in-depth. (2) **Stuck EXIT** — the account reconciliation pass (`reconcile.py`) gains an age watchdog over active `EXIT_NOT_FLAT` episodes: after 2 minutes it re-drives the reduction through a fresh recovery EXIT, at most 3 times, then raises a durable, operator-visible `EXIT_STUCK` uncertainty. (3) **F18** — the flatten executor is a **new recovery-catalog action `execute_safe_flatten`** that consumes the already-built `SafeFlattenPlan` and drives run-fence-exempt recovery EXITs through the existing claimed-broker-IO EXIT machine — **not** the `flatten_stop` panel performer.

**Locked design decision (F18 executor), with the code evidence:** the `flatten_stop` performer (`panel_data_source.py:1030-1067`) cannot serve the F18 case, for three reasons verified in the tree: (a) `accept_exit` runs `require_active_run` (`idempotency.py:77-89`) — a crashed bot has no active run (`runtime.recover()` at `runtime.py:806-819` retires every active run on restart), and `_flatten_stop`'s own first step (`registry.stop`) retires the run its second step (`execute_for_instance(run_id=binding.run_id)`) then requires; (b) `accept_exit` rejects colons in `decision_id` (`reject_colon`, `exit.py:68`) while the performer passes `decision_id=f"panel-flatten:{idempotency_key}"` — it would fail before broker contact even on a running bot (it predates SQLite custody and was never exercised under it); (c) the recovery-capability surface is already presented and executable end-to-end (`sqlite_panel_adapter.py:93-100` renders `projection.recovery_actions` as panel actions; `sqlite_panel_source.py:execute_sqlite_panel_action` dispatches them through `execute_recovery_action`) — F17 proved this live. So the executor is a recovery-catalog action: presentation comes free, the frozen `live_instances.py` router is untouched, and every broker contact flows through the per-op claim CAS (`resolve_exit` → `claim_before_broker_contact` → `ClaimedBrokerIO`). The run fence is replaced, not deleted: a recovery EXIT anchors its `run_id` to the run recorded on the targeted entry's effect operation, is reduction-only by construction (quantity re-derived from `repo.position` downstream, `require_capability(Capability.REDUCE, …)` enforces movement toward zero per leg), and is admitted only by the SafeFlattenPlan recheck gates or the watchdog policy.

**Tech Stack:** Python 3.11+ (`PythonDataService/`), event-sourced SQLite Clerk (`app/broker/alpaca/clerk/sqlite/`), pytest + pytest-asyncio, ruff. Frontend touch is limited to the generated vocabulary snapshot plus two closed copy/tone maps (Angular 22, Vitest).

**Spec:** docs/audits/strategy-execution-research-directions-2026-08-24.md (Direction 1). Supporting detail: docs/audits/bot-launch-ops-study-2026-08-24.md §8–§9; docs/known-gaps.md §1.

## Global Constraints

- **Time:** every temporal value in flight, at rest, or on the wire is `int64 ms UTC`; no ISO strings, no naive datetimes, no `DateTime` wire types (`.claude/rules/temporal-rigor.md`). All new fields carrying time are named `*_ms` and typed `int`.
- **Errors:** typed exceptions only; no silent catches (`except: pass` banned). Every new `except` clause either handles explicitly with a structured log or re-raises.
- **Logging:** `logger = logging.getLogger(__name__)` per module; structured `extra={...}` dicts with an `action` key; never `print()`, never f-string message interpolation.
- **Tests:** every behavior change ships a regression test that fails before the fix and passes after. pytest conventions per `.claude/rules/testing.md`: `test_<function>_<scenario>` names, function-scoped fixtures, explicit `atol`/`rtol` on any float comparison (`position_quantity_is_nonzero` is the custody-quantity comparator — use it, never a bare `==` on float quantities).
- **Router freeze:** `app/routers/live_instances.py` is frozen above 1,000 lines — this plan adds **zero** lines to any router; all new behavior lives in `app/broker/alpaca/clerk/sqlite/` and `app/services/`.
- **Lint:** `ruff check PythonDataService/app/ PythonDataService/tests/` must stay clean at project scope before every commit batch; `npx eslint Frontend/src/ --max-warnings 0` for the frontend touch in Task 10.
- **Paper admission:** the Paper evidence-only override is a permanent operator decision — nothing in this plan tightens paper admission. All new gates are on the *exit/recovery* side (reduction toward zero), never on entering paper.
- **Pre-push:** run the `thermo-nuclear-code-quality-review` skill once before the PR-opening push and fix every major finding; run the full suite `cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" python -m pytest tests` on the final tree (the empty secret prefix avoids ~33 router 403s per `.claude/rules/testing.md`).
- **Test invocation used throughout:** run scoped tests locally as `cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" python -m pytest <path> -v`. Never run the big suite inside the `polygon-data-service` container (cgroup OOM).
- **Vocabulary:** any new presented action id must be registered in `app/broker/v2panel/vocabulary.py` (Literal + `ACTION_IDS` + `OPERATOR_COPY`) and the snapshot regenerated via `scripts/regenerate_broker_v2_vocabulary_snapshot.py`; any Pydantic schema change requires `python scripts/export_openapi_contract.py` (OpenAPI contract CI gate).
- **Line-number caveat:** all `file:line` references below were verified against `origin/master` @ `2fd0df84` on 2026-08-24. Re-verify anchors before editing; match on the quoted code, not the number.

### Direction-1 research-question coverage

| Research question | Where answered |
|---|---|
| RQ1 — safe executor for `SafeFlattenPlan`: `flatten_stop` or new recovery action? | New recovery action (locked decision above); Tasks 4, 8, 9. `flatten_stop`'s two latent defects are recorded in ADR 0045 (Task 12). |
| RQ2 — retry/escalation policy for `EXIT_NOT_FLAT` | Age threshold 120 000 ms, bounded re-drive (max 3), durable `EXIT_STUCK` escalation; Tasks 5, 6, 7. |
| RQ3 — transient-vs-terminal taxonomy + its consumers | Taxonomy in Task 1. Consumers wired here: EXIT resolve boundary (Task 2), runner EXIT path (Task 3). Consumers already correct: the sweep (`reconcile.py:580` defers all `AdmissionBlockedError`), ENTER (`runtime.py:682`), manual orders. Consumers deferred with reason: deploy/resume admission *refusal copy* ordering is Direction 5's F5 slice — refusals there are 422s, not crashes, so no safety exposure. |
| RQ4 — third sweep comparison (strategy-intent vs journal) | **Deferred.** Needs a read-back seam on `SignalSession` lifecycle state that Direction 2's replay work will also want; deferring avoids designing that seam twice. The stuck-EXIT watchdog (Task 7) removes the concrete harm RQ4 cited (a strategy that believes it is flat stops trying to exit — the watchdog now re-drives regardless of strategy belief). Recorded in ADR 0045 §Deferred. |
| Done-when: crash→refuse-resume→flatten→resume-to-flat walkthrough | Task 11. |
| Done-when: stuck EXIT older than N minutes → durable operator-visible escalation | Tasks 6, 7. |
| Done-when: injected `BROKER_SNAPSHOT_STALE` on cohort exit → N deferred exits, zero crashes | Task 2 (cohort test), Task 3. |

---

### Task 1: Refusal taxonomy beside `AdmissionBlockedError`

**Files:**
- Modify: `PythonDataService/app/broker/alpaca/clerk/sqlite/uncertainty.py` (append after `class AdmissionBlockedError`, ~line 561; add to `__all__`)
- Test: `PythonDataService/tests/broker/alpaca/clerk/sqlite/test_uncertainty.py`

**Interfaces:**
- Consumes: existing constants `BROKER_SNAPSHOT_STALE_REASON_CODE`, `RECONCILIATION_INCOMPLETE_REASON_CODE`, `EXIT_NOT_FLAT_REASON_CODE` (already defined/imported in `uncertainty.py`); `StrEnum` (already imported — `Capability` uses it).
- Produces: `RefusalClass` (StrEnum: `TRANSIENT`/`TERMINAL`), `TRANSIENT_ADMISSION_REASON_CODES: frozenset[str]`, `classify_admission_refusal(reason_code: str | None) -> RefusalClass`. Tasks 2 and 3 import all three from `app.broker.alpaca.clerk.sqlite.uncertainty`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/broker/alpaca/clerk/sqlite/test_uncertainty.py` (extend its existing imports from `app.broker.alpaca.clerk.sqlite.uncertainty` with `RefusalClass, classify_admission_refusal`; add `EXIT_NOT_FLAT_REASON_CODE` if not already imported):

```python
def test_classify_admission_refusal_marks_sweep_resolvable_codes_transient() -> None:
    assert classify_admission_refusal(BROKER_SNAPSHOT_STALE_REASON_CODE) is RefusalClass.TRANSIENT
    assert classify_admission_refusal(RECONCILIATION_INCOMPLETE_REASON_CODE) is RefusalClass.TRANSIENT
    assert classify_admission_refusal("RECONCILIATION_IN_PROGRESS") is RefusalClass.TRANSIENT


def test_classify_admission_refusal_fails_closed_for_unknown_or_subject_codes() -> None:
    assert classify_admission_refusal(None) is RefusalClass.TERMINAL
    assert classify_admission_refusal("SOME_FUTURE_CODE") is RefusalClass.TERMINAL
    assert classify_admission_refusal(EXIT_NOT_FLAT_REASON_CODE) is RefusalClass.TERMINAL
```

- [ ] **Step 2: Run to verify failure**

Run: `cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" python -m pytest tests/broker/alpaca/clerk/sqlite/test_uncertainty.py -k classify_admission_refusal -v`
Expected: FAIL — `ImportError: cannot import name 'RefusalClass'`.

- [ ] **Step 3: Implement**

In `uncertainty.py`, directly below `class AdmissionBlockedError` (keep its existing body unchanged):

```python
class RefusalClass(StrEnum):
    """Whether an admission refusal self-heals via the reconciliation sweep."""

    TRANSIENT = "transient"
    TERMINAL = "terminal"


# Codes the automatic reconciliation sweep resolves without operator action:
# a fresh successful broker snapshot clears BROKER_SNAPSHOT_STALE, a complete
# pass clears RECONCILIATION_INCOMPLETE, and RECONCILIATION_IN_PROGRESS ends
# when the in-flight pass ends. Everything else — including every
# CUSTODY_SUBJECT episode and every unknown future code — stays TERMINAL so
# an unclassified refusal keeps today's fail-closed behavior (F19 fix shape:
# ops study §9 "classify snapshot-staleness admission blocks as
# retry-on-next-clock in the runner's error taxonomy").
TRANSIENT_ADMISSION_REASON_CODES: frozenset[str] = frozenset(
    {
        BROKER_SNAPSHOT_STALE_REASON_CODE,
        RECONCILIATION_INCOMPLETE_REASON_CODE,
        "RECONCILIATION_IN_PROGRESS",
    }
)


def classify_admission_refusal(reason_code: str | None) -> RefusalClass:
    if reason_code in TRANSIENT_ADMISSION_REASON_CODES:
        return RefusalClass.TRANSIENT
    return RefusalClass.TERMINAL
```

Add `"RefusalClass"`, `"TRANSIENT_ADMISSION_REASON_CODES"`, `"classify_admission_refusal"` to `__all__` (keep it sorted the way the file already sorts it).

- [ ] **Step 4: Run to verify pass**

Run: `cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" python -m pytest tests/broker/alpaca/clerk/sqlite/test_uncertainty.py -k classify_admission_refusal -v`
Expected: 2 passed.

- [ ] **Step 5: Lint and commit**

```bash
ruff check PythonDataService/app/ PythonDataService/tests/
git add PythonDataService/app/broker/alpaca/clerk/sqlite/uncertainty.py PythonDataService/tests/broker/alpaca/clerk/sqlite/test_uncertainty.py
git commit -m "feat(clerk): transient-vs-terminal refusal taxonomy beside AdmissionBlockedError"
```

---

### Task 2: Transient EXIT refusals defer instead of raising (F19 core)

**Files:**
- Modify: `PythonDataService/app/broker/alpaca/clerk/sqlite/exit.py` (`resolve_accepted_exit`, lines 175–196; module imports; add module logger)
- Test: `PythonDataService/tests/broker/alpaca/clerk/sqlite/test_exit.py`

**Interfaces:**
- Consumes: Task 1's `classify_admission_refusal`, `RefusalClass`; existing `AdmissionBlockedError` (exported by `uncertainty.py`); existing `_FakeTrade`, `_make_entry`, `_broker_order`, `repo` fixture, `SqliteTradeUpdateEvidenceSink` pattern in `test_exit.py` (lines 56–246 and 268–286).
- Produces: `resolve_accepted_exit` now returns the accepted snapshot (state still non-terminal, sweep-selectable) on a TRANSIENT refusal and re-raises on TERMINAL. Callers unchanged: `runtime._execute_effect:785` maps the returned `command.state == "accepted"` through `_effect_receipt` (runtime.py:1134-1185) to `EffectOperationState.ACCEPTED` with next_step "Await fresh broker evidence and automatic reconciliation." — no runtime.py edit needed. The sweep path (`_reconcile_effect` → `resolve_exit`) is untouched and keeps its own deferral at `reconcile.py:580`.

- [ ] **Step 1: Generalize the entry helper for cohort tests**

In `test_exit.py`, change `_make_entry`'s signature (line ~201) from `async def _make_entry(repo, *, decision_id="enter-1", quantity=10, filled_quantity=0.0, status="accepted")` to add `sid: str = SID, run_id: str = RUN_ID`, and replace the two hardcoded uses inside the helper (`strategy_instance_id=SID`, `lifecycle_run_id=RUN_ID`) with the parameters. Run the file to confirm no regressions: `cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" python -m pytest tests/broker/alpaca/clerk/sqlite/test_exit.py -x -q` — all pre-existing tests still pass.

- [ ] **Step 2: Write the failing tests**

Append to `test_exit.py`. Extend its `uncertainty` import block with `classify_admission_refusal` is not needed here — only the production shape of the stale episode (copied from `reconcile.py:430-445`) and the two assertions. Also import `resolve_accepted_exit` from `app.broker.alpaca.clerk.sqlite.exit` and `submit_start_run` is already imported.

```python
def _raise_stale_snapshot(repo: ClerkSqliteRepository) -> None:
    """The exact account-wide episode reconcile.py raises on a failed snapshot."""
    raise_uncertainty(
        repo,
        strategy_instance_id=None,
        reason_code=BROKER_SNAPSHOT_STALE_REASON_CODE,
        headline="Broker account truth is unavailable",
        explanation="test: snapshot read failed",
        operator_impact="New exposure and unproven reduction are paused account-wide.",
        next_step="Reconcile now after broker connectivity is restored.",
        evidence_refs=(),
        cause_facts={"snapshot": "open_orders_and_positions"},
        severity="error",
    )


async def _filled_entry_with_position(
    repo: ClerkSqliteRepository, *, sid: str = SID, run_id: str = RUN_ID
) -> tuple[str, BrokerOrder]:
    """Entry filled 10 @ 100 with an exact execution slice → attributed +10."""
    entry_ref = await _make_entry(
        repo, sid=sid, run_id=run_id, status="filled", filled_quantity=10
    )
    recovered = _broker_order(entry_ref, status="filled", filled_quantity=10, filled_avg_price=100)
    sink = SqliteTradeUpdateEvidenceSink(repo=repo, intake=ReentrantAsyncLock(), reconciler=_NoReconciler())
    await sink.record_lifecycle_event(
        client_order_id=entry_ref,
        event=BrokerOrderEvent(
            event_type="fill",
            occurred_at_ms=1_700_000_000_600,
            price=100,
            quantity=10,
            execution_id=f"exec-{entry_ref}",
        ),
        event_key=f"execution:exec-{entry_ref}",
        order=recovered,
        recovery_source=None,
        recovery_window_limit=None,
    )
    return entry_ref, recovered


async def test_resolve_accepted_exit_defers_transient_snapshot_stale_refusal(
    repo: ClerkSqliteRepository,
) -> None:
    entry_ref, recovered = await _filled_entry_with_position(repo)
    _raise_stale_snapshot(repo)
    accepted = accept_exit(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="exit-stale-defer",
        lifecycle_run_id=RUN_ID,
        entry_order_ref=entry_ref,
    )
    trade = _FakeTrade(lookup_results=[recovered, recovered])

    result = await resolve_accepted_exit(repo, accepted=accepted, trade=trade)

    assert result.reducing_order_ref is None
    assert trade.submit_calls == []
    effect = repo.effect_operation(accepted.effect_operation_id)
    assert effect is not None
    assert effect.state not in ("succeeded", "failed", "rejected")
    assert any(
        item.effect_operation_id == accepted.effect_operation_id
        for item in repo.reconcilable_effect_operations()
    )


async def test_resolve_accepted_exit_reraises_terminal_refusals(
    repo: ClerkSqliteRepository,
) -> None:
    entry_ref, recovered = await _filled_entry_with_position(repo)
    raise_uncertainty(
        repo,
        strategy_instance_id=None,
        reason_code="OPERATOR_CUSTODY_REVIEW",  # unknown code -> fail-closed TERMINAL
        headline="test terminal",
        explanation="unknown-cause episode fails closed account-wide",
        operator_impact="all mutation paused",
        next_step="operator review",
        evidence_refs=(),
        cause_facts={},
        severity="error",
    )
    accepted = accept_exit(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="exit-terminal-refusal",
        lifecycle_run_id=RUN_ID,
        entry_order_ref=entry_ref,
    )

    with pytest.raises(AdmissionBlockedError):
        await resolve_accepted_exit(
            repo, accepted=accepted, trade=_FakeTrade(lookup_results=[recovered, recovered])
        )


async def test_resolve_accepted_exit_defers_whole_cohort_without_a_crash(
    repo: ClerkSqliteRepository,
) -> None:
    """Done-when property: N same-clock exits under a stale snapshot -> N deferrals, zero raises."""
    sid2, run2 = "spy-bot-2", "run-2"
    repo.register_strategy_instance(strategy_instance_id=sid2, symbol="SPY", config_hash="h2")
    submit_start_run(repo, account_id=ACCOUNT_ID, strategy_instance_id=sid2, lifecycle_run_id=run2)
    entry_1, recovered_1 = await _filled_entry_with_position(repo)
    entry_2, recovered_2 = await _filled_entry_with_position(repo, sid=sid2, run_id=run2)
    _raise_stale_snapshot(repo)

    for sid, run_id, entry_ref, recovered in (
        (SID, RUN_ID, entry_1, recovered_1),
        (sid2, run2, entry_2, recovered_2),
    ):
        accepted = accept_exit(
            repo,
            account_id=ACCOUNT_ID,
            strategy_instance_id=sid,
            decision_id="exit-cohort-clock",
            lifecycle_run_id=run_id,
            entry_order_ref=entry_ref,
        )
        trade = _FakeTrade(lookup_results=[recovered, recovered])
        result = await resolve_accepted_exit(repo, accepted=accepted, trade=trade)
        assert result.reducing_order_ref is None
        assert trade.submit_calls == []
```

(`ReentrantAsyncLock`, `SqliteTradeUpdateEvidenceSink`, `BrokerOrderEvent`, `_NoReconciler`, `raise_uncertainty`, `BROKER_SNAPSHOT_STALE_REASON_CODE`, `AdmissionBlockedError` are all already imported at the top of `test_exit.py`; add `BrokerOrder` to the `app.broker.contract.models` import if missing.)

- [ ] **Step 3: Run to verify failure**

Run: `cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" python -m pytest tests/broker/alpaca/clerk/sqlite/test_exit.py -k "defers or reraises_terminal" -v`
Expected: the two `defers` tests FAIL with `AdmissionBlockedError: reduce blocked: BROKER_SNAPSHOT_STALE — …` (today's F19 crash shape); `reraises_terminal` may already pass (it pins current behavior).

- [ ] **Step 4: Implement**

In `exit.py`: add `import logging` and `logger = logging.getLogger(__name__)` below the imports; extend imports with `from app.broker.alpaca.clerk.sqlite.uncertainty import AdmissionBlockedError, RefusalClass, classify_admission_refusal` (no cycle: `uncertainty.py` imports only repository/facts/causes). Replace the body of `resolve_accepted_exit` (keep the docstring, extend it):

```python
async def resolve_accepted_exit(
    repo: ClerkSqliteRepository,
    *,
    accepted: ExitSubmission,
    trade: BrokerTradePort,
) -> ExitSubmission:
    """Drive a previously accepted EXIT outside the intake decision segment.

    A TRANSIENT admission refusal (see ``classify_admission_refusal``) is not
    an error for a durably accepted EXIT: the effect stays non-terminal, so
    ``reconcilable_effect_operations`` keeps selecting it and the 15 s sweep
    re-drives it once the refusal self-heals. Returning the accepted snapshot
    here is the F19 fix — the caller (runner or panel) sees an honest
    "accepted, await reconciliation" receipt instead of a crash. TERMINAL
    refusals still raise.
    """
    assert accepted.effect_operation_id is not None
    try:
        resolved = await resolve_exit(
            repo,
            effect_operation_id=accepted.effect_operation_id,
            trade=trade,
        )
    except OperationClaimError:
        if accepted.created:
            raise
        # A concurrent attempt already owns the broker-contact claim for this
        # exact durable EXIT. A transport retry returns the existing snapshot;
        # it must not turn idempotency into a 500 or contact the broker again.
        return accepted
    except AdmissionBlockedError as exc:
        if classify_admission_refusal(exc.decision.reason_code) is not RefusalClass.TRANSIENT:
            raise
        logger.warning(
            "Deferred a transient Clerk refusal on an accepted EXIT; the sweep re-drives it",
            extra={
                "action": "exit_transient_refusal_deferred",
                "effect_operation_id": accepted.effect_operation_id,
                "reason_code": exc.decision.reason_code,
            },
        )
        return accepted
    return replace(resolved, created=accepted.created)
```

- [ ] **Step 5: Run to verify pass, then the whole file**

Run: `cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" python -m pytest tests/broker/alpaca/clerk/sqlite/test_exit.py -v`
Expected: all pass (pre-existing + 3 new).

- [ ] **Step 6: Lint and commit**

```bash
ruff check PythonDataService/app/ PythonDataService/tests/
git add PythonDataService/app/broker/alpaca/clerk/sqlite/exit.py PythonDataService/tests/broker/alpaca/clerk/sqlite/test_exit.py
git commit -m "fix(clerk): defer transient admission refusals on an accepted EXIT instead of raising (F19)"
```

---

### Task 3: Runner EXIT path honors the taxonomy (F19 boundary)

**Files:**
- Modify: `PythonDataService/app/services/bot_trade_strategy.py` (imports; new helper below `_append_decision_receipt`; try/except around the `clerk.execute_for_instance` call at line ~757 inside `run_trade_bot`)
- Test: `PythonDataService/tests/services/test_bot_trade_strategy_discard.py`

**Interfaces:**
- Consumes: Task 1's `classify_admission_refusal`, `RefusalClass`; existing `AdmissionBlockedError`, `CapabilityDecision`, `Capability` from `app.broker.alpaca.clerk.sqlite.uncertainty`; existing module helpers `_discard_evaluation`, `_append_decision_receipt`, `_decision_bar_ref`; `BrokerBotBinding` factory shape from `tests/broker/alpaca/clerk/sqlite/test_runtime.py:107-121`.
- Produces: `_dispose_transient_exit_refusal(decision_receipts, *, binding, evaluation, exc) -> None` (raises the original `exc` when TERMINAL). Used only by `run_trade_bot`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/services/test_bot_trade_strategy_discard.py` (reuse its existing imports where present; the code below is self-contained modulo those):

```python
from dataclasses import dataclass, field

import pytest

from app.broker.alpaca.clerk.sqlite.uncertainty import (
    AdmissionBlockedError,
    BROKER_SNAPSHOT_STALE_REASON_CODE,
    Capability,
    CapabilityDecision,
)
from app.services import bot_trade_strategy as bts
from app.services.bot_binding_repository import BrokerBotBinding
from app.schemas.action_plan import alpaca_v1_action_plan


def _binding() -> BrokerBotBinding:
    # Same factory as tests/broker/alpaca/clerk/sqlite/test_runtime.py:107-121.
    return BrokerBotBinding(
        strategy_instance_id="spy-bot",
        strategy_key="deployment_validation",
        broker="alpaca",
        symbol="SPY",
        use_rth=True,
        mode="trade",
        quantity=1,
        carryover_policy="FORBID",
        sealed_account_id="PA-TEST",
        action_plan=alpaca_v1_action_plan("SPY"),
        run_id="run-1",
        created_at_ms=1,
    )


@dataclass
class _StubBar:
    feed_id: str = "test-feed"


class _StubEvaluation:
    evaluation_id = "eval-refusal-1"
    decision_bar_close_ms = 1_700_000_000_000
    bar = _StubBar()

    def __init__(self) -> None:
        self.settlements: list[object] = []

    def settle_stage(self, settlement: object) -> None:
        self.settlements.append(settlement)


class _RecorderReceipts:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def append(self, **kwargs: object) -> None:
        self.rows.append(kwargs)


def _refusal(reason_code: str) -> AdmissionBlockedError:
    return AdmissionBlockedError(
        CapabilityDecision(
            allowed=False,
            capability=Capability.REDUCE,
            reason_code=reason_code,
            why="test refusal",
        )
    )


def test_dispose_transient_exit_refusal_discards_and_records_blocked_receipt() -> None:
    receipts = _RecorderReceipts()
    evaluation = _StubEvaluation()

    bts._dispose_transient_exit_refusal(
        receipts,
        binding=_binding(),
        evaluation=evaluation,
        exc=_refusal(BROKER_SNAPSHOT_STALE_REASON_CODE),
    )

    assert evaluation.settlements == [bts.Settlement.DISCARD]
    assert len(receipts.rows) == 1
    assert receipts.rows[0]["outcome"] == "blocked"
    assert receipts.rows[0]["facts"]["reason_code"] == BROKER_SNAPSHOT_STALE_REASON_CODE


def test_dispose_transient_exit_refusal_reraises_terminal_refusals() -> None:
    receipts = _RecorderReceipts()
    evaluation = _StubEvaluation()

    with pytest.raises(AdmissionBlockedError):
        bts._dispose_transient_exit_refusal(
            receipts,
            binding=_binding(),
            evaluation=evaluation,
            exc=_refusal("UNKNOWN_FUTURE_CODE"),
        )

    assert evaluation.settlements == []
    assert receipts.rows == []
```

If `bts.Settlement` is not already a module attribute (check the import block of `bot_trade_strategy.py` — `Settlement` is used at `_settle_evaluation`, line ~551), reference it from wherever `bot_trade_strategy` imports it and assert on that symbol instead; the assertion intent is "exactly one DISCARD settlement".

- [ ] **Step 2: Run to verify failure**

Run: `cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" python -m pytest tests/services/test_bot_trade_strategy_discard.py -k dispose_transient -v`
Expected: FAIL — `AttributeError: module ... has no attribute '_dispose_transient_exit_refusal'`.

- [ ] **Step 3: Implement**

In `bot_trade_strategy.py`: extend the `uncertainty` import (add a new import line — the module does not import from `sqlite.uncertainty` yet):

```python
from app.broker.alpaca.clerk.sqlite.uncertainty import (
    AdmissionBlockedError,
    RefusalClass,
    classify_admission_refusal,
)
```

Add below `_append_decision_receipt` (~line 840):

```python
def _dispose_transient_exit_refusal(
    decision_receipts: SqliteDecisionReceipts,
    *,
    binding: BrokerBotBinding,
    evaluation: StrategyEvaluation,
    exc: AdmissionBlockedError,
) -> None:
    """Settle one staged decision whose Clerk refusal is TRANSIENT (F19).

    TERMINAL refusals re-raise so an unclassified admission failure stays
    honest crash evidence at ``_supervise``'s boundary. TRANSIENT refusals
    (snapshot staleness during same-clock cohort reduces, ops study §9) are
    refused-and-deferred: DISCARD the staged candidate, record a protected
    ``blocked`` receipt, and let the next decision clock retry.
    """
    if classify_admission_refusal(exc.decision.reason_code) is not RefusalClass.TRANSIENT:
        raise exc
    _discard_evaluation(evaluation)
    _append_decision_receipt(
        decision_receipts,
        binding=binding,
        evaluation=evaluation,
        outcome="blocked",
        reason_code=exc.decision.reason_code or "ADMISSION_BLOCKED",
    )
    logger.warning(
        "Trade bot deferred a transient Clerk admission refusal to the next decision clock",
        extra={
            "action": "bot_admission_refusal_deferred",
            "strategy_instance_id": binding.strategy_instance_id,
            "strategy_key": binding.strategy_key,
            "symbol": binding.symbol,
            "reason_code": exc.decision.reason_code,
        },
    )
```

Note the type annotations reference `SqliteDecisionReceipts` and `StrategyEvaluation` exactly as `_append_decision_receipt` (line 811) already does — copy its annotation imports; the test's stubs satisfy them structurally at runtime.

Then wrap the call at line ~757 in `run_trade_bot`:

```python
        try:
            receipt = await clerk.execute_for_instance(
                strategy_instance_id=binding.strategy_instance_id,
                run_id=binding.run_id,
                decision_id=decision_id,
                purpose=_EFFECT_PURPOSE_BY_INTENT[intent.kind],
                action_plan=binding.action_plan,
                quantity=binding.quantity,
                use_rth=binding.use_rth,
                capability_account_id=capability_account_id,
                decision_evidence=EffectDecisionEvidence(
                    evaluation_id=decision_id,
                    bar_ref=_decision_bar_ref(binding, evaluation),
                    symbol=binding.symbol,
                    outcome=(
                        "enter_intent"
                        if intent.kind is SignalIntentKind.ENTER
                        else "exit_intent"
                    ),
                    observed_at_ms=now_ms_utc(),
                ),
            )
        except AdmissionBlockedError as exc:
            _dispose_transient_exit_refusal(
                decision_receipts, binding=binding, evaluation=evaluation, exc=exc
            )
            continue
```

(The argument list is verbatim today's call — only the `try/except/continue` is new.)

- [ ] **Step 4: Run to verify pass**

Run: `cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" python -m pytest tests/services/test_bot_trade_strategy_discard.py -v`
Expected: all pass (pre-existing + 2 new).

- [ ] **Step 5: Lint and commit**

```bash
ruff check PythonDataService/app/ PythonDataService/tests/
git add PythonDataService/app/services/bot_trade_strategy.py PythonDataService/tests/services/test_bot_trade_strategy_discard.py
git commit -m "fix(runner): honor the refusal taxonomy at the EXIT call site instead of crashing (F19)"
```

---

### Task 4: Run-fence-exempt recovery EXIT accept

**Files:**
- Modify: `PythonDataService/app/broker/alpaca/clerk/sqlite/exit.py` (factor the shared capture out of `accept_exit`; add `accept_recovery_exit`)
- Test: `PythonDataService/tests/broker/alpaca/clerk/sqlite/test_exit.py`

**Interfaces:**
- Consumes: everything `accept_exit` already uses (`_exit_identity`, `require_strategy_instance`, `require_active_run`, `require_owned_entry_order`, `entry_order_symbol`, `ExitAcceptedFacts`, `TransitionInput`, `repo.commit_first_transition`, `CommandCreated`/`CommandExistingSame`/`CommandExistingConflict`, `DurableConflictError`); `repo.effect_operation` (read API) for the exposure-anchored `run_id`.
- Produces: `accept_recovery_exit(repo, *, account_id: str, strategy_instance_id: str, decision_id: str, entry_order_ref: str, forbid_active_run: bool = False) -> ExitSubmission` and `class RecoveryRunActiveError(Exception)`. Consumed by Task 7 (watchdog re-drive, default `forbid_active_run=False` — a stuck EXIT on a *running* bot is re-drivable by design) and Task 8 (flatten executor core, `forbid_active_run=True` — re-asserted inside the capture transaction to close the recheck→capture Resume race). `decision_id` must be colon-free (callers use the namespaces `exit-redrive-<episode-hex12>-<n>` and `recovery-flatten-<hex16>`; note `_exit_identity` at `exit.py:36-53` keys idempotency on `(strategy_instance_id, decision_id)` **only** — the entry ref is in the payload hash, not the key — so every caller-namespace must be globally unique per intent, which is why the redrive id carries the episode token).

- [ ] **Step 1: Write the failing tests**

Append to `test_exit.py` (imports: add `accept_recovery_exit, RecoveryRunActiveError` to the `exit` import; `submit_stop_run` add to the `commands` import — `submit_start_run` is already there):

```python
async def test_accept_recovery_exit_captures_reduction_without_active_run(
    repo: ClerkSqliteRepository,
) -> None:
    entry_ref, recovered = await _filled_entry_with_position(repo)
    submit_stop_run(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        lifecycle_run_id=RUN_ID,
        operator_reason="test_crash_analog",
    )
    with pytest.raises(NoActiveRunError):
        accept_exit(
            repo,
            account_id=ACCOUNT_ID,
            strategy_instance_id=SID,
            decision_id="exit-after-stop",
            lifecycle_run_id=RUN_ID,
            entry_order_ref=entry_ref,
        )

    accepted = accept_recovery_exit(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="recovery-flatten-abc123",
        entry_order_ref=entry_ref,
    )

    assert accepted.created is True
    assert accepted.effect_operation_id is not None
    trade = _FakeTrade(
        lookup_results=[recovered, recovered],
        submit_result=_broker_order("placeholder", side="sell", status="accepted"),
    )
    resolved = await resolve_accepted_exit(repo, accepted=accepted, trade=trade)
    assert resolved.reducing_order_ref is not None
    submitted_leg, submitted_ref = trade.submit_calls[0]
    assert submitted_ref == resolved.reducing_order_ref
    assert submitted_leg.side == "sell"
    assert submitted_leg.quantity == 10


async def test_accept_recovery_exit_is_idempotent_per_decision_id(
    repo: ClerkSqliteRepository,
) -> None:
    entry_ref, _ = await _filled_entry_with_position(repo)
    submit_stop_run(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        lifecycle_run_id=RUN_ID,
        operator_reason="test_crash_analog",
    )
    first = accept_recovery_exit(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="recovery-flatten-abc123",
        entry_order_ref=entry_ref,
    )
    retry = accept_recovery_exit(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="recovery-flatten-abc123",
        entry_order_ref=entry_ref,
    )

    assert retry.created is False
    assert retry.effect_operation_id == first.effect_operation_id


async def test_accept_recovery_exit_forbid_active_run_fails_closed_under_live_run(
    repo: ClerkSqliteRepository,
) -> None:
    """P0 race guard: the no-active-run fact must hold inside the capture
    transaction itself, not only at recovery-policy recheck time."""
    entry_ref, _ = await _filled_entry_with_position(repo)  # run still ACTIVE

    with pytest.raises(RecoveryRunActiveError):
        accept_recovery_exit(
            repo,
            account_id=ACCOUNT_ID,
            strategy_instance_id=SID,
            decision_id="recovery-flatten-raceguard",
            entry_order_ref=entry_ref,
            forbid_active_run=True,
        )

    # The watchdog path (default forbid_active_run=False) still captures under
    # a live run — a stuck EXIT on a running bot is re-drivable by design.
    accepted = accept_recovery_exit(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="exit-redrive-abcdef123456-1",
        entry_order_ref=entry_ref,
    )
    assert accepted.created is True
```

- [ ] **Step 2: Run to verify failure**

Run: `cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" python -m pytest tests/broker/alpaca/clerk/sqlite/test_exit.py -k accept_recovery_exit -v`
Expected: FAIL — `ImportError: cannot import name 'accept_recovery_exit'`.

- [ ] **Step 3: Implement**

In `exit.py`, refactor rather than duplicate: extract the body shared by both accepts into one private capture function parameterized on run identity, then make both public functions thin. (Before writing, grep `_fold_exit_accepted` in `folds.py` for `kind` usage; it replays the command kind — keep `kind="strategy_decision"` so the fold path is byte-identical. The durable distinguisher for recovery exits is the `decision_id` namespace, which lands in `ExitAcceptedFacts.decision_id`.)

```python
def _accept_exit_capture(
    repo: ClerkSqliteRepository,
    *,
    account_id: str,
    strategy_instance_id: str,
    decision_id: str,
    entry_order_ref: str,
    resolve_run_id: Callable[[OrderResource], str],
    decision_receipt: AtomicDecisionReceipt | None,
) -> ExitSubmission:
    reject_colon("strategy_instance_id", strategy_instance_id)
    reject_colon("decision_id", decision_id)
    idempotency_key, payload_hash, command_id, effect_idempotency_key = _exit_identity(
        account_id=account_id,
        strategy_instance_id=strategy_instance_id,
        decision_id=decision_id,
        entry_order_ref=entry_order_ref,
    )

    def build_transition() -> TransitionInput:
        require_strategy_instance(repo, strategy_instance_id)
        target = require_owned_entry_order(
            repo,
            strategy_instance_id=strategy_instance_id,
            entry_order_ref=entry_order_ref,
        )
        run_id = resolve_run_id(target)
        symbol = entry_order_symbol(repo, target.order_ref)
        entry_order_refs: list[str] = []
        for candidate in repo.entry_orders_for_strategy(strategy_instance_id):
            if entry_order_symbol(repo, candidate.order_ref) != symbol:
                continue
            require_owned_entry_order(
                repo,
                strategy_instance_id=strategy_instance_id,
                entry_order_ref=candidate.order_ref,
            )
            entry_order_refs.append(candidate.order_ref)
        effect_operation_id = f"effect:{idempotency_key}"
        facts = ExitAcceptedFacts(
            idempotency_key=idempotency_key,
            payload_hash=payload_hash,
            kind="strategy_decision",
            action=ACTION_EXIT,
            intended_end_state=None,
            effect_idempotency_key=effect_idempotency_key,
            effect_kind="EXIT",
            decision_id=decision_id,
            entry_order_ref=entry_order_ref,
            entry_order_refs=entry_order_refs,
        )
        return TransitionInput(
            strategy_instance_id=strategy_instance_id,
            run_id=run_id,
            command_id=command_id,
            effect_operation_id=effect_operation_id,
            order_ref=entry_order_ref,
            transition_kind="EXIT_ACCEPTED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="accepted",
            clerk_observed_at_ms=repo.clock(),
            summary_code="EXIT_ACCEPTED",
            facts_json=facts.to_facts_json(),
        )

    outcome = repo.commit_first_transition(
        command_id=command_id,
        idempotency_key=idempotency_key,
        payload_hash=payload_hash,
        build_transition=build_transition,
        decision_receipt=decision_receipt,
    )
    if isinstance(outcome, CommandExistingConflict):
        raise DurableConflictError(outcome.command)
    if isinstance(outcome, CommandExistingSame):
        return ExitSubmission(
            command=outcome.command,
            effect_operation_id=outcome.command.effect_operation_id,
            entry_order_ref=entry_order_ref,
            reducing_order_ref=_reducing_order_ref(repo, outcome.command.effect_operation_id),
            created=False,
        )
    assert isinstance(outcome, CommandCreated)
    return ExitSubmission(
        command=outcome.command,
        effect_operation_id=outcome.command.effect_operation_id,
        entry_order_ref=entry_order_ref,
        reducing_order_ref=None,
        created=True,
    )
```

`accept_exit` becomes a delegation preserving today's exact semantics and signature (its `build_transition` ordering — `require_strategy_instance`, then `require_active_run`, then `require_owned_entry_order` — is preserved because `resolve_run_id` for the strategy path calls `require_active_run` before the target is otherwise used; run the existing `test_exit.py` suite to prove no ordering-sensitive test regresses; if one does, inline `require_active_run` into a pre-target hook instead of the target-keyed resolver):

```python
def accept_exit(
    repo: ClerkSqliteRepository,
    *,
    account_id: str,
    strategy_instance_id: str,
    decision_id: str,
    lifecycle_run_id: str,
    entry_order_ref: str,
    decision_receipt: AtomicDecisionReceipt | None = None,
) -> ExitSubmission:
    """Capture one EXIT and every same-strategy/symbol entry before contact."""
    reject_colon("lifecycle_run_id", lifecycle_run_id)

    def resolve_run_id(_target: OrderResource) -> str:
        return require_active_run(repo, strategy_instance_id, lifecycle_run_id).run_id

    return _accept_exit_capture(
        repo,
        account_id=account_id,
        strategy_instance_id=strategy_instance_id,
        decision_id=decision_id,
        entry_order_ref=entry_order_ref,
        resolve_run_id=resolve_run_id,
        decision_receipt=decision_receipt,
    )


class RecoveryRunActiveError(Exception):
    """A recovery EXIT that forbids live runs found one at capture time.

    Raised inside the ``build_transition`` closure — i.e. under the
    repository write lock — so a Resume landing between recovery-policy
    recheck and EXIT capture fails closed instead of racing the flatten.
    """

    def __init__(self, strategy_instance_id: str) -> None:
        self.strategy_instance_id = strategy_instance_id
        super().__init__(
            f"strategy instance {strategy_instance_id!r} re-activated a run "
            "before the recovery EXIT was captured"
        )


def accept_recovery_exit(
    repo: ClerkSqliteRepository,
    *,
    account_id: str,
    strategy_instance_id: str,
    decision_id: str,
    entry_order_ref: str,
    forbid_active_run: bool = False,
) -> ExitSubmission:
    """Capture one reduction-only recovery EXIT without the active-run fence.

    ``accept_exit`` requires the caller's ``lifecycle_run_id`` to be the
    currently ACTIVE run (``require_active_run``) — correct for strategy
    decisions, and exactly why crash/stop-held exposure (F18) and a stuck
    EXIT could never be re-driven: after a crash, ``runtime.recover()``
    retires every active run. A recovery EXIT is anchored to the *exposure*,
    not to a live run: its ``run_id`` is the run recorded on the targeted
    entry's effect operation. Admission is owned by the caller (the
    SafeFlattenPlan recheck gates, or the stuck-EXIT watchdog policy) plus
    the downstream ``require_capability(Capability.REDUCE, …)`` in
    ``exit_resolution.py``, which only authorizes movement toward zero.

    ``forbid_active_run=True`` (the safe-flatten executor) additionally
    re-asserts *inside the capture transaction* that no run is ACTIVE —
    recovery-policy already refused presentation with ``RUN_STILL_ACTIVE``,
    but a Resume (approved-carryover resumes are legitimate while exposure
    is held) can land between recheck and capture; the closure runs under
    the repository write lock, so this check cannot race a registration
    commit. The watchdog re-drive keeps the default ``False``: a stuck EXIT
    on a running bot is re-drivable by design.

    Decision-id namespaces: ``recovery-flatten-<hex16>``,
    ``exit-redrive-<episode-hex12>-<n>`` (both colon-free; the idempotency
    key is ``(strategy_instance_id, decision_id)`` only — see
    ``_exit_identity`` — so each namespace must be unique per intent).
    """

    def resolve_run_id(target: OrderResource) -> str:
        if forbid_active_run and repo.active_run(strategy_instance_id) is not None:
            raise RecoveryRunActiveError(strategy_instance_id)
        origin = repo.effect_operation(target.effect_operation_id)
        assert origin is not None, "an owned ENTRY order always has an effect operation"
        return origin.run_id

    return _accept_exit_capture(
        repo,
        account_id=account_id,
        strategy_instance_id=strategy_instance_id,
        decision_id=decision_id,
        entry_order_ref=entry_order_ref,
        resolve_run_id=resolve_run_id,
        decision_receipt=None,
    )
```

Add `Callable` to the module's `collections.abc` imports and `OrderResource` to the `.models` import if not present.

- [ ] **Step 4: Run to verify pass, then the whole file**

Run: `cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" python -m pytest tests/broker/alpaca/clerk/sqlite/test_exit.py -v`
Expected: all pass — the refactor must not change any pre-existing `accept_exit` test outcome.

- [ ] **Step 5: Lint and commit**

```bash
ruff check PythonDataService/app/ PythonDataService/tests/
git add PythonDataService/app/broker/alpaca/clerk/sqlite/exit.py PythonDataService/tests/broker/alpaca/clerk/sqlite/test_exit.py
git commit -m "feat(clerk): run-fence-exempt recovery EXIT accept anchored to exposure custody"
```

---

### Task 5: Redrive-count read

**Files:**
- Modify: `PythonDataService/app/broker/alpaca/clerk/sqlite/reads.py` (append near `reconcilable_effect_operations`, line ~490)
- Modify: `PythonDataService/app/broker/alpaca/clerk/sqlite/repository_read_api.py` (append near `reconcilable_effect_operations`, line ~358; mirror the adjacent method-body pattern exactly)
- Test: `PythonDataService/tests/broker/alpaca/clerk/sqlite/test_exit.py`

**Interfaces:**
- Produces: `reads.exit_effects_created_since(conn, strategy_instance_id: str, since_ms: int) -> int` and the read-API method `repo.exit_effects_created_since(strategy_instance_id, since_ms) -> int`. Consumed by Task 7. Semantics: every EXIT effect row for the strategy with `created_at_ms >= since_ms` — since the original failed EXIT's row predates the `EXIT_NOT_FLAT` episode's `observed_at_ms`, this counts exactly the re-drives.

- [ ] **Step 1: Write the failing test**

Append to `test_exit.py`:

```python
async def test_exit_effects_created_since_counts_only_rows_at_or_after_cutoff(
    repo: ClerkSqliteRepository,
) -> None:
    entry_ref, _ = await _filled_entry_with_position(repo)
    before_accept_ms = repo.clock()
    accept_exit(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="exit-count-1",
        lifecycle_run_id=RUN_ID,
        entry_order_ref=entry_ref,
    )

    assert repo.exit_effects_created_since(SID, before_accept_ms) == 1
    assert repo.exit_effects_created_since(SID, before_accept_ms + 1) == 0
    assert repo.exit_effects_created_since("other-bot", before_accept_ms) == 0
```

- [ ] **Step 2: Run to verify failure**

Run: `cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" python -m pytest tests/broker/alpaca/clerk/sqlite/test_exit.py -k created_since -v`
Expected: FAIL — `AttributeError: 'ClerkSqliteRepository' object has no attribute 'exit_effects_created_since'`.

- [ ] **Step 3: Implement**

`reads.py`:

```python
def exit_effects_created_since(
    conn: sqlite3.Connection, strategy_instance_id: str, since_ms: int
) -> int:
    """Count EXIT effect operations created at or after ``since_ms``.

    The stuck-EXIT watchdog's re-drive audit: the failed EXIT that raised an
    ``EXIT_NOT_FLAT`` episode was created before the episode's
    ``observed_at_ms``, so rows at/after it are exactly the re-drives.
    """
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM effect_operations "
        "WHERE strategy_instance_id = ? AND kind = 'EXIT' AND created_at_ms >= ?",
        (strategy_instance_id, since_ms),
    ).fetchone()
    return int(row["n"])
```

`repository_read_api.py` — add beside `reconcilable_effect_operations`, with the identical body shape its neighbors use (they delegate to `reads.<name>(self._conn, ...)` — copy the `position` method's exact delegation pattern):

```python
    def exit_effects_created_since(
        self: ClerkSqliteRepository, strategy_instance_id: str, since_ms: int
    ) -> int:
        return reads.exit_effects_created_since(self._conn, strategy_instance_id, since_ms)
```

- [ ] **Step 4: Run to verify pass**

Run: `cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" python -m pytest tests/broker/alpaca/clerk/sqlite/test_exit.py -k created_since -v`
Expected: PASS.

- [ ] **Step 5: Lint and commit**

```bash
ruff check PythonDataService/app/ PythonDataService/tests/
git add PythonDataService/app/broker/alpaca/clerk/sqlite/reads.py PythonDataService/app/broker/alpaca/clerk/sqlite/repository_read_api.py PythonDataService/tests/broker/alpaca/clerk/sqlite/test_exit.py
git commit -m "feat(clerk): exit_effects_created_since read for the stuck-EXIT redrive audit"
```

---

### Task 6: `EXIT_STUCK` escalation vocabulary

**Files:**
- Modify: `PythonDataService/app/broker/alpaca/clerk/sqlite/uncertainty_causes.py` (new constant + cause dataclass beside `ExitNotFlatCause`, line ~88)
- Modify: `PythonDataService/app/broker/alpaca/clerk/sqlite/uncertainty.py` (validator, `_REASON_POLICIES` entry, REDUCE-allowance branch in `decide_capability`, `__all__`)
- Test: `PythonDataService/tests/broker/alpaca/clerk/sqlite/test_uncertainty.py`

**Interfaces:**
- Consumes: existing `_require_exact_keys`, `_finite_number` helpers in `uncertainty_causes.py`; `ReasonPolicy`, `raise_uncertainty`, `decide_capability`, `_moves_toward_zero_without_crossing`, `UncertaintyRaisedFacts` in `uncertainty.py`.
- Produces: `EXIT_STUCK_REASON_CODE = "EXIT_STUCK"`, `ExitStuckCause(symbol: str, attributed_qty: float, redrive_count: int, first_observed_at_ms: int)` with `to_mapping()`/`from_mapping()`; a `_REASON_POLICIES` entry (`scope="CUSTODY_SUBJECT"`, `blocks_new_exposure=True`, `allows_reduction=True`); `decide_capability` allows a REDUCE moving toward zero on the episode's symbol (mirroring `EXIT_NOT_FLAT`). Consumed by Task 7 (raise) and Task 9 (execute-decision uncertainty filter).

- [ ] **Step 1: Write the failing tests**

Append to `test_uncertainty.py` (use its existing repo fixture if one exists; otherwise create one exactly like `test_exit.py:56-65` including `register_strategy_instance` + `submit_start_run` — read the file's existing fixtures first and reuse):

```python
def test_exit_stuck_cause_roundtrips_and_rejects_malformed_mappings() -> None:
    cause = ExitStuckCause(
        symbol="SPY", attributed_qty=4.0, redrive_count=3, first_observed_at_ms=1_700_000_000_000
    )
    assert ExitStuckCause.from_mapping(cause.to_mapping()) == cause
    with pytest.raises(ValueError):
        ExitStuckCause.from_mapping({"symbol": "spy", "attributed_qty": 4.0,
                                     "redrive_count": 3, "first_observed_at_ms": 1})
    with pytest.raises(ValueError):
        ExitStuckCause.from_mapping({"symbol": "SPY", "attributed_qty": 4.0,
                                     "redrive_count": -1, "first_observed_at_ms": 1})


def test_exit_stuck_blocks_new_exposure_and_foreign_symbol_reduction(repo) -> None:
    raise_uncertainty(
        repo,
        strategy_instance_id=SID,
        reason_code=EXIT_STUCK_REASON_CODE,
        headline="A stuck EXIT exhausted automatic re-drives",
        explanation="test",
        operator_impact="new exposure paused; exact reduction available",
        next_step="execute the presented safe flatten",
        evidence_refs=(),
        cause_facts=ExitStuckCause(
            symbol="SPY", attributed_qty=4.0, redrive_count=3,
            first_observed_at_ms=1_700_000_000_000,
        ).to_mapping(),
        severity="error",
    )

    blocked_enter = decide_capability(repo, capability=Capability.NEW_EXPOSURE, strategy_instance_id=SID)
    assert blocked_enter.allowed is False
    assert blocked_enter.reason_code == EXIT_STUCK_REASON_CODE

    blocked_reduce = decide_capability(
        repo,
        capability=Capability.REDUCE,
        strategy_instance_id=SID,
        reduction_intent=ReductionIntent(symbol="QQQ", side="SELL", quantity=4),
    )
    assert blocked_reduce.allowed is False
```

(The toward-zero *allowed* branch needs a real attributed position and is exercised end-to-end by Task 7's redrive test and Task 11's walkthrough.)

- [ ] **Step 2: Run to verify failure**

Run: `cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" python -m pytest tests/broker/alpaca/clerk/sqlite/test_uncertainty.py -k exit_stuck -v`
Expected: FAIL — `ImportError: cannot import name 'ExitStuckCause'`.

- [ ] **Step 3: Implement**

`uncertainty_causes.py`, beside `EXIT_NOT_FLAT_REASON_CODE` / `ExitNotFlatCause`:

```python
EXIT_STUCK_REASON_CODE = "EXIT_STUCK"


@dataclass(frozen=True)
class ExitStuckCause:
    """A stale EXIT_NOT_FLAT episode that exhausted automatic re-drives."""

    symbol: str
    attributed_qty: float
    redrive_count: int
    first_observed_at_ms: int

    def to_mapping(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "attributed_qty": self.attributed_qty,
            "redrive_count": self.redrive_count,
            "first_observed_at_ms": self.first_observed_at_ms,
        }

    @classmethod
    def from_mapping(cls, value: Any) -> ExitStuckCause:
        if not isinstance(value, dict):
            raise ValueError("EXIT-stuck cause must be an object")
        _require_exact_keys(
            value, {"symbol", "attributed_qty", "redrive_count", "first_observed_at_ms"}
        )
        symbol = value["symbol"]
        if not isinstance(symbol, str) or not symbol or symbol != symbol.upper():
            raise ValueError("EXIT-stuck symbol must be a non-empty uppercase string")
        redrive_count = value["redrive_count"]
        if not isinstance(redrive_count, int) or isinstance(redrive_count, bool) or redrive_count < 0:
            raise ValueError("EXIT-stuck redrive_count must be a non-negative integer")
        first_observed_at_ms = value["first_observed_at_ms"]
        if (
            not isinstance(first_observed_at_ms, int)
            or isinstance(first_observed_at_ms, bool)
            or first_observed_at_ms < 0
        ):
            raise ValueError("EXIT-stuck first_observed_at_ms must be int64 ms UTC")
        return cls(
            symbol=symbol,
            attributed_qty=_finite_number(value["attributed_qty"], field_name="attributed_qty"),
            redrive_count=redrive_count,
            first_observed_at_ms=first_observed_at_ms,
        )
```

`uncertainty.py`: import `EXIT_STUCK_REASON_CODE, ExitStuckCause` from `uncertainty_causes` (extend the existing import). Beside `_exit_not_flat_cause_is_valid`:

```python
def _exit_stuck_cause_is_valid(value: Any) -> bool:
    try:
        ExitStuckCause.from_mapping(value)
    except ValueError:
        return False
    return True
```

Add to `_REASON_POLICIES` (after the `EXIT_NOT_FLAT_REASON_CODE` entry):

```python
    EXIT_STUCK_REASON_CODE: ReasonPolicy(
        scope="CUSTODY_SUBJECT",
        blocks_new_exposure=True,
        allows_reduction=True,
        cause_is_valid=_exit_stuck_cause_is_valid,
    ),
```

Beside `_exit_not_flat_allows_action`:

```python
def _exit_stuck_allows_action(
    *,
    facts: UncertaintyRaisedFacts,
    intent: ReductionIntent | None,
) -> bool:
    if intent is None or intent.quantity <= 0 or intent.side.upper() not in {"BUY", "SELL"}:
        return False
    try:
        cause = ExitStuckCause.from_mapping(facts.cause_facts)
    except ValueError:
        return False
    return cause.symbol == intent.symbol.upper()
```

In `decide_capability`, inside the existing reduce-allowance `or (...)` chain (lines ~473-499), append a third alternative after the `EXIT_NOT_FLAT` clause, structurally identical to it:

```python
                    or (
                        reason_code == EXIT_STUCK_REASON_CODE
                        and _exit_stuck_allows_action(
                            facts=facts,
                            intent=reduction_intent,
                        )
                        and strategy_instance_id is not None
                        and reduction_intent is not None
                        and _moves_toward_zero_without_crossing(
                            repo.position(
                                strategy_instance_id,
                                reduction_intent.symbol.upper(),
                            ),
                            reduction_intent.signed_delta,
                        )
                    )
```

Add `EXIT_STUCK_REASON_CODE` to `uncertainty.py`'s `__all__`.

- [ ] **Step 4: Run to verify pass**

Run: `cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" python -m pytest tests/broker/alpaca/clerk/sqlite/test_uncertainty.py -v`
Expected: all pass (pre-existing + new).

- [ ] **Step 5: Lint and commit**

```bash
ruff check PythonDataService/app/ PythonDataService/tests/
git add PythonDataService/app/broker/alpaca/clerk/sqlite/uncertainty_causes.py PythonDataService/app/broker/alpaca/clerk/sqlite/uncertainty.py PythonDataService/tests/broker/alpaca/clerk/sqlite/test_uncertainty.py
git commit -m "feat(clerk): EXIT_STUCK escalation reason code with reduction-toward-zero allowance"
```

---

### Task 7: Stuck-EXIT watchdog in the reconciliation pass

**Files:**
- Modify: `PythonDataService/app/broker/alpaca/clerk/sqlite/reconcile.py` (two constants; new `_redrive_or_escalate_stale_exits`; one call in `_reconcile_account_serialized` after `_recover_operations`, line ~745)
- Test: `PythonDataService/tests/broker/alpaca/clerk/sqlite/test_reconcile.py`

**Interfaces:**
- Consumes: Task 4's `accept_recovery_exit` + existing `resolve_accepted_exit` (import both from `app.broker.alpaca.clerk.sqlite.exit`); Task 5's `repo.exit_effects_created_since`; Task 6's `EXIT_STUCK_REASON_CODE`, `ExitStuckCause`; existing `repo.strategy_instances()`, `repo.active_uncertainty(...)`, `repo.position(...)`, `repo.entry_orders_for_strategy(...)`, `repo.active_exit_for_order(...)`, `entry_order_symbol` (from `order_evidence`), `ExitNotFlatCause`, `UncertaintyRaisedFacts` (import from the same module `uncertainty.py` imports it from — check its import block), `raise_uncertainty`, `position_quantity_is_nonzero`, `_under_intake`, `OperationClaimError`, `AdmissionBlockedError`, `DurableConflictError` (from `app.broker.alpaca.clerk.sqlite.idempotency`).
- Produces: module constants `EXIT_NOT_FLAT_REDRIVE_AFTER_MS = 120_000` (8 sweep cycles at the 15 s cadence — long enough to rule out in-flight fill evidence, short enough to matter intraday) and `EXIT_NOT_FLAT_MAX_REDRIVES = 3`; `async _redrive_or_escalate_stale_exits(repo, *, trade, intake) -> None` running inside every reconciliation pass.

- [ ] **Step 1: Write the failing tests**

Append to `test_reconcile.py`. It already has `_FakeRead(orders=…, positions=…)`, `_position(symbol, quantity=…)`, `_FakeTrade`, `_broker_order` (lines 100–258). Add a clocked fixture plus a held-position helper (imports to add: `submit_start_run` from `commands`, `submit_enter` from `enter`, `fold_order_evidence` from `order_evidence`, `SqliteTradeUpdateEvidenceSink` from `app.broker.alpaca.clerk.trade_evidence`, `BrokerOrderEvent` from `app.broker.contract.models`, `ReentrantAsyncLock` from wherever this file's runtime imports resolve it — `app.broker.alpaca.clerk.sqlite.runtime` re-exports it, matching `test_exit.py`; `raise_uncertainty`, `resolve_exit_not_flat_uncertainty`, `EXIT_NOT_FLAT_REASON_CODE` from `uncertainty`; `ExitNotFlatCause`, `EXIT_STUCK_REASON_CODE` from `uncertainty_causes`; `_clock_at` from this package's `conftest`; stdlib `import hashlib`; `reconcile` module itself for monkeypatching):

```python
WATCHDOG_SID = "wd-bot"
WATCHDOG_RUN = "wd-run-1"


@pytest.fixture
def clocked_repo(tmp_path: Path):
    clock = _clock_at(1_700_000_000_000)
    repo = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock, lease_ttl_ms=300_000
    )
    repo.register_strategy_instance(
        strategy_instance_id=WATCHDOG_SID, symbol="SPY", config_hash="wd-h1"
    )
    submit_start_run(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=WATCHDOG_SID,
        lifecycle_run_id=WATCHDOG_RUN,
    )
    yield repo, clock
    repo.close()


class _NoReconciler:
    async def reconcile_account(self, *, trigger: str):
        raise AssertionError(f"unexpected reconciliation trigger: {trigger}")


async def _held_position(repo: ClerkSqliteRepository) -> str:
    """Filled 10-share SPY entry with an exact execution slice -> attributed +10."""
    submission = await submit_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=WATCHDOG_SID,
        decision_id="wd-enter-1",
        lifecycle_run_id=WATCHDOG_RUN,
        leg=_leg(quantity=10),
        trade=_FakeTrade(),
    )
    assert submission.order_ref is not None
    filled = _broker_order(
        submission.order_ref, status="filled", filled_quantity=10, filled_avg_price=100.0
    )
    fold_order_evidence(
        repo, effect_operation_id=submission.effect_operation_id, order=filled
    )
    sink = SqliteTradeUpdateEvidenceSink(
        repo=repo, intake=ReentrantAsyncLock(), reconciler=_NoReconciler()
    )
    await sink.record_lifecycle_event(
        client_order_id=submission.order_ref,
        event=BrokerOrderEvent(
            event_type="fill", occurred_at_ms=1_700_000_000_600,
            price=100, quantity=10, execution_id="wd-exec-1",
        ),
        event_key="execution:wd-exec-1",
        order=filled,
        recovery_source=None,
        recovery_window_limit=None,
    )
    return submission.order_ref


def _raise_exit_not_flat(repo: ClerkSqliteRepository, *, attributed_qty: float) -> None:
    raise_uncertainty(
        repo,
        strategy_instance_id=WATCHDOG_SID,
        reason_code=EXIT_NOT_FLAT_REASON_CODE,
        headline="A completed EXIT left attributed exposure",
        explanation="test: reducing order resolved without flattening",
        operator_impact="New exposure is paused for this strategy.",
        next_step="Run another EXIT or reconcile until attributed exposure is flat.",
        evidence_refs=("wd-evidence",),
        cause_facts=ExitNotFlatCause(symbol="SPY", attributed_qty=attributed_qty).to_mapping(),
        severity="error",
    )


async def test_reconcile_account_redrives_stale_exit_not_flat(clocked_repo) -> None:
    repo, clock = clocked_repo
    await _held_position(repo)
    _raise_exit_not_flat(repo, attributed_qty=10.0)
    episode = repo.active_uncertainty(
        scope="CUSTODY_SUBJECT",
        reason_code=EXIT_NOT_FLAT_REASON_CODE,
        strategy_instance_id=WATCHDOG_SID,
    )
    assert episode is not None
    clock.advance(reconcile_module.EXIT_NOT_FLAT_REDRIVE_AFTER_MS + 1)
    trade = _FakeTrade()

    await reconcile_account(
        repo,
        read=_FakeRead(positions=[_position("SPY", quantity=10.0)]),
        trade=trade,
    )

    assert repo.exit_effects_created_since(WATCHDOG_SID, episode["observed_at_ms"]) == 1
    assert len(trade.submit_calls) == 1  # the recovery reducing order reached the broker


async def test_reconcile_account_does_not_redrive_a_fresh_exit_not_flat(clocked_repo) -> None:
    repo, clock = clocked_repo
    await _held_position(repo)
    _raise_exit_not_flat(repo, attributed_qty=10.0)
    episode = repo.active_uncertainty(
        scope="CUSTODY_SUBJECT",
        reason_code=EXIT_NOT_FLAT_REASON_CODE,
        strategy_instance_id=WATCHDOG_SID,
    )
    clock.advance(reconcile_module.EXIT_NOT_FLAT_REDRIVE_AFTER_MS - 1)

    await reconcile_account(
        repo,
        read=_FakeRead(positions=[_position("SPY", quantity=10.0)]),
        trade=_FakeTrade(),
    )

    assert repo.exit_effects_created_since(WATCHDOG_SID, episode["observed_at_ms"]) == 0


async def test_reconcile_account_escalates_exit_stuck_after_redrive_cap(
    clocked_repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, clock = clocked_repo
    await _held_position(repo)
    _raise_exit_not_flat(repo, attributed_qty=10.0)
    clock.advance(reconcile_module.EXIT_NOT_FLAT_REDRIVE_AFTER_MS + 1)
    monkeypatch.setattr(reconcile_module, "EXIT_NOT_FLAT_MAX_REDRIVES", 0)

    await reconcile_account(
        repo,
        read=_FakeRead(positions=[_position("SPY", quantity=10.0)]),
        trade=_FakeTrade(),
    )

    stuck = repo.active_uncertainty(
        scope="CUSTODY_SUBJECT",
        reason_code=EXIT_STUCK_REASON_CODE,
        strategy_instance_id=WATCHDOG_SID,
    )
    assert stuck is not None  # durable, operator-visible escalation


async def test_watchdog_redrive_identity_is_scoped_per_episode(clocked_repo) -> None:
    """P1 regression: a later, independent stuck episode for the same strategy
    must mint fresh redrive identities. A bare `exit-redrive-<n>` collides:
    `_exit_identity` keys idempotency on (strategy_instance_id, decision_id)
    only, so a reused id either replays the earlier episode's terminal effect
    (same entry ref) or raises CommandExistingConflict forever (new entry ref,
    different payload hash)."""
    repo, clock = clocked_repo
    await _held_position(repo)
    _raise_exit_not_flat(repo, attributed_qty=10.0)
    episode_a = repo.active_uncertainty(
        scope="CUSTODY_SUBJECT",
        reason_code=EXIT_NOT_FLAT_REASON_CODE,
        strategy_instance_id=WATCHDOG_SID,
    )
    assert episode_a is not None
    clock.advance(reconcile_module.EXIT_NOT_FLAT_REDRIVE_AFTER_MS + 1)
    await reconcile_account(
        repo,
        read=_FakeRead(positions=[_position("SPY", quantity=10.0)]),
        trade=_FakeTrade(),
    )
    token_a = hashlib.sha256(episode_a["uncertainty_id"].encode("utf-8")).hexdigest()[:12]
    command_a = repo.get_command(f"cmd:{WATCHDOG_SID}:exit-redrive-{token_a}-1")
    assert command_a is not None

    # Episode A's redrive fills completely -> flat -> the fence resolves A.
    reducing_a = next(
        order
        for order in repo.orders_for_effect_operation(command_a.effect_operation_id)
        if order.role == "REDUCING"
    )
    filled_reducing = _broker_order(
        reducing_a.order_ref, status="filled", filled_quantity=10, filled_avg_price=101.0
    )
    sink = SqliteTradeUpdateEvidenceSink(
        repo=repo, intake=ReentrantAsyncLock(), reconciler=_NoReconciler()
    )
    await sink.record_lifecycle_event(
        client_order_id=reducing_a.order_ref,
        event=BrokerOrderEvent(
            event_type="fill", occurred_at_ms=repo.clock(),
            price=101.0, quantity=10, execution_id="wd-exec-flat-a",
        ),
        event_key="execution:wd-exec-flat-a",
        order=filled_reducing,
        recovery_source=None,
        recovery_window_limit=None,
    )
    await reconcile_account(repo, read=_FakeRead(positions=[]), trade=_FakeTrade())
    assert repo.active_uncertainty(
        scope="CUSTODY_SUBJECT",
        reason_code=EXIT_NOT_FLAT_REASON_CODE,
        strategy_instance_id=WATCHDOG_SID,
    ) is None

    # A fresh entry gets stuck later: independent episode B on a new entry ref.
    submission = await submit_enter(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=WATCHDOG_SID,
        decision_id="wd-enter-2", lifecycle_run_id=WATCHDOG_RUN,
        leg=_leg(quantity=10), trade=_FakeTrade(),
    )
    filled_entry = _broker_order(
        submission.order_ref, status="filled", filled_quantity=10, filled_avg_price=100.0
    )
    fold_order_evidence(
        repo, effect_operation_id=submission.effect_operation_id, order=filled_entry
    )
    await sink.record_lifecycle_event(
        client_order_id=submission.order_ref,
        event=BrokerOrderEvent(
            event_type="fill", occurred_at_ms=repo.clock(),
            price=100.0, quantity=10, execution_id="wd-exec-enter-2",
        ),
        event_key="execution:wd-exec-enter-2",
        order=filled_entry,
        recovery_source=None,
        recovery_window_limit=None,
    )
    clock.advance(1_000)
    _raise_exit_not_flat(repo, attributed_qty=10.0)
    episode_b = repo.active_uncertainty(
        scope="CUSTODY_SUBJECT",
        reason_code=EXIT_NOT_FLAT_REASON_CODE,
        strategy_instance_id=WATCHDOG_SID,
    )
    assert episode_b is not None
    assert episode_b["uncertainty_id"] != episode_a["uncertainty_id"]
    clock.advance(reconcile_module.EXIT_NOT_FLAT_REDRIVE_AFTER_MS + 1)
    await reconcile_account(
        repo,
        read=_FakeRead(positions=[_position("SPY", quantity=10.0)]),
        trade=_FakeTrade(),
    )

    token_b = hashlib.sha256(episode_b["uncertainty_id"].encode("utf-8")).hexdigest()[:12]
    assert token_b != token_a
    assert repo.get_command(f"cmd:{WATCHDOG_SID}:exit-redrive-{token_b}-1") is not None
    assert repo.exit_effects_created_since(WATCHDOG_SID, episode_b["observed_at_ms"]) == 1
```

Add `import app.broker.alpaca.clerk.sqlite.reconcile as reconcile_module` to the imports (the file may already import names from it — keep both).

- [ ] **Step 2: Run to verify failure**

Run: `cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" python -m pytest tests/broker/alpaca/clerk/sqlite/test_reconcile.py -k "redrives_stale or does_not_redrive or escalates_exit_stuck" -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'EXIT_NOT_FLAT_REDRIVE_AFTER_MS'`.

- [ ] **Step 3: Implement**

In `reconcile.py`, after the existing module constants:

```python
# Stuck-EXIT watchdog policy (Direction 1 RQ2): after ~8 sweep cycles a
# terminal EXIT_NOT_FLAT is re-driven through a fresh recovery EXIT, at most
# EXIT_NOT_FLAT_MAX_REDRIVES times, then escalated durably as EXIT_STUCK.
EXIT_NOT_FLAT_REDRIVE_AFTER_MS = 120_000
EXIT_NOT_FLAT_MAX_REDRIVES = 3
```

New function (place directly after `_resolve_flat_exit_fences`):

```python
async def _redrive_or_escalate_stale_exits(
    repo: ClerkSqliteRepository,
    *,
    trade: BrokerTradePort,
    intake: ReentrantAsyncLock,
) -> None:
    """Age-gate active EXIT_NOT_FLAT episodes: bounded re-drive, then escalate.

    A terminal EXIT_NOT_FLAT folds its effect to ``failed``, which
    ``reconcilable_effect_operations`` never re-selects, and
    ``_resolve_flat_exit_fences`` clears the episode only if exposure happens
    to reach flat — without this step a stuck EXIT is re-driven never,
    forever (research directions 2026-08-24, Direction 1).
    """
    now_ms = repo.clock()
    for instance in repo.strategy_instances():
        sid = instance["strategy_instance_id"]
        episode = repo.active_uncertainty(
            scope="CUSTODY_SUBJECT",
            reason_code=EXIT_NOT_FLAT_REASON_CODE,
            strategy_instance_id=sid,
        )
        if episode is None or now_ms - episode["observed_at_ms"] < EXIT_NOT_FLAT_REDRIVE_AFTER_MS:
            continue
        try:
            facts = UncertaintyRaisedFacts.from_facts_json(episode["facts_json"])
            cause = ExitNotFlatCause.from_mapping(facts.cause_facts)
        except (TypeError, ValueError, KeyError):
            logger.error(
                "stale EXIT_NOT_FLAT episode carries unreadable cause facts",
                extra={
                    "action": "exit_watchdog_unreadable_cause",
                    "account_id": repo.account_id,
                    "strategy_instance_id": sid,
                    "uncertainty_id": episode["uncertainty_id"],
                },
            )
            continue
        remaining = repo.position(sid, cause.symbol)
        if not position_quantity_is_nonzero(remaining):
            continue  # the flat fence resolver clears this episode in this pass
        redrives = repo.exit_effects_created_since(sid, episode["observed_at_ms"])
        if redrives >= EXIT_NOT_FLAT_MAX_REDRIVES:
            escalated = await _under_intake(
                intake,
                raise_uncertainty,
                repo,
                strategy_instance_id=sid,
                reason_code=EXIT_STUCK_REASON_CODE,
                headline="A stuck EXIT exhausted automatic re-drives",
                explanation=(
                    f"{remaining:g} {cause.symbol} remains attributed after "
                    f"{redrives} automatic EXIT re-drives."
                ),
                operator_impact=(
                    "New exposure stays paused for this strategy and automatic "
                    "re-drives stopped. Exact operator reduction remains available."
                ),
                next_step="Run Reconcile now, then execute the presented safe flatten.",
                evidence_refs=(episode["uncertainty_id"],),
                cause_facts=ExitStuckCause(
                    symbol=cause.symbol,
                    attributed_qty=remaining,
                    redrive_count=redrives,
                    first_observed_at_ms=episode["observed_at_ms"],
                ).to_mapping(),
                severity="error",
            )
            if escalated:
                logger.error(
                    "stuck EXIT escalated to a durable operator-visible EXIT_STUCK episode",
                    extra={
                        "action": "exit_stuck_escalated",
                        "account_id": repo.account_id,
                        "strategy_instance_id": sid,
                        "symbol": cause.symbol,
                        "redrive_count": redrives,
                        "age_ms": now_ms - episode["observed_at_ms"],
                    },
                )
            continue
        entries = [
            order
            for order in repo.entry_orders_for_strategy(sid)
            if entry_order_symbol(repo, order.order_ref).upper() == cause.symbol
            and repo.active_exit_for_order(order.order_ref) is None
        ]
        if not entries:
            continue
        # Episode-scoped redrive identity. `_exit_identity` keys idempotency on
        # (strategy_instance_id, decision_id) only, so a bare `exit-redrive-<n>`
        # would collide with an earlier, independent episode's redrives — either
        # replaying its terminal effect (same entry) or conflicting durably
        # (new entry). Uncertainty ids are minted as "uncertainty:<seq>"
        # (colon-bearing), so hash to a colon-free hex token.
        episode_token = hashlib.sha256(
            episode["uncertainty_id"].encode("utf-8")
        ).hexdigest()[:12]
        try:
            accepted = await _under_intake(
                intake,
                accept_recovery_exit,
                repo,
                account_id=repo.account_id,
                strategy_instance_id=sid,
                decision_id=f"exit-redrive-{episode_token}-{redrives + 1}",
                entry_order_ref=entries[-1].order_ref,
            )
            await resolve_accepted_exit(repo, accepted=accepted, trade=trade)
        except (OperationClaimError, AdmissionBlockedError, DurableConflictError):
            logger.info(
                "deferred a contended or policy-blocked stuck-EXIT re-drive",
                extra={
                    "action": "exit_redrive_deferred",
                    "account_id": repo.account_id,
                    "strategy_instance_id": sid,
                },
            )
            continue
        logger.warning(
            "re-drove a stale EXIT_NOT_FLAT episode with a fresh recovery EXIT",
            extra={
                "action": "exit_redrive_submitted",
                "account_id": repo.account_id,
                "strategy_instance_id": sid,
                "symbol": cause.symbol,
                "attempt": redrives + 1,
            },
        )
```

Call site in `_reconcile_account_serialized`, directly after the `resolved_count = await _recover_operations(...)` block and before the "Re-read broker truth" comment (the existing comment already anticipates recovery submitting reducing orders — the final snapshot re-read folds this watchdog's orders too):

```python
    await _redrive_or_escalate_stale_exits(repo, trade=trade, intake=intake)
```

Imports to add at the top of `reconcile.py`: `import hashlib` (stdlib block); `accept_recovery_exit, resolve_accepted_exit` from `.exit`; `DurableConflictError` from `.idempotency`; `entry_order_symbol` from `.order_evidence`; `EXIT_NOT_FLAT_REASON_CODE` (extend the existing `.uncertainty` import; `raise_uncertainty`, `AdmissionBlockedError` may already be there — check), `EXIT_STUCK_REASON_CODE, ExitNotFlatCause, ExitStuckCause` from `.uncertainty_causes`; `UncertaintyRaisedFacts` from the module `uncertainty.py` imports it from (check `uncertainty.py`'s import block — likely `.facts`); `position_quantity_is_nonzero` from `.folds` if not already imported.

Note on Task 2 interplay: `resolve_accepted_exit` now absorbs TRANSIENT refusals itself; the `except AdmissionBlockedError` here still catches TERMINAL ones (e.g. a concurrent unknown-cause account episode) — both classes defer the redrive to the next pass, matching `_recover_operations`' policy at `reconcile.py:580`.

- [ ] **Step 4: Run to verify pass, then the whole file**

Run: `cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" python -m pytest tests/broker/alpaca/clerk/sqlite/test_reconcile.py -v`
Expected: all pass. If a pre-existing reconcile test now sees an extra broker submit, that test seeded an aged EXIT_NOT_FLAT episode — inspect rather than loosen: the watchdog must only fire on episodes older than the threshold.

- [ ] **Step 5: Lint and commit**

```bash
ruff check PythonDataService/app/ PythonDataService/tests/
git add PythonDataService/app/broker/alpaca/clerk/sqlite/reconcile.py PythonDataService/tests/broker/alpaca/clerk/sqlite/test_reconcile.py
git commit -m "feat(clerk): stuck-EXIT watchdog - bounded redrive then durable EXIT_STUCK escalation"
```

---

### Task 8: Safe-flatten executor core + facade method

**Files:**
- Create: `PythonDataService/app/broker/alpaca/clerk/sqlite/safe_flatten_execution.py`
- Modify: `PythonDataService/app/broker/alpaca/clerk/sqlite/runtime.py` (facade method after `stop_strategy_run`, line ~475; imports)
- Test: `PythonDataService/tests/broker/alpaca/clerk/sqlite/test_safe_flatten_execution.py` (new)

**Interfaces:**
- Consumes: Task 4's `accept_recovery_exit` (called with `forbid_active_run=True`) and `RecoveryRunActiveError`; existing `resolve_accepted_exit`, `entry_order_symbol`, `SafeFlattenPlan` (dataclass in `projection_models.py:186-197`: `version_token, account_id, authority_generation, db_identity_token, control_revision, scope, strategy_instance_id, reconciliation_id, prepared_at_ms, expires_at_ms, legs`), `SafeFlattenPlanLeg` (`strategy_instance_id, symbol, side, quantity, position_updated_at_ms`), `OrderResource`, `ClerkSqliteRepository`, `BrokerTradePort`, `ReentrantAsyncLock` — import the lock type from its defining module (check: `app.broker.alpaca.clerk.sqlite.intake_fence`; `runtime.py` re-exports it, but `safe_flatten_execution` must NOT import `runtime` or a cycle forms with the facade import added below).
- Produces: `class SafeFlattenExecutionError(Exception)`; `async execute_safe_flatten_plan(repo, *, plan: SafeFlattenPlan, trade: BrokerTradePort, intake: ReentrantAsyncLock, account_id: str) -> tuple[OrderResource, ...]`; facade method `SqliteAlpacaClerkFacade.execute_safe_flatten(*, plan: SafeFlattenPlan, reason: str | None = None) -> tuple[OrderResource, ...]`. Consumed by Task 9's dispatcher branch and Task 11.

- [ ] **Step 1: Write the failing test**

Create `tests/broker/alpaca/clerk/sqlite/test_safe_flatten_execution.py`. Reuse the proven harness pieces by import where the repo's test-suite style allows or by local copy where they are module-private (copy `_leg`, `_broker_order`, `_position`, `_FakeTrade`, `_FakeRead` from `test_reconcile.py:96-229`, and the `_held_position`-style entry builder from Task 7 — module-private helpers are copied per file in this suite; keep the copies byte-identical):

```python
"""Executor-side acceptance for the prepared SafeFlattenPlan (F18)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.sqlite.commands import submit_start_run, submit_stop_run
from app.broker.alpaca.clerk.sqlite.projections import SqliteClerkProjectionReader
from app.broker.alpaca.clerk.sqlite.reconcile import reconcile_account
from app.broker.alpaca.clerk.sqlite.recovery_policy import build_recovery_catalog
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import ReentrantAsyncLock
from app.broker.alpaca.clerk.sqlite.safe_flatten_execution import (
    SafeFlattenExecutionError,
    execute_safe_flatten_plan,
)
from tests.broker.alpaca.clerk.sqlite.conftest import _clock_at

ACCOUNT_ID = "PA-FLATTEN"
SID = "crashed-bot"
RUN_ID = "run-1"

# ... copied helpers (_leg, _broker_order, _position, _FakeTrade, _FakeRead,
# _NoReconciler, _held_position adapted to ACCOUNT_ID/SID/RUN_ID) ...


@pytest.fixture
def crashed_with_exposure(tmp_path: Path):
    """F18 shape: filled entry, attributed +10, run stopped (crash analog)."""
    clock = _clock_at(1_700_000_000_000)
    repo = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock, lease_ttl_ms=300_000
    )
    repo.register_strategy_instance(strategy_instance_id=SID, symbol="SPY", config_hash="h1")
    submit_start_run(repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id=RUN_ID)
    yield repo, clock
    repo.close()


async def _reconciled_flatten_plan(repo):
    """Operator flow: Reconcile now -> presented plan (production path)."""
    await reconcile_account(
        repo,
        read=_FakeRead(positions=[_position("SPY", quantity=10.0)]),
        trade=_FakeTrade(),
        trigger="OPERATOR_RECONCILE_NOW",
    )
    reader = SqliteClerkProjectionReader.from_repository(repo)
    try:
        context = reader.recovery_context(strategy_instance_id=SID)
    finally:
        reader.close()
    assert context is not None
    catalog = {item.action_id: item for item in build_recovery_catalog(context)}
    prepare = catalog["prepare_safe_flatten"]
    assert prepare.available, prepare.unavailable_reason
    assert prepare.reduction_plan is not None
    return prepare.reduction_plan


async def test_execute_safe_flatten_plan_reduces_attributed_exposure_exactly(
    crashed_with_exposure,
) -> None:
    repo, _clock = crashed_with_exposure
    await _held_position(repo)
    submit_stop_run(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID,
        lifecycle_run_id=RUN_ID, operator_reason="crash_analog",
    )
    plan = await _reconciled_flatten_plan(repo)
    trade = _FakeTrade()

    orders = await execute_safe_flatten_plan(
        repo, plan=plan, trade=trade, intake=ReentrantAsyncLock(), account_id=ACCOUNT_ID
    )

    assert len(orders) == 1
    assert len(trade.submit_calls) == 1
    reducing = repo.order(orders[0].order_ref)
    assert reducing is not None and reducing.role == "REDUCING"


async def test_execute_safe_flatten_plan_refuses_expired_plans(
    crashed_with_exposure,
) -> None:
    repo, clock = crashed_with_exposure
    await _held_position(repo)
    submit_stop_run(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID,
        lifecycle_run_id=RUN_ID, operator_reason="crash_analog",
    )
    plan = await _reconciled_flatten_plan(repo)
    clock.advance(plan.expires_at_ms - clock.value + 1)

    with pytest.raises(SafeFlattenExecutionError, match="expired"):
        await execute_safe_flatten_plan(
            repo, plan=plan, trade=_FakeTrade(), intake=ReentrantAsyncLock(),
            account_id=ACCOUNT_ID,
        )


async def test_execute_safe_flatten_plan_refuses_when_a_resume_landed_after_recheck(
    crashed_with_exposure,
) -> None:
    """P0 race regression: policy checks no-active-run at presentation/recheck,
    but execution happens later. A Resume landing before EXIT capture must fail
    closed inside the capture transaction, never submit a reduction."""
    repo, _clock = crashed_with_exposure
    await _held_position(repo)
    submit_stop_run(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID,
        lifecycle_run_id=RUN_ID, operator_reason="crash_analog",
    )
    plan = await _reconciled_flatten_plan(repo)
    # Resume analog lands between recheck and capture (approved-carryover
    # resumes are legitimate while custody holds exposure).
    submit_start_run(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id="run-2"
    )
    trade = _FakeTrade()

    with pytest.raises(SafeFlattenExecutionError, match="re-activated"):
        await execute_safe_flatten_plan(
            repo, plan=plan, trade=trade, intake=ReentrantAsyncLock(),
            account_id=ACCOUNT_ID,
        )

    assert trade.submit_calls == []
```

Note on `_FakeTrade` variant: the `test_reconcile.py` copy always submits `side="buy"` orders — extend the copied helper so `submit` echoes `leg.side` into the returned order (one-line change in the copy: pass `side=leg.side` through `_broker_order`, which the `test_exit.py` variant already does). Use whichever copied variant makes the reducing-order lookups deterministic; the `test_exit.py` `_FakeTrade` (queue-based `lookup_results`) is the better base if entry-terminal refresh lookups occur — the entry here is already terminal (`status="filled"` folded), so the default lookup path suffices.

- [ ] **Step 2: Run to verify failure**

Run: `cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" python -m pytest tests/broker/alpaca/clerk/sqlite/test_safe_flatten_execution.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.broker.alpaca.clerk.sqlite.safe_flatten_execution'`.

- [ ] **Step 3: Implement the core module**

Create `app/broker/alpaca/clerk/sqlite/safe_flatten_execution.py`:

```python
"""Execute one prepared SafeFlattenPlan as reduction-only recovery EXITs (F18).

``recovery_policy`` builds and version-token-gates the plan
(`_build_safe_flatten_plan`); this module owns only the execute side: for each
leg, capture a run-fence-exempt recovery EXIT (``accept_recovery_exit``)
against the newest owned entry order for the leg's symbol, then drive it
through the standard EXIT machine (``resolve_accepted_exit`` → ``resolve_exit``
→ per-op claim CAS → ``ClaimedBrokerIO``). The reducing quantity is derived
downstream from durable attributed custody (``repo.position``), never from the
plan leg — attributed-quantity-exact by construction; the leg is presentation
and gating evidence. Idempotent: the decision id is derived from the plan's
version token, so a retried execute re-drives the same durable EXIT instead of
minting a second reduction.
"""

from __future__ import annotations

import hashlib
import logging

from app.broker.alpaca.clerk.sqlite.exit import (
    RecoveryRunActiveError,
    accept_recovery_exit,
    resolve_accepted_exit,
)
from app.broker.alpaca.clerk.sqlite.intake_fence import ReentrantAsyncLock
from app.broker.alpaca.clerk.sqlite.models import OrderResource
from app.broker.alpaca.clerk.sqlite.order_evidence import entry_order_symbol
from app.broker.alpaca.clerk.sqlite.projection_models import SafeFlattenPlan
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.contract.ports import BrokerTradePort

logger = logging.getLogger(__name__)


class SafeFlattenExecutionError(Exception):
    """The prepared plan cannot be executed against current custody."""


async def execute_safe_flatten_plan(
    repo: ClerkSqliteRepository,
    *,
    plan: SafeFlattenPlan,
    trade: BrokerTradePort,
    intake: ReentrantAsyncLock,
    account_id: str,
) -> tuple[OrderResource, ...]:
    if plan.account_id != account_id:
        raise SafeFlattenExecutionError(
            "The prepared plan belongs to a different account authority."
        )
    if repo.clock() > plan.expires_at_ms:
        raise SafeFlattenExecutionError(
            "The prepared reduction plan expired; prepare a fresh plan."
        )
    if not plan.legs:
        raise SafeFlattenExecutionError("The prepared plan has no reduction legs.")
    decision_token = hashlib.sha256(plan.version_token.encode("utf-8")).hexdigest()[:16]
    submitted: list[OrderResource] = []
    for leg in plan.legs:
        async with intake:
            entries = [
                order
                for order in repo.entry_orders_for_strategy(leg.strategy_instance_id)
                if entry_order_symbol(repo, order.order_ref).upper() == leg.symbol.upper()
                and repo.active_exit_for_order(order.order_ref) is None
            ]
            if not entries:
                raise SafeFlattenExecutionError(
                    f"No owned entry order proves a reduction target for {leg.symbol!r}."
                )
            try:
                accepted = accept_recovery_exit(
                    repo,
                    account_id=account_id,
                    strategy_instance_id=leg.strategy_instance_id,
                    decision_id=f"recovery-flatten-{decision_token}",
                    entry_order_ref=entries[-1].order_ref,
                    # Re-asserted inside the capture transaction: recovery
                    # policy refused presentation with RUN_STILL_ACTIVE, but a
                    # Resume can land between recheck and capture (approved-
                    # carryover resumes are legitimate with exposure held).
                    forbid_active_run=True,
                )
            except RecoveryRunActiveError as exc:
                raise SafeFlattenExecutionError(
                    f"A run re-activated for {leg.strategy_instance_id!r} after "
                    "the flatten was presented; stop the bot and prepare a fresh plan."
                ) from exc
        resolved = await resolve_accepted_exit(repo, accepted=accepted, trade=trade)
        if resolved.reducing_order_ref is not None:
            reducing = repo.order(resolved.reducing_order_ref)
            if reducing is not None:
                submitted.append(reducing)
        logger.info(
            "safe-flatten leg driven through recovery EXIT custody",
            extra={
                "action": "safe_flatten_leg_executed",
                "account_id": account_id,
                "strategy_instance_id": leg.strategy_instance_id,
                "symbol": leg.symbol,
                "effect_operation_id": accepted.effect_operation_id,
            },
        )
    if not submitted:
        raise SafeFlattenExecutionError(
            "No reducing order was submitted; inspect the EXIT custody timeline."
        )
    return tuple(submitted)
```

(If `ReentrantAsyncLock` is defined elsewhere than `intake_fence.py`, import from its true defining module — never from `runtime`.)

- [ ] **Step 4: Add the facade method**

In `runtime.py`, after `stop_strategy_run` (line ~475), add (plus `from app.broker.alpaca.clerk.sqlite.safe_flatten_execution import execute_safe_flatten_plan` and `SafeFlattenPlan` to the `projection_models` import — check whether runtime already imports from `projection_models`; if not, add the import):

```python
    async def execute_safe_flatten(
        self,
        *,
        plan: SafeFlattenPlan,
        reason: str | None = None,
    ) -> tuple[OrderResource, ...]:
        """Execute the presented SafeFlattenPlan as recovery EXIT custody (F18)."""
        orders = await execute_safe_flatten_plan(
            self._repo,
            plan=plan,
            trade=self._trade,
            intake=self._intake,
            account_id=self.account_id,
        )
        logger.info(
            "operator safe flatten executed",
            extra={
                "action": "safe_flatten_executed",
                "account_id": self.account_id,
                "reason": reason,
                "order_count": len(orders),
            },
        )
        return orders
```

`OrderResource` is likely already imported in `runtime.py` (it types `orders_for_effect_operation` results) — add if missing.

- [ ] **Step 5: Run to verify pass**

Run: `cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" python -m pytest tests/broker/alpaca/clerk/sqlite/test_safe_flatten_execution.py tests/broker/alpaca/clerk/sqlite/test_runtime.py -v`
Expected: all pass.

- [ ] **Step 6: Lint and commit**

```bash
ruff check PythonDataService/app/ PythonDataService/tests/
git add PythonDataService/app/broker/alpaca/clerk/sqlite/safe_flatten_execution.py PythonDataService/app/broker/alpaca/clerk/sqlite/runtime.py PythonDataService/tests/broker/alpaca/clerk/sqlite/test_safe_flatten_execution.py
git commit -m "feat(clerk): SafeFlattenPlan executor - recovery EXITs under claimed broker IO (F18)"
```

---

### Task 9: `execute_safe_flatten` recovery-catalog action + dispatcher

**Files:**
- Modify: `PythonDataService/app/broker/alpaca/clerk/sqlite/recovery_policy.py` (`RecoveryActionId` Literal line 40-50; `_DESCRIPTORS` after the `prepare_safe_flatten` descriptor line ~187; `_decision` line ~512; new `_execute_safe_flatten_decision`; `build_recovery_catalog` reduction-plan condition line ~791-799; `_primary_action_id` priority line ~817-828)
- Modify: `PythonDataService/app/broker/alpaca/clerk/sqlite/recovery_execution.py` (Protocol method; dispatcher branch before the final `raise` at line 193)
- Test: `PythonDataService/tests/broker/alpaca/clerk/sqlite/test_safe_flatten_execution.py` (catalog gating), `PythonDataService/tests/broker/alpaca/clerk/sqlite/test_recovery_policy.py` (closed-set lists — its line 66 region enumerates action ids; extend)

**Interfaces:**
- Consumes: `_safe_flatten_decision` (recovery_policy.py:517-618), `_build_safe_flatten_plan`, `replace` (dataclasses, already imported), Task 6's `EXIT_STUCK_REASON_CODE` and existing `EXIT_NOT_FLAT_REASON_CODE` (recovery_policy must import them from `uncertainty_causes` — check current imports), Task 8's facade method.
- Produces: presented + executable `execute_safe_flatten` capability (mutation=True, confirmation, carries `reduction_plan`); dispatcher branch calling `facade.execute_safe_flatten(plan=capability.reduction_plan, reason=request.reason)`; `ActiveSqliteRecoveryFacade` Protocol gains `execute_safe_flatten(*, plan: SafeFlattenPlan, reason: str | None = None) -> tuple[OrderResource, ...]`. The panel presents and dispatches it with **zero** panel-code changes (`sqlite_panel_adapter.py:93-100` + `sqlite_panel_source.py:execute_sqlite_panel_action` are generic over the catalog; the 409 view-action set at `sqlite_panel_source.py:839` stays exactly `{"open_custody_timeline", "prepare_safe_flatten"}`).

- [ ] **Step 1: Write the failing tests**

Append to `test_safe_flatten_execution.py`:

```python
async def test_execute_safe_flatten_presented_for_stopped_bot_with_exposure(
    crashed_with_exposure,
) -> None:
    repo, _clock = crashed_with_exposure
    await _held_position(repo)
    submit_stop_run(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID,
        lifecycle_run_id=RUN_ID, operator_reason="crash_analog",
    )
    await reconcile_account(
        repo,
        read=_FakeRead(positions=[_position("SPY", quantity=10.0)]),
        trade=_FakeTrade(),
        trigger="OPERATOR_RECONCILE_NOW",
    )
    reader = SqliteClerkProjectionReader.from_repository(repo)
    try:
        context = reader.recovery_context(strategy_instance_id=SID)
    finally:
        reader.close()
    catalog = {item.action_id: item for item in build_recovery_catalog(context)}

    execute = catalog["execute_safe_flatten"]
    assert execute.available, execute.unavailable_reason
    assert execute.mutation is True
    assert execute.confirmation is not None
    assert execute.reduction_plan is not None
    assert [leg.symbol for leg in execute.reduction_plan.legs] == ["SPY"]


async def test_execute_safe_flatten_blocked_while_a_run_is_active(
    crashed_with_exposure,
) -> None:
    repo, _clock = crashed_with_exposure
    await _held_position(repo)  # run still ACTIVE - no stop
    await reconcile_account(
        repo,
        read=_FakeRead(positions=[_position("SPY", quantity=10.0)]),
        trade=_FakeTrade(),
        trigger="OPERATOR_RECONCILE_NOW",
    )
    reader = SqliteClerkProjectionReader.from_repository(repo)
    try:
        context = reader.recovery_context(strategy_instance_id=SID)
    finally:
        reader.close()
    catalog = {item.action_id: item for item in build_recovery_catalog(context)}

    execute = catalog["execute_safe_flatten"]
    assert execute.available is False
    assert execute.unavailable_reason_code == "RUN_STILL_ACTIVE"
```

Also update the closed-set expectations in `test_recovery_policy.py` (its healthy-catalog action-id list around line 66) to include `"execute_safe_flatten"` — run the file first to see the exact assertion shapes that fail, then extend them; do not weaken any assertion.

- [ ] **Step 2: Run to verify failure**

Run: `cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" python -m pytest tests/broker/alpaca/clerk/sqlite/test_safe_flatten_execution.py -k presented_or_blocked -v` (adjust `-k` to the two new test names)
Expected: FAIL — `KeyError: 'execute_safe_flatten'`.

- [ ] **Step 3: Implement in `recovery_policy.py`**

1. Add `"execute_safe_flatten",` to the `RecoveryActionId` Literal after `"prepare_safe_flatten",`.
2. Import `EXIT_NOT_FLAT_REASON_CODE, EXIT_STUCK_REASON_CODE` from `.uncertainty_causes` (check for an existing import to extend).
3. Insert into `_DESCRIPTORS` directly after the `prepare_safe_flatten` descriptor:

```python
    _Descriptor(
        action_id="execute_safe_flatten",
        label="Execute safe flatten",
        explanation=(
            "Submit the prepared exact reduction as recovery EXIT custody; "
            "quantities are re-derived from durable attributed positions at submit time."
        ),
        mutation=True,
        confirmation=_confirmation(
            "Flatten attributed exposure?",
            "The Clerk will submit reduction-only orders for the exact attributed "
            "quantities in the prepared plan.",
            "Flatten now",
        ),
    ),
```

4. In `_decision` (healthy branch), after the `prepare_safe_flatten` clause:

```python
    if action_id == "execute_safe_flatten":
        return _execute_safe_flatten_decision(ctx)
```

5. New decision function after `_safe_flatten_decision`:

```python
def _execute_safe_flatten_decision(ctx: RecoveryPolicyContext) -> _Decision:
    """Executor gates = prepare gates, two deltas.

    (1) EXIT_NOT_FLAT / EXIT_STUCK episodes do not block: both declare
    ``allows_reduction=True`` over a proven attributed quantity — they are
    the exact states this executor exists to clear, and the downstream
    ``require_capability(REDUCE, …)`` still enforces movement toward zero
    per leg. (2) No run may be ACTIVE: a running strategy could re-enter
    right after the flatten; the operator stops decisions first
    (``stop_bot_decisions`` is presented alongside). This gate is
    presentation/recheck-time only — the same fact is re-asserted inside the
    capture transaction by ``accept_recovery_exit(forbid_active_run=True)``,
    closing the recheck→capture Resume race (Task 4/Task 8).
    """
    reduction_safe_ctx = replace(
        ctx,
        uncertainties=tuple(
            item
            for item in ctx.uncertainties
            if item.reason_code not in (EXIT_NOT_FLAT_REASON_CODE, EXIT_STUCK_REASON_CODE)
        ),
    )
    base = _safe_flatten_decision(reduction_safe_ctx)
    active_run_ids = [run.run_id for run in ctx.runs if run.state == "ACTIVE"]
    token_facts = {"base": base.token_facts, "active_runs": active_run_ids}
    if base.available and active_run_ids:
        return replace(
            base,
            available=False,
            reason_code="RUN_STILL_ACTIVE",
            reason="Stop the bot's active run before executing a recovery flatten.",
            next_step="Stop bot decisions first, then execute the prepared flatten.",
            token_facts=token_facts,
        )
    return replace(base, token_facts=token_facts)
```

Verify `ProjectedRun.state`'s ACTIVE literal against `projections.py`'s run projection (the `runs` table state vocabulary — `repo.active_run` selects it; if the projected value is e.g. `"active"` lowercase, match it exactly and fix the test to the projected casing).

6. In `build_recovery_catalog`, widen the reduction-plan condition:

```python
                reduction_plan=(
                    _build_safe_flatten_plan(
                        ctx,
                        version_token=concurrency_token,
                    )
                    if descriptor.action_id in ("prepare_safe_flatten", "execute_safe_flatten")
                    and decision.available
                    else None
                ),
```

7. In `_primary_action_id`, insert `"execute_safe_flatten",` immediately before `"prepare_safe_flatten",`.

- [ ] **Step 4: Implement in `recovery_execution.py`**

Extend imports with `SafeFlattenPlan` (from `.projection_models`) and `SafeFlattenExecutionError` (from `.safe_flatten_execution`). Add to the `ActiveSqliteRecoveryFacade` Protocol:

```python
    async def execute_safe_flatten(
        self,
        *,
        plan: SafeFlattenPlan,
        reason: str | None = None,
    ) -> tuple[OrderResource, ...]: ...
```

Insert before the final `raise RecoveryExecutionError(...)` in `execute_recovery_action`:

```python
    if request.action_id == "execute_safe_flatten":
        if capability.reduction_plan is None:
            raise RecoveryExecutionError(
                "The presented flatten action carries no prepared reduction plan."
            )
        try:
            orders = await facade.execute_safe_flatten(
                plan=capability.reduction_plan,
                reason=request.reason,
            )
        except SafeFlattenExecutionError as exc:
            raise RecoveryExecutionError(str(exc)) from exc
        return RecoveryExecutionResult(
            action_id=request.action_id,
            applied=True,
            receipt_id=orders[0].order_ref,
            recorded_at_ms=max(order.updated_at_ms for order in orders),
            orders=orders,
        )
```

(`recheck_recovery_action` above this point already rejected stale tokens and unavailable states effect-free — RUN_STILL_ACTIVE, missing reconciliation, working orders — so this branch executes only a currently-authorized plan.)

- [ ] **Step 5: Run to verify pass**

Run: `cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" python -m pytest tests/broker/alpaca/clerk/sqlite/test_safe_flatten_execution.py tests/broker/alpaca/clerk/sqlite/test_recovery_policy.py tests/broker/alpaca/clerk/sqlite/test_projections.py -v`
Expected: all pass after extending the closed-set expectations in `test_recovery_policy.py` / `test_projections.py` (they enumerate presented action ids; add the new id — never delete existing expectations).

- [ ] **Step 6: Lint and commit**

```bash
ruff check PythonDataService/app/ PythonDataService/tests/
git add PythonDataService/app/broker/alpaca/clerk/sqlite/recovery_policy.py PythonDataService/app/broker/alpaca/clerk/sqlite/recovery_execution.py PythonDataService/tests/broker/alpaca/clerk/sqlite/test_safe_flatten_execution.py PythonDataService/tests/broker/alpaca/clerk/sqlite/test_recovery_policy.py PythonDataService/tests/broker/alpaca/clerk/sqlite/test_projections.py
git commit -m "feat(clerk): present and dispatch execute_safe_flatten as a recovery capability (F18)"
```

---

### Task 10: Vocabulary, operator copy, snapshots, contracts

**Files:**
- Modify: `PythonDataService/app/broker/v2panel/vocabulary.py` (`ActionId` Literal line ~83-101; `ACTION_IDS` tuple line ~102-121; `OPERATOR_COPY` map — add after the `prepare_safe_flatten` entry, line ~311)
- Modify (generated): `PythonDataService/app/broker/v2panel/vocabulary.snapshot.json`, `Frontend/src/app/components/broker/v2-panel/lib/broker-v2-vocabulary.snapshot.json`
- Modify: `Frontend/src/app/components/broker/v2-panel/bot-detail-banner/lifecycle-action.ts` (tone map, line ~27), `Frontend/src/app/components/broker/v2-panel/lib/broker-v2-emergency-copy.ts` (fallback copy, line ~155)
- Modify (generated): the OpenAPI contract snapshot under `contracts/` (via the export script — `ActionId` flows into `app/schemas/broker_v2_panel.py:112` `PanelAction.action_id`)
- Test: `PythonDataService/tests/broker/v2panel/test_vocabulary_snapshot.py` (existing gate — no new test code needed; it fails until the snapshot is regenerated)

**Interfaces:**
- Consumes: Task 9's action id.
- Produces: `"execute_safe_flatten"` as a registered `ActionId` with server-authored copy; regenerated snapshots and OpenAPI contract. Trader-lens exclusion is automatic: `TRADER_LIFECYCLE_ACTION_IDS` (vocabulary.py:129) stays `{"resume", "continue", "stop"}` — the new action is Operator-only, matching the recovery vocabulary.

- [ ] **Step 1: See the gates fail first**

Run: `cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" python -m pytest tests/broker/v2panel/ -v`
Expected: FAIL — the vocabulary/copy-coverage gates report the unknown/uncovered `execute_safe_flatten` id (they compare the presented-action universe against `ACTION_IDS`/`OPERATOR_COPY`/snapshot). If they pass, the catalog id has not reached the vocabulary surface — stop and re-check Task 9.

- [ ] **Step 2: Register the id and copy**

In `vocabulary.py`: insert `"execute_safe_flatten",` after `"prepare_safe_flatten",` in both the `ActionId` Literal and the `ACTION_IDS` tuple; add to `OPERATOR_COPY` after the `prepare_safe_flatten` entry:

```python
    "execute_safe_flatten": OperatorCopy(
        "Execute safe flatten",
        "Submit the prepared reduction as recovery EXIT custody with exact attributed quantities.",
    ),
```

- [ ] **Step 3: Regenerate the snapshots and contract**

```bash
cd PythonDataService && python scripts/regenerate_broker_v2_vocabulary_snapshot.py
# If the script does not also write the Frontend copy (check its output paths):
cp app/broker/v2panel/vocabulary.snapshot.json ../Frontend/src/app/components/broker/v2-panel/lib/broker-v2-vocabulary.snapshot.json
python scripts/export_openapi_contract.py
```

(OpenAPI-regen gotcha: run the export immediately after touching the Pydantic-visible Literal — the contract CI gate diffs the committed snapshot.)

- [ ] **Step 4: Frontend closed maps**

- `lifecycle-action.ts` tone map: add `execute_safe_flatten` with the same tone the map gives the existing destructive recovery mutations (read the map's values first — e.g. if `reset_authority` is `'danger'`, use `'danger'`; `prepare_safe_flatten` is `'neutral'` because it is a view action — the execute action is not).
- `broker-v2-emergency-copy.ts`: add an `execute_safe_flatten` entry mirroring the `prepare_safe_flatten` entry's shape (line ~155) with label "Execute safe flatten" and a one-line explanation matching the backend copy above. This is the emergency fallback only — the backend `OPERATOR_COPY` remains the semantic authority.

- [ ] **Step 5: Verify all gates**

```bash
# Scoped for iteration only — the Final gate below runs the FULL pytest suite,
# which is the repo's one pre-push authority (.claude/rules/testing.md).
cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" python -m pytest tests/broker/v2panel/ tests/schemas/ -v
ruff check PythonDataService/app/ PythonDataService/tests/
npx eslint Frontend/src/ --max-warnings 0

# Frontend spec surface: enumerate the specs that cover the touched maps, then
# run each with an EXACT --include (repo rule: exact spec filenames, never
# directory globs, which sweep .scss/.html into the build and fail it).
ls Frontend/src/app/components/broker/v2-panel/bot-detail-banner/*.spec.ts \
   Frontend/src/app/components/broker/v2-panel/lib/*.spec.ts
# For EVERY file the ls prints, run (no || true — a gate that cannot fail is
# not a gate; a failure here blocks the commit):
podman exec my-frontend npx ng test --watch=false --include='<one spec path relative to Frontend/, exactly as printed>'
# If ls prints nothing, skip the ng-test step explicitly and record the reason
# in the PR description: "no spec covers the tone/copy maps; change is
# gated by eslint + the vocabulary-snapshot parity test instead."

git status --short contracts/
```

Expected: pytest green, eslint clean, every listed spec green (or the explicit no-spec skip recorded), the contracts diff shows only the new enum value.

- [ ] **Step 6: Commit**

```bash
git add PythonDataService/app/broker/v2panel/ Frontend/src/app/components/broker/v2-panel/ contracts/
git commit -m "feat(v2panel): register execute_safe_flatten vocabulary, copy, snapshots, contract"
```

---

### Task 11: Walkthrough — crash → refuse-resume → flatten → resume-to-flat

**Files:**
- Test: `PythonDataService/tests/broker/alpaca/clerk/sqlite/test_exposure_lifecycle_walkthrough.py` (new; test-only task — it must pass with **zero** production edits; any production change it forces is a defect found by the walkthrough and belongs in its own fix commit with the failing step narrowed into a unit test first)

**Interfaces:**
- Consumes: everything landed in Tasks 1–9: `SqliteAlpacaClerkFacade` (constructed exactly as `test_runtime.py:133-135` does: `SqliteAlpacaClerkFacade(repo=repo, read=fake_read, trade=fake_trade)` — the facade wraps raw ports in guards itself), `execute_recovery_action` + `RecoveryExecutionRequest` (recovery_execution.py:58-77), `SqliteClerkProjectionReader.recovery_context`, `build_recovery_catalog`, `decide_capability`, `SqliteTradeUpdateEvidenceSink`, and the copied fake ports/helpers from Tasks 7–8's files.

- [ ] **Step 1: Write the walkthrough (it should pass immediately if Tasks 1–9 are correct)**

```python
"""Direction-1 done-when: crash-with-exposure -> refuse-resume -> flatten
(via the presented recovery action) -> resume-to-flat, entirely under SQLite
Clerk custody with fake broker ports."""

# imports: ClerkSqliteRepository, SqliteAlpacaClerkFacade (from runtime),
# submit_start_run/submit_stop_run, execute_recovery_action,
# RecoveryExecutionRequest, SqliteClerkProjectionReader, build_recovery_catalog,
# decide_capability, Capability (uncertainty), reconcile_account,
# SqliteTradeUpdateEvidenceSink, BrokerOrderEvent, ReentrantAsyncLock,
# copied _FakeRead/_FakeTrade/_position/_broker_order/_held_position helpers,
# _clock_at from conftest.


async def test_crashed_exposure_walks_to_flat_and_readmits_resume(tmp_path) -> None:
    clock = _clock_at(1_700_000_000_000)
    repo = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock, lease_ttl_ms=300_000
    )
    repo.register_strategy_instance(strategy_instance_id=SID, symbol="SPY", config_hash="h1")
    submit_start_run(repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id=RUN_ID)
    await _held_position(repo)  # filled entry, attributed +10

    # 1. Crash analog: the same durable STOP runtime.recover() commits on restart.
    submit_stop_run(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID,
        lifecycle_run_id=RUN_ID, operator_reason="service_restart_recovery",
    )

    # 2. Refuse-resume: fresh exposure is refused while custody holds the position
    #    (the Resume surface additionally refuses via RESUME_CARRYOVER_UNSUPPORTED;
    #    ATTRIBUTED_EXPOSURE_EXISTS is the custody-level face of the same fact).
    refused = decide_capability(repo, capability=Capability.NEW_EXPOSURE, strategy_instance_id=SID)
    assert refused.allowed is False
    assert refused.reason_code == "ATTRIBUTED_EXPOSURE_EXISTS"

    # 3. Operator reconciles, the panel presents execute_safe_flatten.
    broker_read = _FakeRead(positions=[_position("SPY", quantity=10.0)])
    flatten_trade = _FakeTrade()
    facade = SqliteAlpacaClerkFacade(repo=repo, read=broker_read, trade=flatten_trade)
    await facade.reconcile_account(trigger="OPERATOR_RECONCILE_NOW")

    async def current_context():
        reader = SqliteClerkProjectionReader.from_repository(repo)
        try:
            context = reader.recovery_context(strategy_instance_id=SID)
        finally:
            reader.close()
        assert context is not None
        return context

    catalog = {
        item.action_id: item for item in build_recovery_catalog(await current_context())
    }
    capability = catalog["execute_safe_flatten"]
    assert capability.available and capability.mutation

    # 4. Execute through the same dispatcher the panel uses.
    result = await execute_recovery_action(
        facade,
        request=RecoveryExecutionRequest(
            action_id="execute_safe_flatten",
            concurrency_token=capability.concurrency_token,
            execution_ref=capability.execution_ref,
            reason="walkthrough",
        ),
        current_context=current_context,
    )
    assert result.applied is True
    assert len(result.orders) == 1
    reducing_ref = result.orders[0].order_ref

    # 5. The reducing fill arrives (websocket analog) and the sweep proves flat.
    sink = SqliteTradeUpdateEvidenceSink(
        repo=repo, intake=ReentrantAsyncLock(), reconciler=_NoReconciler()
    )
    filled_reducing = _broker_order(
        reducing_ref, side="sell", status="filled", filled_quantity=10, filled_avg_price=101.0
    )
    await sink.record_lifecycle_event(
        client_order_id=reducing_ref,
        event=BrokerOrderEvent(
            event_type="fill", occurred_at_ms=repo.clock(),
            price=101.0, quantity=10, execution_id="walk-exec-2",
        ),
        event_key="execution:walk-exec-2",
        order=filled_reducing,
        recovery_source=None,
        recovery_window_limit=None,
    )
    await reconcile_account(
        repo, read=_FakeRead(positions=[]), trade=_FakeTrade(), trigger="AUTOMATIC",
    )
    assert repo.attributed_positions_for_strategy(SID) == {} or not any(
        position_quantity_is_nonzero(qty)
        for qty in repo.attributed_positions_for_strategy(SID).values()
    )

    # 6. Resume-to-flat: fresh exposure is admissible again.
    readmitted = decide_capability(repo, capability=Capability.NEW_EXPOSURE, strategy_instance_id=SID)
    assert readmitted.allowed is True

    repo.close()
```

(The copied `_broker_order` helper must accept `side` — the `test_exit.py` variant does; use that one.)

- [ ] **Step 2: Run**

Run: `cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" python -m pytest tests/broker/alpaca/clerk/sqlite/test_exposure_lifecycle_walkthrough.py -v`
Expected: PASS. If any step fails, that is the walkthrough doing its job: reduce the failing step to a unit test in the owning task's test file, fix in production code, and only then return here.

- [ ] **Step 3: Commit**

```bash
ruff check PythonDataService/app/ PythonDataService/tests/
git add PythonDataService/tests/broker/alpaca/clerk/sqlite/test_exposure_lifecycle_walkthrough.py
git commit -m "test(clerk): crash -> refuse-resume -> flatten -> resume-to-flat walkthrough (Direction 1 done-when)"
```

---

### Task 12: ADR 0045, ADR 0010 supersession, known-gaps update

**Files:**
- Create: `docs/architecture/adrs/0045-exposure-lifecycle-closure.md`
- Modify: `docs/architecture/adrs/0010-operator-action-contract-flatten-pause-stop.md` (Status value + Provenance line — ADR 0039 idiom)
- Modify: `docs/doc-authority.md` (ADR index table — add the 0045 row after 0044, currently line ~129)
- Modify: `CONTEXT.md` (new glossary section named by ADR 0045's `Vocabulary:` line)
- Modify: `docs/known-gaps.md` (§1 rewrite; §9 pointer line)

**Interfaces:** documentation only; no code. Governance gates this task must satisfy (verified against `docs/doc-authority.md:76-83` and ADR 0039): (a) every newly accepted ADR carries a `Vocabulary:` header line — unconditional (ADR 0040 Decision 4); (b) the doc-authority ADR index gains the 0045 row in the same PR; (c) `**Status:**` values come from the closed set and match ADR 0039's regex `^\*\*Status:\*\* (Accepted|Proposed|Superseded|Retired)( \d{4}-\d{2}-\d{2})?$` — exactly one Status line per file, supersession narrative goes in `**Provenance:**` (ADR 0013 is the corpus idiom).

- [ ] **Step 1: Write ADR 0045**

Create `docs/architecture/adrs/0045-exposure-lifecycle-closure.md`. The header block is exact (the `Status:` line must match ADR 0039's regex; the `Vocabulary:` line is unconditional per ADR 0040 Decision 4 and names the CONTEXT.md section Step 2 adds):

```markdown
# ADR 0045 — Exposure lifecycle closure: recovery flatten executor, EXIT refusal taxonomy, stuck-EXIT watchdog

**Status:** Accepted 2026-08-24
**Provenance:** Authored with the exposure-lifecycle-closure implementation (this PR), from `docs/superpowers/plans/2026-08-24-exposure-lifecycle-closure.md`; spec: `docs/audits/strategy-execution-research-directions-2026-08-24.md` Direction 1. Supersedes [ADR 0010](0010-operator-action-contract-flatten-pause-stop.md) for the active Alpaca/SQLite control plane.
**Decision drivers:** F18/F19 (ops study 2026-08-24 §8–§9); the "correct mechanism exists, unwired" failure mode named by the same-day research directions.
**Related:** ADR 0035 (SQLite sole Alpaca custody authority), ADR 0038 (Alpaca sole bot control plane), ADR 0041 (generated Button Reference), ADR 0010 (superseded).
**Vocabulary:** `CONTEXT.md` § "Exposure lifecycle closure" — Recovery EXIT, Safe flatten, Redrive, `EXIT_STUCK`.
```

Then Context / Decision / Consequences sections recording verbatim from this plan:

1. The flatten executor is the recovery-catalog action `execute_safe_flatten` consuming the prepared `SafeFlattenPlan` through the claimed-broker-IO EXIT machine — not the `flatten_stop` panel performer. Record the performer's two latent defects (active-run fence self-defeat; colon in `decision_id`) and that `flatten_stop`, `pause`, `continue`, `retire` remain unpresented dead vocabulary whose fate belongs to Direction 5's one-lifecycle-surface work.
2. Recovery EXITs are run-fence-exempt but exposure-anchored (`run_id` from the originating entry's effect operation) and reduction-only; admission is the SafeFlattenPlan gates or the watchdog policy, plus `require_capability(REDUCE)` toward zero. The no-active-run fact is enforced twice: `RUN_STILL_ACTIVE` at presentation/recheck, and again inside the capture transaction (`accept_recovery_exit(forbid_active_run=True)` raising `RecoveryRunActiveError`) so a Resume landing between recheck and capture fails closed.
3. The refusal taxonomy: `TRANSIENT = {BROKER_SNAPSHOT_STALE, RECONCILIATION_INCOMPLETE, RECONCILIATION_IN_PROGRESS}`, everything else TERMINAL (fail-closed); consumers: EXIT resolve boundary, runner EXIT path, sweep (pre-existing), ENTER (pre-existing).
4. Stuck-EXIT policy: re-drive after 120 000 ms, max 3 re-drives per episode, then durable `EXIT_STUCK` escalation; constants live in `reconcile.py`. Redrive identity is episode-scoped — `exit-redrive-<sha256(uncertainty_id)[:12]>-<attempt>` — because `_exit_identity` keys idempotency on `(strategy_instance_id, decision_id)` alone, so an unscoped id would replay or conflict with an earlier episode's redrives.
5. **Deferred (with reasons):** RQ4 strategy-intent-vs-journal comparison (needs the `SignalSession` read-back seam Direction 2 will design; concrete harm removed by the watchdog); presented-action `mutation`/executability facts (F17) and admission-refusal copy ordering (F5) — Direction 5.
6. ADR 0010's `FLATTEN_NOW`/desired-state vocabulary was retired with the IBKR control plane (evaluator control plane #1678, legacy broker control #1679); the Alpaca/SQLite plane's operator flatten contract is this ADR.

- [ ] **Step 2: Add the CONTEXT.md vocabulary section and the doc-authority index row**

Append to `CONTEXT.md`, matching the file's established section form (`## <Name> (resolved YYYY-MM-DD)` + a `**Lineage: …**` marker, per ADR 0040):

```markdown
## Exposure lifecycle closure (resolved 2026-08-24)

**Lineage: live.**

- **Recovery EXIT** — a reduction-only EXIT captured without the active-run fence, anchored to the run recorded on the targeted entry's effect operation. Admitted only by the safe-flatten gates or the stuck-EXIT watchdog policy, and always subject to the REDUCE capability's movement-toward-zero check.
- **Safe flatten** — the two-step operator capability over a prepared `SafeFlattenPlan`: `prepare_safe_flatten` (view) builds the versioned exact-close plan; `execute_safe_flatten` (mutation) submits it as recovery EXITs, re-deriving quantities from durable attributed positions and re-asserting no-active-run inside the capture transaction.
- **Redrive** — the watchdog's bounded automatic re-submission of a reduction for a stale `EXIT_NOT_FLAT` episode; identity `exit-redrive-<episode-hex12>-<attempt>`, at most 3 per episode.
- **`EXIT_STUCK`** — the durable custody-subject escalation raised when redrives exhaust; blocks new exposure, allows reduction toward zero.
```

In `docs/doc-authority.md`, add to the ADR index table directly after the `| 0044 | … |` row:

```markdown
| 0045 | Exposure lifecycle closure: `execute_safe_flatten` recovery action over run-fence-exempt recovery EXITs; transient-vs-terminal EXIT refusal taxonomy; stuck-EXIT watchdog with bounded episode-scoped redrives and durable `EXIT_STUCK` escalation (supersedes 0010) |
```

- [ ] **Step 3: Mark ADR 0010 superseded (ADR 0039 idiom — closed Status value, narrative in Provenance)**

In ADR 0010, replace the Status line's value (it must still match ADR 0039's regex, one Status line per file):

```markdown
**Status:** Superseded 2026-08-24
```

and prepend the supersession narrative to the existing `**Provenance:**` line, keeping the original text after it verbatim:

```markdown
**Provenance:** Superseded by [ADR 0045](0045-exposure-lifecycle-closure.md) for the active Alpaca/SQLite control plane — the FLATTEN_NOW / durable-desired-state vocabulary specified here was retired with the IBKR control plane (evaluator control plane #1678, legacy broker control #1679). Original provenance: Promoted from `Proposed` to `Accepted` on 2026-08-18 under [ADR 0039](0039-adr-status-is-decision-standing.md) Decision 1 — …(rest of the existing line unchanged).
```

- [ ] **Step 4: Update known-gaps**

In `docs/known-gaps.md` §1 ("Safety-critical"), per the file's own convention ("When an item is fixed, delete its bullet — git history is the record"): delete the **F18** and **F19** bullets and replace the section body with a closure note in the same voice as §2, e.g.: "No known-open items. The two 2026-08-24 safety-critical findings (F18 crash-held exposure path-to-flat; F19 retryable EXIT refusal crashing bots) were closed on <merge date> by the exposure-lifecycle-closure work (ADR 0045): `execute_safe_flatten` recovery action, run-fence-exempt recovery EXITs, the transient-refusal taxonomy at the EXIT boundary, and the stuck-EXIT watchdog with durable `EXIT_STUCK` escalation. The three stranded 1-share evidence positions on `PA3KWXU1C4C3` can now be flattened through the presented action — flattening them remains the operator's call." Also update §9's line "F18/F19 are lifted to §1 above" to "F18/F19 were closed by ADR 0045 (see §1)". Do this step **only in the PR that lands the code** — never mark a gap closed ahead of the fix merging.

- [ ] **Step 5: Verify the governance gates, then commit**

```bash
# ADR 0039 Status-regex check on both touched ADRs (exactly one match each):
grep -cE '^\*\*Status:\*\* (Accepted|Proposed|Superseded|Retired)( [0-9]{4}-[0-9]{2}-[0-9]{2})?$' docs/architecture/adrs/0045-exposure-lifecycle-closure.md docs/architecture/adrs/0010-operator-action-contract-flatten-pause-stop.md
grep -c '^\*\*Vocabulary:\*\*' docs/architecture/adrs/0045-exposure-lifecycle-closure.md
grep -n '| 0045 |' docs/doc-authority.md
git add docs/architecture/adrs/0045-exposure-lifecycle-closure.md docs/architecture/adrs/0010-operator-action-contract-flatten-pause-stop.md docs/doc-authority.md CONTEXT.md docs/known-gaps.md
git commit -m "docs(adr): ADR 0045 exposure lifecycle closure; supersede ADR 0010; close F18/F19 in known-gaps"
```

---

### Final gate (before the PR-opening push)

- [ ] Full Python suite on the final tree: `cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" python -m pytest tests` — baseline any failure against `origin/master` before treating it as inherited; surface pre-existing failures in the PR description.
- [ ] Project-scope lint: `ruff check PythonDataService/app/ PythonDataService/tests/` and `npx eslint Frontend/src/ --max-warnings 0`.
- [ ] Invoke the `thermo-nuclear-code-quality-review` skill (one-shot per PR); fix every **major** finding in-branch before push.
- [ ] PR description: map the diff to Direction 1's three capabilities and the done-when bullets; note the RQ4 deferral and its ADR 0045 record; note that the stranded `PA3KWXU1C4C3` positions are now actionable but untouched (operator's call).
