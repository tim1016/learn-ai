"""The owner ends an expired option contract that nothing will ever settle (#2913).

A Dry Run that kept no price of the underlying at the close cannot settle a
contract, and no price comes by itself. A real account's broker may hold none
of a contract and report nothing of how it ended. Either way the position
would stay on the books for ever. So the owner can end it at their word: the
lot closes at nothing, and what the contract cost is the loss.

The Dry Run here is the real SQLite Clerk of a ``sim:`` account with its real
simulated broker. The real account is the Paper harness: the real Clerk, with
its venue and its account read as doubles. The action is pressed as the bot's
page presses it. Nothing was sent to Alpaca or recorded from it.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.broker.alpaca.clerk.active_authority import close_synthetic_clerk_runtimes, get_clerk_runtime
from app.broker.alpaca.clerk.simulated_option_settlement import OwnersWord
from app.broker.alpaca.clerk.sqlite import option_events
from app.broker.alpaca.clerk.sqlite.off_loop import run_inline
from app.broker.alpaca.clerk.sqlite.option_event_books import recorded_option_endings
from app.broker.alpaca.clerk.sqlite.option_events import OwnerEndingRefused, poll_option_events
from app.broker.alpaca.clerk.sqlite.option_owner_ending import (
    END_EXPIRED_OPTION_LABEL,
    OWNER_ENDING_LATE_ROW_DAYS,
    OWNER_ENDING_WAIT_SESSIONS,
    OwnerEndingRequest,
    owner_ending_offered_from_ms,
)
from app.broker.alpaca.clerk.sqlite.projection_models import RecoveryCapability
from app.broker.alpaca.clerk.sqlite.projections import read_recovery_context
from app.broker.alpaca.clerk.sqlite.recovery_execution import (
    RecoveryExecutionError,
    RecoveryExecutionRequest,
    RecoveryExecutionResult,
    execute_recovery_action,
)
from app.broker.alpaca.clerk.sqlite.recovery_policy import (
    RecoveryActionUnavailableError,
    RecoveryPolicyContext,
    StaleRecoveryTokenError,
    build_recovery_catalog,
)
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.simulated_account import SimulationEvidenceUnavailable
from app.broker.contract.errors import BrokerUnavailable
from app.schemas.option_endings import OptionEndingView
from app.services.option_ending_views import option_ending_view
from app.services.session_authority import et_minute_of_day_ms
from app.utils.session_anchors import et_midnight_ms
from tests.broker.alpaca.clerk.sqlite.conftest import _FakeReadPort, documented_option_activity
from tests.broker.alpaca.clerk.sqlite.option_event_fixtures import (
    bookings,
    books_at,
    broker_holds,
    fifo,
    held,
    holds_option,
    later,
    option_holds,
    poll,
)
from tests.broker.alpaca.clerk.sqlite.resting_entry_harness import (  # noqa: F401
    PaperOptions,
    paper,
)
from tests.broker.alpaca.clerk.sqlite.test_dry_run_option_expiry import _booked, _budget, _fees, _keeps_spy
from tests.broker.alpaca.clerk.sqlite.test_option_close_fences import (
    EXPIRY_CLOSE,
    MONDAY_NOON,
    SATURDAY_NOON,
    _notice,
    _page_notices,
    _passes,
    _stopped_on_thursday_with_nothing_bid,
    _wait_until,
)
from tests.broker.alpaca.clerk.sqlite.test_option_entry_and_exit import (  # noqa: F401
    SID,
    SYMBOL,
    _DryRun,
    _open,
    dry_run,
)
from tests.broker.alpaca.clerk.sqlite.test_option_events_on_an_account import _expiry_row, _holding_one_call

END = "end_expired_option"
MINUTE = 60_000
DAY = 86_400_000
# The contract's last session is Friday 2026-09-18. The session after it is Monday the 21st.
MONDAY_CLOSE = et_minute_of_day_ms(date(2026, 9, 21), 16 * 60)


async def _context(clerk: Any, sid: str) -> RecoveryPolicyContext:
    context = await read_recovery_context(clerk, strategy_instance_id=sid)
    assert context is not None
    return context


async def _offered(clerk: Any, sid: str, action_id: str = END) -> RecoveryCapability:
    """One action of the bot's recovery catalog, read the way the bot's page reads it."""
    return next(item for item in build_recovery_catalog(await _context(clerk, sid)) if item.action_id == action_id)


async def _press(
    clerk: Any, sid: str, *, shown: RecoveryCapability | None = None, operator: str | None = "the-owner"
) -> RecoveryExecutionResult:
    """The owner presses "End the expired contract" as the bot's page shows it. Who they are comes from the session."""
    presented = shown or await _offered(clerk, sid)

    async def current_context() -> RecoveryPolicyContext:
        return await _context(clerk, sid)

    return await execute_recovery_action(
        clerk,
        request=RecoveryExecutionRequest(
            action_id=END, concurrency_token=presented.concurrency_token, execution_ref=presented.execution_ref,
            reason="nothing will settle it", operator=operator,
        ),
        current_context=current_context,
    )


# ── a Dry Run ────────────────────────────────────────────────────────────


async def _stopped_before_the_last_session_with_no_price_kept(account: _DryRun) -> None:
    """The usual road: stopped on Thursday with nothing bid, expired on Friday, and no bar of Friday kept,
    because only a running bot keeps bars. On Monday the Clerk has passed over it twice."""
    await _stopped_on_thursday_with_nothing_bid(account)
    _wait_until(account, MONDAY_NOON)
    await _passes(account, 2)
    # The pass also records the scheduled close the contract's last session ended on (ADR 0076 decision 20).
    assert set(await _page_notices(account)) == {
        "EXIT_NOT_FLAT", "OPTION_EXPIRED_NOT_SETTLED", "SCHEDULED_CLOSE_MISSED"
    }


async def test_the_notice_of_a_contract_a_dry_run_cannot_settle_promises_no_price_and_says_what_the_owner_can_do(
    dry_run: _DryRun,  # noqa: F811
) -> None:
    await _stopped_before_the_last_session_with_no_price_kept(dry_run)

    notice = (await _page_notices(dry_run))["OPTION_EXPIRED_NOT_SETTLED"]

    said = f"{notice.explanation} {notice.next_step}".lower()
    # No end is promised that cannot come: no price will be kept by itself.
    assert "until one is kept" not in said and "is kept" not in said
    assert "no price will come by itself" in said and "end the contract at your word" in said


async def test_a_dry_run_contract_with_no_price_is_ended_at_the_owners_word_and_what_it_cost_is_the_loss(
    dry_run: _DryRun,  # noqa: F811
) -> None:
    """One call bought at $1.94: $194.00. Ended at the owner's word it is counted as worth nothing.

    Realised: -$194.00. No fee for the ending: the six cents are the
    entry's. Balance: 1,000.00 - 194.00 - 0.06 = $805.94, nothing in
    position. It is not a write-off, which would leave the $194.00 "in
    position" for good.
    """
    await _stopped_before_the_last_session_with_no_price_kept(dry_run)
    with pytest.raises(SimulationEvidenceUnavailable):
        dry_run.account_reading()
    shown = await _offered(dry_run.clerk, SID)
    assert shown.available and shown.execution_ref == SYMBOL

    ended = await _press(dry_run.clerk, SID, shown=shown)

    assert ended.applied
    assert dry_run.held() == {} and dry_run.realized_pnl() == Decimal("-194")
    [booked] = _booked(dry_run)
    assert (booked["summary_code"], booked["ended_by"], booked["close_price"]) == (
        "OPTION_ENDED_BY_OWNER", "ended_by_owner", "0"
    )
    assert booked["simulated"] is True and "shares" not in booked
    # Who, when, and why no settlement could be made.
    word = booked["owner_word"]
    assert (word["operator"], word["reason"], word["said_at_ms"]) == ("the-owner", "nothing will settle it", dry_run.clock())
    assert "SPY" in word["why_no_settlement"] and "last session" in word["why_no_settlement"]
    assert _fees(dry_run) == Decimal("0.06")
    budget = _budget(dry_run)
    assert (budget.realized_gross, budget.position_cost, budget.balance) == (Decimal("-194"), 0, Decimal("805.94"))
    # The simulation let go of it too, the hold is over, and the account reads once more.
    assert await dry_run.clerk._read.list_positions() == []
    assert _notice(dry_run, "OPTION_EXPIRED_NOT_SETTLED") is None
    assert dry_run.account_reading().equity_usd == Decimal("805.94")
    await _passes(dry_run, 1)
    assert await _page_notices(dry_run) == {}


async def test_a_running_dry_run_whose_feed_was_down_all_of_the_last_session_can_be_ended_too(dry_run: _DryRun) -> None:  # noqa: F811
    """No stop and no failed close: the bot is running and simply kept no bar that day. The ending asks for
    neither a stopped run nor a notice that the bot is not flat."""
    await dry_run.enter(dry_run.evidence())
    _wait_until(dry_run, SATURDAY_NOON)
    await dry_run.clerk.reconcile_once()
    # Its scheduled close was never taken either: that is the other notice (ADR 0076 decision 20).
    assert set(await _page_notices(dry_run)) == {"OPTION_EXPIRED_NOT_SETTLED", "SCHEDULED_CLOSE_MISSED"}
    assert dry_run.repo.active_run(SID) is not None

    await _press(dry_run.clerk, SID)

    assert dry_run.held() == {}
    assert [booked["summary_code"] for booked in _booked(dry_run)] == ["OPTION_ENDED_BY_OWNER"]


async def test_a_contract_whose_only_price_is_from_too_long_before_the_close_is_ended_and_the_record_says_how_old(
    dry_run: _DryRun,  # noqa: F811
) -> None:
    """The newest bar of the last session ended at 09:35, 385 minutes before the close. That is no price at the
    close, so nothing settles the contract. The owner ends it, and the record keeps why."""
    await dry_run.enter(dry_run.evidence())
    _keeps_spy(dry_run, "590.25", end_ms=et_minute_of_day_ms(date(2026, 9, 18), 9 * 60 + 35))
    _wait_until(dry_run, SATURDAY_NOON)
    await dry_run.clerk.reconcile_once()

    await _press(dry_run.clerk, SID)

    [booked] = _booked(dry_run)
    assert booked["summary_code"] == "OPTION_ENDED_BY_OWNER"
    assert "385 minutes before the close" in booked["owner_word"]["why_no_settlement"]
    assert dry_run.realized_pnl() == Decimal("-194")


async def test_an_ending_is_not_offered_before_the_simulation_has_said_it_has_no_price(dry_run: _DryRun) -> None:  # noqa: F811
    """The contract expired a day ago and nothing has yet asked the simulation. It may still settle it."""
    await dry_run.enter(dry_run.evidence())
    # The Clerk's own task would ask the simulation within a round. Stopped, so that nothing has asked yet.
    runtime = get_clerk_runtime(dry_run.repo.account_id)
    assert runtime is not None and runtime.deadline_keeper is not None
    await runtime.deadline_keeper.stop()
    _wait_until(dry_run, SATURDAY_NOON)

    before_any_read = await _offered(dry_run.clerk, SID)

    assert not before_any_read.available
    assert before_any_read.unavailable_reason_code == "EXPIRED_OPTION_CONTRACT_MAY_STILL_BE_SETTLED"
    with pytest.raises(RecoveryActionUnavailableError):
        await _press(dry_run.clerk, SID, shown=before_any_read)
    assert _booked(dry_run) == []


async def test_a_contract_that_still_trades_or_was_settled_has_nothing_to_end(dry_run: _DryRun) -> None:  # noqa: F811
    await dry_run.enter(dry_run.evidence())
    while_it_trades = await _offered(dry_run.clerk, SID)
    _keeps_spy(dry_run, "590.25")
    _wait_until(dry_run, SATURDAY_NOON)
    await dry_run.clerk.reconcile_once()

    once_settled = await _offered(dry_run.clerk, SID)

    for shown in (while_it_trades, once_settled):
        assert not shown.available and not shown.needed
        assert shown.unavailable_reason_code == "NO_EXPIRED_OPTION_CONTRACT"


async def test_at_the_press_a_price_that_has_come_since_settles_the_contract_and_the_owner_is_told_that_instead(
    dry_run: _DryRun,  # noqa: F811
) -> None:
    """The page offered the ending while the simulation had no price. A start's history then brought the bar of
    the close, 590.25. The press asks the simulation once more: the contract is sold at 5.25 a share by the
    rule, $331.00 made, and nothing is ended at the owner's word."""
    await dry_run.enter(dry_run.evidence())
    _wait_until(dry_run, SATURDAY_NOON)
    await dry_run.clerk.reconcile_once()
    shown = await _offered(dry_run.clerk, SID)
    assert shown.available
    _keeps_spy(dry_run, "590.25")

    with pytest.raises(RecoveryExecutionError) as told:
        await _press(dry_run.clerk, SID, shown=shown)

    said = str(told.value)
    assert "not ended at your word" in said and "settled it in cash" in said
    [booked] = _booked(dry_run)
    assert (booked["summary_code"], booked["close_price"]) == ("OPTION_SETTLED_IN_CASH", "5.25")
    assert "owner_word" not in booked
    assert dry_run.held() == {} and dry_run.realized_pnl() == Decimal("331")


async def test_an_ending_pressed_twice_is_booked_once(dry_run: _DryRun) -> None:  # noqa: F811
    await _stopped_before_the_last_session_with_no_price_kept(dry_run)
    shown = await _offered(dry_run.clerk, SID)
    first = await _press(dry_run.clerk, SID, shown=shown)

    # The page's second press, from the same screen: what it was shown is no longer so.
    with pytest.raises(StaleRecoveryTokenError):
        await _press(dry_run.clerk, SID, shown=shown)
    # And from a fresh screen the action is no longer there to press.
    with pytest.raises(RecoveryActionUnavailableError):
        await _press(dry_run.clerk, SID)
    # And the Clerk asked twice directly, as a retry after a lost answer: the same record, not a second.
    again = await dry_run.clerk.end_expired_option(
        strategy_instance_id=SID, symbol=SYMBOL, operator="the-owner", reason="nothing will settle it"
    )

    assert f"transition:{again.sequence}" == first.receipt_id
    assert [booked["summary_code"] for booked in _booked(dry_run)] == ["OPTION_ENDED_BY_OWNER"]
    assert dry_run.realized_pnl() == Decimal("-194")


async def test_a_stop_between_the_simulation_letting_go_and_the_booking_books_the_ending_once(dry_run: _DryRun) -> None:  # noqa: F811
    """The simulation lets go of the contract in its own ledger first. The process stops before the Clerk has
    booked anything. The next Clerk is handed the same ending, with the owner's word, and books it once."""
    await _stopped_before_the_last_session_with_no_price_kept(dry_run)
    word = OwnersWord(operator="the-owner", reason="nothing will settle it", said_at_ms=dry_run.clock())
    dry_run.clerk._trade.settle_expired_options(after_ms=0, at_the_owners_word={SYMBOL: word})
    assert _booked(dry_run) == [] and dry_run.held() == {SYMBOL: 1.0}
    assert await dry_run.clerk._read.list_positions() == []

    for _restart in range(2):
        await close_synthetic_clerk_runtimes()
        reopened = await _open(dry_run.artifacts_root, dry_run.clock, dry_run.binding)
        await reopened.clerk.reconcile_once()
        reopened.wait(option_events.OPTION_EVENT_POLL_INTERVAL_MS)
        await reopened.clerk.reconcile_once()

        [booked] = _booked(reopened)
        assert booked["summary_code"] == "OPTION_ENDED_BY_OWNER"
        assert booked["owner_word"]["operator"] == "the-owner"
        assert reopened.held() == {} and reopened.realized_pnl() == Decimal("-194")


async def test_a_price_kept_after_an_ending_changes_nothing(dry_run: _DryRun) -> None:  # noqa: F811
    """The bar of the close turns up after the owner ended the contract: 590.25, which would have paid $525.00.
    The simulation holds nothing to settle, under the id a settlement would have used. Nothing is booked twice."""
    await _stopped_before_the_last_session_with_no_price_kept(dry_run)
    await _press(dry_run.clerk, SID)
    written = dry_run.repo.last_custody_sequence()
    _keeps_spy(dry_run, "590.25")

    dry_run.wait(option_events.OPTION_EVENT_POLL_INTERVAL_MS)
    settled = dry_run.clerk._trade.settle_expired_options(after_ms=0)
    await dry_run.clerk.reconcile_once()

    assert [item.record.outcome.value for item in settled.settled] == ["ended_by_owner"]
    assert [booked["summary_code"] for booked in _booked(dry_run)] == ["OPTION_ENDED_BY_OWNER"]
    assert dry_run.realized_pnl() == Decimal("-194") and dry_run.held() == {}
    with dry_run.repo.write_fence() as conn:
        later_bookings = conn.execute(
            "SELECT COUNT(*) FROM custody_transitions WHERE transition_kind = 'OPTION_POSITION_ENDED' AND sequence > ?",
            (written,),
        ).fetchone()[0]
    assert later_bookings == 0


async def test_an_ending_that_names_nobody_is_refused(dry_run: _DryRun) -> None:  # noqa: F811
    await _stopped_before_the_last_session_with_no_price_kept(dry_run)

    with pytest.raises(RecoveryExecutionError, match="names no operator"):
        await _press(dry_run.clerk, SID, operator=None)

    assert _booked(dry_run) == [] and dry_run.held() == {SYMBOL: 1.0}


# ── a real account ───────────────────────────────────────────────────────


def _at(account: PaperOptions, at_ms: int) -> None:
    assert at_ms >= account.clock()
    account.clock.value = at_ms
    account.repo.revive_execution_lease()


async def _gone_at_the_broker_with_no_record(account: PaperOptions, sid: str = "a") -> None:
    """The bot holds one call. On Saturday the broker holds none of it and has published nothing about it."""
    await _holding_one_call(account, sid)
    _at(account, max(account.clock(), SATURDAY_NOON))
    account.account.settled.add(SYMBOL)
    await account.pass_()
    assert (sid, "OPTION_EXPIRED_NOT_SETTLED") in option_holds(account.repo)


def test_a_real_accounts_ending_waits_one_full_session_after_the_contracts_last() -> None:
    """Friday's contract: the record is waited for until Monday's close. A Thursday before a Friday half day:
    until that half day's early close."""
    assert (OWNER_ENDING_WAIT_SESSIONS, OWNER_ENDING_LATE_ROW_DAYS) == (1, 7)
    assert owner_ending_offered_from_ms(date(2026, 9, 18)) == MONDAY_CLOSE
    assert owner_ending_offered_from_ms(date(2026, 11, 25)) == et_minute_of_day_ms(date(2026, 11, 27), 13 * 60)


async def test_on_a_real_account_the_ending_is_offered_only_once_the_next_session_has_closed(
    paper: PaperOptions,  # noqa: F811
) -> None:
    """Paper publishes an expiry the next morning. Until a full session has gone by, the record may still come."""
    await _gone_at_the_broker_with_no_record(paper)

    on_saturday = await _offered(paper.clerk, "a")
    _at(paper, MONDAY_CLOSE - 1)
    a_millisecond_before = await _offered(paper.clerk, "a")
    _at(paper, MONDAY_CLOSE)
    at_the_close = await _offered(paper.clerk, "a")

    for early in (on_saturday, a_millisecond_before):
        assert not early.available and early.needed
        assert early.unavailable_reason_code == "EXPIRED_OPTION_CONTRACT_RECORD_MAY_STILL_COME"
        assert "Mon Sep 21, 16:00 ET" in (early.unavailable_reason or "")
    assert at_the_close.available and at_the_close.execution_ref == SYMBOL


async def test_on_a_real_account_the_owner_ends_a_contract_no_record_came_for_and_what_it_cost_is_the_loss(
    paper: PaperOptions,  # noqa: F811
) -> None:
    """One call bought at $1.94. The broker holds none of it and a full session later has said nothing of how
    it ended. Ended at the owner's word: -$194.00 realised, the lot closed, and the account agrees once more."""
    await _gone_at_the_broker_with_no_record(paper)
    _at(paper, MONDAY_CLOSE)

    ended = await _press(paper.clerk, "a")

    assert ended.applied and paper.held() == {}
    assert fifo(paper.repo, "a").exact_realized_pnl == Decimal("-194")
    assert fifo(paper.repo, "a").open_lots == []
    [booked] = bookings(paper.repo)
    facts = json.loads(booked["facts_json"])
    assert (booked["strategy_instance_id"], booked["summary_code"]) == ("a", "OPTION_ENDED_BY_OWNER")
    assert (facts["simulated"], facts["close_price"], facts["contracts"]) == (False, "0", 1)
    assert "simulation" not in facts and "shares" not in facts
    assert (facts["owner_word"]["operator"], facts["owner_word"]["said_at_ms"]) == ("the-owner", MONDAY_CLOSE)
    assert "no record" in facts["owner_word"]["why_no_settlement"]
    # Counted in the contract's own last session, as an expiry is, and dated its expiry date.
    assert facts["booked_as_of_ms"] == EXPIRY_CLOSE - 1
    assert facts["event_date_ms"] == et_midnight_ms(date(2026, 9, 18))
    # No fee is priced for it: it is no trade.
    with paper.repo.write_fence() as conn:
        assert conn.execute("SELECT fill_id FROM fills WHERE fill_id LIKE 'option-event:close:%'").fetchone()
    assert option_holds(paper.repo) == {}
    assert (await paper.pass_()).verdict == "clean"


async def test_at_the_press_a_record_that_has_come_is_booked_in_the_endings_place_and_the_owner_is_told(
    paper: PaperOptions,  # noqa: F811
) -> None:
    await _gone_at_the_broker_with_no_record(paper)
    _at(paper, MONDAY_CLOSE)
    shown = await _offered(paper.clerk, "a")
    paper.account.option_events.rows = [_expiry_row()]

    with pytest.raises(RecoveryExecutionError) as told:
        await _press(paper.clerk, "a", shown=shown)

    assert "not ended at your word" in str(told.value) and "expired" in str(told.value)
    assert [row["summary_code"] for row in bookings(paper.repo)] == ["OPTION_EXPIRED"]
    assert paper.held() == {} and fifo(paper.repo, "a").exact_realized_pnl == Decimal("-194")


@pytest.mark.parametrize(
    ("at_the_broker", "refused_for"),
    [
        pytest.param("holds_it", "still shows 1", id="the broker holds the contract after all"),
        pytest.param("read_fails", "could not be read whole", id="the option events cannot be read"),
        pytest.param("read_cut_short", "could not be read whole", id="the walk of the events is cut short"),
        pytest.param("positions_fail", "positions could not be read", id="the positions cannot be read"),
        pytest.param("share_trade", "may be", id="a share trade in the underlying that may be its exercise"),
        pytest.param("row_not_executed", "may be", id="a record for the contract that is not reported as executed"),
    ],
)
async def test_at_the_press_the_clerk_looks_once_more_and_ends_nothing_it_cannot_prove_has_no_settlement(
    paper: PaperOptions, monkeypatch: pytest.MonkeyPatch, at_the_broker: str, refused_for: str  # noqa: F811
) -> None:
    await _gone_at_the_broker_with_no_record(paper)
    _at(paper, MONDAY_CLOSE)
    shown = await _offered(paper.clerk, "a")
    if at_the_broker == "holds_it":
        paper.account.settled.clear()
    elif at_the_broker == "read_fails":
        paper.account.option_events.error = BrokerUnavailable("Alpaca is not answering.", broker="alpaca")
    elif at_the_broker == "read_cut_short":
        paper.account.option_events.cut_short = True
    elif at_the_broker == "positions_fail":
        async def no_positions() -> Any:
            raise BrokerUnavailable("Alpaca is not answering.", broker="alpaca")

        monkeypatch.setattr(paper.account, "list_positions", no_positions)
    elif at_the_broker == "share_trade":
        paper.account.option_events.rows = [documented_option_activity(
            "trading_api", "option_exercise", 1, id="trd-1", symbol="SPY", date="2026-09-18", qty="100", price="585",
            net_amount="-58500",
        )]
    else:
        paper.account.option_events.rows = [_expiry_row(status="canceled")]

    with pytest.raises(RecoveryExecutionError, match=refused_for) as refused:
        await _press(paper.clerk, "a", shown=shown)

    assert "Nothing was ended" in str(refused.value)
    assert bookings(paper.repo) == [] and paper.held() == {SYMBOL: 1.0}


async def test_two_bots_hold_the_contract_and_the_owner_ends_each_bots_own(paper: PaperOptions) -> None:  # noqa: F811
    """Each bot is ended by name. Ending one closes that bot's lot and leaves the other's, and its hold, alone."""
    resting = {sid: await paper.resting(sid) for sid in ("a", "b")}
    for sid, order_ref in resting.items():
        await paper.stream_fill(order_ref, contracts=1, filled_in_all=1, price="1.94", execution_id=f"x-{sid}-buy")
    _at(paper, SATURDAY_NOON)
    paper.account.settled.add(SYMBOL)
    await paper.pass_()
    assert set(option_holds(paper.repo)) == {("a", "OPTION_EXPIRED_NOT_SETTLED"), ("b", "OPTION_EXPIRED_NOT_SETTLED")}
    _at(paper, MONDAY_CLOSE)

    await _press(paper.clerk, "a")

    assert paper.held("a") == {} and paper.held("b") == {SYMBOL: 1.0}
    assert set(option_holds(paper.repo)) == {("b", "OPTION_EXPIRED_NOT_SETTLED")}

    await _press(paper.clerk, "b")

    assert paper.held("b") == {}
    assert [(row["strategy_instance_id"], row["summary_code"]) for row in bookings(paper.repo)] == [
        ("a", "OPTION_ENDED_BY_OWNER"), ("b", "OPTION_ENDED_BY_OWNER")
    ]
    assert option_holds(paper.repo) == {}


async def test_the_write_off_is_never_offered_for_an_expired_contract_on_a_real_account(paper: PaperOptions) -> None:  # noqa: F811
    """A write-off would take the position off the books and leave its lot open: the $194.00 would never be a
    loss. The owner's ending is what is offered in its place."""
    await _gone_at_the_broker_with_no_record(paper)
    _at(paper, MONDAY_CLOSE)

    write_off = await _offered(paper.clerk, "a", "discharge_attributed_residue")

    assert not write_off.available and not write_off.needed
    assert write_off.unavailable_reason_code == "EXPIRED_OPTION_CONTRACT_IS_NOT_WRITTEN_OFF"
    assert "written off" in (write_off.unavailable_reason or "")
    assert (await _offered(paper.clerk, "a")).available


# ── a record that comes after the owner's ending ─────────────────────────


async def _ended_at_the_owners_word(account: PaperOptions) -> int:
    """The owner ended bot a's call on Monday's close. How often Alpaca had been asked for option events by then."""
    await _gone_at_the_broker_with_no_record(account)
    _at(account, MONDAY_CLOSE)
    await _press(account.clerk, "a")
    assert account.held() == {}
    return len(account.account.option_events.reads)


async def test_after_an_ending_the_accounts_option_events_are_still_read_back_to_the_contracts_expiry_date(
    paper: PaperOptions,  # noqa: F811
) -> None:
    """No bot holds an option any more and no hold is open. A late record must still be read, so the read goes
    on for some days, and its window still starts on the contract's expiry date."""
    asked = await _ended_at_the_owners_word(paper)

    paper.wait(option_events.OPTION_EVENT_POLL_INTERVAL_MS)
    await paper.pass_()

    reads = paper.account.option_events.reads
    assert len(reads) == asked + 1
    assert reads[-1] <= et_midnight_ms(date(2026, 9, 18))

    _at(paper, MONDAY_CLOSE + OWNER_ENDING_LATE_ROW_DAYS * DAY + MINUTE)
    await paper.pass_()
    assert len(paper.account.option_events.reads) == asked + 1, "the read went on after the days it is kept up for"


async def test_an_expiry_read_after_an_ending_books_nothing_and_holds_nobody(paper: PaperOptions) -> None:  # noqa: F811
    """The record says what the owner's ending already booked: the contract ended worth nothing."""
    await _ended_at_the_owners_word(paper)
    paper.account.option_events.rows = [_expiry_row()]

    paper.wait(option_events.OPTION_EVENT_POLL_INTERVAL_MS)
    result = await paper.pass_()

    assert [row["summary_code"] for row in bookings(paper.repo)] == ["OPTION_ENDED_BY_OWNER"]
    assert option_holds(paper.repo) == {} and result.verdict == "clean"
    assert fifo(paper.repo, "a").exact_realized_pnl == Decimal("-194")


async def test_an_exercise_read_after_an_ending_books_nothing_and_holds_that_bot_by_name(paper: PaperOptions) -> None:  # noqa: F811
    """The contract the owner ended as worth nothing was in fact exercised: the account holds 100 shares.

    The lot is closed, so nothing is booked a second time. The shares are
    real and on no bot's books, and the bot that held the contract is told
    so by name. The other bot is not held.
    """
    await _ended_at_the_owners_word(paper)
    paper.account.shares["SPY"] = 100.0
    paper.account.option_events.rows = [
        documented_option_activity("trading_api", "option_exercise", id="exc-1", symbol=SYMBOL, date="2026-09-18", qty="-1"),
        documented_option_activity(
            "trading_api", "option_exercise", 1, id="trd-1", symbol="SPY", date="2026-09-18", qty="100", price="585",
            net_amount="-58500",
        ),
    ]

    paper.wait(option_events.OPTION_EVENT_POLL_INTERVAL_MS)
    await paper.pass_()

    assert [row["summary_code"] for row in bookings(paper.repo)] == ["OPTION_ENDED_BY_OWNER"]
    assert paper.held("a") == {}
    holds = option_holds(paper.repo)
    assert ("a", "OPTION_EVENT_CONTRACT_NOT_HELD") in holds
    said = json.loads(holds[("a", "OPTION_EVENT_CONTRACT_NOT_HELD")]["facts_json"])["explanation"]
    assert "at the owner's word" in said and "exercised" in said and "on no bot's books" in said
    assert not any(bot in (None, "b") for bot, _reason in holds)


async def test_a_record_for_both_bots_that_comes_after_one_was_ended_books_the_other_bots_expiry(
    paper: PaperOptions,  # noqa: F811
) -> None:
    """Two bots held the call. The owner ended bot a's at their word. The record that comes late is for both.

    Bot b's lot is still on the books and the expiry closes it: $194.00
    lost, and the account agrees once more. Bot a's is closed already and
    nothing is booked for it a second time.
    """
    resting = {sid: await paper.resting(sid) for sid in ("a", "b")}
    for sid, order_ref in resting.items():
        await paper.stream_fill(order_ref, contracts=1, filled_in_all=1, price="1.94", execution_id=f"x-{sid}-buy")
    _at(paper, SATURDAY_NOON)
    paper.account.settled.add(SYMBOL)
    await paper.pass_()
    _at(paper, MONDAY_CLOSE)
    await _press(paper.clerk, "a")
    paper.account.option_events.rows = [_expiry_row(qty="-2")]

    paper.wait(option_events.OPTION_EVENT_POLL_RETRY_MS)
    result = await paper.pass_()

    assert [(row["strategy_instance_id"], row["summary_code"]) for row in bookings(paper.repo)] == [
        ("a", "OPTION_ENDED_BY_OWNER"), ("b", "OPTION_EXPIRED")
    ]
    assert paper.held("a") == {} and paper.held("b") == {}
    assert fifo(paper.repo, "b").exact_realized_pnl == Decimal("-194")
    assert option_holds(paper.repo) == {} and result.verdict == "clean"


# ── the batch function's own refusals, on the books ──────────────────────

CALL = "AAPL230721C00150000"
THE_DAY_BEFORE = date(2023, 7, 20)
# AAPL230721C00150000's last session is Friday 2023-07-21. The session after it closes on Monday the 24th.
AFTER_THE_WAIT = et_minute_of_day_ms(date(2023, 7, 24), 16 * 60)


def _books(tmp_path: Path, **contracts: int) -> ClerkSqliteRepository:
    repo = books_at(tmp_path, THE_DAY_BEFORE, {"bot-a": "AAPL", "bot-b": "AAPL"})
    for bot, count in contracts.items():
        holds_option(repo, bot.replace("_", "-"), CALL, contracts=count)
    return repo


async def _asked_to_end(
    repo: ClerkSqliteRepository, account: _FakeReadPort, bot: str = "bot-a", *, symbol: str = CALL
) -> option_events.OwnerEndingOutcome:
    polled = await poll_option_events(
        repo, read=account, off_loop=run_inline,
        owner_ending=OwnerEndingRequest(strategy_instance_id=bot, symbol=symbol, operator="the-owner"),
    )
    assert polled.read and polled.round is not None and polled.round.owner_ending is not None
    return polled.round.owner_ending


async def test_an_ending_is_made_now_whatever_the_cadence_of_the_read(tmp_path: Path) -> None:
    """The read was made a second ago and none is due for minutes. The owner's ending rests on one look more,
    so it is made at once."""
    repo = _books(tmp_path, bot_a=1)
    account = _FakeReadPort()
    try:
        later(repo, AFTER_THE_WAIT - repo.clock())
        await poll_option_events(repo, read=account, off_loop=run_inline)
        later(repo, 1_000)
        assert (await poll_option_events(repo, read=account, off_loop=run_inline)).read is False

        outcome = await _asked_to_end(repo, account)

        assert outcome.result == "ended" and len(account.option_events.reads) == 2
        assert held(repo, "bot-a") == {}
    finally:
        repo.close()


@pytest.mark.parametrize(
    ("when", "contracts", "broker", "reason_code"),
    [
        pytest.param("while it trades", 1, {}, "OPTION_OWNER_ENDING_CONTRACT_STILL_TRADES", id="a contract that still trades"),
        pytest.param("before the wait is over", 1, {}, "OPTION_OWNER_ENDING_RECORD_MAY_STILL_COME", id="before the next session has closed"),
        pytest.param("after the wait", -1, {}, "OPTION_OWNER_ENDING_SOLD_CONTRACT", id="a sold contract"),
        pytest.param("after the wait", 1, {CALL: 1.0}, "OPTION_OWNER_ENDING_BROKER_STILL_HOLDS", id="the broker still holds it"),
    ],
)
async def test_the_batch_function_ends_nothing_it_is_not_shown_to_have_no_settlement(
    tmp_path: Path, when: str, contracts: int, broker: dict[str, float], reason_code: str
) -> None:
    repo = _books(tmp_path, bot_a=contracts)
    account = _FakeReadPort(positions=broker_holds(**broker))
    try:
        if when == "before the wait is over":
            later(repo, AFTER_THE_WAIT - 1 - repo.clock())
        elif when == "after the wait":
            later(repo, AFTER_THE_WAIT - repo.clock())

        outcome = await _asked_to_end(repo, account)

        assert (outcome.result, outcome.reason_code) == ("refused", reason_code)
        assert bookings(repo) == [] and held(repo, "bot-a") == {CALL: float(contracts)}
    finally:
        repo.close()


async def test_an_ending_asked_of_a_bot_that_holds_none_of_the_contract_is_refused(tmp_path: Path) -> None:
    repo = _books(tmp_path, bot_a=1)
    account = _FakeReadPort()
    try:
        later(repo, AFTER_THE_WAIT - repo.clock())

        outcome = await _asked_to_end(repo, account, "bot-b")

        assert (outcome.result, outcome.reason_code) == ("refused", "OPTION_OWNER_ENDING_NOTHING_HELD")
        assert bookings(repo) == [] and held(repo, "bot-a") == {CALL: 1.0}
    finally:
        repo.close()


async def test_the_clerk_refuses_an_ending_through_its_own_door_with_the_reason(paper: PaperOptions) -> None:  # noqa: F811
    """Asked directly, past the page's own check: the Clerk's look is what refuses, by name, and writes nothing."""
    await _gone_at_the_broker_with_no_record(paper)

    with pytest.raises(OwnerEndingRefused) as refused:
        await paper.clerk.end_expired_option(
            strategy_instance_id="a", symbol=SYMBOL, operator="the-owner", reason=None
        )

    assert refused.value.reason_code == "OPTION_OWNER_ENDING_RECORD_MAY_STILL_COME"
    assert bookings(paper.repo) == []


# ── a record that came and that the rule refused by name ─────────────────
#
# The broker did report the contract's end, in a row the canonical rule cannot match to the lots on the
# books. Nothing of it is booked and nothing will be. The lot stays open, the broker holds none of the
# contract, the write-off is never offered for an expired contract, and an acknowledgement is refused
# while the books differ from the broker. The owner's ending is the way out.

LAST_DAY = "2023-07-21"


def _expiry_of(contracts: int, **changes: Any) -> dict[str, Any]:
    return documented_option_activity(
        "trading_api", "otm_option_expiry", **{"id": "exp-1", "qty": str(-contracts), **changes}
    )


def _assignment_of_one_with_its_shares() -> list[dict[str, Any]]:
    """One call assigned on the contract's last day, and the share trade Alpaca reports with it: 100 sold at 150."""
    return [
        documented_option_activity("trading_api", "option_assignment", id="asn-1", qty="1", date=LAST_DAY),
        documented_option_activity(
            "trading_api", "option_assignment", 1, id="trd-1", qty="-100", net_amount="15000", date=LAST_DAY
        ),
    ]


def _served(repo: ClerkSqliteRepository) -> list[OptionEndingView]:
    with repo.write_fence() as conn:
        return [option_ending_view(ending) for ending in recorded_option_endings(conn)]


def _summaries(repo: ClerkSqliteRepository) -> list[tuple[str, str]]:
    return [(row["strategy_instance_id"], row["summary_code"]) for row in bookings(repo)]


# Each case: the lots on the books, the rows the broker reports, the row the rule refuses and by which name,
# and what the same rows do when they are read again after bot-a's contract was ended.
_REFUSED_BY_NAME = [
    pytest.param(
        {"bot_a": 1}, [_expiry_of(2)], "exp-1", "OPTION_EVENT_MORE_CONTRACTS_THAN_HELD", [], set(),
        id="an expiry of more contracts than are held",
    ),
    pytest.param(
        {"bot_a": 1}, _assignment_of_one_with_its_shares(), "asn-1", "OPTION_EVENT_WRONG_SIDE", [], set(),
        id="an assignment of a contract held bought",
    ),
    pytest.param(
        # The rule then books the side that is left, as it books any expiry of a contract held one way.
        {"bot_a": 2, "bot_b": -1}, [_expiry_of(1)], "exp-1", "OPTION_EVENT_HELD_ON_BOTH_SIDES",
        [("bot-b", "OPTION_EXPIRED")], {("bot-b", "OPTION_EXPIRY_SIGNED_FOR_OTHER_SIDE")},
        id="an expiry of a contract held bought by one bot and sold by another",
    ),
]


@pytest.mark.parametrize(("lots", "rows", "refused_id", "refused_as", "_booked_later", "_held_later"), _REFUSED_BY_NAME)
async def test_a_contract_whose_record_the_rule_refused_by_name_is_ended_at_the_owners_word_and_its_record_names_the_row(
    tmp_path: Path, lots: dict[str, int], rows: list[dict[str, Any]], refused_id: str, refused_as: str,
    _booked_later: list[tuple[str, str]], _held_later: set[tuple[str, str]],
) -> None:
    """Each call cost $194.00. Ended at the owner's word, what bot-a paid for its contracts is its loss."""
    repo = _books(tmp_path, **lots)
    account = _FakeReadPort()
    account.option_events.rows = rows
    try:
        later(repo, AFTER_THE_WAIT - repo.clock())
        await poll(repo, account)
        assert {("bot-a", refused_as), ("bot-a", "OPTION_EXPIRED_NOT_SETTLED")} <= set(option_holds(repo))
        assert bookings(repo) == []

        outcome = await _asked_to_end(repo, account)

        assert outcome.result == "ended"
        assert _summaries(repo) == [("bot-a", "OPTION_ENDED_BY_OWNER")]
        word = json.loads(bookings(repo)[0]["facts_json"])["owner_word"]
        assert [(row["ref"], row["refused_as"]) for row in word["rows_not_booked"]] == [
            (f"option-activity:{refused_id}", refused_as)
        ]
        assert refused_id in word["why_no_settlement"]
        assert held(repo, "bot-a") == {} and fifo(repo, "bot-a").open_lots == []
        assert fifo(repo, "bot-a").exact_realized_pnl == Decimal(-194 * lots["bot_a"])
        # The bot whose contract was ended is held for nothing more, and the account for nothing.
        assert not any(bot in ("bot-a", None) for bot, _reason in option_holds(repo))
    finally:
        repo.close()


@pytest.mark.parametrize(("lots", "rows", "_refused_id", "_refused_as", "booked_later", "held_later"), _REFUSED_BY_NAME)
async def test_after_such_an_ending_the_same_record_read_again_books_nothing_for_that_bot_and_holds_nobody_for_it(
    tmp_path: Path, lots: dict[str, int], rows: list[dict[str, Any]], _refused_id: str, _refused_as: str,
    booked_later: list[tuple[str, str]], held_later: set[tuple[str, str]],
) -> None:
    """The row stays inside the read's window for days. The owner ended the contract knowing of it."""
    repo = _books(tmp_path, **lots)
    account = _FakeReadPort()
    account.option_events.rows = rows
    try:
        later(repo, AFTER_THE_WAIT - repo.clock())
        assert (await _asked_to_end(repo, account)).result == "ended"

        for _read in range(2):
            later(repo, option_events.OPTION_EVENT_POLL_RETRY_MS)
            assert (await poll(repo, account, asked_for=True)).read

        assert _summaries(repo) == [("bot-a", "OPTION_ENDED_BY_OWNER"), *booked_later]
        assert set(option_holds(repo)) == held_later
        assert held(repo, "bot-a") == {}
        assert fifo(repo, "bot-a").exact_realized_pnl == Decimal(-194 * lots["bot_a"])
    finally:
        repo.close()


async def test_a_bot_that_still_holds_the_contract_is_still_held_for_a_record_another_bots_ending_named(
    tmp_path: Path,
) -> None:
    """Two bots hold one call each and the broker reports an expiry of three. The owner ends bot-a's.

    Bot-b still holds its call, and the row is as unbookable for it as it
    was. It stays held for the row by name until its own contract is ended.
    Then nobody holds the contract, and the row is held for by nobody.
    """
    repo = _books(tmp_path, bot_a=1, bot_b=1)
    account = _FakeReadPort()
    account.option_events.rows = [_expiry_of(3)]
    try:
        later(repo, AFTER_THE_WAIT - repo.clock())
        assert (await _asked_to_end(repo, account, "bot-a")).result == "ended"
        still_held_for = {("bot-b", "OPTION_EVENT_MORE_CONTRACTS_THAN_HELD"), ("bot-b", "OPTION_EXPIRED_NOT_SETTLED")}
        assert set(option_holds(repo)) == still_held_for

        later(repo, option_events.OPTION_EVENT_POLL_RETRY_MS)
        await poll(repo, account, asked_for=True)

        assert set(option_holds(repo)) == still_held_for

        assert (await _asked_to_end(repo, account, "bot-b")).result == "ended"
        later(repo, option_events.OPTION_EVENT_POLL_RETRY_MS)
        await poll(repo, account, asked_for=True)

        assert _summaries(repo) == [("bot-a", "OPTION_ENDED_BY_OWNER"), ("bot-b", "OPTION_ENDED_BY_OWNER")]
        assert option_holds(repo) == {}
    finally:
        repo.close()


@pytest.mark.parametrize(
    ("lots", "also_reported", "in_the_way"),
    [
        pytest.param(
            # An expiry takes the newest lot, which is bot-b's: it is booked, and for bot-a it settles nothing.
            {"bot_a": 1, "bot_b": 1}, [_expiry_of(1, id="exp-2")], "exp-2",
            id="a record the rule books for another bot's lot",
        ),
        pytest.param({"bot_a": 1}, [_expiry_of(1, id="exp-2", qty="-0.5")], "exp-2", id="a record the Clerk cannot read"),
        pytest.param(
            {"bot_a": 1}, [_expiry_of(1, id="exp-2", status="canceled")], "exp-2",
            id="a record that is not reported as executed",
        ),
        pytest.param(
            {"bot_a": 1},
            [documented_option_activity(
                "trading_api", "option_exercise", 1, id="trd-9", qty="100", price="150", net_amount="-15000",
                date=LAST_DAY,
            )],
            "trd-9", id="a share trade no record read explains",
        ),
    ],
)
async def test_a_refused_record_does_not_let_the_ending_past_another_record_that_may_be_the_contracts_end(
    tmp_path: Path, lots: dict[str, int], also_reported: list[dict[str, Any]], in_the_way: str
) -> None:
    """Beside the row the rule refused there is one it did not refuse by name. That one may still be the
    contract's end, so nothing is ended, and the refusal names it and not the refused row."""
    repo = _books(tmp_path, **lots)
    account = _FakeReadPort()
    account.option_events.rows = [_expiry_of(5), *also_reported]
    try:
        later(repo, AFTER_THE_WAIT - repo.clock())

        outcome = await _asked_to_end(repo, account)

        assert (outcome.result, outcome.reason_code) == ("refused", "OPTION_OWNER_ENDING_ROW_NOT_BOOKED")
        assert in_the_way in outcome.message and "exp-1" not in outcome.message
        assert held(repo, "bot-a") == {CALL: 1.0}
        assert ("bot-a", "OPTION_ENDED_BY_OWNER") not in _summaries(repo)
    finally:
        repo.close()


async def test_a_record_the_rule_refused_under_another_name_still_stands_in_the_way(tmp_path: Path) -> None:
    """Three of the rule's refusals set a row aside: each says the books hold lots the row cannot be matched
    to, by its count or its side. An exercise whose cash does not come to whole cents is refused under another
    name. One contract struck at $150.005 that delivers one share: $150.005. The contract is not ended beside it."""
    odd_call = "AAPL230721C00150005"
    repo = books_at(tmp_path, THE_DAY_BEFORE, {"bot-a": "AAPL"})
    holds_option(repo, "bot-a", odd_call, contracts=1, multiplier=1)
    account = _FakeReadPort()
    account.option_events.rows = [
        documented_option_activity("trading_api", "option_exercise", id="exc-1", qty="-1", symbol=odd_call)
    ]
    try:
        later(repo, AFTER_THE_WAIT - repo.clock())

        outcome = await _asked_to_end(repo, account, symbol=odd_call)

        assert ("bot-a", "OPTION_EVENT_CASH_NOT_WHOLE_CENTS") in option_holds(repo)
        assert (outcome.result, outcome.reason_code) == ("refused", "OPTION_OWNER_ENDING_ROW_NOT_BOOKED")
        assert bookings(repo) == [] and held(repo, "bot-a") == {odd_call: 1.0}
    finally:
        repo.close()


async def test_a_record_the_rule_refused_for_another_contract_is_no_part_of_this_contracts_ending(
    tmp_path: Path,
) -> None:
    """Bot-a holds the 150 call, and nothing was reported for it. Bot-b holds the 160 call, and the broker
    reports an expiry of two of those, which the rule refuses. That row is the other contract's: bot-a's
    ending is made as one no record came for, and names no row."""
    other_call = "AAPL230721C00160000"
    repo = _books(tmp_path, bot_a=1)
    holds_option(repo, "bot-b", other_call, contracts=1)
    account = _FakeReadPort()
    account.option_events.rows = [_expiry_of(2, symbol=other_call)]
    try:
        later(repo, AFTER_THE_WAIT - repo.clock())

        outcome = await _asked_to_end(repo, account)

        assert outcome.result == "ended"
        word = json.loads(bookings(repo)[0]["facts_json"])["owner_word"]
        assert "rows_not_booked" not in word and "exp-1" not in word["why_no_settlement"]
        assert held(repo, "bot-a") == {} and held(repo, "bot-b") == {other_call: 1.0}
    finally:
        repo.close()


async def test_a_record_that_can_be_booked_is_booked_in_the_endings_place_though_another_was_refused(
    tmp_path: Path,
) -> None:
    """The broker reports an expiry of two, which the rule refuses, and an expiry of one, which is the bot's.
    The one that can be booked is booked: the contract expired, and nothing is ended at the owner's word."""
    repo = _books(tmp_path, bot_a=1)
    account = _FakeReadPort()
    account.option_events.rows = [_expiry_of(2), _expiry_of(1, id="exp-2")]
    try:
        later(repo, AFTER_THE_WAIT - repo.clock())

        outcome = await _asked_to_end(repo, account)

        assert (outcome.result, outcome.ended_by) == ("settled", "expired")
        assert _summaries(repo) == [("bot-a", "OPTION_EXPIRED")]
        assert "owner_word" not in json.loads(bookings(repo)[0]["facts_json"])
        assert held(repo, "bot-a") == {}
    finally:
        repo.close()


@pytest.mark.parametrize(
    ("at_the_broker", "reason_code"),
    [
        pytest.param("holds_it", "OPTION_OWNER_ENDING_BROKER_STILL_HOLDS", id="the broker still holds the contract"),
        pytest.param("read_cut_short", "OPTION_OWNER_ENDING_READ_NOT_WHOLE", id="the read of the events is not whole"),
        pytest.param("positions_fail", "OPTION_OWNER_ENDING_BROKER_POSITION_UNREAD", id="the positions cannot be read"),
    ],
)
async def test_a_refused_record_changes_nothing_else_the_look_at_the_press_asks_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, at_the_broker: str, reason_code: str
) -> None:
    """The row the rule refused is set aside and no more. The read must still be whole, the broker's positions
    read, and none of the contract held there."""
    repo = _books(tmp_path, bot_a=1)
    account = _FakeReadPort(positions=broker_holds(**({CALL: 1.0} if at_the_broker == "holds_it" else {})))
    account.option_events.rows = [_expiry_of(2)]
    try:
        later(repo, AFTER_THE_WAIT - repo.clock())
        if at_the_broker == "read_cut_short":
            account.option_events.cut_short = True
        elif at_the_broker == "positions_fail":
            async def no_positions() -> Any:
                raise BrokerUnavailable("Alpaca is not answering.", broker="alpaca")

            monkeypatch.setattr(account, "list_positions", no_positions)

        outcome = await _asked_to_end(repo, account)

        assert (outcome.result, outcome.reason_code) == ("refused", reason_code)
        assert bookings(repo) == [] and held(repo, "bot-a") == {CALL: 1.0}
    finally:
        repo.close()


@pytest.mark.parametrize(
    ("lots", "rows", "at_the_broker", "bot", "says_it_will_not_be_booked", "ended_at_the_press"),
    [
        pytest.param({"bot_a": 1}, [], {}, "bot-a", False, True, id="no record came"),
        pytest.param({"bot_a": 1}, [_expiry_of(2)], {}, "bot-a", True, True, id="the record that came was refused by name"),
        pytest.param(
            {"bot_a": 1}, [_expiry_of(2), _expiry_of(1, id="exp-2", status="canceled")], {}, "bot-a", False, False,
            id="another record stands in the way",
        ),
        pytest.param(
            {"bot_a": 1}, [_expiry_of(2)], {CALL: 3.0}, "bot-a", False, False,
            id="the broker still shows some of the contract",
        ),
        pytest.param(
            {"bot_a": 2, "bot_b": -1}, [_expiry_of(1)], {}, "bot-b", False, False, id="the bot holds the contract sold",
        ),
    ],
)
async def test_the_notice_of_an_expired_contract_says_its_record_will_not_be_booked_only_where_the_owner_can_end_it(
    tmp_path: Path, lots: dict[str, int], rows: list[dict[str, Any]], at_the_broker: dict[str, float], bot: str,
    says_it_will_not_be_booked: bool, ended_at_the_press: bool,
) -> None:
    """The notice sends the owner to "End the expired contract" when no record has come, or when the notice
    itself says the one that came will not be booked. So it says that of a record only where the press then
    ends the contract, and names the record. It promises no end that cannot come."""
    repo = _books(tmp_path, **lots)
    account = _FakeReadPort(positions=broker_holds(**at_the_broker))
    account.option_events.rows = rows
    try:
        later(repo, AFTER_THE_WAIT - repo.clock())
        await poll(repo, account)

        notice = option_holds(repo)[(bot, "OPTION_EXPIRED_NOT_SETTLED")]

        said = json.loads(notice["facts_json"])["explanation"]
        assert ("will not be booked" in said) is says_it_will_not_be_booked
        assert ("exp-1" in said) is says_it_will_not_be_booked
        assert END_EXPIRED_OPTION_LABEL in notice["next_step"]
        assert ((await _asked_to_end(repo, account, bot)).result == "ended") is ended_at_the_press
    finally:
        repo.close()


async def test_on_a_real_account_a_contract_whose_record_could_not_be_booked_is_ended_as_the_page_presses_it(
    paper: PaperOptions,  # noqa: F811
) -> None:
    """One call bought at $1.94. The broker reports an expiry of two and holds none. The rule refuses the row.

    The write-off is not offered and no acknowledgement is taken while the
    books differ from the broker. The owner's ending is offered once the
    next session has closed, and pressed it goes ahead: -$194.00 realised,
    the notices gone, the account agreeing once more, and History told that
    the broker's record could not be booked.
    """
    await _holding_one_call(paper, "a")
    _at(paper, max(paper.clock(), SATURDAY_NOON))
    paper.account.settled.add(SYMBOL)
    paper.account.option_events.rows = [_expiry_row(qty="-2")]
    await paper.pass_()
    assert {("a", "OPTION_EVENT_MORE_CONTRACTS_THAN_HELD"), ("a", "OPTION_EXPIRED_NOT_SETTLED")} <= set(
        option_holds(paper.repo)
    )
    _at(paper, MONDAY_CLOSE)
    shown = await _offered(paper.clerk, "a")
    assert shown.available and not (await _offered(paper.clerk, "a", "discharge_attributed_residue")).available

    ended = await _press(paper.clerk, "a", shown=shown)

    assert ended.applied and paper.held() == {}
    assert fifo(paper.repo, "a").exact_realized_pnl == Decimal("-194")
    [served] = _served(paper.repo)
    assert (served.kind, served.simulated, served.ended_by_operator) == ("ended_by_owner", False, "the-owner")
    assert served.no_settlement_reason is not None and "exp-1" in served.no_settlement_reason
    assert "could not be booked" in served.headline and "exp-1" not in served.headline
    assert option_holds(paper.repo) == {}
    assert (await paper.pass_()).verdict == "clean"

    # The row is read again for days, and changes nothing.
    paper.wait(option_events.OPTION_EVENT_POLL_INTERVAL_MS)
    later_pass = await paper.pass_()
    assert [row["summary_code"] for row in bookings(paper.repo)] == ["OPTION_ENDED_BY_OWNER"]
    assert option_holds(paper.repo) == {} and later_pass.verdict == "clean"


async def test_a_dry_runs_ending_names_no_record_of_a_brokers_and_reads_as_it_did(dry_run: _DryRun) -> None:  # noqa: F811
    """No broker reports a simulated account's option events, so its ending has no such row to name."""
    await _stopped_before_the_last_session_with_no_price_kept(dry_run)

    await _press(dry_run.clerk, SID)

    [booked] = _booked(dry_run)
    assert "rows_not_booked" not in booked["owner_word"]
    [served] = _served(dry_run.repo)
    assert served.simulated and "could not be booked" not in served.headline
