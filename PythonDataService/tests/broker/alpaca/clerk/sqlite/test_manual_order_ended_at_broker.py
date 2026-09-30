"""A manual order that Alpaca ends without filling it in full (#2647).

The owner can cancel a Clerk manual order in Alpaca's own website, a DAY limit
expires at the close, and Alpaca can reject an order it accepted. Before
#2647 the order projection recorded the ending but the ``MANUAL_ORDER``
effect stayed ``in_progress`` for ever: every bot's entries were refused as
``MANUAL_ORDER_OUTSTANDING`` and a bot's refused exit was held as Clerk work
in flight on the symbol. These tests pin the one terminal fold that ends it,
on the ``trade_updates`` route and on the reconciliation sweep.
"""

from __future__ import annotations

import importlib
import logging

import pytest

from app.broker.alpaca.clerk.recovery_reduction import UNPRICEABLE_RECOVERY
from app.broker.alpaca.clerk.sqlite import manual_order_completion, order_projection
from app.broker.alpaca.clerk.sqlite.exit import accept_exit, resolve_exit
from app.broker.alpaca.clerk.sqlite.exit_recovery import DEFAULT_RECOVERY_INTERVAL_MS
from app.broker.alpaca.clerk.sqlite.facts import ManualOrderCancelResultFacts, OrderSubmitAckedFacts
from app.broker.alpaca.clerk.sqlite.intake_fence import ReentrantAsyncLock
from app.broker.alpaca.clerk.sqlite.manual_order_cancellation import (
    ManualOrderCancelTerminalError,
    submit_manual_order_cancellation,
)
from app.broker.alpaca.clerk.sqlite.manual_orders import ManualOrderSubmission, submit_manual_order
from app.broker.alpaca.clerk.sqlite.models import TransitionInput
from app.broker.alpaca.clerk.sqlite.reconcile import reconcile_account
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty import Capability, decide_capability
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import EXIT_NOT_FLAT_REASON_CODE
from app.broker.alpaca.clerk.trade_evidence import SqliteTradeUpdateEvidenceSink
from app.broker.contract.models import (
    BrokerOrder,
    BrokerOrderEvent,
    BrokerOrderLeg,
    OrderSide,
    OrderType,
)
from app.schemas.manual_orders import ManualOrderLegResponse
from tests.broker.alpaca.clerk.sqlite.conftest import _FakeTradePort
from tests.broker.alpaca.clerk.sqlite.test_manual_orders import LEG_ID, OPERATOR_ID, TICKET_ID
from tests.broker.alpaca.clerk.sqlite.test_manual_orders import FakeTrade as _AlpacaWebsite
from tests.broker.alpaca.clerk.sqlite.test_reconcile import (
    ACCOUNT_ID,
    WATCHDOG_RUN,
    WATCHDOG_SID,
    _exit_not_flat_redrive_policy,
    _FakeRead,
    _held_position,
    _NoReconciler,
    _position,
    _register_second_spy_lane,
    clocked_repo,  # noqa: F401 -- pytest fixture, used by name
)
from tests.broker.alpaca.clerk.sqlite.test_two_bots_one_symbol import _wash_trade_rejection

_TERMINAL_EFFECT_STATES = {"succeeded", "failed", "rejected"}
_MANUAL_ENDINGS = {"MANUAL_ORDER_CANCELED", "MANUAL_ORDER_TERMINAL", "MANUAL_ORDER_FILLED"}

# ending -> (time in force, terminal transition, leg state, ticket state, what the owner reads)
_ENDINGS = {
    "canceled": ("gtc", "MANUAL_ORDER_CANCELED", "CANCELED", "CANCELED", "Cancelled at Alpaca."),
    "expired": ("day", "MANUAL_ORDER_TERMINAL", "FAILED", "COMPLETED", "Expired at the close."),
    "rejected": ("day", "MANUAL_ORDER_TERMINAL", "FAILED", "COMPLETED", "Rejected by Alpaca."),
}


async def _buy_limit(
    repo: ClerkSqliteRepository, website: _AlpacaWebsite, *, time_in_force: str = "gtc", quantity: float = 5
) -> ManualOrderSubmission:
    """The owner's Clerk manual buy limit on SPY, working at Alpaca."""
    submitted = await submit_manual_order(
        repo, account_id=ACCOUNT_ID, operator_id=OPERATOR_ID, ticket_id=TICKET_ID, leg_id=LEG_ID,
        leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=quantity, order_type="limit",
                           limit_price=99.90, time_in_force=time_in_force),
        trade=website,
    )
    assert submitted.leg.order_ref is not None and submitted.leg.effect_operation_id is not None
    effect = repo.effect_operation(submitted.leg.effect_operation_id)
    assert effect is not None and effect.state == "in_progress"
    return submitted


def _ended_at_alpaca(
    repo: ClerkSqliteRepository,
    website: _AlpacaWebsite,
    order_ref: str,
    status: str,
    *,
    filled_quantity: float = 0,
) -> BrokerOrder:
    """Alpaca ends the order now; its exact lookup reports the ending from here on."""
    now = repo.clock()
    ended = website.orders[order_ref].model_copy(update={
        "status": status,
        "filled_quantity": filled_quantity,
        "filled_avg_price": 99.90 if filled_quantity else None,
        "canceled_at_ms": now if status == "canceled" else None,
        "expired_at_ms": now if status == "expired" else None,
        "updated_at_ms": now,
        "observed_at_ms": now,
    })
    website.orders[order_ref] = ended
    return ended


async def _frame(
    repo: ClerkSqliteRepository,
    order: BrokerOrder,
    *,
    event_type: str,
    execution_id: str | None = None,
    quantity: float | None = None,
) -> None:
    """One ``trade_updates`` frame for ``order``, folded by the Clerk's sink."""
    sink = SqliteTradeUpdateEvidenceSink(repo=repo, intake=ReentrantAsyncLock(), reconciler=_NoReconciler())
    assert order.client_order_id is not None
    await sink.record_lifecycle_event(
        client_order_id=order.client_order_id,
        event=BrokerOrderEvent(
            event_type=event_type, occurred_at_ms=order.updated_at_ms or repo.clock(),
            price=99.90 if execution_id else None, quantity=quantity, execution_id=execution_id,
        ),
        event_key=f"{event_type}:{execution_id or order.client_order_id}",
        order=order,
        recovery_source=None,
        recovery_window_limit=None,
    )


async def _reconciliation_pass(repo: ClerkSqliteRepository, trade: object, *, spy_held: float = 0.0) -> None:
    """One sweep: Alpaca's open orders no longer list an ended order."""
    positions = [_position("SPY", quantity=spy_held)] if spy_held else []
    await reconcile_account(repo, read=_FakeRead(orders=[], positions=positions), trade=trade,
                            pricing=UNPRICEABLE_RECOVERY)


def _manual_endings(repo: ClerkSqliteRepository, order_ref: str) -> list[dict]:
    return [t for t in repo.transitions_for_order(order_ref) if t["transition_kind"] in _MANUAL_ENDINGS]


def _owner_reads(repo: ClerkSqliteRepository) -> ManualOrderLegResponse:
    ticket = repo.manual_order_ticket(TICKET_ID)
    assert ticket is not None
    return ManualOrderLegResponse.from_resource(ticket.legs[0], repo=repo)


def _another_bots_entry(repo: ClerkSqliteRepository, sid: str) -> tuple[bool, str | None]:
    decision = decide_capability(repo, capability=Capability.NEW_EXPOSURE, strategy_instance_id=sid)
    return decision.allowed, decision.reason_code


# ── The ending, on both routes ────────────────────────────────────────────────


@pytest.mark.parametrize("route", ["trade_updates", "reconcile_sweep"])
@pytest.mark.parametrize("ending", sorted(_ENDINGS))
async def test_a_manual_limit_alpaca_ends_unfilled_ends_its_effect_and_bots_may_enter_again(
    clocked_repo, ending: str, route: str,  # noqa: F811
) -> None:
    """Cancelled in Alpaca's website, expired at the close, or rejected after acceptance.

    Either route ends the ``MANUAL_ORDER`` effect exactly once, under its own
    effect, and the next pass lets another bot's entry through.
    """
    repo, _clock = clocked_repo
    time_in_force, terminal_kind, leg_state, ticket_state, owner_copy = _ENDINGS[ending]
    website = _AlpacaWebsite(repo=repo)
    manual = await _buy_limit(repo, website, time_in_force=time_in_force)
    order_ref, effect_id = manual.leg.order_ref, manual.leg.effect_operation_id
    assert order_ref is not None and effect_id is not None
    sid_b, _run_b = _register_second_spy_lane(repo)
    assert _another_bots_entry(repo, sid_b) == (False, "MANUAL_ORDER_OUTSTANDING")

    ended = _ended_at_alpaca(repo, website, order_ref, ending)
    if route == "trade_updates":
        await _frame(repo, ended, event_type=ending)
        effect = repo.effect_operation(effect_id)
        assert effect is not None and effect.state == "failed", "the frame itself must end the order"
        await _frame(repo, ended, event_type=ending)  # a redelivered frame changes nothing
    await _reconciliation_pass(repo, website)
    await _reconciliation_pass(repo, website)

    effect = repo.effect_operation(effect_id)
    assert effect is not None and effect.state == "failed" and effect.terminal_receipt_id is not None
    assert not repo.has_nonterminal_manual_order()
    assert _another_bots_entry(repo, sid_b) == (True, None)
    [terminal] = _manual_endings(repo, order_ref)
    assert terminal["transition_kind"] == terminal_kind
    assert (terminal["effect_operation_id"], terminal["command_id"]) == (effect_id, manual.command.command_id)
    ticket = repo.manual_order_ticket(TICKET_ID)
    assert ticket is not None and (ticket.state, ticket.legs[0].state) == (ticket_state, leg_state)
    assert _owner_reads(repo).ending == owner_copy


async def test_a_gtc_limit_alpaca_expires_reads_as_expired_at_alpaca(clocked_repo) -> None:  # noqa: F811
    repo, _clock = clocked_repo
    website = _AlpacaWebsite(repo=repo)
    manual = await _buy_limit(repo, website, time_in_force="gtc")
    assert manual.leg.order_ref is not None

    await _frame(repo, _ended_at_alpaca(repo, website, manual.leg.order_ref, "expired"), event_type="expired")

    assert _owner_reads(repo).ending == "Expired at Alpaca."


async def test_a_working_or_filled_manual_order_has_no_broker_ending_to_show(clocked_repo) -> None:  # noqa: F811
    repo, _clock = clocked_repo
    website = _AlpacaWebsite(repo=repo)
    manual = await _buy_limit(repo, website, quantity=5)
    assert manual.leg.order_ref is not None
    assert _owner_reads(repo).ending is None

    filled = _ended_at_alpaca(repo, website, manual.leg.order_ref, "filled", filled_quantity=5)
    await _frame(repo, filled, event_type="fill", execution_id="manual-exec-all", quantity=5)

    assert (_owner_reads(repo).state, _owner_reads(repo).ending) == ("SUCCEEDED", None)
    assert [t["transition_kind"] for t in _manual_endings(repo, manual.leg.order_ref)] == ["MANUAL_ORDER_FILLED"]


async def test_a_share_count_of_a_million_or_more_reads_in_plain_digits(clocked_repo) -> None:  # noqa: F811
    repo, _clock = clocked_repo
    website = _AlpacaWebsite(repo=repo)
    manual = await _buy_limit(repo, website, quantity=2_000_000)
    assert manual.leg.order_ref is not None
    _ended_at_alpaca(repo, website, manual.leg.order_ref, "canceled", filled_quantity=1_250_000.5)

    await _reconciliation_pass(repo, website, spy_held=1_250_000.5)

    assert _owner_reads(repo).ending == "Cancelled at Alpaca with 1250000.5 of 2000000 shares filled."


# ── The owner copy covers exactly the states the fold ends ────────────────────


def test_the_owner_copy_covers_exactly_the_unfilled_terminal_states() -> None:
    assert frozenset(manual_order_completion._ENDING_COPY) == order_projection.UNFILLED_TERMINAL_STATES


def test_an_unfilled_state_with_no_owner_copy_stops_the_import_not_the_sweep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Widening ``UNFILLED_TERMINAL_STATES`` without copy fails when the module loads.

    Otherwise the first acknowledgement of a manual order in the new state
    would raise inside ``fold_order_acknowledgement`` and stop the
    trade_updates sink and the account's reconciliation pass.
    """
    widened = order_projection.UNFILLED_TERMINAL_STATES | {"done_for_day"}
    monkeypatch.setattr(order_projection, "UNFILLED_TERMINAL_STATES", widened)
    try:
        with pytest.raises(RuntimeError, match=r"missing \['done_for_day'\]"):
            importlib.reload(manual_order_completion)
    finally:
        monkeypatch.undo()
        importlib.reload(manual_order_completion)


# ── A partial fill keeps its shares; only the remainder ends ─────────────────


@pytest.mark.parametrize("route", ["trade_updates", "reconcile_sweep"])
async def test_a_partly_filled_manual_limit_cancelled_at_alpaca_keeps_its_fill_and_ends(
    clocked_repo, route: str,  # noqa: F811
) -> None:
    repo, _clock = clocked_repo
    website = _AlpacaWebsite(repo=repo)
    manual = await _buy_limit(repo, website, quantity=5)
    order_ref, effect_id = manual.leg.order_ref, manual.leg.effect_operation_id
    assert order_ref is not None and effect_id is not None

    if route == "trade_updates":
        partly = _ended_at_alpaca(repo, website, order_ref, "partially_filled", filled_quantity=3)
        await _frame(repo, partly, event_type="partial_fill", execution_id="manual-exec-1", quantity=3)
        await _frame(repo, _ended_at_alpaca(repo, website, order_ref, "canceled", filled_quantity=3),
                     event_type="canceled")
    else:
        _ended_at_alpaca(repo, website, order_ref, "canceled", filled_quantity=3)
    await _reconciliation_pass(repo, website, spy_held=3)

    effect = repo.effect_operation(effect_id)
    assert effect is not None and effect.state == "failed"
    assert repo.attributed_positions_by_symbol() == pytest.approx({"SPY": 3.0}, abs=1e-9, rel=0)
    assert len(_manual_endings(repo, order_ref)) == 1
    assert _owner_reads(repo).ending == "Cancelled at Alpaca with 3 of 5 shares filled."
    assert _another_bots_entry(repo, _register_second_spy_lane(repo)[0]) == (True, None)


async def test_a_cancel_frame_that_outruns_its_fill_never_ends_the_order_short_of_its_shares(
    clocked_repo,  # noqa: F811
) -> None:
    """Alpaca reports 3 filled on the cancel frame before the fill's own frame lands.

    Ending the effect then would take the order off the sweep's worklist with
    3 shares the Clerk never recorded. It waits; the sweep's exact lookup
    records the shares and the order ends on the same pass.
    """
    repo, _clock = clocked_repo
    website = _AlpacaWebsite(repo=repo)
    manual = await _buy_limit(repo, website, quantity=5)
    order_ref, effect_id = manual.leg.order_ref, manual.leg.effect_operation_id
    assert order_ref is not None and effect_id is not None

    await _frame(repo, _ended_at_alpaca(repo, website, order_ref, "canceled", filled_quantity=3),
                 event_type="canceled")

    effect = repo.effect_operation(effect_id)
    assert effect is not None and effect.state not in _TERMINAL_EFFECT_STATES
    assert effect_id in {item.effect_operation_id for item in repo.reconcilable_effect_operations()}
    assert _owner_reads(repo).ending is None  # the ticket names no ending the Clerk has not reached

    await _reconciliation_pass(repo, website, spy_held=3)

    effect = repo.effect_operation(effect_id)
    assert effect is not None and effect.state == "failed"
    assert repo.attributed_positions_by_symbol() == pytest.approx({"SPY": 3.0}, abs=1e-9, rel=0)


# ── A cancel sent through the Clerk, and never twice ─────────────────────────


async def test_a_cancel_sent_through_the_clerk_ends_the_order_once_whatever_alpaca_reports_after(
    clocked_repo,  # noqa: F811
) -> None:
    repo, _clock = clocked_repo
    website = _AlpacaWebsite(repo=repo)
    manual = await _buy_limit(repo, website)
    order_ref, effect_id = manual.leg.order_ref, manual.leg.effect_operation_id
    assert order_ref is not None and effect_id is not None

    cancelled = await submit_manual_order_cancellation(
        repo, account_id=ACCOUNT_ID, operator_id=OPERATOR_ID, order_ref=order_ref,
        cancel_request_id="2d4b7e8e-5a57-4bb4-9f5a-3c2b1e0d9f11", trade=website,
    )
    receipt = repo.effect_operation(effect_id).terminal_receipt_id
    await _frame(repo, website.orders[order_ref], event_type="canceled")
    await _reconciliation_pass(repo, website)

    assert cancelled.cancellation.state == "SUCCEEDED"
    assert len(website.cancel_calls) == 1
    effect = repo.effect_operation(effect_id)
    assert effect is not None and effect.state == "failed" and effect.terminal_receipt_id == receipt
    [terminal] = _manual_endings(repo, order_ref)
    assert (terminal["transition_kind"], terminal["effect_operation_id"]) == ("MANUAL_ORDER_CANCELED", effect_id)
    ticket = repo.manual_order_ticket(TICKET_ID)
    assert ticket is not None and (ticket.state, ticket.legs[0].state) == ("CANCELED", "CANCELED")
    # The owner cancelled from the Clerk, not in Alpaca's website.
    assert ManualOrderCancelResultFacts.from_facts_json(terminal["facts_json"]).why == "Cancelled."
    assert _owner_reads(repo).ending == "Cancelled."


async def test_a_clerk_cancel_after_alpaca_cancelled_the_order_is_refused_without_a_delete(
    clocked_repo,  # noqa: F811
) -> None:
    repo, _clock = clocked_repo
    website = _AlpacaWebsite(repo=repo)
    manual = await _buy_limit(repo, website)
    order_ref, effect_id = manual.leg.order_ref, manual.leg.effect_operation_id
    assert order_ref is not None and effect_id is not None
    await _frame(repo, _ended_at_alpaca(repo, website, order_ref, "canceled"), event_type="canceled")
    assert repo.effect_operation(effect_id).state == "failed"

    with pytest.raises(ManualOrderCancelTerminalError, match="already terminal"):
        await submit_manual_order_cancellation(
            repo, account_id=ACCOUNT_ID, operator_id=OPERATOR_ID, order_ref=order_ref,
            cancel_request_id="0f3e0f1c-6b7a-4c38-9e0e-8a1f1b0c4d22", trade=website,
        )

    assert website.cancel_calls == []
    assert repo.manual_order_cancellation(order_ref=order_ref) is None
    assert len(_manual_endings(repo, order_ref)) == 1
    assert _owner_reads(repo).ending == "Cancelled at Alpaca."


async def test_alpacas_cancel_landing_while_a_clerk_cancel_is_unknown_ends_the_order_once(
    clocked_repo,  # noqa: F811
) -> None:
    """The Clerk's DELETE reaches Alpaca but its response is lost; the cancel frame arrives.

    The frame ends the manual order at once. The sweep then settles the
    Clerk's own cancellation as succeeded without a second DELETE, and the
    order is ended once.
    """
    repo, _clock = clocked_repo
    website = _AlpacaWebsite(repo=repo, cancel_unavailable=True)
    manual = await _buy_limit(repo, website)
    order_ref, effect_id = manual.leg.order_ref, manual.leg.effect_operation_id
    assert order_ref is not None and effect_id is not None
    unknown = await submit_manual_order_cancellation(
        repo, account_id=ACCOUNT_ID, operator_id=OPERATOR_ID, order_ref=order_ref,
        cancel_request_id="7c1d2e3f-4a5b-4c6d-8e9f-0a1b2c3d4e55", trade=website,
    )
    assert unknown.cancellation.state == "UNKNOWN"

    await _frame(repo, _ended_at_alpaca(repo, website, order_ref, "canceled"), event_type="canceled")
    assert repo.effect_operation(effect_id).state == "failed"
    await _reconciliation_pass(repo, website)

    assert repo.manual_order_cancellation(order_ref=order_ref).state == "SUCCEEDED"
    assert len(website.cancel_calls) == 1
    assert len(_manual_endings(repo, order_ref)) == 1
    ticket = repo.manual_order_ticket(TICKET_ID)
    assert ticket is not None and (ticket.state, ticket.legs[0].state) == ("CANCELED", "CANCELED")
    assert _owner_reads(repo).ending == "Cancelled."


# ── The ending lands while a bot's EXIT is being processed ───────────────────


class _ExitPortThatSeesTheManualCancel(_FakeTradePort):
    """Alpaca's cancel frame for the manual order arrives while the bot's EXIT is being sent."""

    def __init__(self, *, repo: ClerkSqliteRepository, manual_cancel: BrokerOrder) -> None:
        super().__init__()
        self._repo = repo
        self._manual_cancel = manual_cancel

    async def submit(self, leg: BrokerOrderLeg, *, client_order_id: str) -> BrokerOrder:
        await _frame(self._repo, self._manual_cancel, event_type="canceled")
        return await super().submit(leg, client_order_id=client_order_id)


async def test_an_ending_that_lands_while_a_bot_exit_is_being_sent_ends_only_the_manual_order(
    clocked_repo,  # noqa: F811
) -> None:
    """A Clerk fold ends the effect it is nested under; this one must end the manual order's own.

    The cancel frame is folded in the middle of bot A's EXIT: after the EXIT
    has claimed its reducing order and before the broker answers its sell.
    The manual order ends under its own effect and command, and the EXIT
    carries on to its own outcome with no manual-order transition on it.
    """
    repo, _clock = clocked_repo
    ref_a = await _held_position(repo)
    website = _AlpacaWebsite(repo=repo)
    manual = await _buy_limit(repo, website)
    order_ref, effect_id = manual.leg.order_ref, manual.leg.effect_operation_id
    assert order_ref is not None and effect_id is not None
    exit_a = accept_exit(repo, account_id=ACCOUNT_ID, strategy_instance_id=WATCHDOG_SID, decision_id="a-sell",
                         lifecycle_run_id=WATCHDOG_RUN, entry_order_ref=ref_a)
    port = _ExitPortThatSeesTheManualCancel(
        repo=repo, manual_cancel=_ended_at_alpaca(repo, website, order_ref, "canceled"))

    await resolve_exit(repo, effect_operation_id=exit_a.effect_operation_id, trade=port, pricing=UNPRICEABLE_RECOVERY)

    assert [(leg.side, leg.quantity) for leg in port.submitted_legs] == [(OrderSide.SELL, 10)]
    manual_effect = repo.effect_operation(effect_id)
    assert manual_effect is not None and manual_effect.state == "failed"
    [terminal] = _manual_endings(repo, order_ref)
    assert (terminal["effect_operation_id"], terminal["command_id"]) == (effect_id, manual.command.command_id)
    exit_effect = repo.effect_operation(exit_a.effect_operation_id)
    assert exit_effect is not None and exit_effect.state not in _TERMINAL_EFFECT_STATES
    assert exit_effect.terminal_receipt_id is None
    assert not [
        transition for transition in repo.custody_transitions()
        if transition["effect_operation_id"] == exit_a.effect_operation_id
        and transition["transition_kind"] in _MANUAL_ENDINGS
    ]


# ── A bot's refused exit is no longer held behind it ─────────────────────────


async def test_a_bot_exit_refused_beside_a_manual_buy_limit_is_sent_again_once_alpaca_cancels_the_limit(
    clocked_repo, caplog: pytest.LogCaptureFixture,  # noqa: F811
) -> None:
    """Alpaca refuses bot A's sell as a potential wash trade against the owner's buy limit.

    The owner then cancels the limit in Alpaca's website. Nothing works SPY
    any more and the broker holds A's 10 shares, so the stuck-EXIT watchdog
    re-sends A's sell once the refusal has settled for the policy's re-drive
    age -- never deferring it as Clerk work in flight on the symbol.
    """
    repo, clock = clocked_repo
    ref_a = await _held_position(repo)
    website = _AlpacaWebsite(repo=repo)
    manual = await _buy_limit(repo, website)
    assert manual.leg.order_ref is not None
    exit_a = accept_exit(repo, account_id=ACCOUNT_ID, strategy_instance_id=WATCHDOG_SID, decision_id="a-sell",
                         lifecycle_run_id=WATCHDOG_RUN, entry_order_ref=ref_a)
    await resolve_exit(repo, effect_operation_id=exit_a.effect_operation_id,
                       trade=_FakeTradePort(submit_error=_wash_trade_rejection()), pricing=UNPRICEABLE_RECOVERY)
    assert repo.active_uncertainty(scope="CUSTODY_SUBJECT", reason_code=EXIT_NOT_FLAT_REASON_CODE,
                                   strategy_instance_id=WATCHDOG_SID) is not None
    refused_at = repo.clock()

    await _frame(repo, _ended_at_alpaca(repo, website, manual.leg.order_ref, "canceled"), event_type="canceled")
    policy = _exit_not_flat_redrive_policy()
    redrive = _FakeTradePort()
    with caplog.at_level(logging.INFO):
        while not redrive.submitted_legs:
            assert repo.clock() - refused_at <= policy.after_ms, (
                "A's exit was held behind a manual order Alpaca had already ended"
            )
            await _reconciliation_pass(repo, redrive, spy_held=10)
            if not redrive.submitted_legs:
                clock.advance(DEFAULT_RECOVERY_INTERVAL_MS)

    assert [(leg.side, leg.quantity, leg.order_type) for leg in redrive.submitted_legs] == [
        (OrderSide.SELL, 10, OrderType.MARKET)]
    assert not [
        record for record in caplog.records
        if getattr(record, "action", None) == "exit_redrive_deferred_work_in_flight"
    ]


# ── An ending recorded before #2647 is healed by the next pass ───────────────


async def test_a_manual_order_left_working_over_an_order_alpaca_already_ended_is_ended_by_the_next_pass(
    clocked_repo,  # noqa: F811
) -> None:
    """The residue a Clerk running before #2647 left: the order ``canceled``, its effect ``in_progress``.

    A Clerk cancel refuses that order as already terminal and Alpaca's open
    orders no longer list it, but the sweep still looks every working manual
    order up by its exact identity on each pass; that lookup now ends it.
    """
    repo, _clock = clocked_repo
    website = _AlpacaWebsite(repo=repo)
    manual = await _buy_limit(repo, website)
    order_ref, effect_id = manual.leg.order_ref, manual.leg.effect_operation_id
    assert order_ref is not None and effect_id is not None
    cancelled = _ended_at_alpaca(repo, website, order_ref, "canceled")
    repo.append_transition(TransitionInput(  # what the Clerk's acknowledgement fold recorded before #2647
        command_id=manual.command.command_id, effect_operation_id=effect_id, order_ref=order_ref,
        broker_order_id=cancelled.order_id, broker_state="canceled",
        transition_kind="ORDER_SUBMIT_ACKED", custody_owner="ACCOUNT_CLERK", execution_authority="ACCOUNT_CLERK",
        operation_state="in_progress", source_event_at_ms=cancelled.updated_at_ms,
        clerk_observed_at_ms=repo.clock(), summary_code="ORDER_SUBMIT_ACKED",
        facts_json=OrderSubmitAckedFacts(reported_filled_quantity=None).to_facts_json(),
    ))
    assert repo.effect_operation(effect_id).state == "in_progress"
    assert repo.has_nonterminal_manual_order()
    sid_b, _run_b = _register_second_spy_lane(repo)

    await _reconciliation_pass(repo, website)

    assert repo.effect_operation(effect_id).state == "failed"
    assert _another_bots_entry(repo, sid_b) == (True, None)
    assert _owner_reads(repo).ending == "Cancelled at Alpaca."
