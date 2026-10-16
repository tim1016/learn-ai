"""Switching Live option bots off never leaves a held contract with no way out (#2913, ADR 0076 decision 24).

A fence against opening never stops a close. A Live account can hold a
contract bought while ``ALPACA_LIVE_OPTION_BOTS_ENABLED`` was on. With the
setting off again, nothing that opens is taken, and every order that only
closes still goes out: the bot's own EXIT, the Clerk's scheduled close, and
the owner's Flatten.

The books are a real account's, behind a Clerk that learned its account is
Live. The venue is the stand-in trade port, behind the very rule the Alpaca
port holds a live binding to (``live_option_bots.live_refuses``), so an
order passes both fences here or neither. Nothing is sent to a broker.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import pytest

from app.broker.alpaca.clerk.models import EffectOperationState
from app.broker.alpaca.clerk.option_effects import OptionPlanRefused
from app.broker.alpaca.clerk.sqlite.commands import submit_stop_run
from app.broker.alpaca.clerk.sqlite.deadline_keeper import DeadlineKeeper, Nudge
from app.broker.alpaca.clerk.sqlite.off_loop import run_inline
from app.broker.alpaca.clerk.sqlite.projections import read_recovery_context
from app.broker.alpaca.clerk.sqlite.recovery_execution import RecoveryExecutionRequest, execute_recovery_action
from app.broker.alpaca.clerk.sqlite.recovery_policy import RecoveryPolicyContext
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.alpaca.live_option_bots import SETTING, live_refuses
from app.broker.contract.errors import BrokerOptionsNotSupported
from app.broker.contract.options import OptionOrderRequest, only_closes
from app.config import settings
from app.schemas.option_order_terms import SealedOptionOrderTerms
from app.services.bot_binding_repository import BrokerBotBinding
from tests._helpers.exit_terms import DEPLOY_EXIT_TERMS
from tests.broker.alpaca.clerk.sqlite.resting_entry_harness import (
    BUYS_ONE_CALL,
    SYMBOL,
    PaperOptions,
    paper,  # noqa: F401 -- pytest fixture, used by name
)
from tests.broker.alpaca.clerk.sqlite.test_resting_entries import _the_flatten_offered
from tests.broker.alpaca.clerk.sqlite.test_scheduled_close import (
    DUE,
    SELLS_ONE_TO_CLOSE,
    _closes_sent,
    _holds_one_call,
    _the_close_fills,
    _until,
)

pytestmark = pytest.mark.usefixtures("legacy_run_registration")

_ACCOUNT_NOT_OPEN = "OPTION_PLAN_ACCOUNT_NOT_OPEN"


@dataclass
class _LiveVenue:
    """What the Live port's own rule turned away before the stand-in venue, and a moment to act in on the way."""

    refused: list[OptionOrderRequest] = field(default_factory=list)
    on_the_way: Callable[[], None] = lambda: None


@pytest.fixture
def live(paper: PaperOptions) -> tuple[PaperOptions, _LiveVenue]:  # noqa: F811
    """The same books and venue behind a Clerk that learned at activation that the account is Live."""
    port = _LiveVenue()
    send = paper.venue.submit_option_order

    async def as_a_live_binding_sends(order: OptionOrderRequest, *, client_order_id: str):
        port.on_the_way()
        if live_refuses(order):
            port.refused.append(order)
            raise BrokerOptionsNotSupported(
                "This app sends no option order that opens a position to a Live Alpaca account yet.", broker="alpaca"
            )
        return await send(order, client_order_id=client_order_id)

    paper.venue.submit_option_order = as_a_live_binding_sends
    nudge = Nudge()
    paper.clerk = SqliteAlpacaClerkFacade(
        repo=paper.repo, read=paper.account, trade=paper.venue, account_mode="live",
        live_envelope=paper.clerk.live_envelope, entry_reading=paper.sync.read_for_entry, entry_deadline_nudge=nudge,
    )
    paper.keeper = DeadlineKeeper(
        repo=paper.repo, trade=paper.venue, intake=paper.clerk.intake, pricing=paper.clerk.recovery_pricing,
        nudge=nudge, off_loop=run_inline,
    )
    return paper, port


def _switch(monkeypatch: pytest.MonkeyPatch, on: bool) -> None:
    monkeypatch.setattr(settings, SETTING, on)


def _a_new_run_of(account: PaperOptions, sid: str = "a") -> BrokerBotBinding:
    return BrokerBotBinding(
        strategy_instance_id=sid, strategy_key="ema_crossover_signal", broker="alpaca", symbol="SPY", mode="trade",
        quantity=1, action_plan=BUYS_ONE_CALL, run_id=f"run-{sid}-2", created_at_ms=0,
        sealed_account_id=account.repo.account_id, exit_terms=DEPLOY_EXIT_TERMS,
        option_order_terms=SealedOptionOrderTerms(
            gap_limit_percent=8, entry_wait_seconds=20, cost_limit_percent=10, expiry_close_lead_minutes=45
        ),
    )


async def test_a_contract_bought_while_live_was_open_is_closed_by_its_bot_after_live_is_switched_off(
    live: tuple[PaperOptions, _LiveVenue], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Setting on: the bot enters and holds. Setting off: its EXIT is sent and the books are flat, and a new
    ENTER and a new registration are each refused by name."""
    account, port = live
    _switch(monkeypatch, True)
    await _holds_one_call(account)
    assert account.held() == {SYMBOL: 1.0}

    _switch(monkeypatch, False)
    sold = await account.exit()

    assert sold.state is not EffectOperationState.REJECTED, sold.explanation
    assert _closes_sent(account) == [SELLS_ONE_TO_CLOSE]
    await _the_close_fills(account)
    assert account.held() == {}, "the books are flat"

    sent_before = len(account.venue.submitted_option_orders)
    again = await account.enter(decision="enter-with-live-off")
    assert again.state is EffectOperationState.REJECTED
    assert (again.explanation or "").startswith(f"{_ACCOUNT_NOT_OPEN}: ") and SETTING in again.explanation
    with pytest.raises(OptionPlanRefused) as refused:
        await account.clerk.register_strategy_run(_a_new_run_of(account))
    assert refused.value.reason.value == _ACCOUNT_NOT_OPEN
    assert len(account.venue.submitted_option_orders) == sent_before and port.refused == []


async def test_with_live_switched_off_no_order_that_opens_reaches_the_venue_by_any_path(
    live: tuple[PaperOptions, _LiveVenue], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bot's ENTER and a registration are stopped by the Clerk. An ENTER the Clerk took a moment before the
    setting went off is stopped by the port's own rule, on the order's legs. Every close goes through."""
    account, port = live
    refused_at_the_clerk = await account.enter(decision="enter-while-off")
    with pytest.raises(OptionPlanRefused):
        account.clerk.require_option_plan_taken(BUYS_ONE_CALL)
    assert refused_at_the_clerk.state is EffectOperationState.REJECTED
    assert account.venue.submitted_option_orders == [] and port.refused == []

    # The Clerk's fence is passed with the setting on, and it is switched off before the order leaves.
    _switch(monkeypatch, True)
    port.on_the_way = lambda: _switch(monkeypatch, False)
    cut_off = await account.enter(decision="enter-as-it-goes-off")

    assert [only_closes(order) for order in port.refused] == [False], "the opening order was stopped at the port"
    assert account.venue.submitted_option_orders == [], "and never reached the venue"
    assert account.held() == {} and cut_off.state is EffectOperationState.REJECTED, cut_off.state

    # Bot b bought while the setting was on; with it off, its close is the only order the venue is sent.
    port.on_the_way = lambda: None
    _switch(monkeypatch, True)
    await _holds_one_call(account, "b")
    _switch(monkeypatch, False)
    await account.exit("b")

    after_off = account.venue.submitted_option_orders[1:]
    assert after_off == [SELLS_ONE_TO_CLOSE] and all(only_closes(order) for order in after_off)


async def test_with_live_switched_off_a_held_contract_with_no_run_is_closed_at_its_scheduled_time(
    live: tuple[PaperOptions, _LiveVenue], monkeypatch: pytest.MonkeyPatch
) -> None:
    account, port = live
    _switch(monkeypatch, True)
    await _holds_one_call(account)
    submit_stop_run(account.repo, account_id=account.repo.account_id, strategy_instance_id="a", lifecycle_run_id="run-a")
    _switch(monkeypatch, False)
    _until(account, DUE)

    await account.round()

    assert account.repo.active_run("a") is None
    assert _closes_sent(account) == [SELLS_ONE_TO_CLOSE] and port.refused == []
    await _the_close_fills(account)
    assert account.held() == {}


async def test_with_live_switched_off_the_owners_flatten_of_a_stopped_bots_contract_still_goes_out(
    live: tuple[PaperOptions, _LiveVenue], monkeypatch: pytest.MonkeyPatch
) -> None:
    account, port = live
    _switch(monkeypatch, True)
    await _holds_one_call(account)
    submit_stop_run(account.repo, account_id=account.repo.account_id, strategy_instance_id="a", lifecycle_run_id="run-a")
    _switch(monkeypatch, False)
    flatten = await _the_flatten_offered(account)
    assert flatten.available, (flatten.unavailable_reason_code, flatten.unavailable_reason)

    async def current_context() -> RecoveryPolicyContext:
        context = await read_recovery_context(account.clerk, strategy_instance_id="a")
        assert context is not None
        return context

    pressed = await execute_recovery_action(
        account.clerk,
        request=RecoveryExecutionRequest(
            action_id="execute_safe_flatten", concurrency_token=flatten.concurrency_token,
            execution_ref=flatten.execution_ref, reason="owner-flatten", confirmed_limit=None,
        ),
        current_context=current_context,
    )

    assert pressed.applied
    assert _closes_sent(account) == [SELLS_ONE_TO_CLOSE] and port.refused == []

