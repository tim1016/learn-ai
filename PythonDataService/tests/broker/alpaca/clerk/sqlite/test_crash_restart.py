"""What a process restart does to an order caught at each crash point (#2470).

Since #2550 a restart never resumes a run: boot recovery
(:meth:`SqliteAlpacaClerkFacade.recover`) stops every run it finds ACTIVE
(``service_restart_recovery``), and trading again takes a new Deploy under a
fresh bot identity. So each crash point asks of the stopped run's order:
does recovery book what the broker did without sending anything twice, and
does it keep the account from buying on top of a fill it has not booked? A
still-working order does not block the next Deploy's bot (#1793): it counts
against that bot's cash instead.

Each test crashes the Clerk by closing its SQLite handle mid-flight, reopens
the same file the way a new process does, and runs boot recovery against a
broker double that holds what the broker did while the process was down.
Closing the handle releases the execution lease; a lease a killed process
leaves behind is ``test_repository.py::test_expired_lease_allows_a_new_process_to_take_over``.

The crash points of the simulated worlds (#2919) are driven at the bottom of
this file against the real ``sim:`` and ``shadow:`` ports: their order price
binding lives in the accepting process's memory alone, so the crash point
between the send request and the simulated fill is where a restart could
lose the decision's price. The acceptance's durable pricing evidence
(``ExitAcceptedFacts.decision_pricing``) rebuilds it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.broker.alpaca.clerk.account_authority import (
    shadow_evidence_account_id_for_strategy,
    synthetic_account_id_for_strategy,
)
from app.broker.alpaca.clerk.models import EffectPurpose
from app.broker.alpaca.clerk.program_leg import ProgramLegPolicy
from app.broker.alpaca.clerk.shadow_broker import compose_shadow_ports
from app.broker.alpaca.clerk.sqlite.custody_subjects import bot_subject_id
from app.broker.alpaca.clerk.sqlite.repository import DEFAULT_CLAIM_TTL_MS, ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.alpaca.clerk.sqlite.stopped_run_entries import entries_owed_a_cancel
from app.broker.alpaca.clerk.sqlite.uncertainty import admit_new_exposure
from app.broker.alpaca.clerk.synthetic_broker import SyntheticBroker
from app.broker.contract.models import BrokerOrder, BrokerPosition
from app.broker.contract.ports import BrokerReadPort, BrokerTradePort
from app.lean_sidecar.trading_calendar import next_trading_day, session_open_ms_utc
from app.services.session_authority import et_minute_of_day_ms
from app.services.source_bar_ledger import SourceBarLedger
from app.utils.timestamps import Clock, now_ms_utc
from tests._helpers.bot_runner.custody import budget_consent_for_test
from tests._helpers.session_clock import IN_SESSION_DAY, pin_wall_clock_at, pin_wall_clock_in_session
from tests.broker.alpaca.clerk.sqlite.budgeted_run_fixture import (
    accept_budgeted_test_enter,
    submit_budgeted_test_run,
)
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
from tests.broker.alpaca.clerk.sqlite.test_runtime import _binding, _order
from tests.broker.alpaca.clerk.sqlite.test_runtime_program_leg import _binding as _sim_binding
from tests.broker.alpaca.clerk.sqlite.test_stopped_run_entries import (
    _facade,
    _OpenOrdersBroker,
    _working_enter,
)
from tests.broker.alpaca.clerk.test_shadow_broker import DAY, _Clock, _LiveRead, _retain

pytestmark = pytest.mark.usefixtures("budgeted_run_registration")

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
        submit_budgeted_test_run(repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id=RUN_ID)
        accepted = accept_budgeted_test_enter(
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


def _unresolved(repo: ClerkSqliteRepository) -> int:
    return len(repo.reconcilable_effect_operations(subject_id=bot_subject_id(SID)))


async def test_sent_but_unrecorded_working_entry_is_found_and_cancelled_never_resent(
    tmp_path: Path,
) -> None:
    """Crash point 1, order still working: the stopped run's entry is cancelled."""
    clock = _clock_at(session_open_ms_utc(IN_SESSION_DAY) + 60_000)
    order_ref, effect_id = _crash_holding_the_send_claim(tmp_path, clock)
    clock.advance(31_000)
    broker = _BrokerWhileDown()
    # Stamped like every order the double answers for, so its cancel evidence is newer.
    broker.open[order_ref] = _order(order_ref, _leg())
    repo, facade = _reopen(tmp_path, clock=clock, read=broker, trade=broker)
    try:
        await facade.recover()

        # The run is retired. The open-orders snapshot needs no claim, so it
        # books the broker id at once; the dead process's claim defers the
        # cancel. The next Deploy's bot may still enter (see module docstring).
        assert repo.active_run(SID) is None
        assert repo.order(order_ref).broker_order_id == f"broker-{order_ref}"  # type: ignore[union-attr]
        assert broker.cancellations == []
        assert _unresolved(repo) == 1
        assert admit_new_exposure(repo, strategy_instance_id=SID).reason_code == "ENTER_IN_PROGRESS"
        assert admit_new_exposure(repo, strategy_instance_id=NEXT_DEPLOY_SID).allowed

        clock.advance(DEFAULT_CLAIM_TTL_MS + 1)
        assert await facade.reconcile_once() == "clean"

        assert broker.cancellations == [f"broker-{order_ref}"]
        assert broker.submissions == []
        assert repo.order(order_ref).broker_state == "canceled"  # type: ignore[union-attr]
        assert repo.effect_operation(effect_id).state == "failed"  # type: ignore[union-attr]
        assert _unresolved(repo) == 0
    finally:
        repo.close()


async def test_sent_but_unrecorded_fill_blocks_every_entry_until_it_is_booked(
    tmp_path: Path,
) -> None:
    """Crash point 1, order filled: no new Deploy can buy while the fill is unbooked."""
    clock = _clock_at(session_open_ms_utc(IN_SESSION_DAY) + 60_000)
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
    clock = _clock_at(session_open_ms_utc(IN_SESSION_DAY) + 60_000)
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
        assert admit_new_exposure(repo, strategy_instance_id=NEXT_DEPLOY_SID).allowed

        clock.advance(DEFAULT_CLAIM_TTL_MS + 1)
        await facade.reconcile_once()

        assert trade.lookup_calls == [order_ref]
        assert trade.submit_calls == []
        assert trade.cancel_calls == []
        assert repo.effect_operation(effect_id).state == "failed"  # type: ignore[union-attr]
        assert any(t["summary_code"] == "ORDER_SUBMIT_FAILED_ABSENT" for t in repo.transitions_for_order(order_ref))
        assert _unresolved(repo) == 0
    finally:
        repo.close()


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

    The session boundary reaches recovery only through what it did to the
    order: Alpaca expired the DAY entry at the close. The filled share stays
    the ended bot's and nothing sells it, even the next morning: a dead run
    warns and never exits on its own (#2411, #2504). An owner-set end that fell
    due while down is the one sale a restart makes, covered by
    ``test_scheduled_end.py::test_an_end_missed_while_the_clerk_was_down_sells_at_the_next_open_never_after_hours``.
    """
    pin_wall_clock_in_session(monkeypatch)
    broker = _BrokerWhileDown()
    repo, facade = _facade(tmp_path, broker)
    order_ref = await _working_enter(facade, quantity=2)
    repo.close()
    broker.end(order_ref, status="expired", filled_quantity=1, filled_avg_price=500.0,
               expired_at_ms=40, updated_at_ms=40, observed_at_ms=40)
    broker.positions = [_position("SPY", quantity=1.0)]

    # 08:00 ET on the next trading day, before its open.
    pin_wall_clock_at(monkeypatch, session_open_ms_utc(next_trading_day(IN_SESSION_DAY)) - 90 * 60_000)
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


# ── the simulated worlds' crash point (#2919) ───────────────────────────
#
# A simulated decision EXIT binds its price in memory at the first send. The
# crash point below -- the process dies after the send request is durable and
# before the simulated order is recorded -- is where that binding is lost, and
# whichever pass sends the order again must rebuild it from the acceptance's
# durable pricing evidence, never from whatever bar is newest then.

_ENTRY_MINUTE, _EXIT_MINUTE, _AFTER_CRASH_MINUTE = 600, 601, 604
_SHADOW_LIVE_ACCOUNT = "9LIVE0001"


async def _dying_submit(_leg: Any, *, client_order_id: str) -> BrokerOrder:
    """The process's last act: the send request is durable, the fill never lands."""
    raise RuntimeError(f"process death between the send request and the simulated fill: {client_order_id}")


def _sale(orders: list[BrokerOrder]) -> BrokerOrder:
    (the_sale,) = [order for order in orders if order.side == "sell"]
    return the_sale


@pytest.mark.usefixtures("wall_clock_in_session")
async def test_a_live_shadow_decision_exit_re_driven_after_a_process_death_fills_at_its_own_decision_bar(
    tmp_path: Path,
) -> None:
    clock = _Clock(et_minute_of_day_ms(DAY, 602))
    ports = compose_shadow_ports(
        live_read=_LiveRead(), live_account_id=_SHADOW_LIVE_ACCOUNT, artifacts_root=tmp_path
    )
    evidence = SourceBarLedger(
        artifacts_root=tmp_path, account_id=shadow_evidence_account_id_for_strategy(SID)
    )
    entry_decision = _retain(evidence, minute=_ENTRY_MINUTE, close="100.25")
    repo = ClerkSqliteRepository.initialize(
        account_id=f"shadow:{_SHADOW_LIVE_ACCOUNT}", artifacts_root=tmp_path, clock=clock,
        lease_ttl_ms=LEASE_TTL_MS,
    )
    facade = SqliteAlpacaClerkFacade(
        repo=repo, read=ports.read, trade=ports.trade, authority_kind="shadow", account_mode="live",
        program_leg_policy=ProgramLegPolicy.from_read_port(ports.read),
    )
    binding = _sim_binding(use_rth=True, account_id=f"shadow:{_SHADOW_LIVE_ACCOUNT}").model_copy(
        update={"budget_consent": budget_consent_for_test(world="shadow")}
    )
    await facade.register_strategy_run(binding)
    try:
        bought = await facade.execute_for_instance(
            strategy_instance_id=SID, run_id=RUN_ID, decision_id="enter-1", purpose=EffectPurpose.ENTER,
            action_plan=binding.action_plan, quantity=binding.quantity, use_rth=True,
            retained_source_bar=entry_decision,
        )
        assert bought.state == "submitted", bought.explanation
        exit_decision = _retain(evidence, minute=_EXIT_MINUTE, close="100.50")
        ports.trade.submit = _dying_submit  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="process death"):
            await facade.execute_for_instance(
                strategy_instance_id=SID, run_id=RUN_ID, decision_id="exit-1", purpose=EffectPurpose.EXIT,
                action_plan=binding.action_plan, quantity=binding.quantity, use_rth=True,
                retained_source_bar=exit_decision,
            )
    finally:
        repo.close()
        evidence.close()

    # While the process is down, a newer bar for the symbol lands.
    reopened_evidence = SourceBarLedger(
        artifacts_root=tmp_path, account_id=shadow_evidence_account_id_for_strategy(SID)
    )
    try:
        _retain(reopened_evidence, minute=_AFTER_CRASH_MINUTE, close="999.00")
    finally:
        reopened_evidence.close()
    # Past the 30 s submit-absence grace, still inside the session.
    clock.now_ms = et_minute_of_day_ms(DAY, 605)

    restarted = ClerkSqliteRepository.open(
        account_id=f"shadow:{_SHADOW_LIVE_ACCOUNT}", artifacts_root=tmp_path, clock=clock,
        lease_ttl_ms=LEASE_TTL_MS,
    )
    ports2 = compose_shadow_ports(
        live_read=_LiveRead(), live_account_id=_SHADOW_LIVE_ACCOUNT, artifacts_root=tmp_path, clock=clock
    )
    clerk = SqliteAlpacaClerkFacade(
        repo=restarted, read=ports2.read, trade=ports2.trade, authority_kind="shadow", account_mode="live",
        program_leg_policy=ProgramLegPolicy.from_read_port(ports2.read),
    )
    try:
        await clerk.recover()
        assert await clerk.reconcile_once() == "clean"

        sale = _sale(await ports2.read.list_orders(status="all"))
        assert (sale.status, sale.filled_avg_price, sale.filled_at_ms, sale.submitted_at_ms) == (
            "filled", 100.50, exit_decision.end_ms, exit_decision.end_ms,
        )
        assert sale.client_order_id is not None
        anchor = ports2.trade.synthesized_fill_anchors()[sale.client_order_id]
        assert (anchor.fill_model, anchor.decision_bar_end_ms) == (
            "decision_bar_close", exit_decision.end_ms,
        )
        assert await ports2.read.list_positions() == []
        assert restarted.reconcilable_effect_operations(subject_id=bot_subject_id(SID)) == []
    finally:
        restarted.close()


async def test_a_dry_run_decision_exit_re_driven_after_a_process_death_fills_at_its_own_decision_bar(
    tmp_path: Path,
) -> None:
    sim_account = synthetic_account_id_for_strategy(SID)
    clock = _Clock(et_minute_of_day_ms(DAY, 602))
    bars = SourceBarLedger(artifacts_root=tmp_path, account_id=sim_account)
    entry_decision = _retain(bars, minute=_ENTRY_MINUTE, close="100.25")
    broker = SyntheticBroker(account_id=sim_account, source_bars=bars, clock=clock)
    repo = ClerkSqliteRepository.initialize(
        account_id=sim_account, artifacts_root=tmp_path, clock=clock, lease_ttl_ms=LEASE_TTL_MS
    )
    facade = SqliteAlpacaClerkFacade(
        repo=repo, read=broker, trade=broker, authority_kind="synthetic", account_mode="paper"
    )
    binding = _sim_binding(use_rth=True, account_id=sim_account).model_copy(
        update={"budget_consent": budget_consent_for_test(world="synthetic")}
    )
    await facade.register_strategy_run(binding)
    try:
        bought = await facade.execute_for_instance(
            strategy_instance_id=SID, run_id=RUN_ID, decision_id="enter-1", purpose=EffectPurpose.ENTER,
            action_plan=binding.action_plan, quantity=binding.quantity, use_rth=True,
            retained_source_bar=entry_decision,
        )
        assert bought.state == "submitted", bought.explanation
        exit_decision = _retain(bars, minute=_EXIT_MINUTE, close="100.50")
        broker.submit = _dying_submit  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="process death"):
            await facade.execute_for_instance(
                strategy_instance_id=SID, run_id=RUN_ID, decision_id="exit-1", purpose=EffectPurpose.EXIT,
                action_plan=binding.action_plan, quantity=binding.quantity, use_rth=True,
                retained_source_bar=exit_decision,
            )
    finally:
        repo.close()
        bars.close()

    # While the process is down, a newer bar for the symbol lands; the Dry
    # Run's unbound fallback would fill the sale at its close ($999.00).
    reopened_bars = SourceBarLedger(artifacts_root=tmp_path, account_id=sim_account)
    try:
        _retain(reopened_bars, minute=_AFTER_CRASH_MINUTE, close="999.00")
        # Past the 30 s submit-absence grace, still inside the session.
        clock.now_ms = et_minute_of_day_ms(DAY, 605)

        restarted = ClerkSqliteRepository.open(
            account_id=sim_account, artifacts_root=tmp_path, clock=clock, lease_ttl_ms=LEASE_TTL_MS
        )
        broker2 = SyntheticBroker(account_id=sim_account, source_bars=reopened_bars, clock=clock)
        clerk = SqliteAlpacaClerkFacade(
            repo=restarted, read=broker2, trade=broker2, authority_kind="synthetic", account_mode="paper"
        )
        try:
            await clerk.recover()
            assert await clerk.reconcile_once() == "clean"

            sale = _sale(await broker2.list_orders(status="all"))
            assert (sale.status, sale.filled_avg_price, sale.filled_at_ms, sale.submitted_at_ms) == (
                "filled", 100.50, exit_decision.end_ms, exit_decision.end_ms,
            )
            assert sale.client_order_id is not None
            anchor = broker2.synthesized_fill_anchors()[sale.client_order_id]
            assert (anchor.fill_model, anchor.decision_bar_end_ms) == (
                "decision_bar_close", exit_decision.end_ms,
            )
            assert await broker2.list_positions() == []
            assert restarted.reconcilable_effect_operations(subject_id=bot_subject_id(SID)) == []
        finally:
            restarted.close()
    finally:
        reopened_bars.close()
