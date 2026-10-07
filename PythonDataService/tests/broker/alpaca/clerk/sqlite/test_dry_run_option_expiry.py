"""A Dry Run settles a contract its bot holds past expiry (#2913).

The real SQLite Clerk of a ``sim:`` account, with its real simulated broker.
The simulation settles its own position when the Clerk reads the account's
option events, and the Clerk books what it reports exactly as it books a real
broker's. The rule and the price are the simulation's: nothing here says
what a real account would have done.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest

from app.broker.alpaca.clerk.active_authority import get_clerk_runtime
from app.broker.alpaca.clerk.sqlite import option_events
from app.broker.alpaca.clerk.sqlite.simulated_account import SimulationEvidenceUnavailable
from app.marketdata.feed import MarketDataBar
from app.services.source_bar_ledger import SourceBarLedger
from tests.broker.alpaca.clerk.sqlite.test_option_close_fences import (
    EXPIRY_CLOSE,
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
    RUN,
    SID,
    SYMBOL,
    _DryRun,
    dry_run,
)


def _keeps_spy(account: _DryRun, close: str, *, end_ms: int = EXPIRY_CLOSE) -> None:
    """The simulation keeps one minute of SPY, as the runner's bars reach it."""
    price = Decimal(close)
    ledger = SourceBarLedger(artifacts_root=account.artifacts_root, account_id=account.repo.account_id)
    try:
        ledger.append(
            MarketDataBar(
                feed_id="ibkr", symbol="SPY", start_ms=end_ms - 60_000, end_ms=end_ms, open=price, high=price,
                low=price, close=price, volume=100, fetched_at_ms=end_ms, session_phase="RTH",
            ),
            run_id=RUN,
        )
    finally:
        ledger.close()


async def _keepers_round(account: _DryRun) -> Any:
    """One round of the Clerk's own task: the only thing that acts on a Dry Run while no pass runs."""
    runtime = get_clerk_runtime(account.repo.account_id)
    assert runtime is not None and runtime.deadline_keeper is not None
    return await runtime.deadline_keeper.tick()


def _booked(account: _DryRun) -> list[dict[str, Any]]:
    with account.repo.write_fence() as conn:
        rows = conn.execute(
            "SELECT summary_code, facts_json FROM custody_transitions WHERE transition_kind = 'OPTION_POSITION_ENDED' "
            "ORDER BY sequence"
        ).fetchall()
    return [{"summary_code": row[0], **json.loads(row[1])} for row in rows]


async def test_a_call_held_past_expiry_out_of_the_money_ends_at_nothing_and_the_bot_is_flat(dry_run: _DryRun) -> None:  # noqa: F811
    """One call bought at $1.94; SPY last traded at 580.00 under the 585 strike: $194.00 lost, and nothing held."""
    await dry_run.enter(dry_run.evidence())
    _keeps_spy(dry_run, "580.00")
    _wait_until(dry_run, SATURDAY_NOON)

    await dry_run.clerk.reconcile_once()

    assert dry_run.held() == {}
    assert dry_run.realized_pnl() == Decimal("-194")
    [booked] = _booked(dry_run)
    assert booked["summary_code"] == "OPTION_EXPIRED" and booked["simulated"] is True
    assert await _page_notices(dry_run) == {}
    # The account can be read again: it holds cash and nothing else.
    assert dry_run.account_reading() is not None


async def test_the_clerks_own_task_settles_an_expired_contract_with_no_pass_and_no_owner(dry_run: _DryRun) -> None:  # noqa: F811
    """A Dry Run has no pass on a timer. Its keeper's round is what reads the events and books them."""
    await dry_run.enter(dry_run.evidence())
    _keeps_spy(dry_run, "580.00")
    _wait_until(dry_run, SATURDAY_NOON)

    done = await _keepers_round(dry_run)

    assert done.option_events is not None and done.option_events.read
    assert dry_run.held() == {}
    # Nothing is read again until the next read is due, and nothing more is written.
    written = dry_run.repo.last_custody_sequence()
    assert (await _keepers_round(dry_run)).option_events.read is False
    assert dry_run.repo.last_custody_sequence() == written


async def test_a_call_in_the_money_is_exercised_and_the_bot_holds_the_shares_at_the_strike(dry_run: _DryRun) -> None:  # noqa: F811
    """SPY last traded at 590.25 over the 585 strike: 100 shares bought at $585.00, and the premium is the loss."""
    await dry_run.enter(dry_run.evidence())
    _keeps_spy(dry_run, "590.25")
    _wait_until(dry_run, SATURDAY_NOON)

    await dry_run.clerk.reconcile_once()

    assert dry_run.held() == {"SPY": 100.0}
    assert ("SPY", "buy", 100.0, 585.0, None) in dry_run.fills()
    assert dry_run.realized_pnl() == Decimal("-194")
    [booked] = _booked(dry_run)
    assert booked["summary_code"] == "OPTION_EXERCISED"
    assert booked["shares"]["cash_cents"] == -5_850_000
    # The simulation's broker holds what the books hold.
    assert {
        position.symbol: position.quantity for position in await dry_run.clerk._read.list_positions()
    } == {"SPY": 100.0}
    assert set(await _page_notices(dry_run)) == {"OPTION_SHARES_HELD"}


async def test_the_record_says_it_is_a_simulations_rule_with_the_price_it_used_and_where_it_came_from(
    dry_run: _DryRun,  # noqa: F811
) -> None:
    await dry_run.enter(dry_run.evidence())
    _keeps_spy(dry_run, "590.25", end_ms=EXPIRY_CLOSE - 60_000)
    _wait_until(dry_run, SATURDAY_NOON)

    await dry_run.clerk.reconcile_once()

    [booked] = _booked(dry_run)
    said = booked["simulation"]
    assert said["underlying_price"] == "590.25"
    assert said["bar_end_ms"] == EXPIRY_CLOSE - 60_000 and said["last_session_close_ms"] == EXPIRY_CLOSE
    assert said["bar_ref"] and "simulation" in said["rule"].lower()
    assert booked["broker"] == "synthetic" and booked["source_code"] == "SIMULATED_EXERCISE"


async def test_with_no_price_of_the_underlying_from_the_last_session_the_contract_stays_held_and_the_hold_says_why(
    dry_run: _DryRun,  # noqa: F811
) -> None:
    await dry_run.enter(dry_run.evidence())
    _keeps_spy(dry_run, "590.25", end_ms=THURSDAY_NOON)
    _wait_until(dry_run, SATURDAY_NOON)

    await dry_run.clerk.reconcile_once()

    assert dry_run.held() == {SYMBOL: 1.0} and _booked(dry_run) == []
    held = _notice(dry_run, "OPTION_EXPIRED_NOT_SETTLED")
    assert held is not None
    said = json.loads(held["facts_json"])["explanation"]
    assert SYMBOL in said and "SPY" in said and "last session" in said
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


async def test_the_shares_an_exercise_left_are_sold_when_the_run_ends_as_a_dry_run_sells_whatever_a_run_left(
    dry_run: _DryRun,  # noqa: F811
) -> None:
    """A Dry Run never ends holding. The shares bought at 585.00 are sold at the last price it kept, 590.25:
    100 x 5.25 = $525.00 on the shares, less the $194.00 premium."""
    await dry_run.enter(dry_run.evidence())
    _keeps_spy(dry_run, "590.25")
    _wait_until(dry_run, SATURDAY_NOON)
    await dry_run.clerk.reconcile_once()
    assert dry_run.held() == {"SPY": 100.0}

    await _stop(dry_run)
    await dry_run.clerk.reconcile_once()
    await dry_run.clerk.reconcile_once()

    assert dry_run.held() == {}
    assert dry_run.realized_pnl() == Decimal("331")
    assert await _page_notices(dry_run) == {}
