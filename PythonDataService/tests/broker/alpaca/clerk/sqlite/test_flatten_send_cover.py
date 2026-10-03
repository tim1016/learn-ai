"""An operator's Flatten checks the Alpaca account just before the sale is sent (#2839).

The reducing order is sized from the Clerk's own record of the position. As
it is about to go to a real broker, the Clerk reads the account's open
orders, then its positions, and sends only a sale the account covers:
``position - unfilled open sells in the symbol >= the sale``. These tests
drive the real facade against an account whose state changes between the
reconciliation that offers the Flatten and the send.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import pytest

from app.broker.alpaca.clerk.recovery_reduction import UNPRICEABLE_RECOVERY
from app.broker.alpaca.clerk.sqlite.account_open_work import MAX_OPEN_ORDER_SNAPSHOT
from app.broker.alpaca.clerk.sqlite.commands import submit_start_run, submit_stop_run
from app.broker.alpaca.clerk.sqlite.exit import (
    accept_exit,
    accept_recovery_exit,
    resolve_accepted_exit,
    resolve_exit,
)
from app.broker.alpaca.clerk.sqlite.exit_resolution import (
    RECOVERY_FLATTEN_DECISION_PREFIX,
    flatten_send_refusal,
)
from app.broker.alpaca.clerk.sqlite.flatten_cover import (
    FLATTEN_COVER_UNPROVEN,
    FLATTEN_NOT_COVERED_AT_BROKER,
    flatten_cover_refusal,
)
from app.broker.alpaca.clerk.sqlite.folds import POSITION_QTY_EPSILON
from app.broker.alpaca.clerk.sqlite.projection_models import SafeFlattenPlan
from app.broker.alpaca.clerk.sqlite.projections import SqliteClerkProjectionReader
from app.broker.alpaca.clerk.sqlite.recovery_execution import RecoveryExecutionError
from app.broker.alpaca.clerk.sqlite.recovery_policy import (
    RecoveryPolicyContext,
    build_recovery_catalog,
)
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.alpaca.clerk.sqlite.safe_flatten_execution import SafeFlattenExecutionError
from app.broker.alpaca.clerk.sqlite.uncertainty import EXIT_NOT_FLAT_REASON_CODE
from app.broker.contract.errors import BrokerError, BrokerUnavailable
from app.broker.contract.models import BrokerOrder, BrokerOrderLeg, BrokerPosition, OrderSide
from tests._helpers.exit_terms import DEPLOY_EXIT_TERMS
from tests.broker.alpaca.clerk.sqlite import test_safe_flatten_execution as safe_flatten
from tests.broker.alpaca.clerk.sqlite.conftest import _make_held_position, _walk_clock_to
from tests.broker.alpaca.clerk.sqlite.test_safe_flatten_execution import (
    ACCOUNT_ID,
    RUN_ID,
    SID,
    crashed_with_exposure,  # noqa: F401 -- the stopped-bot repository these tests reuse
)

SIBLING_SID = "sibling-bot"
SIBLING_RUN_ID = "sibling-run-1"


class _Account:
    """The Alpaca account an operator's Flatten is checked against.

    What it holds, what is open there and whether it answers are the test's
    to change between the reconciliation that offers the Flatten and the
    send. Every read it answers is recorded, in order.
    """

    def __init__(self, *, holds: float = 10.0) -> None:
        self.holds = holds
        self.open_orders: list[BrokerOrder] = []
        self.unreadable: BrokerError | None = None
        self.reads: list[str] = []

    async def list_orders(
        self, *, status: str | None = None, limit: int | None = None, after_ms: int | None = None
    ) -> list[BrokerOrder]:
        self.reads.append("orders")
        if self.unreadable is not None:
            raise self.unreadable
        return list(self.open_orders)

    async def list_positions(self) -> list[BrokerPosition]:
        self.reads.append("positions")
        if self.unreadable is not None:
            raise self.unreadable
        return [safe_flatten._position("SPY", quantity=self.holds)] if self.holds else []


class _TradeIntoAccount(safe_flatten._FakeTrade):
    """A trade port whose accepted orders are then working in the account it sells from."""

    def __init__(self, account: _Account) -> None:
        super().__init__()
        self._account = account

    async def submit(self, leg: BrokerOrderLeg, *, client_order_id: str) -> BrokerOrder:
        accepted = await super().submit(leg, client_order_id=client_order_id)
        self._account.open_orders.append(accepted)
        return accepted


class _NoSubmitTrade(safe_flatten._FakeTrade):
    """A Dry Run's or a Shadow's port: it fills inside ``submit`` and reaches no account."""

    submission_response_is_authoritative_evidence = True


def _open_sell(order_id: str, *, quantity: float | None, symbol: str = "SPY") -> BrokerOrder:
    """A sell working at Alpaca that this Flatten did not place."""
    return safe_flatten._broker_order(
        f"elsewhere:{order_id}", order_id=order_id, symbol=symbol, status="new", side="sell",
    ).model_copy(update={"quantity": quantity})


type _Context = Callable[[], Awaitable[RecoveryPolicyContext]]


def _context_of(repo: ClerkSqliteRepository, sid: str) -> _Context:
    async def current_context() -> RecoveryPolicyContext:
        reader = SqliteClerkProjectionReader.from_repository(repo, clock=repo.clock)
        try:
            context = reader.recovery_context(strategy_instance_id=sid)
        finally:
            reader.close()
        assert context is not None
        return context

    return current_context


async def _stopped_bot_offered_its_flatten(
    repo: ClerkSqliteRepository, *, account: _Account, trade: safe_flatten._FakeTrade
) -> tuple[SqliteAlpacaClerkFacade, _Context]:
    """A stopped bot holding 10 SPY, reconciled clean against ``account``, so its Flatten is offered.

    The account's reads so far are forgotten: what is left in ``account.reads``
    afterwards is what the Flatten itself read.
    """
    await safe_flatten._held_position(repo)
    submit_stop_run(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID,
        lifecycle_run_id=RUN_ID, operator_reason="operator_stop",
    )
    facade = SqliteAlpacaClerkFacade(account_mode="paper", repo=repo, read=account, trade=trade)
    result = await facade.reconcile_account(trigger="OPERATOR_RECONCILE_NOW")
    assert result.verdict == "clean"
    account.reads.clear()
    return facade, _context_of(repo, SID)


async def _flatten(facade: SqliteAlpacaClerkFacade, current_context: _Context):
    """The operator's Flatten, dispatched exactly as the panel and the recovery route dispatch it."""
    return await safe_flatten._execute(facade, current_context, confirmed_limit=None)


def _exit_not_flat(repo: ClerkSqliteRepository, sid: str = SID) -> dict | None:
    return repo.active_uncertainty(
        scope="CUSTODY_SUBJECT", reason_code=EXIT_NOT_FLAT_REASON_CODE, strategy_instance_id=sid
    )


def _flatten_exit(repo: ClerkSqliteRepository, sid: str = SID) -> str:
    """The effect of the one EXIT ``sid``'s Flatten was accepted as."""
    (effect_operation_id,) = {
        order.effect_operation_id for order in repo.orders_for_strategy(sid) if order.role == "REDUCING"
    }
    return effect_operation_id


def _account_holds_nothing(account: _Account) -> None:
    account.holds = 0.0


def _account_holds_six(account: _Account) -> None:
    account.holds = 6.0


def _an_unknown_sell_takes_four(account: _Account) -> None:
    account.open_orders = [_open_sell("unknown-sell-1", quantity=4.0)]


def _account_cannot_be_read(account: _Account) -> None:
    account.unreadable = BrokerUnavailable("Alpaca did not answer")


def _open_order_page_is_full(account: _Account) -> None:
    account.open_orders = [
        _open_sell(f"other-{index}", quantity=1.0, symbol="MSFT")
        for index in range(MAX_OPEN_ORDER_SNAPSHOT)
    ]


def _an_open_sell_states_no_quantity(account: _Account) -> None:
    account.open_orders = [_open_sell("notional-sell-1", quantity=None)]


@pytest.mark.parametrize(
    ("at_alpaca", "reason_code", "told"),
    [
        pytest.param(
            _account_holds_nothing, FLATTEN_NOT_COVERED_AT_BROKER,
            "Alpaca holds no SPY, less than the 10 this flatten would sell; nothing was sent.",
            id="the-account-is-flat",
        ),
        pytest.param(
            _account_holds_six, FLATTEN_NOT_COVERED_AT_BROKER,
            "Alpaca holds 6 SPY, less than the 10 this flatten would sell; nothing was sent.",
            id="fewer-shares-than-the-clerk-attributes",
        ),
        pytest.param(
            _an_unknown_sell_takes_four, FLATTEN_NOT_COVERED_AT_BROKER,
            "Alpaca holds 10 SPY with 4 of it already in open sell orders, less than the 10 "
            "this flatten would sell; nothing was sent.",
            id="an-unknown-open-sell-takes-the-cover",
        ),
        pytest.param(
            _account_cannot_be_read, FLATTEN_COVER_UNPROVEN,
            "The Clerk could not read the Alpaca account just before sending this flatten "
            "(Alpaca did not answer)",
            id="the-account-cannot-be-read",
        ),
        pytest.param(
            _open_order_page_is_full, FLATTEN_COVER_UNPROVEN,
            "Alpaca returned 500 open orders, the most one read returns",
            id="the-open-order-page-is-full",
        ),
        pytest.param(
            _an_open_sell_states_no_quantity, FLATTEN_COVER_UNPROVEN,
            "An open SPY order at Alpaca (notional-sell-1) states no share quantity",
            id="an-open-sell-states-no-quantity",
        ),
    ],
)
async def test_a_flatten_the_account_does_not_cover_is_refused_and_nothing_is_sent(
    crashed_with_exposure,  # noqa: F811
    at_alpaca: Callable[[_Account], None],
    reason_code: str,
    told: str,
) -> None:
    """The Clerk attributes 10 SPY; by the time the sale is sent, Alpaca does not cover it.

    Before #2839 every one of these sold 10 SPY: nothing read the account at
    the send. Now nothing reaches the broker, the operator is told why under
    the check's own code, and the bot keeps the open-exit notice a broker's
    refusal leaves.
    """
    repo, _clock = crashed_with_exposure
    account, trade = _Account(holds=10.0), safe_flatten._FakeTrade()
    facade, current_context = await _stopped_bot_offered_its_flatten(repo, account=account, trade=trade)
    at_alpaca(account)

    with pytest.raises(RecoveryExecutionError) as refused:
        await _flatten(facade, current_context)

    assert trade.submit_calls == []
    assert refused.value.refusal is not None
    assert refused.value.refusal.reason_code == reason_code
    assert told in str(refused.value)
    assert "nothing was sent" in str(refused.value)
    # Folded as a broker's refusal is: the EXIT failed, the entry is free for
    # another reduction, and the bot stays flagged with exposure still held.
    assert repo.active_exit_for_strategy(SID) is None
    assert repo.position(SID, "SPY") == 10
    episode = _exit_not_flat(repo)
    assert episode is not None
    assert told in episode["explanation"]
    # Never recorded as sent: the watchdog counts no broker attempt against it.
    (reducing,) = [order for order in repo.orders_for_strategy(SID) if order.role == "REDUCING"]
    assert not repo.has_order_transition(
        order_ref=reducing.order_ref, transition_kind="ORDER_SUBMIT_REQUESTED"
    )


async def test_a_flatten_alpaca_covers_exactly_is_sent_after_reading_orders_then_positions(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """Open orders are read first, then positions: a sell that fills in between is counted twice, never missed."""
    repo, _clock = crashed_with_exposure
    account, trade = _Account(holds=10.0), safe_flatten._FakeTrade()
    facade, current_context = await _stopped_bot_offered_its_flatten(repo, account=account, trade=trade)

    result = await _flatten(facade, current_context)

    assert result.applied is True
    assert [(leg.side, leg.quantity) for leg in trade.submitted_legs] == [("sell", 10)]
    assert account.reads == ["orders", "positions"]


async def test_a_flatten_is_sent_past_another_orders_open_sell_that_leaves_cover(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """Cover, not equality: a sibling's sell of 10 working out of 20 leaves this bot's 10 covered."""
    repo, _clock = crashed_with_exposure
    account, trade = _Account(holds=10.0), safe_flatten._FakeTrade()
    facade, current_context = await _stopped_bot_offered_its_flatten(repo, account=account, trade=trade)
    account.holds = 20.0
    account.open_orders = [_open_sell("sibling-sell-1", quantity=10.0)]

    result = await _flatten(facade, current_context)

    assert result.applied is True
    assert [(leg.side, leg.quantity) for leg in trade.submitted_legs] == [("sell", 10)]


async def _two_stopped_bots_on_spy(
    repo: ClerkSqliteRepository, *, account: _Account, trade: safe_flatten._FakeTrade
) -> tuple[SqliteAlpacaClerkFacade, SafeFlattenPlan, SafeFlattenPlan]:
    """Two stopped bots holding 10 SPY each in one account, with the Flatten each is offered."""
    await safe_flatten._held_position(repo)
    repo.register_strategy_instance(
        exit_terms=DEPLOY_EXIT_TERMS, strategy_instance_id=SIBLING_SID, symbol="SPY", config_hash="h2"
    )
    submit_start_run(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SIBLING_SID, lifecycle_run_id=SIBLING_RUN_ID
    )
    await _make_held_position(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SIBLING_SID, run_id=SIBLING_RUN_ID,
        decision_id="sibling-enter-1", execution_id="sibling-exec-1",
    )
    for sid, run_id in ((SID, RUN_ID), (SIBLING_SID, SIBLING_RUN_ID)):
        submit_stop_run(
            repo, account_id=ACCOUNT_ID, strategy_instance_id=sid,
            lifecycle_run_id=run_id, operator_reason="operator_stop",
        )
    facade = SqliteAlpacaClerkFacade(account_mode="paper", repo=repo, read=account, trade=trade)
    result = await facade.reconcile_account(trigger="OPERATOR_RECONCILE_NOW")
    assert result.verdict == "clean"
    plans = []
    for sid in (SID, SIBLING_SID):
        capability = {
            item.action_id: item for item in build_recovery_catalog(await _context_of(repo, sid)())
        }["execute_safe_flatten"]
        assert capability.available, capability.unavailable_reason
        assert capability.reduction_plan is not None
        plans.append(capability.reduction_plan)
    return facade, plans[0], plans[1]


async def test_two_cohort_legs_on_one_symbol_are_both_sent_back_to_back(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """A cohort Flatten sends its legs one after another: the first leg's working sell must not refuse the second.

    Reconciliation's rule -- the broker's position equals the Clerk's and
    nothing is working on the symbol -- would refuse the second leg here.
    """
    repo, _clock = crashed_with_exposure
    account = _Account(holds=20.0)
    trade = _TradeIntoAccount(account)
    facade, first_leg, second_leg = await _two_stopped_bots_on_spy(repo, account=account, trade=trade)

    await facade.execute_safe_flatten(plan=first_leg)
    assert len(account.open_orders) == 1  # the first leg is working, unfilled, at Alpaca
    await facade.execute_safe_flatten(plan=second_leg)

    assert [(leg.side, leg.quantity) for leg in trade.submitted_legs] == [("sell", 10), ("sell", 10)]


async def test_the_second_cohort_leg_is_refused_when_alpaca_holds_only_one_legs_shares(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """The first leg's working sell already takes the 10 Alpaca holds, so the second would sell short."""
    repo, _clock = crashed_with_exposure
    account = _Account(holds=20.0)
    trade = _TradeIntoAccount(account)
    facade, first_leg, second_leg = await _two_stopped_bots_on_spy(repo, account=account, trade=trade)
    account.holds = 10.0

    await facade.execute_safe_flatten(plan=first_leg)
    with pytest.raises(SafeFlattenExecutionError) as refused:
        await facade.execute_safe_flatten(plan=second_leg)

    assert len(trade.submit_calls) == 1
    assert refused.value.refusal is not None
    assert refused.value.refusal.reason_code == FLATTEN_NOT_COVERED_AT_BROKER
    assert (
        "Alpaca holds 10 SPY with 10 of it already in open sell orders, less than the 10 "
        "this flatten would sell; nothing was sent."
    ) in str(refused.value)


async def test_a_deferred_flatten_is_checked_when_a_reconcile_pass_sends_it(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """A Flatten accepted while its entry could not be proven sends nothing; the sweep sends it later.

    A check made only before acceptance would miss that later send: the
    account is flat by then, and the pass must not sell.
    """
    repo, clock = crashed_with_exposure
    account, trade = _Account(holds=10.0), safe_flatten._LookupOutageTrade()
    facade, current_context = await _stopped_bot_offered_its_flatten(repo, account=account, trade=trade)

    deferred = await _flatten(facade, current_context)
    assert deferred.applied is True and deferred.orders == ()
    assert trade.submit_calls == []
    effect = repo.active_exit_for_strategy(SID)
    assert effect is not None

    trade.lookups_fail = False
    account.holds = 0.0
    _walk_clock_to(repo, clock.value + 15_000)
    await facade.reconcile_account(trigger="AUTOMATIC")

    assert trade.submit_calls == []
    refusal = flatten_send_refusal(repo, effect.effect_operation_id)
    assert refusal is not None
    assert refusal.reason_code == FLATTEN_NOT_COVERED_AT_BROKER
    assert repo.active_exit_for_strategy(SID) is None
    episode = _exit_not_flat(repo)
    assert episode is not None
    assert "Alpaca holds no SPY, less than the 10 this flatten would sell" in episode["explanation"]


async def test_a_deferred_flatten_the_account_still_covers_is_sent_by_the_reconcile_pass(
    crashed_with_exposure,  # noqa: F811
) -> None:
    repo, clock = crashed_with_exposure
    account, trade = _Account(holds=10.0), safe_flatten._LookupOutageTrade()
    facade, current_context = await _stopped_bot_offered_its_flatten(repo, account=account, trade=trade)
    await _flatten(facade, current_context)
    assert trade.submit_calls == []

    trade.lookups_fail = False
    _walk_clock_to(repo, clock.value + 15_000)
    await facade.reconcile_account(trigger="AUTOMATIC")

    assert [(leg.side, leg.quantity) for leg in trade.submitted_legs] == [("sell", 10)]


async def test_a_resumed_flatten_is_checked_again_before_it_is_sent_again(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """A submit that was lost is sent again only once proven absent -- and only if still covered."""
    repo, clock = crashed_with_exposure
    account = _Account(holds=10.0)
    lost = safe_flatten._FakeTrade(submit_error=BrokerUnavailable("timeout"))
    facade, current_context = await _stopped_bot_offered_its_flatten(repo, account=account, trade=lost)
    await _flatten(facade, current_context)
    assert len(lost.submit_calls) == 1
    effect_operation_id = _flatten_exit(repo)

    class _Absent(safe_flatten._FakeTrade):
        async def get_order_by_client_order_id(self, client_order_id: str) -> BrokerOrder | None:
            self.lookup_calls.append(client_order_id)
            return None

    # Past the 30 s submit-absence grace, the exact lookup proves the order
    # never reached Alpaca -- and by now the account is flat.
    _walk_clock_to(repo, clock.value + 40_000)
    account.holds = 0.0
    resumed = _Absent()
    await resolve_exit(
        repo, effect_operation_id=effect_operation_id, trade=resumed,
        pricing=UNPRICEABLE_RECOVERY, read=account,
    )

    assert resumed.submit_calls == []
    refusal = flatten_send_refusal(repo, effect_operation_id)
    assert refusal is not None and refusal.reason_code == FLATTEN_NOT_COVERED_AT_BROKER


async def test_a_refused_flatten_is_left_to_the_watchdog_which_sends_once_the_account_agrees(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """Owner decision #2839: a refused send is treated as a broker's refusal is.

    The open-exit notice stays, and the stuck-EXIT watchdog re-drives it under
    its own check of the account: nothing while the unknown sell is open, one
    sale of the attributed 10 once Alpaca holds them free again.
    """
    repo, clock = crashed_with_exposure
    account, trade = _Account(holds=10.0), safe_flatten._FakeTrade()
    facade, current_context = await _stopped_bot_offered_its_flatten(repo, account=account, trade=trade)
    account.open_orders = [_open_sell("unknown-sell-1", quantity=4.0)]
    with pytest.raises(RecoveryExecutionError):
        await _flatten(facade, current_context)
    refused_at_ms = clock.value

    async def passes(first_s: int, last_s: int) -> None:
        for after_s in range(first_s, last_s + 1, 15):
            _walk_clock_to(repo, refused_at_ms + after_s * 1_000)
            await facade.reconcile_account(trigger="AUTOMATIC")

    await passes(15, 150)  # past the watchdog's 120 s settle wait, the sell still open
    assert trade.submit_calls == []
    assert _exit_not_flat(repo) is not None

    account.open_orders = []
    await passes(165, 225)

    assert [(leg.side, leg.quantity) for leg in trade.submitted_legs] == [("sell", 10)]


async def test_a_programs_exit_is_sent_without_reading_the_account(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """Only an operator's Flatten is checked: a deciding program's EXIT sends as before, even where a read port is at hand."""
    repo, _clock = crashed_with_exposure
    entry_ref = await safe_flatten._held_position(repo)
    accepted = accept_exit(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, decision_id="program-exit-1",
        lifecycle_run_id=RUN_ID, entry_order_ref=entry_ref,
    )
    account, trade = _Account(holds=10.0), safe_flatten._FakeTrade()

    # The reconciliation sweep drives every EXIT it finds with the read port.
    await resolve_exit(
        repo, effect_operation_id=accepted.effect_operation_id, trade=trade,
        pricing=UNPRICEABLE_RECOVERY, read=account,
    )

    assert [(leg.side, leg.quantity) for leg in trade.submitted_legs] == [("sell", 10)]
    assert account.reads == []


async def test_a_flatten_through_a_no_submit_port_is_sent_without_reading_the_account(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """A Dry Run's or a Shadow's Flatten fills in its own simulation: no account is read, and none can refuse it."""
    repo, _clock = crashed_with_exposure
    account, trade = _Account(holds=10.0), _NoSubmitTrade()
    facade, current_context = await _stopped_bot_offered_its_flatten(repo, account=account, trade=trade)
    account.holds = 0.0  # a real port's Flatten would be refused here

    result = await _flatten(facade, current_context)

    assert result.applied is True
    assert [(leg.side, leg.quantity) for leg in trade.submitted_legs] == [("sell", 10)]
    assert account.reads == []


async def test_a_flatten_with_no_read_port_is_refused_never_sent_unchecked(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """Fail closed: a Flatten driven to a real broker by a caller that named no read port sends nothing."""
    repo, _clock = crashed_with_exposure
    entry_ref = await safe_flatten._held_position(repo)
    submit_stop_run(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID,
        lifecycle_run_id=RUN_ID, operator_reason="operator_stop",
    )
    accepted = accept_recovery_exit(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID,
        decision_id=f"{RECOVERY_FLATTEN_DECISION_PREFIX}noreadport000001", entry_order_ref=entry_ref,
    )
    assert accepted.effect_operation_id is not None
    trade = safe_flatten._FakeTrade()

    await resolve_accepted_exit(
        repo, accepted=accepted, trade=trade, pricing=UNPRICEABLE_RECOVERY, read=None
    )

    assert trade.submit_calls == []
    effect = repo.effect_operation(accepted.effect_operation_id)
    assert effect is not None and effect.state == "failed"
    refusal = flatten_send_refusal(repo, accepted.effect_operation_id)
    assert refusal is not None
    assert refusal.reason_code == FLATTEN_COVER_UNPROVEN
    assert "This Clerk has no connection to read the Alpaca account as it sends" in refusal.explanation
    assert _exit_not_flat(repo) is not None


def _refusal_code(
    *, side: OrderSide, quantity: float, holds: float, open_orders: list[BrokerOrder] | None = None
) -> str | None:
    positions = (
        [safe_flatten._position("SPY", quantity=holds, side="long" if holds > 0 else "short")]
        if holds
        else []
    )
    refusal = flatten_cover_refusal(
        symbol="SPY", side=side, quantity=quantity, observed=(open_orders or [], positions)
    )
    return None if refusal is None else refusal.reason_code


def _open_order(side: str, *, quantity: float, filled: float = 0.0, symbol: str = "SPY") -> BrokerOrder:
    return safe_flatten._broker_order(
        f"elsewhere:{side}-{quantity}", order_id=f"{side}-{quantity}", symbol=symbol,
        status="partially_filled" if filled else "new", side=side, quantity=quantity, filled_quantity=filled,
    )


@pytest.mark.parametrize(
    ("holds", "open_orders", "covered"),
    [
        pytest.param(-10.0, [], True, id="a-short-of-ten-covers-a-buy-of-ten"),
        pytest.param(-6.0, [], False, id="a-short-of-six-does-not"),
        pytest.param(0.0, [], False, id="a-flat-account-does-not"),
        pytest.param(10.0, [], False, id="a-long-position-does-not"),
        pytest.param(-10.0, [_open_order("buy", quantity=4.0)], False, id="an-open-buy-takes-the-cover"),
        pytest.param(-20.0, [_open_order("buy", quantity=10.0)], True, id="an-open-buy-that-leaves-cover"),
        pytest.param(-10.0, [_open_order("sell", quantity=10.0)], True, id="an-open-sell-takes-no-cover-from-a-buy"),
    ],
)
def test_a_buy_that_covers_a_short_is_held_to_the_mirrored_rule(
    holds: float, open_orders: list[BrokerOrder], covered: bool
) -> None:
    code = _refusal_code(side=OrderSide.BUY, quantity=10.0, holds=holds, open_orders=open_orders)

    assert code == (None if covered else FLATTEN_NOT_COVERED_AT_BROKER)


@pytest.mark.parametrize(
    ("holds", "open_orders", "covered"),
    [
        pytest.param(10.0, [_open_order("buy", quantity=10.0)], True, id="an-open-buy-takes-no-cover-from-a-sell"),
        pytest.param(
            10.0, [_open_order("sell", quantity=10.0, symbol="QQQ")], True,
            id="a-sell-in-another-symbol-takes-none",
        ),
        pytest.param(
            16.0, [_open_order("sell", quantity=10.0, filled=4.0)], True,
            id="only-the-unfilled-part-of-an-open-sell-counts",
        ),
        pytest.param(
            15.0, [_open_order("sell", quantity=10.0, filled=4.0)], False,
            id="the-unfilled-part-of-an-open-sell-counts",
        ),
        pytest.param(
            10.0, [_open_order("", quantity=1.0)], False,
            id="an-order-whose-side-alpaca-left-unreadable-counts-as-a-sell",
        ),
        pytest.param(
            10.0, [_open_order("sell", quantity=10.0).model_copy(update={"status": "canceled"})], True,
            id="an-ended-sell-takes-none",
        ),
        pytest.param(10.0 - POSITION_QTY_EPSILON / 2, [], True, id="short-by-less-than-the-custody-epsilon"),
        pytest.param(10.0 - POSITION_QTY_EPSILON * 2, [], False, id="short-by-more-than-the-custody-epsilon"),
    ],
)
def test_a_sells_cover_counts_only_what_can_still_take_its_shares(
    holds: float, open_orders: list[BrokerOrder], covered: bool
) -> None:
    code = _refusal_code(side=OrderSide.SELL, quantity=10.0, holds=holds, open_orders=open_orders)

    assert code == (None if covered else FLATTEN_NOT_COVERED_AT_BROKER)
