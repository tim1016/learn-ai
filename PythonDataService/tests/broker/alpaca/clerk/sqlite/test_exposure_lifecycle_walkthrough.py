"""Direction-1 done-when: crash-with-exposure -> refuse-resume -> flatten
(via the presented recovery action) -> resume-to-flat, entirely under SQLite
Clerk custody with fake broker ports (PRD #1752).

Test-only proof: it pins the whole exposure-lifecycle guarantee with one
executable chain. If any link regresses, this fails. The refuse/resume
assertions exercise the custody-level admission gate
(``decide_capability(NEW_EXPOSURE)`` -> ``ATTRIBUTED_EXPOSURE_EXISTS``) that
backs the Resume operation; the higher-layer Resume seam
(``evaluate_run_admission`` -> ``RESUME_CARRYOVER_UNSUPPORTED``) has its own
focused coverage in ``tests/services/test_run_admission.py``.
"""

from __future__ import annotations

from pathlib import Path

from app.broker.alpaca.clerk.sqlite.commands import submit_start_run, submit_stop_run
from app.broker.alpaca.clerk.sqlite.folds import position_quantity_is_nonzero
from app.broker.alpaca.clerk.sqlite.projections import SqliteClerkProjectionReader
from app.broker.alpaca.clerk.sqlite.reconcile import reconcile_account
from app.broker.alpaca.clerk.sqlite.recovery_execution import (
    RecoveryExecutionRequest,
    execute_recovery_action,
)
from app.broker.alpaca.clerk.sqlite.recovery_policy import build_recovery_catalog
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import ReentrantAsyncLock, SqliteAlpacaClerkFacade
from app.broker.alpaca.clerk.sqlite.uncertainty import Capability, decide_capability
from app.broker.alpaca.clerk.trade_evidence import SqliteTradeUpdateEvidenceSink
from app.broker.contract.models import BrokerOrderEvent
from tests.broker.alpaca.clerk.sqlite.conftest import (
    _AssertingNoReconciler,
    _broker_order_fixture,
    _broker_position_fixture,
    _clock_at,
    _FakeReadPort,
    _FakeTradePort,
    _make_held_position,
)

ACCOUNT_ID = "PA-WALK"
SID = "walk-bot"
RUN_ID = "run-1"


async def test_crashed_exposure_walks_to_flat_and_readmits_resume(tmp_path: Path) -> None:
    clock = _clock_at(1_700_000_000_000)
    repo = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock, lease_ttl_ms=300_000
    )
    repo.register_strategy_instance(strategy_instance_id=SID, symbol="SPY", config_hash="h1")
    submit_start_run(repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id=RUN_ID)
    await _make_held_position(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, run_id=RUN_ID
    )  # filled entry, attributed +10

    # 1. Crash analog: the same durable STOP runtime.recover() commits on restart.
    submit_stop_run(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID,
        lifecycle_run_id=RUN_ID, operator_reason="service_restart_recovery",
    )

    # 2. Refuse-resume: fresh exposure is refused while custody holds the position.
    refused = decide_capability(repo, capability=Capability.NEW_EXPOSURE, strategy_instance_id=SID)
    assert refused.allowed is False
    assert refused.reason_code == "ATTRIBUTED_EXPOSURE_EXISTS"

    # 3. Operator reconciles; the panel presents execute_safe_flatten.
    broker_read = _FakeReadPort(positions=[_broker_position_fixture("SPY", quantity=10.0)])
    flatten_trade = _FakeTradePort()
    facade = SqliteAlpacaClerkFacade(repo=repo, read=broker_read, trade=flatten_trade)
    await facade.reconcile_account(trigger="OPERATOR_RECONCILE_NOW")

    async def current_context():
        reader = SqliteClerkProjectionReader.from_repository(repo, clock=repo.clock)
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

    # The reduction that actually reached the broker is an exact 10-share SELL —
    # the honest fake echoes the submitted leg, so a wrong-sized reduction fails.
    assert len(flatten_trade.submitted_legs) == 1
    reducing_leg = flatten_trade.submitted_legs[0]
    assert reducing_leg.side == "sell"
    assert reducing_leg.quantity == 10

    # 5. The reducing fill arrives (websocket analog) and the sweep proves flat.
    sink = SqliteTradeUpdateEvidenceSink(
        repo=repo, intake=ReentrantAsyncLock(), reconciler=_AssertingNoReconciler()
    )
    filled_reducing = _broker_order_fixture(
        reducing_ref, side="sell", status="filled", quantity=10.0,
        filled_quantity=10, filled_avg_price=101.0,
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
        repo, read=_FakeReadPort(positions=[]), trade=_FakeTradePort(), trigger="AUTOMATIC"
    )
    attributed = repo.attributed_positions_for_strategy(SID)
    assert not any(position_quantity_is_nonzero(qty) for qty in attributed.values())

    # 6. Resume-to-flat: fresh exposure is admissible again.
    readmitted = decide_capability(repo, capability=Capability.NEW_EXPOSURE, strategy_instance_id=SID)
    assert readmitted.allowed is True

    repo.close()
