"""Operator discharge of a stranded attributed residue (#2381).

Real code on a real temp SQLite Clerk (``test_reconcile``'s watchdog harness):
attributed +10 SPY with an open ``EXIT_NOT_FLAT``. Only the broker read port
is faked, standing in for the Alpaca account the operator checked.
"""

from __future__ import annotations

import pytest

from app.broker.alpaca.clerk.sqlite.commands import submit_start_run, submit_stop_run
from app.broker.alpaca.clerk.sqlite.facts import AttributedResidueDischargedFacts
from app.broker.alpaca.clerk.sqlite.folds import DEFAULT_FOLD_REGISTRY
from app.broker.alpaca.clerk.sqlite.lane_quiet import observe_account_quiet
from app.broker.alpaca.clerk.sqlite.order_evidence import fold_order_acknowledgement
from app.broker.alpaca.clerk.sqlite.projections import SqliteClerkProjectionReader
from app.broker.alpaca.clerk.sqlite.reconcile import MAX_OPEN_ORDER_SNAPSHOT
from app.broker.alpaca.clerk.sqlite.recovery_execution import (
    RecoveryExecutionError,
    RecoveryExecutionRequest,
    execute_recovery_action,
)
from app.broker.alpaca.clerk.sqlite.recovery_policy import (
    RecoveryPolicyContext,
    build_recovery_catalog,
)
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.residue_discharge import (
    ATTRIBUTED_RESIDUE_DISCHARGED,
    ResidueDischargeRefused,
    discharge_attributed_residue,
)
from app.broker.alpaca.clerk.sqlite.runtime import ReentrantAsyncLock, SqliteAlpacaClerkFacade
from app.broker.alpaca.clerk.sqlite.uncertainty import (
    EXIT_NOT_FLAT_REASON_CODE,
    raise_uncertainty,
)
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    EXIT_STUCK_REASON_CODE,
    ExitStuckCause,
)
from app.broker.contract.errors import BrokerUnavailable
from tests.broker.alpaca.clerk.sqlite.conftest import FIXTURE_RTH_MS, _clock_at
from tests.broker.alpaca.clerk.sqlite.test_reconcile import (  # noqa: F401  (fixture import)
    ACCOUNT_ID,
    WATCHDOG_RUN,
    WATCHDOG_SID,
    _broker_order,
    _FakeRead,
    _FakeTrade,
    _held_position,
    _position,
    _raise_exit_not_flat,
    _SequentialRead,
    clocked_repo,
)

REASON = "Checked Alpaca: SPY is flat; the reducing fill was never attributed."


def _stop_run(repo: ClerkSqliteRepository) -> None:
    submit_stop_run(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=WATCHDOG_SID,
        lifecycle_run_id=WATCHDOG_RUN,
    )


async def _stranded(repo: ClerkSqliteRepository) -> None:
    """Attributed +10 SPY, EXIT_NOT_FLAT open, run stopped."""
    await _held_position(repo)
    _raise_exit_not_flat(repo, attributed_qty=10.0)
    _stop_run(repo)


async def _discharge(
    repo: ClerkSqliteRepository,
    read: _FakeRead,
    *,
    reason: str | None = REASON,
):
    return await discharge_attributed_residue(
        repo,
        read=read,
        intake=ReentrantAsyncLock(),
        strategy_instance_id=WATCHDOG_SID,
        symbol="spy",
        operator_reason=reason,
    )


def _discharges(repo: ClerkSqliteRepository) -> list[dict]:
    return [
        transition
        for transition in repo.custody_transitions()
        if transition["transition_kind"] == ATTRIBUTED_RESIDUE_DISCHARGED
    ]


async def test_flat_broker_discharges_the_residue_and_resolves_the_exit_fences(clocked_repo) -> None:  # noqa: F811
    repo, _clock = clocked_repo
    await _stranded(repo)
    raise_uncertainty(
        repo,
        strategy_instance_id=WATCHDOG_SID,
        reason_code=EXIT_STUCK_REASON_CODE,
        headline="Stuck EXIT",
        explanation="test: re-drives exhausted",
        operator_impact="New exposure is paused for this strategy.",
        next_step="Flatten or discharge.",
        evidence_refs=("wd-stuck",),
        cause_facts=ExitStuckCause(
            symbol="SPY", attributed_qty=10.0, redrive_count=2, first_observed_at_ms=1
        ).to_mapping(),
        severity="error",
    )

    receipt = await _discharge(repo, _FakeRead(positions=[]))

    assert receipt.discharged_qty == pytest.approx(10.0, abs=1e-9, rel=0)
    assert repo.position(WATCHDOG_SID, "SPY") == 0.0
    [transition] = _discharges(repo)
    facts = AttributedResidueDischargedFacts.from_facts_json(transition["facts_json"])
    assert facts.operator_reason == REASON
    assert facts.broker_qty == 0.0
    assert facts.account_attributed_qty == pytest.approx(10.0, abs=1e-9, rel=0)
    assert len(facts.uncertainty_ids) == 2
    assert not any(
        episode["reason_code"] in {EXIT_NOT_FLAT_REASON_CODE, EXIT_STUCK_REASON_CODE}
        for episode in repo.active_uncertainties()
    )
    # The account can now leave the lane: lane quiet answers flat (#2344).
    quiet = await observe_account_quiet(repo, _FakeRead(positions=[]))
    assert quiet is not None and quiet.account_flat


async def test_broker_holding_the_shares_refuses_and_writes_nothing(clocked_repo) -> None:  # noqa: F811
    repo, _clock = clocked_repo
    await _stranded(repo)

    with pytest.raises(ResidueDischargeRefused) as refused:
        await _discharge(repo, _FakeRead(positions=[_position("SPY", quantity=10.0)]))

    assert refused.value.reason_code == "BROKER_DISAGREES_WITH_DISCHARGE"
    assert repo.position(WATCHDOG_SID, "SPY") == 10.0
    assert _discharges(repo) == []


async def test_partial_broker_position_refuses(clocked_repo) -> None:  # noqa: F811
    repo, _clock = clocked_repo
    await _stranded(repo)

    with pytest.raises(ResidueDischargeRefused) as refused:
        await _discharge(repo, _FakeRead(positions=[_position("SPY", quantity=4.0)]))

    assert refused.value.reason_code == "BROKER_DISAGREES_WITH_DISCHARGE"
    assert _discharges(repo) == []


async def test_a_working_broker_order_on_the_symbol_refuses(clocked_repo) -> None:  # noqa: F811
    repo, _clock = clocked_repo
    await _stranded(repo)
    foreign = _broker_order("foreign-1", status="new", side="buy", quantity=10.0)

    with pytest.raises(ResidueDischargeRefused) as refused:
        await _discharge(repo, _FakeRead(orders=[foreign], positions=[]))

    assert refused.value.reason_code == "BROKER_ORDER_WORKING"
    assert _discharges(repo) == []


async def test_an_active_run_refuses(clocked_repo) -> None:  # noqa: F811
    repo, _clock = clocked_repo
    await _held_position(repo)
    _raise_exit_not_flat(repo, attributed_qty=10.0)

    with pytest.raises(ResidueDischargeRefused) as refused:
        await _discharge(repo, _FakeRead(positions=[]))

    assert refused.value.reason_code == "RUN_STILL_ACTIVE"
    assert _discharges(repo) == []


async def test_a_residue_no_exit_episode_names_refuses(clocked_repo) -> None:  # noqa: F811
    repo, _clock = clocked_repo
    await _held_position(repo)
    _stop_run(repo)

    with pytest.raises(ResidueDischargeRefused) as refused:
        await _discharge(repo, _FakeRead(positions=[]))

    assert refused.value.reason_code == "NO_STRANDED_EXIT_EPISODE"
    assert _discharges(repo) == []


@pytest.mark.parametrize("reason", [None, "", "   "])
async def test_a_blank_operator_reason_is_recorded_as_absent(clocked_repo, reason) -> None:  # noqa: F811
    """The panel's typed confirmation is the acknowledgement; it sends no reason."""
    repo, _clock = clocked_repo
    await _stranded(repo)

    await _discharge(repo, _FakeRead(positions=[]), reason=reason)

    [transition] = _discharges(repo)
    assert AttributedResidueDischargedFacts.from_facts_json(
        transition["facts_json"]
    ).operator_reason is None


async def test_a_netted_account_discharges_only_the_residue(clocked_repo) -> None:  # noqa: F811
    """A's stale +10 beside B's real -10: the broker shows -10, and discharging
    A's residue is exactly what restores broker = attribution."""
    repo, _clock = clocked_repo
    other_sid, other_run = "wd-bot-b", "wd-run-b"
    repo.register_strategy_instance(
        strategy_instance_id=other_sid, symbol="SPY", config_hash="wd-h2"
    )
    submit_start_run(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=other_sid, lifecycle_run_id=other_run
    )
    await _held_position(repo, suffix="b", sid=other_sid, run_id=other_run, side="sell")
    await _stranded(repo)

    await _discharge(repo, _FakeRead(positions=[_position("SPY", quantity=10.0, side="short")]))

    assert repo.position(WATCHDOG_SID, "SPY") == 0.0
    assert repo.position(other_sid, "SPY") == -10.0


def test_the_fold_refuses_a_residue_the_ledger_does_not_hold() -> None:
    """A replay against different attribution fails instead of zeroing it."""
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE positions (subject_id TEXT, strategy_instance_id TEXT, symbol TEXT, "
        "attributed_qty REAL, updated_at_ms INTEGER)"
    )
    conn.execute("INSERT INTO positions VALUES ('s', 'bot', 'SPY', 4.0, 1)")
    facts = AttributedResidueDischargedFacts(
        symbol="SPY",
        discharged_qty=10.0,
        broker_qty=0.0,
        account_attributed_qty=10.0,
        broker_observed_at_ms=1,
        uncertainty_ids=["u"],
        operator_reason=REASON,
    )
    payload = {
        "transition_kind": ATTRIBUTED_RESIDUE_DISCHARGED,
        "strategy_instance_id": "bot",
        "recorded_at_ms": 2,
        "facts_json": facts.to_facts_json(),
    }

    with pytest.raises(ValueError, match="does not hold"):
        DEFAULT_FOLD_REGISTRY.apply(conn, payload)

    assert conn.execute("SELECT attributed_qty FROM positions").fetchone()[0] == 4.0


async def test_a_mirror_rebuild_replays_the_discharge(tmp_path) -> None:
    clock = _clock_at(FIXTURE_RTH_MS)
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
    await _stranded(repo)
    await _discharge(repo, _FakeRead(positions=[]))
    transitions = repo.custody_transitions()
    db_path = repo.db_path
    repo.close()
    db_path.rename(db_path.with_suffix(".db.corrupt"))

    rebuilt = ClerkSqliteRepository.rebuild_from_mirror(
        account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock
    )
    try:
        assert rebuilt.custody_transitions() == transitions
        assert rebuilt.position(WATCHDOG_SID, "SPY") == 0.0
    finally:
        rebuilt.close()


async def test_the_panel_dispatcher_discharges_a_drifted_residue_end_to_end(clocked_repo) -> None:  # noqa: F811
    """The stranded state #2381 names, driven the way the panel drives it:
    a real reconcile against a flat broker raises drift, the catalog offers the
    discharge, the dispatcher executes it, and the next pass reads clean."""
    repo, _clock = clocked_repo
    await _stranded(repo)
    flat = _FakeRead(positions=[])
    facade = SqliteAlpacaClerkFacade(
        account_mode="paper", repo=repo, read=flat, trade=_FakeTrade()
    )
    drifted = await facade.reconcile_account(trigger="OPERATOR_RECONCILE_NOW")
    assert drifted.verdict == "position_drift"

    async def current_context() -> RecoveryPolicyContext:
        reader = SqliteClerkProjectionReader.from_repository(repo, clock=repo.clock)
        try:
            context = reader.recovery_context(strategy_instance_id=WATCHDOG_SID)
        finally:
            reader.close()
        assert context is not None
        return context

    catalog = {item.action_id: item for item in build_recovery_catalog(await current_context())}
    capability = catalog["discharge_attributed_residue"]
    assert capability.available, capability.unavailable_reason

    result = await execute_recovery_action(
        facade,
        request=RecoveryExecutionRequest(
            action_id="discharge_attributed_residue",
            concurrency_token=capability.concurrency_token,
            execution_ref=capability.execution_ref,
            reason=None,
        ),
        current_context=current_context,
    )

    assert result.applied is True
    assert repo.position(WATCHDOG_SID, "SPY") == 0.0
    healed = await facade.reconcile_account(trigger="OPERATOR_RECONCILE_NOW")
    assert healed.verdict == "clean"
    assert repo.active_uncertainties() == []


async def test_an_uncertainty_that_doubts_a_fill_refuses_the_discharge(clocked_repo) -> None:  # noqa: F811
    """Review of #2381: an open episode meaning the Clerk may not have recorded
    a fill yet (here, an unknown order outcome) refuses — the late fill would
    otherwise fold against a zeroed residue and leave a phantom short."""
    repo, _clock = clocked_repo
    await _stranded(repo)
    raise_uncertainty(
        repo,
        strategy_instance_id=WATCHDOG_SID,
        reason_code="ORDER_OUTCOME_UNKNOWN",
        headline="Order outcome unknown",
        explanation="test: a reducing order's outcome is not yet known",
        operator_impact="New exposure is paused for this strategy.",
        next_step="Wait for broker evidence.",
        evidence_refs=("wd-unknown",),
        cause_facts={},
        severity="error",
    )

    with pytest.raises(ResidueDischargeRefused) as refused:
        await _discharge(repo, _FakeRead(positions=[]))

    assert refused.value.reason_code == "UNCERTAINTY_REFUSES_DISCHARGE"
    assert repo.position(WATCHDOG_SID, "SPY") == 10.0
    assert _discharges(repo) == []


def test_every_registered_reason_declares_its_residue_discharge_role() -> None:
    """Pin the registry's answer, so a new cause is refused until someone decides."""
    from app.broker.alpaca.clerk.sqlite.uncertainty_policies import (
        _REASON_POLICIES,
        residue_discharge_role,
    )

    roles = {code: policy.residue_discharge for code, policy in _REASON_POLICIES.items()}

    assert {code for code, role in roles.items() if role == "strands"} == {
        "EXIT_NOT_FLAT",
        "EXIT_STUCK",
    }
    assert {code for code, role in roles.items() if role == "admits"} == {
        "POSITION_DRIFT",
        "UNFOLDABLE_BROKER_ORDER",
        "LIVE_ENVELOPE_LOSS_HOLD",
        # #2460: doubts the cost of fills whose quantity both sides agree on,
        # so it cannot move a residue.
        "EXECUTION_PRICE_CONFLICT",
    }
    assert residue_discharge_role("NOT_A_REGISTERED_CODE") == "refuses"


# ── PR #2404 review ──────────────────────────────────────────────────────────


async def test_a_full_open_order_page_refuses(clocked_repo) -> None:  # noqa: F811
    """A page at the read's limit cannot prove no older order is working."""
    repo, _clock = clocked_repo
    await _stranded(repo)
    page = [
        _broker_order(f"other-{index}", order_id=f"bo-{index}", symbol="QQQ", status="new")
        for index in range(MAX_OPEN_ORDER_SNAPSHOT)
    ]

    with pytest.raises(ResidueDischargeRefused) as refused:
        await _discharge(repo, _FakeRead(orders=page, positions=[]))

    assert refused.value.reason_code == "OPEN_ORDER_SNAPSHOT_INCOMPLETE"
    assert _discharges(repo) == []


async def test_a_fill_landing_between_the_order_and_position_reads_refuses(clocked_repo) -> None:  # noqa: F811
    """Read 1 sees the working BUY gone but the position still flat (orders read
    after the fill, positions before it); read 2 sees the +10 the fill left."""
    repo, _clock = clocked_repo
    await _stranded(repo)
    read = _SequentialRead(
        order_snapshots=[[], []],
        position_snapshots=[[], [_position("SPY", quantity=10.0)]],
    )

    with pytest.raises(ResidueDischargeRefused) as refused:
        await _discharge(repo, read)

    assert refused.value.reason_code == "BROKER_SNAPSHOT_UNSETTLED"
    assert repo.position(WATCHDOG_SID, "SPY") == 10.0


async def test_a_non_finite_broker_quantity_refuses(clocked_repo) -> None:  # noqa: F811
    repo, _clock = clocked_repo
    await _stranded(repo)
    nan_position = _position("SPY", quantity=10.0).model_copy(update={"quantity": float("nan")})

    with pytest.raises(ResidueDischargeRefused) as refused:
        await _discharge(repo, _FakeRead(positions=[nan_position]))

    assert refused.value.reason_code == "EXPOSURE_NOT_PROVEN"
    assert _discharges(repo) == []


async def test_an_order_short_of_its_broker_cumulative_refuses(clocked_repo) -> None:  # noqa: F811
    """A late slice the Clerk still expects (#2305) must not fold onto a zeroed
    residue: while any order on the symbol is short of the broker's cumulative
    it stays reconcilable, and the discharge refuses as work in flight."""
    repo, _clock = clocked_repo
    entry_ref = await _held_position(repo)
    _raise_exit_not_flat(repo, attributed_qty=10.0)
    _stop_run(repo)
    entry = repo.order(entry_ref)
    assert entry is not None
    fold_order_acknowledgement(
        repo,
        effect_operation_id=entry.effect_operation_id,
        order=_broker_order(
            entry_ref, status="filled", quantity=12.0, filled_quantity=12.0,
            filled_avg_price=100.0,
        ).model_copy(update={"updated_at_ms": 1_700_000_000_900}),
    )
    assert repo.order_fills_short_of_broker_cumulative(entry_ref)

    with pytest.raises(ResidueDischargeRefused) as refused:
        await _discharge(repo, _FakeRead(positions=[]))

    assert refused.value.reason_code == "CLERK_WORK_IN_FLIGHT"
    assert _discharges(repo) == []


async def test_an_unreadable_broker_is_a_recovery_refusal(clocked_repo) -> None:  # noqa: F811
    repo, _clock = clocked_repo
    await _stranded(repo)
    facade = SqliteAlpacaClerkFacade(
        account_mode="paper",
        repo=repo,
        read=_FakeRead(error=BrokerUnavailable("alpaca down")),
        trade=_FakeTrade(),
    )

    async def current_context() -> RecoveryPolicyContext:
        reader = SqliteClerkProjectionReader.from_repository(repo, clock=repo.clock)
        try:
            context = reader.recovery_context(strategy_instance_id=WATCHDOG_SID)
        finally:
            reader.close()
        assert context is not None
        return context

    capability = next(
        item
        for item in build_recovery_catalog(await current_context())
        if item.action_id == "discharge_attributed_residue"
    )
    assert capability.available, capability.unavailable_reason

    with pytest.raises(RecoveryExecutionError, match="nothing was discharged"):
        await execute_recovery_action(
            facade,
            request=RecoveryExecutionRequest(
                action_id="discharge_attributed_residue",
                concurrency_token=capability.concurrency_token,
                execution_ref=capability.execution_ref,
                reason=None,
            ),
            current_context=current_context,
        )
    assert _discharges(repo) == []
