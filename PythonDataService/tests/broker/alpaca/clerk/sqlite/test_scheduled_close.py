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
import json
from dataclasses import replace
from datetime import date
from types import SimpleNamespace

import pytest

from app.broker.alpaca.clerk.models import EffectOperationState
from app.broker.alpaca.clerk.sqlite import scheduled_close as scheduled_close_module
from app.broker.alpaca.clerk.sqlite.commands import submit_stop_run
from app.broker.alpaca.clerk.sqlite.deadline_keeper import IDLE_WAKE_MS
from app.broker.alpaca.clerk.sqlite.entry_outcome import EntryStanding
from app.broker.alpaca.clerk.sqlite.facts import ExitAcceptedFacts, ScheduledCloseFacts
from app.broker.alpaca.clerk.sqlite.option_admission import entry_past_cutoff
from app.broker.alpaca.clerk.sqlite.option_entry import OptionEntryFacts
from app.broker.alpaca.clerk.sqlite.residue_discharge import discharge_attributed_residue
from app.broker.alpaca.clerk.sqlite.scheduled_close import (
    CloseStanding,
    ScheduledCloseRound,
    held_contracts,
    scheduled_close_state,
)
from app.broker.alpaca.clerk.sqlite.uncertainty import (
    Capability,
    ReductionIntent,
    admit_new_exposure,
    decide_capability,
    open_exit_stuck,
)
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    EXIT_NOT_FLAT_REASON_CODE,
    FAILED_ENTER_FILLED_REASON_CODE,
    SCHEDULED_CLOSE_MISSED_REASON_CODE,
    MissedScheduledClose,
    ScheduledCloseMissedCause,
)
from app.broker.contract.errors import BrokerOrderRejected
from app.broker.contract.models import OrderSide, OrderType
from app.broker.contract.options import OptionPositionIntent, SingleOptionOrderRequest
from app.options.contract import OptionContract
from app.options.deadlines import CloseKind, CloseTerms, ScheduledClose
from app.options.structure import ResolvedLeg, ResolvedStructure
from app.schemas.market_liveness import MarketLivenessFact, SymbolTradingStatusEvidence
from app.services.broker_v2_panel import lane_summary
from app.services.market_liveness import unknown_market_liveness
from app.services.session_authority import et_minute_of_day_ms
from tests._helpers.market_quotes import publish_quotes
from tests.broker.alpaca.clerk.sqlite.conftest import (
    _FakeReadPort,
    documented_option_listing,
)
from tests.broker.alpaca.clerk.sqlite.resting_entry_harness import (
    CALL,
    LEG,
    SYMBOL,
    WAIT_MS,
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


# ── a Stop, a restart: the close is owed by what is held ─────────────────


async def test_after_the_owner_stops_the_bot_its_close_still_goes_out_at_its_time(paper: PaperOptions) -> None:  # noqa: F811
    await _holds_one_call(paper)
    submit_stop_run(paper.repo, account_id=paper.repo.account_id, strategy_instance_id="a", lifecycle_run_id="run-a")
    _until(paper, DUE)

    await paper.round()

    assert paper.repo.active_run("a") is None
    assert _closes_sent(paper) == [SELLS_ONE_TO_CLOSE]


async def test_a_restart_before_the_close_is_due_cancels_nothing_and_the_close_goes_out_at_its_time(
    paper: PaperOptions,  # noqa: F811
) -> None:
    """The process dies with its runner. The new one stops every run, and still owes the close."""
    await _holds_one_call(paper)
    _until(paper, DUE - 10 * 60_000)
    await paper.restart()
    assert paper.repo.active_run("a") is None and _closes_sent(paper) == []

    _until(paper, DUE)
    await paper.round()

    assert _closes_sent(paper) == [SELLS_ONE_TO_CLOSE]


async def test_a_restart_after_the_close_was_due_with_the_session_open_sends_it_at_once_and_says_how_late(
    paper: PaperOptions,  # noqa: F811
) -> None:
    await _holds_one_call(paper)
    twenty_minutes = 20 * 60_000
    _until(paper, DUE + twenty_minutes)

    await paper.restart()

    # The restart's own pass sends it: nothing waits for the keeper.
    assert _closes_sent(paper) == [SELLS_ONE_TO_CLOSE]
    [state] = scheduled_close_state(paper.repo, "a")
    assert state.standing is CloseStanding.SENT
    assert (state.due_ms, state.taken_at_ms, state.late_by_ms, state.late) == (
        DUE, DUE + twenty_minutes, twenty_minutes, True
    )


async def test_a_restart_after_the_session_closed_sends_nothing_and_records_the_close_as_missed(
    paper: PaperOptions,  # noqa: F811
) -> None:
    await _holds_one_call(paper)
    _until(paper, CLOSE + 60_000)

    await paper.restart()
    await paper.round()

    assert _closes_sent(paper) == [] and paper.held() == {SYMBOL: 1.0}
    notice = _notice(paper)
    assert notice is not None
    assert ScheduledCloseMissedCause.from_mapping(json.loads(notice["facts_json"])["cause_facts"]).closes == (
        MissedScheduledClose(
            symbol=SYMBOL, attributed_qty=1.0, kind="expiry_day_close", due_ms=DUE, session_open_ms=OPEN
        ),
    )


# ── a close its session ended on ─────────────────────────────────────────


async def _the_clerk_was_down_through_the_close(paper: PaperOptions, *, until_ms: int = MONDAY_AFTER_THE_OPEN) -> None:  # noqa: F811
    """Bot ``a`` holds one call, nothing looks at the books on its expiry day, and then a round runs."""
    await _holds_one_call(paper)
    _until(paper, until_ms)
    await paper.round()


async def test_a_missed_close_names_the_contract_and_how_many_and_holds_that_bots_entries_and_no_other_bots(
    paper: PaperOptions,  # noqa: F811
) -> None:
    await _the_clerk_was_down_through_the_close(paper)

    notice = _notice(paper)
    assert notice is not None and _notice(paper, "b") is None
    said = f"{notice['headline']} {notice['explanation']} {notice['next_step']}"
    assert f"1 {SYMBOL}" in notice["explanation"] and "expired" in said
    # Its own bot takes no entry under it, another bot does, and getting out of the contract stays allowed.
    refused = admit_new_exposure(paper.repo, strategy_instance_id="a")
    assert (refused.allowed, refused.reason_code) == (False, SCHEDULED_CLOSE_MISSED_REASON_CODE)
    assert admit_new_exposure(paper.repo, strategy_instance_id="b").allowed
    assert decide_capability(
        paper.repo, capability=Capability.REDUCE, strategy_instance_id="a",
        reduction_intent=ReductionIntent(symbol=SYMBOL, side="SELL", quantity=1),
    ).allowed
    # A sale of more than it holds is no reduction, and stays refused.
    assert not decide_capability(
        paper.repo, capability=Capability.REDUCE, strategy_instance_id="a",
        reduction_intent=ReductionIntent(symbol=SYMBOL, side="SELL", quantity=2),
    ).allowed


async def test_a_missed_close_is_a_line_on_the_lanes_bell_that_opens_its_bot(
    paper: PaperOptions, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    await _the_clerk_was_down_through_the_close(paper)
    monkeypatch.setattr(lane_summary, "get_active_clerk_runtime", lambda: SimpleNamespace(
        sqlite_repository=paper.repo, clerk=None, startup_failure=None, selected_account_authority_kind="real_paper",
    ))

    [line] = [
        item for item in (await lane_summary.lane_attention_read()).items
        if item.reason_code == SCHEDULED_CLOSE_MISSED_REASON_CODE
    ]

    # Listed with the exits that have not flattened, and it opens the bot that holds the contract.
    assert (line.kind, line.strategy_instance_id, line.action.destination) == ("exit", "a", "bot")


async def test_nothing_is_ever_sent_for_a_contract_after_its_last_session(paper: PaperOptions) -> None:  # noqa: F811
    await _the_clerk_was_down_through_the_close(paper)
    sent = len(paper.venue.option_submit_calls)

    for _ in range(4):
        paper.wait(PASS_MS)
        await paper.pass_()
        await paper.round()

    assert len(paper.venue.option_submit_calls) == sent and _closes_sent(paper) == []
    [state] = scheduled_close_state(paper.repo, "a")
    assert state.standing is CloseStanding.MISSED and state.taken_at_ms is None


async def test_a_missed_close_is_written_once_and_later_rounds_and_passes_add_nothing(paper: PaperOptions) -> None:  # noqa: F811
    await _the_clerk_was_down_through_the_close(paper)
    written = paper.repo.last_custody_sequence()

    for _ in range(3):
        paper.wait(5_000)
        await paper.round()

    assert paper.repo.last_custody_sequence() == written


async def test_a_missed_close_ends_once_the_bot_no_longer_holds_the_contract(paper: PaperOptions) -> None:  # noqa: F811
    """Alpaca takes the expired contract off the account, and the owner writes the bot's position off."""
    await _the_clerk_was_down_through_the_close(paper)
    assert _notice(paper) is not None
    submit_stop_run(paper.repo, account_id=paper.repo.account_id, strategy_instance_id="a", lifecycle_run_id="run-a")

    await discharge_attributed_residue(
        paper.repo, read=_FakeReadPort(), intake=paper.clerk.intake, strategy_instance_id="a", symbol=SYMBOL,
        operator_reason="expired worthless",
    )
    assert paper.held() == {} and _notice(paper) is not None
    await paper.round()

    assert _notice(paper) is None
    assert admit_new_exposure(paper.repo, strategy_instance_id="a").reason_code != SCHEDULED_CLOSE_MISSED_REASON_CODE


# ── the one read for the screens ─────────────────────────────────────────


async def test_before_its_time_the_close_reads_as_scheduled_with_when_it_is_due(paper: PaperOptions) -> None:  # noqa: F811
    await _holds_one_call(paper)
    _until(paper, OPEN + 60_000)

    [state] = scheduled_close_state(paper.repo, "a")

    assert (state.standing, state.contract, state.held, state.kind) == (
        CloseStanding.SCHEDULED, SYMBOL, 1.0, CloseKind.EXPIRY_DAY
    )
    assert (state.due_ms, state.session_open_ms, state.taken_at_ms, state.late_by_ms, state.late) == (
        DUE, OPEN, None, None, False
    )
    assert SYMBOL in state.sentence and not state.left_to_owner


async def test_a_close_sent_and_not_filled_reads_as_sent_then_partly_closed_then_flat(paper: PaperOptions) -> None:  # noqa: F811
    order_ref = await paper.resting("a", units=2)
    await paper.stream_fill(order_ref, contracts=2, filled_in_all=2, price="1.94", execution_id="x-buy")
    _until(paper, DUE)
    await paper.round()

    [sent] = scheduled_close_state(paper.repo, "a")
    assert (sent.standing, sent.held, sent.taken_at_ms, sent.late_by_ms, sent.late) == (
        CloseStanding.SENT, 2.0, DUE, 0, False
    )

    [closing_ref] = _closing_refs(paper)
    await paper.stream_fill(closing_ref, contracts=1, filled_in_all=1, price="2.10", execution_id="x-sell-1")
    [part] = scheduled_close_state(paper.repo, "a")
    assert (part.standing, part.held) == (CloseStanding.PARTLY_CLOSED, 1.0)

    await paper.stream_fill(closing_ref, contracts=1, filled_in_all=2, price="2.10", execution_id="x-sell-2")
    await paper.pass_()
    [flat] = scheduled_close_state(paper.repo, "a")
    assert (flat.standing, flat.contract, flat.held, flat.taken_at_ms, flat.late) == (
        CloseStanding.FLAT, SYMBOL, 0.0, DUE, False
    )


async def test_a_bot_that_closed_its_own_position_before_the_time_has_no_scheduled_close_to_its_name(
    paper: PaperOptions,  # noqa: F811
) -> None:
    await _holds_one_call(paper)
    _until(paper, DUE - 30 * 60_000)
    await paper.exit("a")
    await _the_close_fills(paper)

    _until(paper, DUE)
    done = await paper.round()

    assert done.closes == ScheduledCloseRound() and scheduled_close_state(paper.repo, "a") == ()
    assert _closes_sent(paper) == [SELLS_ONE_TO_CLOSE]


# ── a close that fails ───────────────────────────────────────────────────


def _sent_at(paper: PaperOptions, sid: str = "a") -> list[int]:  # noqa: F811
    """When each closing order of the bot was sent, on the Clerk's clock, oldest first."""
    sends = [
        row["recorded_at_ms"]
        for order_ref in _closing_refs(paper, sid)
        for row in paper.recorded(order_ref, "ORDER_SUBMIT_REQUESTED")
    ]
    return sorted(sends)


async def test_a_close_alpaca_refuses_is_sent_again_every_thirty_seconds_and_not_given_up_on_while_the_session_is_open(
    paper: PaperOptions,  # noqa: F811
) -> None:
    await _holds_one_call(paper)
    _until(paper, DUE)
    paper.venue.option_submit_error = BrokerOrderRejected("the venue refused the close", broker="alpaca")
    await paper.round()
    assert _notice(paper, reason_code=EXIT_NOT_FLAT_REASON_CODE) is not None

    for _ in range(24):  # six minutes of passes, fifteen seconds apart
        paper.wait(PASS_MS)
        await paper.pass_()

    sends = _sent_at(paper)
    # Thirteen tries in six minutes, thirty seconds apart: an ordinary exit gets three, two minutes apart.
    assert sends == [DUE + 30_000 * attempt for attempt in range(13)]
    assert open_exit_stuck(paper.repo, "a") is None
    [state] = scheduled_close_state(paper.repo, "a")
    assert (state.standing, state.taken_at_ms, state.late) == (CloseStanding.FAILED, DUE, False)

    # Alpaca takes the next one, and it closes the position.
    paper.venue.option_submit_error = None
    paper.wait(PASS_MS)
    await paper.pass_()
    paper.wait(PASS_MS)
    await paper.pass_()
    await _the_close_fills(paper)

    # Of all those tries the venue took one order, and it sold the one contract.
    assert paper.held() == {}
    assert [taken["side"] for taken in paper.venue.option_orders.values()] == ["buy", "sell"]
    assert _notice(paper, reason_code=EXIT_NOT_FLAT_REASON_CODE) is None


async def test_every_retry_of_a_scheduled_close_records_the_same_close_on_its_own_acceptance(paper: PaperOptions) -> None:  # noqa: F811
    await _holds_one_call(paper)
    _until(paper, DUE)
    paper.venue.option_submit_error = BrokerOrderRejected("the venue refused the close", broker="alpaca")
    await paper.round()
    for _ in range(4):
        paper.wait(PASS_MS)
        await paper.pass_()

    exits = [
        ExitAcceptedFacts.from_facts_json(row["facts_json"])
        for row in paper.repo.custody_transitions()
        if row["transition_kind"] == "EXIT_ACCEPTED"
    ]

    assert len(exits) == 3 and exits[1].decision_id.startswith("exit-redrive-")
    assert {accepted.the_scheduled_close for accepted in exits} == {
        ScheduledCloseFacts(kind="expiry_day_close", due_ms=DUE, session_open_ms=OPEN)
    }


async def test_a_close_still_refused_when_the_session_ends_is_recorded_as_missed_and_never_sent_again(
    paper: PaperOptions,  # noqa: F811
) -> None:
    await _holds_one_call(paper)
    _until(paper, CLOSE - 60_000)
    paper.venue.option_submit_error = BrokerOrderRejected("the venue refused the close", broker="alpaca")
    await paper.round()
    paper.wait(30_000)
    await paper.pass_()
    tried = len(paper.venue.option_submit_calls)
    assert tried == 3  # the entry, the close, and one retry

    _until(paper, CLOSE)
    await paper.pass_()
    paper.venue.option_submit_error = None
    _until(paper, MONDAY_AFTER_THE_OPEN)
    for _ in range(4):
        paper.wait(PASS_MS)
        await paper.pass_()

    assert len(paper.venue.option_submit_calls) == tried and paper.held() == {SYMBOL: 1.0}
    assert _notice(paper) is not None
    [state] = scheduled_close_state(paper.repo, "a")
    assert (state.standing, state.taken_at_ms) == (CloseStanding.MISSED, CLOSE - 60_000)


# ── a halt ───────────────────────────────────────────────────────────────


def _halts(paper: PaperOptions, halted: set[str], asked: list[str]) -> None:  # noqa: F811
    """IBKR reports each symbol in ``halted`` as halted, to the keeper and to the pass alike."""

    def status(symbol: str, now_ms: int) -> MarketLivenessFact:
        asked.append(symbol)
        fact = unknown_market_liveness(symbol, observed_at_ms=now_ms)
        if symbol not in halted:
            return fact
        return fact.model_copy(update={
            "state": "HALTED",
            "symbol_status": SymbolTradingStatusEvidence(
                symbol=symbol, state="HALTED", source="ibkr.market_data.status", observed_at_ms=now_ms,
            ),
        })

    paper.keeper.keep_scheduled_closes(
        intake=paper.clerk.intake, pricing=replace(paper.clerk.recovery_pricing, liveness_source=status)
    )


async def test_a_halt_of_the_underlying_holds_the_close_and_it_goes_out_when_the_halt_lifts(paper: PaperOptions) -> None:  # noqa: F811
    await _holds_one_call(paper)
    halted, asked = {"SPY"}, []
    _halts(paper, halted, asked)
    _until(paper, DUE)

    taken = await paper.round()

    # Taken, and held: the halt is asked of the underlying, never of the contract, and nothing is sent.
    assert len(taken.closes.taken) == 1 and _closes_sent(paper) == []
    assert asked and set(asked) == {"SPY"}
    [state] = scheduled_close_state(paper.repo, "a")
    assert (state.standing, state.taken_at_ms) == (CloseStanding.SCHEDULED, DUE)

    paper.wait(60_000)
    still = await paper.round()
    assert still.closes.taken == () and still.closes.driven == taken.closes.taken and _closes_sent(paper) == []

    halted.clear()
    paper.wait(5_000)
    await paper.round()

    assert _closes_sent(paper) == [SELLS_ONE_TO_CLOSE]


async def test_a_halt_that_does_not_lift_before_the_close_leaves_the_position_open_and_the_close_missed(
    paper: PaperOptions,  # noqa: F811
) -> None:
    await _holds_one_call(paper)
    halted, asked = {"SPY"}, []
    _halts(paper, halted, asked)
    _until(paper, DUE)
    await paper.round()

    _until(paper, CLOSE)
    await paper.round()
    halted.clear()
    paper.wait(5_000)
    await paper.round()

    assert _closes_sent(paper) == [] and paper.held() == {SYMBOL: 1.0}
    assert _notice(paper) is not None and paper.repo.active_exit_for_strategy("a") is None


# ── the Clerk has stopped exiting the bot by itself ──────────────────────


async def _the_clerk_gives_up_on_the_bots_own_exit(paper: PaperOptions) -> None:  # noqa: F811
    """On the expiry day's morning the bot's exit is refused, and refused again until the Clerk stops trying."""
    await _holds_one_call(paper)
    _until(paper, OPEN + 60 * 60_000)
    paper.venue.option_submit_error = BrokerOrderRejected("the venue refused the close", broker="alpaca")
    await paper.exit("a")
    for _ in range(48):
        paper.wait(PASS_MS)
        await paper.pass_()
    assert open_exit_stuck(paper.repo, "a") is not None, "the Clerk never gave up"
    paper.venue.option_submit_error = None


async def test_no_scheduled_close_is_taken_while_the_clerk_has_stopped_exiting_the_bot_by_itself(
    paper: PaperOptions,  # noqa: F811
) -> None:
    await _the_clerk_gives_up_on_the_bots_own_exit(paper)
    sent = len(paper.venue.option_submit_calls)
    _until(paper, DUE)

    done = await paper.round()
    await paper.pass_()

    # Nothing is sent: no read of the account shows whether the last order traded.
    assert done.closes.taken == () and len(paper.venue.option_submit_calls) == sent
    [state] = scheduled_close_state(paper.repo, "a")
    assert (state.standing, state.left_to_owner, state.taken_at_ms) == (CloseStanding.SCHEDULED, True, None)

    _until(paper, CLOSE)
    await paper.round()

    assert len(paper.venue.option_submit_calls) == sent
    notice = _notice(paper)
    assert notice is not None and "stopped exiting" in notice["explanation"]


# ── a position the strategy does not know it holds ───────────────────────


async def test_a_fill_on_an_entry_the_strategy_was_told_it_does_not_hold_is_closed_on_its_day(paper: PaperOptions) -> None:  # noqa: F811
    """The entry's wait ran out and it was proven unfilled. Then a fill arrived. No exit of the strategy is coming."""
    order_ref = await paper.resting("a")
    paper.wait(WAIT_MS)
    await paper.round()
    assert paper.outcome(order_ref).standing is EntryStanding.UNFILLED
    await paper.stream_fill(order_ref, contracts=1, filled_in_all=1, price="1.94", execution_id="x-after")
    assert paper.held() == {SYMBOL: 1.0} and _notice(paper, reason_code=FAILED_ENTER_FILLED_REASON_CODE) is not None
    assert paper.repo.active_run("a") is not None

    _until(paper, DUE)
    await paper.round()

    assert _closes_sent(paper) == [SELLS_ONE_TO_CLOSE]
    await _the_close_fills(paper)
    assert paper.held() == {}


async def test_what_filled_of_an_entry_cancelled_at_its_wait_is_closed_on_its_day(paper: PaperOptions) -> None:  # noqa: F811
    order_ref = await paper.resting("a", units=2)
    await paper.stream_fill(order_ref, contracts=1, filled_in_all=1, price="1.94", execution_id="x-1")
    paper.wait(WAIT_MS)
    await paper.round()
    assert paper.outcome(order_ref).standing is EntryStanding.PART_FILLED

    _until(paper, DUE)
    await paper.round()

    # One contract filled, so one is closed: the close is sized from what is held, never from what was asked.
    assert _closes_sent(paper) == [SELLS_ONE_TO_CLOSE]


async def test_two_bots_each_holding_the_contract_are_each_closed_by_an_order_of_their_own(paper: PaperOptions) -> None:  # noqa: F811
    await _holds_one_call(paper, "a")
    # The account is read again after a's fill, as its own cadence reads it, before b enters.
    paper.wait(1_000)
    paper.account.account_observed_at_ms = paper.clock()
    assert await paper.sync.tick() == "observed"
    await _holds_one_call(paper, "b")
    _until(paper, DUE)

    done = await paper.round()

    assert len(done.closes.taken) == 2 and _closes_sent(paper) == [SELLS_ONE_TO_CLOSE, SELLS_ONE_TO_CLOSE]
    assert len(_closing_refs(paper, "a")) == 1 and len(_closing_refs(paper, "b")) == 1


# ── a structure with a sold leg: shown at this level only, until the Clerk takes one ──


def _with_its_leg_sold(entry: OptionEntryFacts) -> OptionEntryFacts:
    """The same entry with its one leg sold, not bought. No such entry can be made through the Clerk yet."""
    return entry.model_copy(update={"legs": (entry.the_leg.model_copy(update={"position": "short"}),)})


async def test_a_holding_whose_entry_has_a_sold_leg_owes_a_close_ten_minutes_before_every_sessions_close(
    paper: PaperOptions, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    await _holds_one_call(paper)
    as_entered = scheduled_close_module.option_entry_of_order
    monkeypatch.setattr(
        scheduled_close_module, "option_entry_of_order", lambda repo, ref: _with_its_leg_sold(as_entered(repo, ref))
    )

    def owes_on(day: date, *, closes_at: int) -> ScheduledClose:
        return ScheduledClose(
            CloseKind.SOLD_LEG, et_minute_of_day_ms(day, (closes_at - 1) * 60 + 50),
            et_minute_of_day_ms(day, 9 * 60 + 30), et_minute_of_day_ms(day, closes_at * 60),
        )

    # The day after the entry, the day after that, and a half day two months on: one close each session.
    for day, closes_at in ((date(2026, 9, 9), 16), (date(2026, 9, 10), 16), (HALF_DAY, 13)):
        [held] = held_contracts(paper.repo, et_minute_of_day_ms(day, 10 * 60)).held
        assert held.owed == owes_on(day, closes_at=closes_at)
    # On the contract's own expiry day the earlier close binds: 15:15, not 15:50.
    [on_expiry] = held_contracts(paper.repo, et_minute_of_day_ms(EXPIRY, 10 * 60)).held
    assert on_expiry.owed is not None and (on_expiry.owed.kind, on_expiry.owed.due_ms) == (CloseKind.EXPIRY_DAY, DUE)
    # Still held on a Saturday: nothing is owed that day, and Friday's close was missed.
    [weekend] = held_contracts(paper.repo, et_minute_of_day_ms(date(2026, 9, 12), 12 * 60)).held
    assert weekend.owed is None and weekend.missed == owes_on(date(2026, 9, 11), closes_at=16)
    assert not weekend.expired


@pytest.mark.parametrize(
    ("day", "closes_at"),
    [pytest.param(date(2026, 9, 9), 16, id="a full day"), pytest.param(HALF_DAY, 13, id="a half day")],
)
def test_an_entry_on_a_structure_with_a_sold_leg_is_too_late_from_eighty_seconds_before_that_sessions_close_of_it(
    day: date, closes_at: int
) -> None:
    """The Clerk's own cutoff is counted from the close the position would owe that day, whichever close it is."""
    far = date(2026, 12, 18)
    structure = ResolvedStructure(legs=(
        ResolvedLeg(
            leg_id="bought", contract=OptionContract.of(root="SPY", expiry_date=far, right="call", strike=585),
            position="long", ratio=1,
        ),
        ResolvedLeg(
            leg_id="sold", contract=OptionContract.of(root="SPY", expiry_date=far, right="call", strike=590),
            position="short", ratio=1,
        ),
    ))
    # Ten minutes before the close, less a 20 second wait, 30 to prove a cancel and 30 of margin.
    cutoff = et_minute_of_day_ms(day, (closes_at - 1) * 60 + 48) + 40_000

    assert entry_past_cutoff(structure, now_ms=cutoff - 1, terms=CloseTerms()) is None
    refused = entry_past_cutoff(structure, now_ms=cutoff, terms=CloseTerms())
    assert refused is not None and refused.reason_code == "OPTION_CONTRACT_PAST_ENTRY_CUTOFF"
