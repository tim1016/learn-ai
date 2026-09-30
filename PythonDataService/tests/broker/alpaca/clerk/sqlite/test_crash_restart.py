"""What a process restart does to an order caught at each crash point (#2470).

Since #2550 a restart never resumes a run: boot recovery
(:meth:`SqliteAlpacaClerkFacade.recover`) stops every run it finds ACTIVE
(``service_restart_recovery``), and trading again takes a new Deploy under a
fresh bot identity. So each crash point asks one question of the stopped
run's order: does recovery book what the broker did, without sending anything
twice, and does it keep a new entry off the account while that is unknown?

Each test crashes the Clerk by closing its SQLite handle mid-flight, reopens
the same file the way a new process does, and runs boot recovery against a
broker double that holds what the broker did while the process was down.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest

from app.broker.alpaca.clerk.models import EffectPurpose
from app.broker.alpaca.clerk.sqlite.commands import submit_start_run
from app.broker.alpaca.clerk.sqlite.custody_subjects import bot_subject_id
from app.broker.alpaca.clerk.sqlite.enter import accept_enter
from app.broker.alpaca.clerk.sqlite.repository import DEFAULT_CLAIM_TTL_MS, ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.alpaca.clerk.sqlite.stopped_run_entries import entries_owed_a_cancel
from app.broker.alpaca.clerk.sqlite.uncertainty import admit_new_exposure
from app.broker.contract.models import BrokerOrder, BrokerPosition
from app.broker.contract.ports import BrokerReadPort, BrokerTradePort
from app.lean_sidecar.trading_calendar import session_open_ms_utc
from app.utils.timestamps import Clock, now_ms_utc
from tests._helpers.session_clock import pin_wall_clock_at
from tests.broker.alpaca.clerk.sqlite.conftest import _clock_at, _TestClock
from tests.broker.alpaca.clerk.sqlite.test_reconcile import (
    ACCOUNT_ID,
    RUN_ID,
    SID,
    _broker_order,
    _FakeRead,
    _FakeTrade,
    _leg,
    _position,
)
from tests.broker.alpaca.clerk.sqlite.test_runtime import _binding
from tests.broker.alpaca.clerk.sqlite.test_stopped_run_entries import (
    _facade,
    _OpenOrdersBroker,
    _working_enter,
)

# A second bot on the same account: what a new Deploy would register.
NEXT_DEPLOY_SID = "spy-bot-next"
# Long enough that no test here outlives the execution lease.
LEASE_TTL_MS = 300_000


def _crash_holding_the_send_claim(tmp_path: Path, clock: _TestClock) -> tuple[str, str]:
    """Accept one ENTER, claim it for the broker call, and die holding the claim.

    This is the durable state a process leaves when it is killed anywhere
    between claiming the send (``submit_accepted_enter``) and recording the
    broker's answer: the order ref the broker would know the order by is
    saved, the ENTER is ``accepted`` with no broker id, and the claim was never
    released. Returns ``(order_ref, effect_operation_id)``.
    """
    repo = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock, lease_ttl_ms=LEASE_TTL_MS
    )
    try:
        repo.register_strategy_instance(strategy_instance_id=SID, symbol="SPY", config_hash="h1")
        repo.register_strategy_instance(strategy_instance_id=NEXT_DEPLOY_SID, symbol="SPY", config_hash="h2")
        submit_start_run(repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id=RUN_ID)
        accepted = accept_enter(
            repo,
            account_id=ACCOUNT_ID,
            strategy_instance_id=SID,
            decision_id="entry-1",
            lifecycle_run_id=RUN_ID,
            leg=_leg(),
        )
        assert accepted.order_ref is not None and accepted.effect_operation_id is not None
        repo.claim_before_broker_contact(accepted.effect_operation_id)
    finally:
        repo.close()
    return accepted.order_ref, accepted.effect_operation_id


def _reopen(
    tmp_path: Path, *, read: BrokerReadPort, trade: BrokerTradePort, clock: Clock = now_ms_utc
) -> tuple[ClerkSqliteRepository, SqliteAlpacaClerkFacade]:
    """Open the dead process's SQLite file the way a new process does."""
    repo = ClerkSqliteRepository.open(
        account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock, lease_ttl_ms=LEASE_TTL_MS
    )
    return repo, SqliteAlpacaClerkFacade(account_mode="paper", repo=repo, read=read, trade=trade)


def _unresolved(repo: ClerkSqliteRepository) -> int:
    return len(repo.reconcilable_effect_operations(subject_id=bot_subject_id(SID)))


async def test_sent_but_unrecorded_working_entry_is_found_and_cancelled_never_resent(
    tmp_path: Path,
) -> None:
    """Crash point 1, order still working: the stopped run's entry is cancelled."""
    clock = _clock_at(1_700_000_000_000)
    order_ref, effect_id = _crash_holding_the_send_claim(tmp_path, clock)
    clock.advance(31_000)
    working = _broker_order(order_ref, order_id=f"bo-{order_ref}", status="new")
    trade = _FakeTrade(lookup_result=working)
    repo, facade = _reopen(tmp_path, clock=clock, read=_FakeRead(orders=[working]), trade=trade)
    try:
        await facade.recover()

        # The run is retired. The open-orders snapshot needs no claim, so it
        # books the broker id at once; the dead process's claim defers the
        # exact lookup and the cancel.
        assert repo.active_run(SID) is None
        assert repo.order(order_ref).broker_order_id == f"bo-{order_ref}"  # type: ignore[union-attr]
        assert trade.lookup_calls == [] and trade.cancel_calls == []
        assert _unresolved(repo) == 1
        assert admit_new_exposure(repo, strategy_instance_id=SID).reason_code == "ENTER_IN_PROGRESS"

        clock.advance(DEFAULT_CLAIM_TTL_MS + 1)
        await facade.reconcile_once()

        assert trade.cancel_calls == [f"bo-{order_ref}"]
        assert trade.submit_calls == []
        assert repo.effect_operation(effect_id).state == "in_progress"  # type: ignore[union-attr]
    finally:
        repo.close()


async def test_sent_but_unrecorded_fill_blocks_every_entry_until_it_is_booked(
    tmp_path: Path,
) -> None:
    """Crash point 1, order filled: no new Deploy can buy while the fill is unbooked."""
    clock = _clock_at(1_700_000_000_000)
    order_ref, effect_id = _crash_holding_the_send_claim(tmp_path, clock)
    clock.advance(31_000)
    filled = _broker_order(
        order_ref, order_id=f"bo-{order_ref}", status="filled", filled_quantity=1.0, filled_avg_price=100.0
    )
    trade = _FakeTrade(lookup_result=filled)
    read = _FakeRead(orders=[], positions=[_position("SPY", quantity=1.0)])
    repo, facade = _reopen(tmp_path, clock=clock, read=read, trade=trade)
    try:
        await facade.recover()

        # A share the Clerk cannot attribute is drift, and drift fences the
        # whole account: the next Deploy's bot is refused too.
        assert repo.active_run(SID) is None
        assert repo.effect_operation(effect_id).state == "accepted"  # type: ignore[union-attr]
        assert repo.position(SID, "SPY") == 0.0
        assert admit_new_exposure(repo, strategy_instance_id=NEXT_DEPLOY_SID).reason_code == "POSITION_DRIFT"

        clock.advance(DEFAULT_CLAIM_TTL_MS + 1)
        verdict = await facade.reconcile_once()

        assert verdict == "clean"
        assert trade.submit_calls == []
        assert repo.position(SID, "SPY") == 1.0
        assert _unresolved(repo) == 0
        assert admit_new_exposure(repo, strategy_instance_id=NEXT_DEPLOY_SID).allowed
        # The stopped bot's share is its own: it cannot enter on top of it.
        assert admit_new_exposure(repo, strategy_instance_id=SID).reason_code == "ATTRIBUTED_EXPOSURE_EXISTS"
    finally:
        repo.close()


async def test_recorded_but_unsent_entry_is_voided_never_sent(tmp_path: Path) -> None:
    """Crash point 2: the broker never saw it, so recovery voids it without sending."""
    clock = _clock_at(1_700_000_000_000)
    order_ref, effect_id = _crash_holding_the_send_claim(tmp_path, clock)
    clock.advance(1_000)
    trade = _FakeTrade(lookup_absent=True)
    repo, facade = _reopen(tmp_path, clock=clock, read=_FakeRead(), trade=trade)
    try:
        await facade.recover()

        # Within the dead claim nothing is looked up, and the ENTER keeps the
        # bot fenced: an absent order may still be about to land.
        assert repo.active_run(SID) is None
        assert repo.effect_operation(effect_id).state == "accepted"  # type: ignore[union-attr]
        assert trade.lookup_calls == []
        assert _unresolved(repo) == 1
        assert admit_new_exposure(repo, strategy_instance_id=SID).reason_code == "ENTER_IN_PROGRESS"

        clock.advance(DEFAULT_CLAIM_TTL_MS + 1)
        await facade.reconcile_once()

        assert trade.lookup_calls == [order_ref]
        assert trade.submit_calls == []
        assert repo.effect_operation(effect_id).state == "failed"  # type: ignore[union-attr]
        assert any(t["summary_code"] == "ORDER_SUBMIT_FAILED_ABSENT" for t in repo.transitions_for_order(order_ref))
        assert _unresolved(repo) == 0
    finally:
        repo.close()


class _BrokerWhileDown(_OpenOrdersBroker):
    """Alpaca as a restarted Clerk finds it: some orders ended while the process was down.

    An ended order leaves the open-orders list and is answered by the exact
    lookup only, as Alpaca answers a filled or expired order.
    """

    def __init__(self) -> None:
        super().__init__()
        self.release_submit.set()
        self.ended: dict[str, BrokerOrder] = {}
        self.positions: list[BrokerPosition] = []

    async def list_positions(self) -> list[BrokerPosition]:
        return list(self.positions)

    async def get_order_by_client_order_id(self, client_order_id: str) -> BrokerOrder | None:
        if client_order_id in self.ended:
            return self.ended[client_order_id]
        return await super().get_order_by_client_order_id(client_order_id)

    def end(self, order_ref: str, **update: Any) -> None:
        self.ended[order_ref] = self.open.pop(order_ref).model_copy(update=update)

    def fill(self, order_ref: str, *, price: float) -> None:
        order = self.open[order_ref]
        self.end(order_ref, status="filled", filled_quantity=order.quantity, filled_avg_price=price,
                 filled_at_ms=20, updated_at_ms=20, observed_at_ms=20)


def _stop_reasons(repo: ClerkSqliteRepository) -> list[str]:
    return [
        transition["facts_json"]
        for transition in repo.custody_transitions()
        if transition["transition_kind"] == "RUN_STOPPED"
    ]


@pytest.mark.usefixtures("wall_clock_in_session")
async def test_an_entry_filled_while_down_is_booked_to_the_ended_bot(tmp_path: Path) -> None:
    """Crash point 3: the fill is the stopped bot's, and nothing is cancelled or sent."""
    broker = _BrokerWhileDown()
    repo, facade = _facade(tmp_path, broker)
    order_ref = await _working_enter(facade)
    repo.close()
    broker.fill(order_ref, price=500.0)
    broker.positions = [_position("SPY", quantity=1.0)]

    restarted, clerk = _reopen(tmp_path, read=broker, trade=broker)
    try:
        await clerk.recover()

        assert restarted.active_run(SID) is None
        (stop,) = _stop_reasons(restarted)
        assert "service_restart_recovery" in stop
        assert restarted.order(order_ref).broker_state == "filled"  # type: ignore[union-attr]
        assert restarted.attributed_positions_for_strategy(SID) == {"SPY": 1.0}
        assert broker.submissions == [order_ref]
        assert broker.cancellations == []
        assert entries_owed_a_cancel(restarted) == []
        assert await clerk.reconcile_once() == "clean"
    finally:
        restarted.close()


@pytest.mark.usefixtures("wall_clock_in_session")
async def test_an_entry_working_at_restart_is_cancelled(tmp_path: Path) -> None:
    """Crash point 4, entry: an ended run's entry must not open a position later."""
    broker = _BrokerWhileDown()
    repo, facade = _facade(tmp_path, broker)
    order_ref = await _working_enter(facade)
    repo.close()

    restarted, clerk = _reopen(tmp_path, read=broker, trade=broker)
    try:
        await clerk.recover()

        assert restarted.active_run(SID) is None
        assert broker.cancellations == [f"broker-{order_ref}"]
        assert restarted.order(order_ref).broker_state == "canceled"  # type: ignore[union-attr]
        assert not any(restarted.attributed_positions_for_strategy(SID).values())
        assert await clerk.reconcile_once() == "clean"
    finally:
        restarted.close()


@pytest.mark.usefixtures("wall_clock_in_session")
async def test_an_exit_working_at_restart_is_left_working_until_it_fills(tmp_path: Path) -> None:
    """Crash point 4, exit: the Clerk owns an exit whether or not its run is alive (#2504)."""
    broker = _BrokerWhileDown()
    repo, facade = _facade(tmp_path, broker)
    entry_ref = await _working_enter(facade)
    broker.fill(entry_ref, price=500.0)
    broker.positions = [_position("SPY", quantity=1.0)]
    await facade.reconcile_account(trigger="AUTOMATIC")
    binding = _binding()
    exit_receipt = await facade.execute_for_instance(
        strategy_instance_id=binding.strategy_instance_id,
        run_id=binding.run_id,
        decision_id="decision-exit",
        purpose=EffectPurpose.EXIT,
        action_plan=binding.action_plan,
        quantity=1,
    )
    # The receipt also names the entry the exit reduces; the exit's own order is the one still open.
    (exit_ref,) = [ref for ref in exit_receipt.child_order_refs if ref in broker.open]
    sent_before_crash = list(broker.submissions)
    repo.close()

    restarted, clerk = _reopen(tmp_path, read=broker, trade=broker)
    try:
        await clerk.recover()

        assert restarted.active_run(SID) is None
        assert broker.cancellations == []
        assert broker.submissions == sent_before_crash
        assert restarted.attributed_positions_for_strategy(SID) == {"SPY": 1.0}
        assert restarted.active_exit_for_strategy(SID) is not None

        broker.fill(exit_ref, price=510.0)
        broker.positions = []
        assert await clerk.reconcile_once() == "clean"
        assert not any(restarted.attributed_positions_for_strategy(SID).values())
        assert broker.submissions == sent_before_crash
    finally:
        restarted.close()


async def test_a_restart_the_next_session_books_the_day_entry_alpaca_expired_and_sells_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Crash point 5: a DAY entry half-filled, then expired at the close while the process was down.

    The filled share stays the ended bot's and nothing sells it: a dead run
    warns and never exits on its own (#2411, #2504). An owner-set end that fell
    due while down is the one sale a restart makes, covered by
    ``test_scheduled_end.py::test_an_end_missed_while_the_clerk_was_down_sells_at_the_next_open_never_after_hours``.
    """
    pin_wall_clock_at(monkeypatch, session_open_ms_utc(date(2026, 9, 25)) + 60_000)
    broker = _BrokerWhileDown()
    repo, facade = _facade(tmp_path, broker)
    order_ref = await _working_enter(facade, quantity=2)
    repo.close()
    broker.end(order_ref, status="expired", filled_quantity=1, filled_avg_price=500.0,
               expired_at_ms=40, updated_at_ms=40, observed_at_ms=40)
    broker.positions = [_position("SPY", quantity=1.0)]

    # 08:00 ET on the next trading day, before its open.
    pin_wall_clock_at(monkeypatch, session_open_ms_utc(date(2026, 9, 28)) - 90 * 60_000)
    restarted, clerk = _reopen(tmp_path, read=broker, trade=broker)
    try:
        await clerk.recover()

        assert restarted.active_run(SID) is None
        assert restarted.order(order_ref).broker_state == "expired"  # type: ignore[union-attr]
        assert restarted.attributed_positions_for_strategy(SID) == {"SPY": 1.0}
        assert broker.submissions == [order_ref]
        assert broker.cancellations == []
        assert entries_owed_a_cancel(restarted) == []
        assert await clerk.reconcile_once() == "clean"
    finally:
        restarted.close()
