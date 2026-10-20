"""A Dry Run settles a contract its bot holds past expiry (#2913).

The real SQLite Clerk of a ``sim:`` account, with its real simulated broker.
No broker ends a Dry Run's positions, so its simulation does, by a rule of
its own: the contract is sold, in cash, at its exercise value at the close
of its last session, or expires at nothing. It is never exercised into
shares. The Clerk books what the simulation hands back exactly as it books a
real broker's events. Nothing here says what a real account would have got.

The scene is the one of ``test_option_entry_and_exit``: a $1,000.00 budget,
one SPY 585 call expiring Friday 2026-09-18, bought at noon on Tuesday
2026-09-08 at $1.94 a share: $194.00, and six cents of fees.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from app.broker.alpaca.clerk.active_authority import close_synthetic_clerk_runtimes, get_clerk_runtime
from app.broker.alpaca.clerk.simulated_option_settlement import SETTLEMENT_GRACE_MS
from app.broker.alpaca.clerk.sqlite import option_events
from app.broker.alpaca.clerk.sqlite.day_pnl import observed_day_pnl
from app.broker.alpaca.clerk.sqlite.simulated_account import SimulationEvidenceUnavailable
from app.marketdata.feed import MarketDataBar
from app.options.contract import OptionContract
from app.services.session_authority import et_minute_of_day_ms
from app.services.source_bar_ledger import SourceBarLedger
from tests._helpers.market_quotes import publish_quotes
from tests.broker.alpaca.clerk.sqlite.test_option_close_fences import (
    EXPIRY,
    EXPIRY_CLOSE,
    EXPIRY_NOON,
    MONDAY_NOON,
    SATURDAY_NOON,
    THURSDAY_NOON,
    _hand_over,
    _notice,
    _page_notices,
    _stop,
    _wait_until,
)
from tests.broker.alpaca.clerk.sqlite.test_option_entry_and_exit import (  # noqa: F401
    LEG,
    RUN,
    SID,
    SYMBOL,
    _DryRun,
    _open,
    _option_leg,
    _plan,
    dry_run,
)

MINUTE = 60_000
PUT = OptionContract.of(root="SPY", expiry_date=EXPIRY, right="put", strike=585)


def _keeps_spy(account: _DryRun, close: str, *, end_ms: int = EXPIRY_CLOSE) -> None:
    """The simulation keeps one minute of SPY, as the runner's bars reach it."""
    price = Decimal(close)
    ledger = SourceBarLedger(artifacts_root=account.artifacts_root, account_id=account.repo.account_id)
    try:
        ledger.append(
            MarketDataBar(
                feed_id="ibkr", symbol="SPY", start_ms=end_ms - MINUTE, end_ms=end_ms, open=price, high=price,
                low=price, close=price, volume=100, fetched_at_ms=end_ms, session_phase="RTH",
            ),
            run_id=RUN,
        )
    finally:
        ledger.close()


async def _keepers_round(account: _DryRun) -> Any:
    """One round of the Clerk's own task, the only thing that acts on a Dry Run while no pass runs, and the
    read of option events that round started, once it has finished."""
    runtime = get_clerk_runtime(account.repo.account_id)
    assert runtime is not None and runtime.deadline_keeper is not None
    await runtime.deadline_keeper.tick()
    return await runtime.deadline_keeper.option_events_read()


def _booked(account: _DryRun) -> list[dict[str, Any]]:
    with account.repo.write_fence() as conn:
        rows = conn.execute(
            "SELECT summary_code, facts_json FROM custody_transitions WHERE transition_kind = 'OPTION_POSITION_ENDED' "
            "ORDER BY sequence"
        ).fetchall()
    return [{"summary_code": row[0], **json.loads(row[1])} for row in rows]


def _budget(account: _DryRun) -> Any:
    """The bot's own budget as its statement reads it: what it made, its fees, what it holds and its balance."""
    return account.repo.account_budget(cash=1000, seen_before_ms=account.clock() + 1).deployments[0]


def _fees(account: _DryRun) -> Decimal:
    return account.fees().predicted_total


# ── what the bot's money says after each outcome, by hand ────────────────


async def test_a_call_held_past_expiry_out_of_the_money_ends_at_nothing_and_the_bot_is_flat(dry_run: _DryRun) -> None:  # noqa: F811
    """SPY closed at 580.00 under the 585 strike: the call is worth nothing. $194.00 is lost.

    An expiry is no trade, so it has no fee: the six cents are the entry's.
    Balance: 1,000.00 - 194.00 - 0.06 = $805.94, with nothing in position.
    """
    await dry_run.enter(dry_run.evidence())
    _keeps_spy(dry_run, "580.00")
    _wait_until(dry_run, SATURDAY_NOON)

    await dry_run.clerk.reconcile_once()

    assert dry_run.held() == {}
    assert dry_run.realized_pnl() == Decimal("-194")
    [booked] = _booked(dry_run)
    assert booked["summary_code"] == "OPTION_EXPIRED" and booked["simulated"] is True
    assert (booked["close_price"], booked["simulation"]["fee_cents"]) == ("0", None)
    assert _fees(dry_run) == Decimal("0.06")
    budget = _budget(dry_run)
    assert (budget.realized_gross, budget.fees, budget.position_cost) == (Decimal("-194"), Decimal("0.06"), 0)
    assert (budget.balance, budget.free) == (Decimal("805.94"), Decimal("805.94"))
    assert await _page_notices(dry_run) == {}
    # The account can be read again: it holds cash and nothing else.
    reading = dry_run.account_reading()
    assert (reading.cash_available_usd, reading.equity_usd, reading.position_count) == (
        Decimal("805.94"), Decimal("805.94"), 0
    )


async def test_a_call_in_the_money_is_settled_in_cash_at_its_exercise_value_and_the_bot_holds_no_shares(
    dry_run: _DryRun,  # noqa: F811
) -> None:
    """SPY closed at 590.25, 5.25 over the 585 strike. The call is sold for 5.25 a share: 100 x 5.25 = $525.00.

    Made: 525.00 - 194.00 = $331.00. Fees: the entry's six cents and the
    sale's nine (SEC 525 x 0.0000206 = 0.010815, two cents; TAF 0.00329,
    one; CAT 0.0003, one; ORF 0.015, two; OCC 0.025, three). Balance:
    1,000.00 + 331.00 - 0.15 = $1,330.85, with nothing in position.

    Exercised, it would have bought 100 shares for $58,500.00 on a $1,000.00
    budget. No share is bought, and the bot is held for none.

    A Dry Run's fills are never recorded as a real account's evidence, and a
    settlement's are no exception.
    """
    await dry_run.enter(dry_run.evidence())
    _keeps_spy(dry_run, "590.25")
    _wait_until(dry_run, SATURDAY_NOON)

    await dry_run.clerk.reconcile_once()

    assert dry_run.held() == {}, "the bot holds no contract and no share after the settlement"
    assert dry_run.fills() == [(SYMBOL, "buy", 1.0, 1.94, 100), (SYMBOL, "sell", 1.0, 5.25, 100)], "the books hold the buy and the cash sale at the exercise value"
    assert dry_run.realized_pnl() == Decimal("331"), "made 525.00 - 194.00"
    [booked] = _booked(dry_run)
    assert booked["summary_code"] == "OPTION_SETTLED_IN_CASH" and booked["ended_by"] == "settled_in_cash", "booked as a settlement in cash"
    assert (booked["close_price"], booked.get("shares"), booked["simulated"]) == ("5.25", None, True), "sold at 5.25 a share, never exercised into shares, marked simulated"
    assert _fees(dry_run) == Decimal("0.15"), "fees: the entry's six cents and the sale's nine"
    budget = _budget(dry_run)
    assert (budget.realized_gross, budget.fees, budget.position_cost) == (Decimal("331"), Decimal("0.15"), 0), "the bot's statement: made, fees and nothing in position"
    assert (budget.balance, budget.free) == (Decimal("1330.85"), Decimal("1330.85")), "balance 1,000.00 + 331.00 - 0.15"
    reading = dry_run.account_reading()
    assert (reading.cash_available_usd, reading.equity_usd, reading.position_count) == (
        Decimal("1330.85"), Decimal("1330.85"), 0
    ), "the account reads cash and equity, with no position"
    # The simulation's broker holds what the books hold: nothing.
    assert await dry_run.clerk._read.list_positions() == [], "the simulation's broker holds nothing"
    assert await _page_notices(dry_run) == {}, "no notice is raised"
    with dry_run.repo.write_fence() as conn:
        sources = {
            row[0] for row in conn.execute("SELECT evidence_source FROM fills WHERE fill_id LIKE 'option-event:%'")
        }
    assert sources == {"simulated_execution"}, "a settlement's fills are on the books as a simulated execution, never a real account's evidence"


async def test_a_put_in_the_money_is_settled_in_cash_and_leaves_no_short_and_a_budget_that_still_reads(
    dry_run: _DryRun,  # noqa: F811
) -> None:
    """One 585 put bought at $1.94; SPY closed at 580.00. Sold for 5.00 a share: $500.00, $306.00 made.

    Fees: six cents and nine (SEC 500 x 0.0000206 = 0.0103, two cents; the
    rest as for the call). Balance: 1,000.00 + 306.00 - 0.15 = $1,305.85.

    Exercised, it would have left the bot short 100 shares, and the account's
    budget unreadable for it.
    """
    await dry_run.enter(dry_run.evidence(picked={LEG: PUT}), plan=_plan(_option_leg(LEG, right="put")))
    assert dry_run.held() == {PUT.occ_symbol: 1.0}
    _keeps_spy(dry_run, "580.00")
    _wait_until(dry_run, SATURDAY_NOON)

    await dry_run.clerk.reconcile_once()

    assert dry_run.held() == {}
    assert dry_run.realized_pnl() == Decimal("306")
    [booked] = _booked(dry_run)
    assert (booked["summary_code"], booked["close_price"]) == ("OPTION_SETTLED_IN_CASH", "5")
    assert _fees(dry_run) == Decimal("0.15")
    budget = _budget(dry_run)
    assert (budget.realized_gross, budget.fees, budget.position_cost, budget.balance) == (
        Decimal("306"), Decimal("0.15"), 0, Decimal("1305.85")
    )
    # The account's money map reads too: nothing is short and nothing is unvalued.
    assert dry_run.repo.account_money(cash=1000, seen_before_ms=dry_run.clock() + 1).unvalued == ()
    assert await _page_notices(dry_run) == {}


async def test_a_close_a_fraction_of_a_cent_in_the_money_rounds_down_to_an_expiry_with_no_fee(dry_run: _DryRun) -> None:  # noqa: F811
    """SPY closed at 585.009: nine tenths of a cent over the strike. Rounded down to the cent that is nothing.

    So it is an expiry, the same as at 585.00: $194.00 lost, no sale and no
    fee beyond the entry's six cents. Balance: $805.94.
    """
    await dry_run.enter(dry_run.evidence())
    _keeps_spy(dry_run, "585.009")
    _wait_until(dry_run, SATURDAY_NOON)

    await dry_run.clerk.reconcile_once()

    [booked] = _booked(dry_run)
    assert (booked["summary_code"], booked["close_price"]) == ("OPTION_EXPIRED", "0")
    # Both figures are on the record: what it was worth as worked out, and as booked.
    assert (booked["simulation"]["exercise_value"], booked["simulation"]["booked_price"]) == ("0.009", "0")
    assert dry_run.fills() == [(SYMBOL, "buy", 1.0, 1.94, 100), (SYMBOL, "sell", 1.0, 0.0, 100)]
    assert (dry_run.realized_pnl(), _fees(dry_run)) == (Decimal("-194"), Decimal("0.06"))
    assert _budget(dry_run).balance == Decimal("805.94")


async def test_a_contract_bought_on_its_expiry_day_is_settled_and_the_days_pnl_is_what_it_made(dry_run: _DryRun) -> None:  # noqa: F811
    """Bought at noon on the expiry day itself at $1.94, closed 5.25 in the money. The read of that day's
    settlements starts on that same date.

    Both fills are of one day, and a day's SEC, TAF, CAT and ORF are each rounded
    up once over all its fills: SEC 0.010815, two cents; TAF 0.00329, one;
    CAT 0.0003 + 0.0003, one; ORF 0.015 + 0.015, three. OCC is rounded up on
    each fill: 0.025 to 0.03, twice, six. Thirteen cents, where the two fills on
    two days would be fifteen.

    The day's P&L, read at the close: the account began the day with
    $1,000.00 and ends it with 1,000.00 + 331.00 - 0.13 = $1,330.87. It made $330.87.
    """
    _wait_until(dry_run, EXPIRY_NOON)
    publish_quotes(EXPIRY_NOON, SPY=585.0)
    await dry_run.read_account()
    await dry_run.enter(dry_run.evidence())
    assert dry_run.held() == {SYMBOL: 1.0}
    _keeps_spy(dry_run, "590.25")
    _wait_until(dry_run, EXPIRY_CLOSE)

    await dry_run.clerk.reconcile_once()

    assert dry_run.held() == {}
    assert [booked["summary_code"] for booked in _booked(dry_run)] == ["OPTION_SETTLED_IN_CASH"]
    reading = dry_run.account_reading()
    day = observed_day_pnl(observation=reading, now_ms=dry_run.clock())
    assert _fees(dry_run) == Decimal("0.13")
    assert (reading.last_equity_usd, reading.equity_usd) == (Decimal(1000), Decimal("1330.87"))
    assert day.display_total_usd == Decimal("330.87")


# ── the record, and what is settled as ───────────────────────────────────


async def test_the_books_record_says_it_is_a_simulations_rule_with_the_price_it_used_and_where_it_came_from(
    dry_run: _DryRun,  # noqa: F811
) -> None:
    await dry_run.enter(dry_run.evidence())
    _keeps_spy(dry_run, "590.25", end_ms=EXPIRY_CLOSE - MINUTE)
    _wait_until(dry_run, SATURDAY_NOON)

    await dry_run.clerk.reconcile_once()

    [booked] = _booked(dry_run)
    said = booked["simulation"]
    assert "simulation" in said["rule"].lower() and "not a broker" in said["rule"].lower()
    assert (said["underlying_price"], said["strike"], said["exercise_value"], said["booked_price"]) == (
        "590.25", "585", "5.25", "5.25"
    )
    assert (said["contracts"], said["multiplier"], said["cash_cents"], said["fee_cents"]) == (1, 100, 52_500, 9)
    assert said["bar_ref"] and said["bar_provider"] == "ibkr"
    assert (said["bar_end_ms"], said["last_session_close_ms"], said["bar_before_close_ms"]) == (
        EXPIRY_CLOSE - MINUTE, EXPIRY_CLOSE, MINUTE
    )
    assert said["settled_at_ms"] == SATURDAY_NOON
    assert booked["broker"] == "synthetic" and booked["source_code"] == "SIMULATED_CASH_SETTLEMENT"


# ── when it is settled ───────────────────────────────────────────────────


async def test_the_clerks_own_task_settles_an_expired_contract_with_no_pass_and_no_owner(dry_run: _DryRun) -> None:  # noqa: F811
    """A Dry Run has no pass on a timer. Its keeper's round is what asks the simulation and books its answer."""
    await dry_run.enter(dry_run.evidence())
    _keeps_spy(dry_run, "580.00")
    _wait_until(dry_run, SATURDAY_NOON)

    await _keepers_round(dry_run)

    # The running keeper's own round may have made the read a moment before this one asked: either way
    # the contract is settled, by a round and by no pass.
    assert dry_run.held() == {}
    assert _booked(dry_run)[0]["summary_code"] == "OPTION_EXPIRED"
    # The same round had first recorded the scheduled close the contract's last session ended on
    # (ADR 0076 decision 20). The next round finds nothing held and ends that notice.
    assert _notice(dry_run, "SCHEDULED_CLOSE_MISSED") is not None
    assert (await _keepers_round(dry_run)).read is False
    assert _notice(dry_run, "SCHEDULED_CLOSE_MISSED") is None
    # Nothing is read again until the next read is due, and nothing more is written.
    written = dry_run.repo.last_custody_sequence()
    assert (await _keepers_round(dry_run)).read is False
    assert dry_run.repo.last_custody_sequence() == written


async def test_at_the_close_the_clerk_waits_for_the_closing_bar_and_books_from_it_and_not_from_the_bar_before(
    dry_run: _DryRun,  # noqa: F811
) -> None:
    """The race at the close. The 15:59 bar closed at 584.50, out of the money. The Clerk reads half a
    second after the close, before the 16:00 bar has landed. Nothing is booked and no notice is raised: the
    simulation is waiting. The closing bar lands at 586.00, and the next read, within a minute, books the
    sale at 1.00 a share: $100.00, not an expiry at nothing."""
    await dry_run.enter(dry_run.evidence())
    _wait_until(dry_run, EXPIRY_NOON)
    _keeps_spy(dry_run, "584.50", end_ms=EXPIRY_CLOSE - MINUTE)
    _wait_until(dry_run, EXPIRY_CLOSE + 500)

    await dry_run.clerk.reconcile_once()

    assert _booked(dry_run) == [] and dry_run.held() == {SYMBOL: 1.0}
    assert _notice(dry_run, "OPTION_EXPIRED_NOT_SETTLED") is None

    _keeps_spy(dry_run, "586.00")
    dry_run.wait(option_events.OPTION_EVENT_POLL_RETRY_MS)
    await dry_run.clerk.reconcile_once()

    [booked] = _booked(dry_run)
    assert (booked["summary_code"], booked["close_price"]) == ("OPTION_SETTLED_IN_CASH", "1")
    assert booked["simulation"]["bar_before_close_ms"] == 0
    assert dry_run.realized_pnl() == Decimal("-94")


async def test_once_the_wait_for_the_closing_bar_is_over_the_newest_bar_of_the_session_settles_it(dry_run: _DryRun) -> None:  # noqa: F811
    """No bar ends at the close. Two minutes after it the 15:59 bar stands for the close, and the record says
    it ended a minute before."""
    await dry_run.enter(dry_run.evidence())
    _wait_until(dry_run, EXPIRY_NOON)
    _keeps_spy(dry_run, "590.25", end_ms=EXPIRY_CLOSE - MINUTE)
    _wait_until(dry_run, EXPIRY_CLOSE + SETTLEMENT_GRACE_MS)

    await dry_run.clerk.reconcile_once()

    [booked] = _booked(dry_run)
    assert (booked["summary_code"], booked["simulation"]["bar_before_close_ms"]) == ("OPTION_SETTLED_IN_CASH", MINUTE)


async def test_a_restart_between_the_simulations_settlement_and_its_booking_books_it_once(dry_run: _DryRun) -> None:  # noqa: F811
    """The simulation settles in its own ledger, and the process stops before the Clerk has booked anything.
    The next Clerk is handed the same settlement and books it. Restarted twice more, it books nothing more."""
    await dry_run.enter(dry_run.evidence())
    _keeps_spy(dry_run, "590.25")
    _wait_until(dry_run, SATURDAY_NOON)
    dry_run.clerk._trade.settle_expired_options(after_ms=0)
    assert _booked(dry_run) == [] and dry_run.held() == {SYMBOL: 1.0}

    for _restart in range(3):
        await close_synthetic_clerk_runtimes()
        reopened = await _open(dry_run.artifacts_root, dry_run.clock, dry_run.binding)
        await reopened.clerk.reconcile_once()
        reopened.wait(option_events.OPTION_EVENT_POLL_INTERVAL_MS)
        await reopened.clerk.reconcile_once()

        assert reopened.held() == {}
        assert [booked["summary_code"] for booked in _booked(reopened)] == ["OPTION_SETTLED_IN_CASH"]
        assert reopened.realized_pnl() == Decimal("331")


async def test_a_settlement_handed_over_once_more_after_it_was_booked_books_nothing_and_holds_nobody(
    dry_run: _DryRun,  # noqa: F811
) -> None:
    """A read whose window still reaches the contract's expiry date is handed the settlement on every poll.

    It was booked as a settlement in cash, and it travels as an expiry. The
    second time it is the same settlement, not a row that changed: nothing
    more is booked and no notice is raised.
    """
    await dry_run.enter(dry_run.evidence())
    _keeps_spy(dry_run, "590.25")
    _wait_until(dry_run, SATURDAY_NOON)
    await dry_run.clerk.reconcile_once()
    written = dry_run.repo.last_custody_sequence()
    settled = dry_run.clerk._trade.settle_expired_options(after_ms=0)
    assert [item.record.outcome.value for item in settled.settled] == ["settled_in_cash"]

    again = option_events.book_option_events(
        dry_run.repo, option_events.OptionEventRead(after_ms=0, history_complete=True), settled=settled
    )

    assert (again.booked, again.held) == ((), ())
    assert dry_run.repo.last_custody_sequence() == written
    assert [booked["summary_code"] for booked in _booked(dry_run)] == ["OPTION_SETTLED_IN_CASH"]


async def test_a_stopped_bot_whose_close_failed_before_expiry_is_flat_once_its_contract_is_settled(
    dry_run: _DryRun,  # noqa: F811
) -> None:
    """Thursday: nothing is bid, the owner stops the bot, the close is refused. Friday the contract expires
    worthless. The simulation settles it, the bot is flat, and the notice that it is not flat ends."""
    await dry_run.enter(dry_run.evidence())
    _wait_until(dry_run, THURSDAY_NOON)
    _hand_over(dry_run, "0", "0.05")
    await _stop(dry_run)
    await dry_run.clerk.reconcile_once()
    assert _notice(dry_run) is not None
    _keeps_spy(dry_run, "580.00")
    _wait_until(dry_run, MONDAY_NOON)

    await dry_run.clerk.reconcile_once()
    await dry_run.clerk.reconcile_once()

    assert dry_run.held() == {}
    assert await _page_notices(dry_run) == {}
    assert dry_run.realized_pnl() == Decimal("-194")


# ── when it cannot be settled ────────────────────────────────────────────


@pytest.mark.parametrize(
    ("kept_at", "says"),
    [
        pytest.param(THURSDAY_NOON, "no price of SPY", id="only a price from the day before"),
        pytest.param(
            et_minute_of_day_ms(date(2026, 9, 18), 9 * 60 + 35), "385 minutes before the close",
            id="only the 09:35 bar of a bot whose feed then went down",
        ),
    ],
)
async def test_with_no_price_at_the_close_of_the_last_session_the_contract_stays_held_and_the_hold_says_why(
    dry_run: _DryRun, kept_at: int, says: str  # noqa: F811
) -> None:
    """A price from another day, or from six hours before the close, is not what the contract was worth at
    the close. Nothing is settled from it."""
    await dry_run.enter(dry_run.evidence())
    _keeps_spy(dry_run, "590.25", end_ms=kept_at)
    _wait_until(dry_run, SATURDAY_NOON)

    await dry_run.clerk.reconcile_once()

    assert dry_run.held() == {SYMBOL: 1.0} and _booked(dry_run) == []
    held = _notice(dry_run, "OPTION_EXPIRED_NOT_SETTLED")
    assert held is not None
    said = json.loads(held["facts_json"])["explanation"]
    assert SYMBOL in said and says in said and "last session" in said
    with pytest.raises(SimulationEvidenceUnavailable):
        dry_run.account_reading()


async def test_a_price_kept_later_settles_what_was_held_for_want_of_one_and_ends_the_hold(dry_run: _DryRun) -> None:  # noqa: F811
    await dry_run.enter(dry_run.evidence())
    _wait_until(dry_run, SATURDAY_NOON)
    await dry_run.clerk.reconcile_once()
    assert _notice(dry_run, "OPTION_EXPIRED_NOT_SETTLED") is not None
    _keeps_spy(dry_run, "580.00")
    dry_run.wait(option_events.OPTION_EVENT_POLL_RETRY_MS)

    await dry_run.clerk.reconcile_once()

    assert dry_run.held() == {}
    assert _notice(dry_run, "OPTION_EXPIRED_NOT_SETTLED") is None
