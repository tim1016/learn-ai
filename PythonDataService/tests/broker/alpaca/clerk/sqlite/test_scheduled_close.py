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
import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.broker.alpaca.clerk.models import EffectOperationState
from app.broker.alpaca.clerk.sealed_ledger import canonical_sha256
from app.broker.alpaca.clerk.sqlite import deadline_keeper as keeper_module
from app.broker.alpaca.clerk.sqlite import reads
from app.broker.alpaca.clerk.sqlite import runtime as clerk_runtime
from app.broker.alpaca.clerk.sqlite import scheduled_close as scheduled_close_module
from app.broker.alpaca.clerk.sqlite.budget_commands import submit_budgeted_deploy
from app.broker.alpaca.clerk.sqlite.commands import submit_stop_run
from app.broker.alpaca.clerk.sqlite.deadline_keeper import IDLE_WAKE_MS
from app.broker.alpaca.clerk.sqlite.enter import accept_option_enter, submit_accepted_enter
from app.broker.alpaca.clerk.sqlite.entry_outcome import EntryStanding
from app.broker.alpaca.clerk.sqlite.facts import ExitAcceptedFacts, ScheduledCloseFacts
from app.broker.alpaca.clerk.sqlite.option_admission import entry_past_cutoff
from app.broker.alpaca.clerk.sqlite.option_entry import OptionEntryFacts
from app.broker.alpaca.clerk.sqlite.reconcile import reconcile_account
from app.broker.alpaca.clerk.sqlite.repository import ExecutionLeaseLost
from app.broker.alpaca.clerk.sqlite.residue_discharge import discharge_attributed_residue
from app.broker.alpaca.clerk.sqlite.scheduled_close import (
    MAX_CLOSES_AT_ONCE,
    CloseStanding,
    ScheduledCloseRound,
    _accept_while_still_owed,
    _record_missed_closes,
    drive_scheduled_closes,
    held_contracts,
    scheduled_close_state,
)
from app.broker.alpaca.clerk.sqlite.uncertainty import (
    Capability,
    ReductionIntent,
    admit_new_exposure,
    decide_capability,
    open_exit_stuck,
    raise_account_hold,
    resolve_account_hold,
)
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    EXIT_NOT_FLAT_REASON_CODE,
    FAILED_ENTER_FILLED_REASON_CODE,
    SCHEDULED_CLOSE_MISSED_REASON_CODE,
    STREAM_HEALTH_HOLD_REASON_CODE,
    MissedScheduledClose,
    ScheduledCloseMissedCause,
)
from app.broker.alpaca.clerk.sqlite.unknown_order_attestation import OrderOutcomeAttestation
from app.broker.contract.errors import BrokerOrderRejected, BrokerUnavailable
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
    _covering_read,
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
from tests.broker.alpaca.clerk.sqlite.test_budget_commands import TERMS, _gate
from tests.broker.alpaca.clerk.sqlite.test_resting_entries import _the_flatten_offered
from tests.broker.alpaca.clerk.sqlite.test_resting_entry_facts import _PINNED_OPTION

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


async def test_a_contract_whose_expiry_date_has_no_session_owes_no_close_and_is_told_as_missed_when_its_last_session_ends(
    paper: PaperOptions,  # noqa: F811
) -> None:
    """A listed contract should carry no such date. What the Clerk says of one that does is a known gap, held here."""
    thanksgiving, the_day_before = date(2026, 11, 26), date(2026, 11, 25)
    expires_on_a_holiday = OptionContract.of(root="SPY", expiry_date=thanksgiving, right="call", strike=585)
    paper.account.listed[expires_on_a_holiday.occ_symbol] = documented_option_listing(expires_on_a_holiday)
    receipt = await paper.enter("a", evidence=paper.evidence(picked={LEG: expires_on_a_holiday}))
    [order_ref] = receipt.child_order_refs
    await paper.stream_fill(order_ref, contracts=1, filled_in_all=1, price="1.94", execution_id="x-buy")
    its_last_close = et_minute_of_day_ms(the_day_before, 16 * 60)

    _until(paper, its_last_close - 60_000)
    in_its_last_session = await paper.round()
    _until(paper, its_last_close)
    once_it_ended = await paper.round()

    # Nothing is owed in its last session, which is not its expiry date. From that session's close it is missed.
    assert in_its_last_session.closes == ScheduledCloseRound() and _closes_sent(paper) == []
    assert once_it_ended.closes.missed == (("a", expires_on_a_holiday.occ_symbol),) and _notice(paper) is not None


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
    paper: PaperOptions, caplog: pytest.LogCaptureFixture  # noqa: F811
) -> None:
    await _holds_one_call(paper)
    _until(paper, DUE)
    await paper.exit("a")
    caplog.set_level(logging.ERROR)

    done = await paper.round()

    assert done.closes.taken == () and _closes_sent(paper) == [SELLS_ONE_TO_CLOSE]
    # Meeting the bot's own exit is no failure: nothing is logged as one.
    assert caplog.records == []
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


async def test_a_bot_whose_runner_is_gone_still_has_its_contract_closed_at_its_time(paper: PaperOptions) -> None:  # noqa: F811
    """No runner holds the bot's run. The pass retires the run, and the close is owed all the same."""
    await _holds_one_call(paper)
    _until(paper, DUE - 5 * 60_000)
    for _ in range(2):  # the second pass that finds the run with no runner stops it
        paper.wait(PASS_MS)
        await reconcile_account(
            paper.repo, read=paper.account, trade=paper.venue, intake=paper.clerk.intake,
            pricing=paper.clerk.recovery_pricing, run_ownership=paper.clerk.run_ownership,
        )
    assert paper.repo.active_run("a") is None and _closes_sent(paper) == []

    _until(paper, DUE)
    await paper.round()

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
    # Late is more than a minute after it was due: a minute to the millisecond is not.
    assert not replace(state, late_by_ms=60_000).late and replace(state, late_by_ms=60_001).late


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


async def test_one_bots_read_tells_its_own_close_and_not_the_close_of_the_bot_beside_it(paper: PaperOptions) -> None:  # noqa: F811
    await _holds_one_call(paper, "b")
    _until(paper, OPEN + 60_000)

    # Bot b holds the call. Bot a holds nothing and has no close to its name.
    assert scheduled_close_state(paper.repo, "a") == ()
    [of_b] = scheduled_close_state(paper.repo, "b")
    assert (of_b.strategy_instance_id, of_b.standing) == ("b", CloseStanding.SCHEDULED)


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
    # It is told for the rest of its own day. On Monday the bot has no scheduled close to its name.
    _until(paper, MONDAY_AFTER_THE_OPEN)
    assert scheduled_close_state(paper.repo, "a") == ()


async def test_a_close_that_failed_is_not_told_as_the_close_that_left_the_bot_flat(paper: PaperOptions) -> None:  # noqa: F811
    """The close is refused. The bot is stopped and its position is written off: no exit of the Clerk's proved it flat."""
    await _holds_one_call(paper)
    _until(paper, DUE)
    paper.venue.option_submit_error = BrokerOrderRejected("the venue refused the close", broker="alpaca")
    await paper.round()
    submit_stop_run(paper.repo, account_id=paper.repo.account_id, strategy_instance_id="a", lifecycle_run_id="run-a")

    await discharge_attributed_residue(
        paper.repo, read=_FakeReadPort(), intake=paper.clerk.intake, strategy_instance_id="a", symbol=SYMBOL,
        operator_reason="Alpaca sold it",
    )

    # Nothing is held, and the bot's newest exit is the scheduled close that failed. Flat is not said of it.
    assert paper.held() == {} and scheduled_close_state(paper.repo, "a") == ()


async def test_a_close_that_went_through_on_a_later_try_is_told_as_taken_when_it_was_first_taken(
    paper: PaperOptions,  # noqa: F811
) -> None:
    await _holds_one_call(paper)
    _until(paper, DUE)
    paper.venue.option_submit_error = BrokerOrderRejected("the venue refused the close", broker="alpaca")
    await paper.round()
    paper.venue.option_submit_error = None
    for _ in range(2):
        paper.wait(PASS_MS)
        await paper.pass_()
    await _the_close_fills(paper)

    [flat] = scheduled_close_state(paper.repo, "a")

    # The second try sold it, thirty seconds on. The close is still the one taken at its due time, and not late.
    assert _sent_at(paper) == [DUE, DUE + 30_000] and paper.held() == {}
    assert (flat.standing, flat.taken_at_ms, flat.late_by_ms, flat.late) == (CloseStanding.FLAT, DUE, 0, False)


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


# ── a halt, and a hold on the account ────────────────────────────────────


def _halts(paper: PaperOptions, monkeypatch: pytest.MonkeyPatch, halted: set[str], asked: list[str]) -> None:  # noqa: F811
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

    # The Clerk's one pricing seam reads the market's status here. The keeper is built again with that seam.
    monkeypatch.setattr(clerk_runtime, "market_liveness_fact", status)
    paper.keeper = paper.another_keeper()


async def test_a_halt_of_the_underlying_holds_the_close_and_the_next_pass_sends_it_when_the_halt_lifts(
    paper: PaperOptions, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    await _holds_one_call(paper)
    halted, asked = {"SPY"}, []
    _halts(paper, monkeypatch, halted, asked)
    _until(paper, DUE)

    taken = await paper.round()

    # Taken, and held: the halt is asked of the underlying, never of the contract, and nothing is sent.
    assert len(taken.closes.taken) == 1 and _closes_sent(paper) == []
    assert asked and set(asked) == {"SPY"}
    [state] = scheduled_close_state(paper.repo, "a")
    assert (state.standing, state.taken_at_ms) == (CloseStanding.SCHEDULED, DUE)

    # While the halt stands, a pass and a round send nothing and take nothing more.
    paper.wait(PASS_MS)
    await paper.pass_()
    still = await paper.round()
    assert still.closes.taken == () and _closes_sent(paper) == []

    halted.clear()
    paper.wait(PASS_MS)
    await paper.pass_()

    assert _closes_sent(paper) == [SELLS_ONE_TO_CLOSE]


async def test_on_a_real_account_the_keeper_leaves_a_close_it_has_taken_to_the_pass_and_looks_nothing_up(
    paper: PaperOptions,  # noqa: F811
) -> None:
    """The pass drives every unfinished EXIT on. A keeper that did too would ask Alpaca about it three times as often."""
    await _holds_one_call(paper)
    _until(paper, DUE)
    await paper.round()
    looked_up = len(paper.venue.option_lookup_calls)

    for _ in range(3):
        paper.wait(5_000)
        again = await paper.round()
        assert again.closes.taken == () and again.closes.driven == ()

    assert len(paper.venue.option_lookup_calls) == looked_up


async def test_a_halt_that_does_not_lift_before_the_close_leaves_the_position_open_and_the_close_missed(
    paper: PaperOptions, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    await _holds_one_call(paper)
    halted, asked = {"SPY"}, []
    _halts(paper, monkeypatch, halted, asked)
    _until(paper, DUE)
    await paper.round()

    _until(paper, CLOSE)
    await paper.pass_()
    halted.clear()
    paper.wait(PASS_MS)
    await paper.pass_()
    await paper.round()

    # Nothing was ever sent, the held close ended with the contract's last session, and the miss is on record.
    assert _closes_sent(paper) == [] and paper.held() == {SYMBOL: 1.0}
    assert _notice(paper) is not None and paper.repo.active_exit_for_strategy("a") is None


async def test_a_hold_on_the_account_that_refuses_reductions_holds_the_close_and_it_goes_out_once_the_hold_is_gone(
    paper: PaperOptions,  # noqa: F811
) -> None:
    """The refusal is not weakened: the close is taken, sends nothing, and is tried again on every pass."""
    await _holds_one_call(paper)
    raise_account_hold(
        paper.repo, reason_code=STREAM_HEALTH_HOLD_REASON_CODE, evidence_refs=["execution: the order stream is down"]
    )
    _until(paper, DUE)

    held_up = await paper.round()
    paper.wait(PASS_MS)
    await paper.pass_()

    assert len(held_up.closes.taken) == 1 and held_up.closes.deferred == held_up.closes.taken
    assert _closes_sent(paper) == [] and paper.repo.active_exit_for_strategy("a") is not None

    resolve_account_hold(
        paper.repo, reason_code=STREAM_HEALTH_HOLD_REASON_CODE, summary_code="ACCOUNT_HOLD_RESOLVED_BY_STREAM_RECOVERY"
    )
    paper.wait(PASS_MS)
    await paper.pass_()

    assert _closes_sent(paper) == [SELLS_ONE_TO_CLOSE]


# ── the Clerk has stopped exiting the bot by itself ──────────────────────


async def _the_clerk_gives_up_on_the_bots_own_exit(paper: PaperOptions, *, holding: bool = False) -> None:  # noqa: F811
    """On the expiry day's morning the bot's exit is refused, and refused again until the Clerk stops trying.

    ``holding`` says the bot already holds its call; otherwise it buys it first.
    """
    if not holding:
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


async def test_a_failed_close_its_owner_settled_as_never_sent_reads_as_left_to_the_owner_and_no_longer_as_retried(
    paper: PaperOptions,  # noqa: F811
) -> None:
    """Alpaca refuses the close. Its next try gets no answer. The owner stops the bot and says Alpaca has no such order."""
    await _holds_one_call(paper)
    _until(paper, DUE)
    paper.venue.option_submit_error = BrokerOrderRejected("the venue refused the close", broker="alpaca")
    await paper.round()
    [retried] = scheduled_close_state(paper.repo, "a")
    paper.venue.option_submit_error = BrokerUnavailable("no answer", broker="alpaca")
    paper.wait(30_000)
    await paper.pass_()
    paper.venue.option_submit_error = None
    submit_stop_run(paper.repo, account_id=paper.repo.account_id, strategy_instance_id="a", lifecycle_run_id="run-a")
    paper.wait(60_000)
    (unknown,) = paper.repo.unknown_exit_orders("a")
    await paper.clerk.settle_unknown_exit_order(
        strategy_instance_id="a", presented=unknown, attestation=OrderOutcomeAttestation(kind="absent"),
        operator="owner", reason=None,
    )

    [left] = scheduled_close_state(paper.repo, "a")

    # Failed both times, and the same contract is still held. Only the first is the Clerk's to try again.
    assert (retried.standing, retried.left_to_owner) == (CloseStanding.FAILED, False)
    assert (left.standing, left.left_to_owner, paper.held()) == (CloseStanding.FAILED, True, {SYMBOL: 1.0})
    # The Clerk now sends nothing more for this bot, so the read no longer says what it said of the retry.
    assert left.sentence != retried.sentence


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


async def _two_bots_each_hold_one_call(paper: PaperOptions) -> None:  # noqa: F811
    await _holds_one_call(paper, "a")
    # The account is read again after a's fill, as its own cadence reads it, before b enters.
    paper.wait(1_000)
    paper.account.account_observed_at_ms = paper.clock()
    assert await paper.sync.tick() == "observed"
    await _holds_one_call(paper, "b")


async def test_two_bots_each_holding_the_contract_are_each_closed_by_an_order_of_their_own(paper: PaperOptions) -> None:  # noqa: F811
    await _two_bots_each_hold_one_call(paper)
    _until(paper, DUE)

    done = await paper.round()

    assert len(done.closes.taken) == 2 and _closes_sent(paper) == [SELLS_ONE_TO_CLOSE, SELLS_ONE_TO_CLOSE]
    assert len(_closing_refs(paper, "a")) == 1 and len(_closing_refs(paper, "b")) == 1


# ── each close is handled by itself ──────────────────────────────────────


def _at_the_venue(paper: PaperOptions, sid: str) -> bool:  # noqa: F811
    """Whether the venue has taken the bot's closing order."""
    return any(order_ref in paper.venue.option_orders for order_ref in _closing_refs(paper, sid))


async def _comes_true(what: Callable[[], bool]) -> bool:
    """Whether ``what`` comes true within two seconds, while the event loop and its worker threads run."""
    for _ in range(200):
        if what():
            return True
        await asyncio.sleep(0.01)
    return what()


def _driving_of(bot: str, does: Callable[[], Awaitable[None]], monkeypatch: pytest.MonkeyPatch) -> None:
    """Driving a close of ``bot`` first does ``does``; every other close is driven as it is."""
    drive = scheduled_close_module.resolve_exit

    async def driven(repo: object, *, effect_operation_id: str, **rest: object) -> object:
        if effect_operation_id.startswith(f"effect:{bot}:"):
            await does()
        return await drive(repo, effect_operation_id=effect_operation_id, **rest)

    monkeypatch.setattr(scheduled_close_module, "resolve_exit", driven)


async def test_a_slow_answer_to_one_bots_close_does_not_hold_up_another_bots(
    paper: PaperOptions, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    await _two_bots_each_hold_one_call(paper)
    _until(paper, DUE)
    answered = asyncio.Event()
    _driving_of("a", answered.wait, monkeypatch)

    round_ = asyncio.create_task(paper.round())

    # The second bot's close is at the venue while the first is still waiting.
    assert await _comes_true(lambda: _at_the_venue(paper, "b"))
    assert not _at_the_venue(paper, "a") and not round_.done()

    answered.set()
    done = await asyncio.wait_for(round_, timeout=5)
    assert len(done.closes.driven) == 2 and _closes_sent(paper) == [SELLS_ONE_TO_CLOSE, SELLS_ONE_TO_CLOSE]


async def test_a_close_that_breaks_is_left_for_the_pass_and_the_other_bots_close_still_goes_out(
    paper: PaperOptions, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    await _two_bots_each_hold_one_call(paper)
    _until(paper, DUE)

    async def breaks() -> None:
        raise RuntimeError("an answer nothing here expected")

    with monkeypatch.context() as broken:
        _driving_of("a", breaks, broken)
        done = await paper.round()

    # Both were taken. The one that broke is told as left, and the other was sent.
    assert len(done.closes.taken) == 2
    assert [effect.split(":")[1] for effect in done.closes.driven] == ["b"]
    assert [effect.split(":")[1] for effect in done.closes.deferred] == ["a"]
    assert _at_the_venue(paper, "b") and not _at_the_venue(paper, "a")

    # It is an EXIT like any other from here, and the next pass sends it.
    paper.wait(PASS_MS)
    await paper.pass_()
    assert _closes_sent(paper) == [SELLS_ONE_TO_CLOSE, SELLS_ONE_TO_CLOSE]


async def test_a_lost_hold_on_the_account_is_raised_and_only_once_the_closes_in_flight_have_come_to_rest(
    paper: PaperOptions, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    await _two_bots_each_hold_one_call(paper)
    _until(paper, DUE)
    answered = asyncio.Event()

    async def loses_the_lease() -> None:
        raise ExecutionLeaseLost("the Clerk's hold on its account was lost", account_id=paper.repo.account_id)

    _driving_of("a", loses_the_lease, monkeypatch)
    _driving_of("b", answered.wait, monkeypatch)

    step = asyncio.create_task(
        drive_scheduled_closes(
            paper.repo, trade=paper.venue, intake=paper.clerk.intake, pricing=paper.clerk.recovery_pricing
        )
    )

    # The first bot's close has lost the lease. The step does not end while the second is still on its way.
    assert await _comes_true(lambda: paper.repo.active_exit_for_strategy("b") is not None)
    await asyncio.sleep(0.05)
    assert not step.done()

    answered.set()
    # It is no one contract's failure: it is raised, and never told as a close that was left.
    with pytest.raises(ExecutionLeaseLost):
        await asyncio.wait_for(step, timeout=5)
    assert _at_the_venue(paper, "b") and not _at_the_venue(paper, "a")


async def test_a_close_another_owner_is_driving_is_left_alone_and_sent_once_it_is_free(
    paper: PaperOptions, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """Shown with the step asked to drive a taken close on, as a Dry Run's keeper asks it."""
    await _holds_one_call(paper)
    halted, asked = {"SPY"}, []
    _halts(paper, monkeypatch, halted, asked)
    _until(paper, DUE)
    [taken] = (await paper.round()).closes.taken
    halted.clear()
    held = paper.repo.claim_before_broker_contact(taken)

    async def step() -> ScheduledCloseRound:
        return await drive_scheduled_closes(
            paper.repo, trade=paper.venue, intake=paper.clerk.intake, pricing=paper.clerk.recovery_pricing,
            no_pass_on_a_timer=True,
        )

    left = await step()

    assert (left.driven, left.deferred) == ((), (taken,)) and _closes_sent(paper) == []

    paper.repo.release_operation_claim(effect_operation_id=taken, token=held.token)
    sent = await step()

    assert sent.driven == (taken,) and _closes_sent(paper) == [SELLS_ONE_TO_CLOSE]


# ── each step of a round is handled by itself ────────────────────────────


async def _a_holds_a_call_and_bs_entry_rests_past_its_wait(paper: PaperOptions) -> str:  # noqa: F811
    """Bot a holds a call and bot b's entry rests. Nothing looks until a's close is due. Returns b's order reference."""
    await _holds_one_call(paper, "a")
    # The account is read again after a's fill, as its own cadence reads it, before b enters.
    paper.wait(1_000)
    paper.account.account_observed_at_ms = paper.clock()
    assert await paper.sync.tick() == "observed"
    resting = await paper.resting("b")
    _until(paper, DUE)
    return resting


async def test_an_entry_past_its_wait_and_a_close_that_is_due_are_both_kept_in_one_round(paper: PaperOptions) -> None:  # noqa: F811
    resting = await _a_holds_a_call_and_bs_entry_rests_past_its_wait(paper)

    done = await paper.round()

    # One round: the entry is cancelled and proven unfilled, and the close is sent.
    assert done.failed == () and done.entries.ended == (resting,)
    assert paper.outcome(resting).standing is EntryStanding.UNFILLED
    assert len(done.closes.taken) == 1 and _closes_sent(paper) == [SELLS_ONE_TO_CLOSE]


async def test_an_entry_step_that_breaks_is_logged_by_name_and_the_close_is_still_sent(
    paper: PaperOptions, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture  # noqa: F811
) -> None:
    await _a_holds_a_call_and_bs_entry_rests_past_its_wait(paper)

    def cannot_be_listed(_repo: object) -> object:
        raise RuntimeError("one entry cannot be listed")

    monkeypatch.setattr(keeper_module, "resting_entries", cannot_be_listed)
    with caplog.at_level(logging.ERROR, logger=keeper_module.__name__):
        done = await paper.round()

    assert done.failed == ("resting_entries",) and _closes_sent(paper) == [SELLS_ONE_TO_CLOSE]
    assert [record.step for record in caplog.records if record.action == "deadline_keeper_step_failed"] == [
        "resting_entries"
    ]


async def test_a_close_step_that_breaks_is_logged_by_name_and_the_entry_is_still_cancelled(
    paper: PaperOptions, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture  # noqa: F811
) -> None:
    resting = await _a_holds_a_call_and_bs_entry_rests_past_its_wait(paper)

    async def cannot_be_read(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("what is held cannot be read")

    monkeypatch.setattr(keeper_module, "drive_scheduled_closes", cannot_be_read)
    with caplog.at_level(logging.ERROR, logger=keeper_module.__name__):
        done = await paper.round()

    assert done.failed == ("scheduled_closes",) and done.entries.ended == (resting,)
    assert paper.outcome(resting).standing is EntryStanding.UNFILLED and _closes_sent(paper) == []
    assert [record.step for record in caplog.records if record.action == "deadline_keeper_step_failed"] == [
        "scheduled_closes"
    ]


async def test_a_lost_hold_on_the_account_in_one_step_ends_the_round_and_is_raised(
    paper: PaperOptions, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    await _a_holds_a_call_and_bs_entry_rests_past_its_wait(paper)

    async def loses_the_lease(*_args: object, **_kwargs: object) -> object:
        raise ExecutionLeaseLost("the Clerk's hold on its account was lost", account_id=paper.repo.account_id)

    monkeypatch.setattr(keeper_module, "cancel_resting_entries", loses_the_lease)

    # It is no step's own failure: nothing more can be written, so the round ends and no close is sent.
    with pytest.raises(ExecutionLeaseLost):
        await paper.round()
    assert _closes_sent(paper) == []


async def test_a_stop_that_comes_out_of_one_step_as_an_error_ends_the_round_before_the_other_step(
    paper: PaperOptions, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """A stop that lands inside a broker call can surface as another error. It is still a stop, not a step's failure."""
    await _a_holds_a_call_and_bs_entry_rests_past_its_wait(paper)
    inside_the_entry_step = asyncio.Event()

    async def turns_a_stop_into_an_error(*_args: object, **_kwargs: object) -> object:
        inside_the_entry_step.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise RuntimeError("the call ended with another error as the keeper was stopped") from None
        raise AssertionError("unreachable")

    monkeypatch.setattr(keeper_module, "cancel_resting_entries", turns_a_stop_into_an_error)
    paper.keeper.start()
    await asyncio.wait_for(inside_the_entry_step.wait(), timeout=2)

    await asyncio.wait_for(paper.keeper.stop(), timeout=2)

    # The keeper stopped, and a stopped keeper sent no close.
    assert _closes_sent(paper) == [] and paper.repo.active_exit_for_strategy("a") is None


# ── one bot's notice is its own ──────────────────────────────────────────


async def test_one_bots_notice_that_cannot_be_written_does_not_keep_the_other_bots_from_being_written(
    paper: PaperOptions, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    await _two_bots_each_hold_one_call(paper)
    _until(paper, CLOSE + 60_000)
    write = scheduled_close_module.raise_uncertainty

    def not_for_bot_a(repo: object, *, strategy_instance_id: str, **notice: object) -> str:
        if strategy_instance_id == "a":
            raise RuntimeError("this bot's notice cannot be written")
        return write(repo, strategy_instance_id=strategy_instance_id, **notice)  # type: ignore[arg-type]

    with monkeypatch.context() as broken:
        broken.setattr(scheduled_close_module, "raise_uncertainty", not_for_bot_a)
        done = await paper.round()
        # The pass runs the same step, and is not ended by it either.
        await paper.pass_()

    # Bot b's missed close is on record. Bot a's is not told as standing, and the next round writes it.
    assert done.closes.missed == (("b", SYMBOL),)
    assert _notice(paper, "a") is None and _notice(paper, "b") is not None
    again = await paper.round()
    assert again.closes.missed == (("a", SYMBOL), ("b", SYMBOL)) and _notice(paper, "a") is not None


async def test_one_bots_notice_that_cannot_be_ended_does_not_keep_the_other_bots_from_ending(
    paper: PaperOptions, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    await _two_bots_each_hold_one_call(paper)
    _until(paper, CLOSE + 60_000)
    await paper.round()
    # Each bot is stopped and its position written off, against a broker that holds what is left.
    for sid, broker_then_shows in (("a", _covering_read(SYMBOL, quantity=1.0)), ("b", _FakeReadPort())):
        submit_stop_run(paper.repo, account_id=paper.repo.account_id, strategy_instance_id=sid, lifecycle_run_id=f"run-{sid}")
        await discharge_attributed_residue(
            paper.repo, read=broker_then_shows, intake=paper.clerk.intake, strategy_instance_id=sid, symbol=SYMBOL,
            operator_reason="expired worthless",
        )
    end = scheduled_close_module._end_missed_notice

    def not_for_bot_a(repo: object, strategy_instance_id: str) -> bool:
        if strategy_instance_id == "a":
            raise RuntimeError("this bot's notice cannot be ended")
        return end(repo, strategy_instance_id)  # type: ignore[arg-type]

    with monkeypatch.context() as broken:
        broken.setattr(scheduled_close_module, "_end_missed_notice", not_for_bot_a)
        await paper.round()

    assert _notice(paper, "a") is not None and _notice(paper, "b") is None
    await paper.round()
    assert _notice(paper, "a") is None


def _losing_the_lease(paper: PaperOptions) -> Callable[..., object]:  # noqa: F811
    def loses_the_lease(*_args: object, **_kwargs: object) -> object:
        raise ExecutionLeaseLost("the Clerk's hold on its account was lost", account_id=paper.repo.account_id)

    return loses_the_lease


async def test_a_lost_hold_on_the_account_while_a_missed_close_is_written_is_raised_and_is_no_one_bots_failure(
    paper: PaperOptions, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    await _holds_one_call(paper)
    _until(paper, CLOSE + 60_000)
    monkeypatch.setattr(scheduled_close_module, "raise_uncertainty", _losing_the_lease(paper))

    # Nothing more can be written under a lost hold, so the round ends. It is not logged and passed over.
    with pytest.raises(ExecutionLeaseLost):
        await paper.round()
    assert _notice(paper) is None


async def test_a_lost_hold_on_the_account_while_a_missed_close_is_ended_is_raised_and_is_no_one_bots_failure(
    paper: PaperOptions, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    await _the_clerk_was_down_through_the_close(paper)
    submit_stop_run(paper.repo, account_id=paper.repo.account_id, strategy_instance_id="a", lifecycle_run_id="run-a")
    await discharge_attributed_residue(
        paper.repo, read=_FakeReadPort(), intake=paper.clerk.intake, strategy_instance_id="a", symbol=SYMBOL,
        operator_reason="expired worthless",
    )
    monkeypatch.setattr(scheduled_close_module, "_end_missed_notice", _losing_the_lease(paper))

    with pytest.raises(ExecutionLeaseLost):
        await paper.round()
    assert _notice(paper) is not None


# ── a structure with a sold leg: shown at this level only, until the Clerk takes one ──


def _with_its_leg_sold(entry: OptionEntryFacts) -> OptionEntryFacts:
    """The same entry with its one leg sold, not bought. No such entry can be made through the Clerk yet."""
    return entry.model_copy(update={"legs": (entry.the_leg.model_copy(update={"position": "short"}),)})


def _its_leg_is_sold(paper: PaperOptions, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    """Every holding is read as a sold leg's would be: its entry's leg sold, and its position below zero.

    No entry with a sold leg can be made through the Clerk yet, so both are
    written by hand over the two reads the close is worked out from. The
    books still hold the bought call, and that is what a close sells.
    """
    as_entered, as_held = scheduled_close_module.option_entry_of_order, reads.held_option_contracts
    monkeypatch.setattr(
        scheduled_close_module, "option_entry_of_order", lambda repo, ref: _with_its_leg_sold(as_entered(repo, ref))
    )
    monkeypatch.setattr(
        reads, "held_option_contracts",
        lambda conn: [position._replace(quantity=-position.quantity) for position in as_held(conn)],
    )


async def test_a_holding_whose_entry_has_a_sold_leg_owes_a_close_ten_minutes_before_every_sessions_close(
    paper: PaperOptions, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    await _holds_one_call(paper)
    _its_leg_is_sold(paper, monkeypatch)

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
    # An hour after the entry it has missed nothing: the session before it ended before it was held.
    [same_day] = held_contracts(paper.repo, et_minute_of_day_ms(date(2026, 9, 8), 13 * 60)).held
    assert same_day.missed is None and same_day.owed == owes_on(date(2026, 9, 8), closes_at=16)


async def test_under_a_missed_close_of_a_contract_that_still_trades_the_owners_flatten_is_offered(
    paper: PaperOptions, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """A sold leg held through a session's close, on a holding whose sold leg is written by hand."""
    await _holds_one_call(paper)
    _its_leg_is_sold(paper, monkeypatch)
    _until(paper, et_minute_of_day_ms(date(2026, 9, 9), 16 * 60) + 60_000)
    await paper.round()
    notice = _notice(paper)
    assert notice is not None and "expired" not in notice["explanation"]
    submit_stop_run(paper.repo, account_id=paper.repo.account_id, strategy_instance_id="a", lifecycle_run_id="run-a")

    _until(paper, et_minute_of_day_ms(date(2026, 9, 10), 10 * 60))
    flatten = await _the_flatten_offered(paper)

    # The notice is still open, and under it the owner can sell what the bot holds.
    assert _notice(paper) is not None
    assert flatten.available, (flatten.unavailable_reason_code, flatten.unavailable_reason)
    assert flatten.reduction_plan is not None
    assert [(leg.symbol, leg.quantity) for leg in flatten.reduction_plan.legs] == [(SYMBOL, 1.0)]


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


# ── the terms the close is counted from ──────────────────────────────────


async def test_the_close_is_counted_back_from_the_lead_the_owner_set_with_the_entry(paper: PaperOptions) -> None:  # noqa: F811
    ninety_minutes = replace(CloseTerms(), expiry_close_lead_ms=90 * 60_000)
    receipt = await paper.enter("a", evidence=paper.evidence(terms=ninety_minutes))
    [order_ref] = receipt.child_order_refs
    await paper.stream_fill(order_ref, contracts=1, filled_in_all=1, price="1.94", execution_id="x-buy")
    half_past_two = et_minute_of_day_ms(EXPIRY, 14 * 60 + 30)
    _until(paper, half_past_two - 1)

    early = await paper.round()
    _until(paper, half_past_two)
    done = await paper.round()

    assert early.closes.taken == () and len(done.closes.taken) == 1 and _closes_sent(paper) == [SELLS_ONE_TO_CLOSE]
    assert _accepted_as(paper, done.closes.taken[0]).the_scheduled_close == ScheduledCloseFacts(
        kind="expiry_day_close", due_ms=half_past_two, session_open_ms=OPEN
    )


async def test_an_entry_taken_before_entries_kept_their_terms_is_closed_at_the_starting_lead(paper: PaperOptions) -> None:  # noqa: F811
    before = OptionEntryFacts.from_facts(json.loads(_PINNED_OPTION))
    assert before.terms is None
    accepted = accept_option_enter(
        paper.repo, account_id=paper.repo.account_id, strategy_instance_id="a", decision_id="from-before",
        lifecycle_run_id="run-a", entry=before, envelope=paper.clerk.live_envelope,
    )
    await submit_accepted_enter(paper.repo, accepted=accepted, leg=before.order, trade=paper.venue)
    await paper.stream_fill(accepted.order_ref, contracts=2, filled_in_all=2, price="1.94", execution_id="x-buy")
    _until(paper, DUE - 1)

    early = await paper.round()
    _until(paper, DUE)
    await paper.round()

    # 45 minutes before the close, the lead an entry starts with, and both contracts it holds.
    assert early.closes.taken == ()
    [sent] = _closes_sent(paper)
    assert (sent.side, sent.intent, sent.quantity) == (OrderSide.SELL, OptionPositionIntent.CLOSE, 2)


# ── asked again in the run that takes the close ──────────────────────────


async def test_a_close_read_as_owed_is_not_taken_once_another_exit_of_the_bot_has_begun(paper: PaperOptions) -> None:  # noqa: F811
    await _holds_one_call(paper)
    _until(paper, DUE)
    [owed] = held_contracts(paper.repo, paper.clock()).held
    assert owed.to_be_taken(paper.clock())
    await paper.exit("a")

    assert _accept_while_still_owed(paper.repo, owed) is None
    assert len(_closing_refs(paper)) == 1


async def test_a_close_read_as_owed_is_not_taken_once_the_bot_holds_nothing(paper: PaperOptions) -> None:  # noqa: F811
    await _holds_one_call(paper)
    _until(paper, DUE)
    [owed] = held_contracts(paper.repo, paper.clock()).held
    await paper.exit("a")
    await _the_close_fills(paper)
    assert paper.held() == {} and paper.repo.active_exit_for_strategy("a") is None

    assert _accept_while_still_owed(paper.repo, owed) is None
    assert len(_closing_refs(paper)) == 1


async def test_a_close_read_as_owed_is_not_taken_once_the_clerk_has_stopped_exiting_the_bot(paper: PaperOptions) -> None:  # noqa: F811
    await _holds_one_call(paper)
    [owed] = held_contracts(paper.repo, DUE).held
    assert owed.to_be_taken(DUE) and not owed.left_to_owner
    await _the_clerk_gives_up_on_the_bots_own_exit(paper, holding=True)
    sent = len(paper.venue.option_submit_calls)
    _until(paper, DUE)

    assert _accept_while_still_owed(paper.repo, owed) is None
    assert len(paper.venue.option_submit_calls) == sent


# ── a close whose turn comes late: asked again whether it is still owed ───

FIVE_BOTS = ("a", "b", "c", "d", "e")


async def _five_bots_each_hold_one_call(paper: PaperOptions) -> None:  # noqa: F811
    """Three more bots are deployed beside ``a`` and ``b``, and each of the five buys one call."""
    for sid in FIVE_BOTS[2:]:
        paper.repo.register_strategy_instance(
            strategy_instance_id=sid, symbol="SPY", config_hash=f"seal-{sid}", exit_terms=TERMS
        )
        submit_budgeted_deploy(
            paper.repo, strategy_instance_id=sid, lifecycle_run_id=f"run-{sid}", world="real_paper",
            committed_cents=50_000, configuration_hash=f"seal-{sid}",
            exit_terms_hash=canonical_sha256(TERMS.model_dump(mode="json")), risk_revision=1, actor="owner",
            envelope=_gate(cash=10_000), minimum_position_cost=Decimal("100.01"),
        )
    for sid in FIVE_BOTS:
        # The account is read again after each fill, as its own cadence reads it, before the next bot enters.
        paper.wait(1_000)
        paper.account.account_observed_at_ms = paper.clock()
        assert await paper.sync.tick() == "observed"
        await _holds_one_call(paper, sid)


def _exit_taken(paper: PaperOptions, sid: str) -> dict | None:  # noqa: F811
    """The newest exit of the bot the Clerk accepted, of any kind, or None when it never accepted one."""
    return paper.repo.last_strategy_transition(strategy_instance_id=sid, transition_kind="EXIT_ACCEPTED")


async def _a_round_whose_fifth_close_waits_for_its_turn(
    paper: PaperOptions,  # noqa: F811
) -> tuple[asyncio.Task, str, asyncio.Event]:
    """A round begins with five closes due. Alpaca takes four orders and answers none, so the fifth waits.

    Returns the round, the bot whose close has not had its turn, and what
    makes Alpaca answer.
    """
    alpaca_answers = asyncio.Event()
    real_send = paper.venue.submit_option_order

    async def send_that_alpaca_is_slow_to_answer(order: object, *, client_order_id: str) -> object:
        sent = await real_send(order, client_order_id=client_order_id)  # type: ignore[arg-type]
        await alpaca_answers.wait()
        return sent

    paper.venue.submit_option_order = send_that_alpaca_is_slow_to_answer  # type: ignore[method-assign]
    round_ = asyncio.create_task(paper.round())
    four_are_out = await _comes_true(lambda: len(_closes_sent(paper)) == MAX_CLOSES_AT_ONCE)
    waiting = [sid for sid in FIVE_BOTS if _exit_taken(paper, sid) is None]
    if not four_are_out or len(waiting) != 1:
        # Judged only once Alpaca has answered, so that a failure leaves no round waiting.
        alpaca_answers.set()
        await round_
    assert four_are_out and len(waiting) == 1
    return round_, waiting[0], alpaca_answers


async def test_a_close_whose_turn_comes_once_its_session_has_closed_is_not_taken_and_is_recorded_as_missed(
    paper: PaperOptions,  # noqa: F811
) -> None:
    """Five closes are due at 15:15 and Alpaca answers none of the first four until four o'clock."""
    await _five_bots_each_hold_one_call(paper)
    _until(paper, DUE)
    round_, waiting, alpaca_answers = await _a_round_whose_fifth_close_waits_for_its_turn(paper)

    _until(paper, CLOSE)
    alpaca_answers.set()
    done = await asyncio.wait_for(round_, timeout=5)

    # Its turn came with the session closed. No close is owed any more: none is accepted, and no exit failed.
    assert len(done.closes.taken) == MAX_CLOSES_AT_ONCE and len(_closes_sent(paper)) == MAX_CLOSES_AT_ONCE
    assert _exit_taken(paper, waiting) is None and _closing_refs(paper, waiting) == []
    assert _notice(paper, waiting, EXIT_NOT_FLAT_REASON_CODE) is None

    after = await paper.round()

    # The next round finds a contract held past its close, as it finds any, and records the close as missed.
    assert (waiting, SYMBOL) in after.closes.missed and _notice(paper, waiting) is not None
    assert _exit_taken(paper, waiting) is None and _notice(paper, waiting, EXIT_NOT_FLAT_REASON_CODE) is None
    assert len(_closes_sent(paper)) == MAX_CLOSES_AT_ONCE


async def test_a_close_whose_turn_comes_a_millisecond_before_its_session_closes_is_taken(paper: PaperOptions) -> None:  # noqa: F811
    await _five_bots_each_hold_one_call(paper)
    _until(paper, DUE)
    round_, waiting, alpaca_answers = await _a_round_whose_fifth_close_waits_for_its_turn(paper)

    _until(paper, CLOSE - 1)
    alpaca_answers.set()
    done = await asyncio.wait_for(round_, timeout=5)

    # The session is still open, so the close is still owed: it is accepted, as that session's close. What
    # becomes of a close taken in a session's last seconds is the rule of every option close, not asked here.
    assert len(done.closes.taken) == 5
    [its_close] = [effect for effect in done.closes.taken if effect.startswith(f"effect:{waiting}:")]
    assert _accepted_as(paper, its_close).the_scheduled_close == ScheduledCloseFacts(
        kind="expiry_day_close", due_ms=DUE, session_open_ms=OPEN
    )


async def test_a_sold_legs_close_whose_turn_comes_at_the_next_open_is_not_taken_and_that_days_close_is_owed_anew(
    paper: PaperOptions, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """Five holdings with a sold leg, each written by hand, owe a close at 15:50 on Wednesday. Alpaca answers
    none of the first four until Thursday's open."""
    await _five_bots_each_hold_one_call(paper)
    _its_leg_is_sold(paper, monkeypatch)
    wednesday, thursday = date(2026, 9, 9), date(2026, 9, 10)
    _until(paper, et_minute_of_day_ms(wednesday, 15 * 60 + 50))
    round_, waiting, alpaca_answers = await _a_round_whose_fifth_close_waits_for_its_turn(paper)

    thursdays_open = et_minute_of_day_ms(thursday, 9 * 60 + 30)
    _until(paper, thursdays_open)
    alpaca_answers.set()
    done = await asyncio.wait_for(round_, timeout=5)

    # Wednesday's decision is not taken on Thursday morning: nothing is accepted for the bot and nothing is sent.
    assert len(done.closes.taken) == MAX_CLOSES_AT_ONCE and len(_closes_sent(paper)) == MAX_CLOSES_AT_ONCE
    assert _exit_taken(paper, waiting) is None and _closing_refs(paper, waiting) == []

    at_the_open = await paper.round()

    # Wednesday's close is recorded as missed, and Thursday's is not due until ten to four.
    assert (waiting, SYMBOL) in at_the_open.closes.missed and _notice(paper, waiting) is not None
    assert _exit_taken(paper, waiting) is None
    thursdays_due = et_minute_of_day_ms(thursday, 15 * 60 + 50)
    _until(paper, thursdays_due - 1)
    early = await paper.round()
    taken_early = _exit_taken(paper, waiting)
    _until(paper, thursdays_due)
    on_time = await paper.round()

    # Thursday's close is owed anew, and is taken at its own time as Thursday's decision.
    assert early.closes.taken == () and taken_early is None
    [its_close] = [effect for effect in on_time.closes.taken if effect.startswith(f"effect:{waiting}:")]
    accepted = _accepted_as(paper, its_close)
    assert accepted.decision_id.startswith("scheduled-close-20260910-")
    assert accepted.the_scheduled_close == ScheduledCloseFacts(
        kind="sold_leg_close", due_ms=thursdays_due, session_open_ms=thursdays_open
    )
    assert len(_closing_refs(paper, waiting)) == 1


async def test_a_contract_no_longer_held_when_the_notice_is_written_is_not_named_in_it(paper: PaperOptions) -> None:  # noqa: F811
    """The books are read, then the position is written off, then the notice's turn comes."""
    await _the_clerk_was_down_through_the_close(paper)
    submit_stop_run(paper.repo, account_id=paper.repo.account_id, strategy_instance_id="a", lifecycle_run_id="run-a")
    read = held_contracts(paper.repo, paper.clock())
    assert read.held[0].missed is not None and read.noticed == ("a",)
    await discharge_attributed_residue(
        paper.repo, read=_FakeReadPort(), intake=paper.clerk.intake, strategy_instance_id="a", symbol=SYMBOL,
        operator_reason="expired worthless",
    )

    named = _record_missed_closes(paper.repo, read, paper.clock())

    assert named == () and _notice(paper) is None


@pytest.mark.parametrize(
    "its_facts", [pytest.param("raise", id="cannot be read back"), pytest.param("none", id="are not an option entry's")]
)
async def test_a_bot_whose_entry_cannot_be_read_keeps_its_notice_and_the_other_bot_is_still_served(
    paper: PaperOptions, monkeypatch: pytest.MonkeyPatch, its_facts: str  # noqa: F811
) -> None:
    unreadable = await _holds_one_call(paper, "a")
    paper.wait(1_000)
    paper.account.account_observed_at_ms = paper.clock()
    assert await paper.sync.tick() == "observed"
    await _holds_one_call(paper, "b")
    _until(paper, MONDAY_AFTER_THE_OPEN)
    await paper.round()
    assert _notice(paper, "a") is not None and _notice(paper, "b") is not None
    as_kept = scheduled_close_module.option_entry_of_order

    def cannot_read_a(repo: object, order_ref: str) -> object:
        if order_ref != unreadable:
            return as_kept(repo, order_ref)
        if its_facts == "raise":
            raise ValueError("this entry's facts cannot be read back")
        return None

    monkeypatch.setattr(scheduled_close_module, "option_entry_of_order", cannot_read_a)
    written = paper.repo.last_custody_sequence()

    done = await paper.round()

    # Nothing is concluded about a: its notice stands. b is read and told as before, and nothing is written.
    assert held_contracts(paper.repo, paper.clock()).unread == frozenset({"a"})
    assert done.closes.missed == (("b", SYMBOL),)
    assert _notice(paper, "a") is not None and paper.repo.last_custody_sequence() == written
