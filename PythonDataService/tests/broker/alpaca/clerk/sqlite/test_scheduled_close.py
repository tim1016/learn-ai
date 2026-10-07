"""The closes the Clerk owes for an option position, on a Paper account (#2913).

A bot holds one SPY call that expires on Friday 2026-09-18, a full day: its
close is due at 15:15 New York time, 45 minutes before the session's close.
Each test lets that day come and asserts what the owner would see: whether a
closing order went to the venue, how many, what the bot still holds, and what
the Clerk says of it.

The venue is the double of ``resting_entry_harness``: it keeps an option
order in the shape Alpaca's documentation shows. Nothing here was sent to
Alpaca or recorded from it.
"""

from __future__ import annotations

import asyncio
from datetime import date

from app.broker.alpaca.clerk.models import EffectOperationState
from app.broker.alpaca.clerk.sqlite.deadline_keeper import IDLE_WAKE_MS
from app.broker.alpaca.clerk.sqlite.facts import ExitAcceptedFacts, ScheduledCloseFacts
from app.broker.alpaca.clerk.sqlite.scheduled_close import (
    CloseStanding,
    ScheduledCloseRound,
    scheduled_close_state,
)
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    EXIT_NOT_FLAT_REASON_CODE,
    SCHEDULED_CLOSE_MISSED_REASON_CODE,
)
from app.broker.contract.models import OrderSide, OrderType
from app.broker.contract.options import OptionPositionIntent, SingleOptionOrderRequest
from app.options.contract import OptionContract
from app.services.session_authority import et_minute_of_day_ms
from tests._helpers.market_quotes import publish_quotes
from tests.broker.alpaca.clerk.sqlite.conftest import (
    documented_option_listing,
)
from tests.broker.alpaca.clerk.sqlite.resting_entry_harness import (
    CALL,
    LEG,
    PaperOptions,
    paper,  # noqa: F401 -- pytest fixture, used by name
)

EXPIRY = CALL.expiry_date
OPEN = et_minute_of_day_ms(EXPIRY, 9 * 60 + 30)
# 45 minutes before the four o'clock close of a full day.
DUE = et_minute_of_day_ms(EXPIRY, 15 * 60 + 15)
CLOSE = et_minute_of_day_ms(EXPIRY, 16 * 60)
MONDAY_AFTER_THE_OPEN = et_minute_of_day_ms(date(2026, 9, 21), 10 * 60)
# The day after Thanksgiving: the session closes at one o'clock.
HALF_DAY = date(2026, 11, 27)
HALF_DAY_CALL = OptionContract.of(root="SPY", expiry_date=HALF_DAY, right="call", strike=585)
PASS_MS = 15_000

SELLS_ONE_TO_CLOSE = SingleOptionOrderRequest(
    contract=CALL, side=OrderSide.SELL, intent=OptionPositionIntent.CLOSE, quantity=1, order_type=OrderType.MARKET
)


async def _holds_one_call(paper: PaperOptions, sid: str = "a") -> str:  # noqa: F811
    """The bot's entry rests and fills: it holds one call. Returns the entry's order reference."""
    order_ref = await paper.resting(sid)
    await paper.stream_fill(order_ref, contracts=1, filled_in_all=1, price="1.94", execution_id=f"x-{sid}-buy")
    return order_ref


def _until(paper: PaperOptions, at_ms: int) -> None:  # noqa: F811
    """Time passes to ``at_ms``. The Clerk's hold on its account is taken up again, and SPY still trades."""
    assert at_ms >= paper.clock()
    paper.clock.value = at_ms
    paper.repo.revive_execution_lease()
    publish_quotes(at_ms, SPY=585.0)


def _closes_sent(paper: PaperOptions) -> list[SingleOptionOrderRequest]:  # noqa: F811
    """Every closing order the venue was sent."""
    return [order for order in paper.venue.submitted_option_orders if order.intent is OptionPositionIntent.CLOSE]


def _closing_refs(paper: PaperOptions, sid: str = "a") -> list[str]:  # noqa: F811
    return [order.order_ref for order in paper.repo.orders_for_strategy(sid) if order.role == "REDUCING"]


def _accepted_as(paper: PaperOptions, effect_operation_id: str) -> ExitAcceptedFacts:  # noqa: F811
    row = paper.repo.first_effect_transition(effect_operation_id=effect_operation_id, transition_kind="EXIT_ACCEPTED")
    assert row is not None
    return ExitAcceptedFacts.from_facts_json(row["facts_json"])


def _notice(paper: PaperOptions, sid: str = "a", reason_code: str = SCHEDULED_CLOSE_MISSED_REASON_CODE) -> dict | None:  # noqa: F811
    return paper.repo.active_uncertainty(scope="CUSTODY_SUBJECT", reason_code=reason_code, strategy_instance_id=sid)


async def _the_close_fills(paper: PaperOptions, *, price: str = "2.10", sid: str = "a") -> None:  # noqa: F811
    """The venue fills the bot's working closing order, Alpaca's stream says so, and the next pass proves it flat."""
    closing_ref = _closing_refs(paper, sid)[-1]
    await paper.stream_fill(closing_ref, contracts=1, filled_in_all=1, price=price, execution_id=f"x-{sid}-sell")
    await paper.pass_()


# ── the close comes due ──────────────────────────────────────────────────


async def test_a_bought_contract_is_closed_45_minutes_before_the_close_on_its_expiry_day_while_its_bot_runs(
    paper: PaperOptions,  # noqa: F811
) -> None:
    await _holds_one_call(paper)
    _until(paper, DUE - 1)

    early = await paper.round()

    # A millisecond before it is due nothing is sent, and the keeper sleeps exactly to the due time.
    assert _closes_sent(paper) == [] and early.closes.taken == () and early.wake_in_ms == 1

    _until(paper, DUE)
    done = await paper.round()

    # One market order that sells to close what is held, sent with no quote, while the run is still the bot's.
    assert paper.repo.active_run("a") is not None
    assert _closes_sent(paper) == [SELLS_ONE_TO_CLOSE]
    [effect_operation_id] = done.closes.taken
    accepted = _accepted_as(paper, effect_operation_id)
    assert accepted.decision_id.startswith("scheduled-close-20260918-")
    assert accepted.the_scheduled_close == ScheduledCloseFacts(kind="expiry_day_close", due_ms=DUE, session_open_ms=OPEN)

    await _the_close_fills(paper)

    assert paper.held() == {} and paper.repo.active_exit_for_strategy("a") is None
    assert _notice(paper) is None and _notice(paper, reason_code=EXIT_NOT_FLAT_REASON_CODE) is None


async def test_on_a_half_day_the_close_is_45_minutes_before_one_o_clock(paper: PaperOptions) -> None:  # noqa: F811
    paper.account.listed[HALF_DAY_CALL.occ_symbol] = documented_option_listing(HALF_DAY_CALL)
    receipt = await paper.enter("a", evidence=paper.evidence(picked={LEG: HALF_DAY_CALL}))
    [order_ref] = receipt.child_order_refs
    await paper.stream_fill(order_ref, contracts=1, filled_in_all=1, price="1.94", execution_id="x-buy")
    half_day_due = et_minute_of_day_ms(HALF_DAY, 12 * 60 + 15)
    _until(paper, half_day_due - 1)

    early = await paper.round()
    _until(paper, half_day_due)
    done = await paper.round()

    assert early.closes.taken == () and len(done.closes.taken) == 1
    [sent] = _closes_sent(paper)
    assert (sent.contract, sent.side, sent.intent, sent.quantity) == (
        HALF_DAY_CALL, OrderSide.SELL, OptionPositionIntent.CLOSE, 1
    )
    [effect_operation_id] = done.closes.taken
    assert _accepted_as(paper, effect_operation_id).the_scheduled_close == ScheduledCloseFacts(
        kind="expiry_day_close", due_ms=half_day_due, session_open_ms=et_minute_of_day_ms(HALF_DAY, 9 * 60 + 30)
    )


async def test_a_contract_that_expires_another_day_owes_no_close_today(paper: PaperOptions) -> None:  # noqa: F811
    await _holds_one_call(paper)
    thursday_at_the_same_hour = et_minute_of_day_ms(date(2026, 9, 17), 15 * 60 + 15)
    _until(paper, thursday_at_the_same_hour)

    done = await paper.round()

    assert done.closes == ScheduledCloseRound() and done.wake_in_ms == IDLE_WAKE_MS
    assert _closes_sent(paper) == [] and scheduled_close_state(paper.repo, "a") == ()


async def test_a_second_round_and_a_pass_never_close_twice(paper: PaperOptions) -> None:  # noqa: F811
    await _holds_one_call(paper)
    _until(paper, DUE)
    await paper.round()

    await paper.round()
    await paper.pass_()
    paper.wait(PASS_MS)
    await paper.round()

    # The order still works at the venue: every later look drives the same EXIT and sends nothing more.
    assert _closes_sent(paper) == [SELLS_ONE_TO_CLOSE] and len(_closing_refs(paper)) == 1

    await _the_close_fills(paper)
    await paper.round()
    await paper.pass_()

    assert paper.held() == {} and _closes_sent(paper) == [SELLS_ONE_TO_CLOSE]


async def test_a_keeper_that_never_ran_is_covered_by_the_next_pass(paper: PaperOptions) -> None:  # noqa: F811
    await _holds_one_call(paper)
    _until(paper, DUE + PASS_MS)

    await paper.pass_()

    assert _closes_sent(paper) == [SELLS_ONE_TO_CLOSE]


# ── a program EXIT in the same moment ────────────────────────────────────


async def test_the_bots_own_exit_and_the_scheduled_close_in_the_same_moment_make_one_closing_order(
    paper: PaperOptions,  # noqa: F811
) -> None:
    await _holds_one_call(paper)
    _until(paper, DUE)

    _round, exited = await asyncio.gather(paper.round(), paper.exit("a"))

    assert exited.state is not EffectOperationState.REJECTED, exited.explanation
    assert _closes_sent(paper) == [SELLS_ONE_TO_CLOSE] and len(_closing_refs(paper)) == 1


async def test_a_scheduled_close_that_comes_second_is_not_taken_and_the_bots_own_exit_closes_the_position(
    paper: PaperOptions,  # noqa: F811
) -> None:
    await _holds_one_call(paper)
    _until(paper, DUE)
    await paper.exit("a")

    done = await paper.round()

    assert done.closes.taken == () and _closes_sent(paper) == [SELLS_ONE_TO_CLOSE]
    [state] = scheduled_close_state(paper.repo, "a")
    assert state.standing is CloseStanding.SCHEDULED and state.taken_at_ms is None


async def test_the_bots_own_exit_that_comes_second_is_answered_from_the_scheduled_close(paper: PaperOptions) -> None:  # noqa: F811
    await _holds_one_call(paper)
    _until(paper, DUE)
    await paper.round()

    exited = await paper.exit("a")

    # The strategy's decision is answered by the EXIT already working. No second order is made.
    assert exited.state is not EffectOperationState.REJECTED and "EXIT_IN_PROGRESS" in (exited.explanation or "")
    assert _closes_sent(paper) == [SELLS_ONE_TO_CLOSE] and len(_closing_refs(paper)) == 1
