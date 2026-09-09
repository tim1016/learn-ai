"""The account-wide day-P&L fact the loss hold judges (ADR 0059 D4).

The window is the ET calendar day containing ``now_ms``, so a lot opened on a
prior session and closed today counts once, today; the entry's own cash
outflow never reads as a loss. Fees net out only when the broker reported
every one of them, and an external order observed today makes the whole fact
*unknown* rather than optimistically zero.

Every stamp here is an explicit ``int64 ms UTC`` built from
``et_minute_of_day_ms``; the repository clock is fixed at ``NOON``. Nothing
reads the wall clock.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from app.broker.alpaca.clerk.et_day import et_day_window_ms
from app.broker.alpaca.clerk.live_envelope import AccountObservation
from app.broker.alpaca.clerk.sqlite.day_pnl import day_pnl_at
from app.broker.alpaca.clerk.sqlite.economic_projection import SqliteEconomicProjectionReader
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from tests.broker.alpaca.clerk.sqlite.conftest import NOON, YESTERDAY_NOON

# The ledger these tests judge -- the seeded fixtures, the two builders and the
# clocked authority -- lives in ``conftest`` because the envelope sync suite
# judges the same one (ADR 0059 D4).


def _observation(*, unrealized: float) -> AccountObservation:
    return AccountObservation(
        observed_at_ms=NOON,
        broker_cash_usd=100_000.0,
        cash_available_usd=100_000.0,
        last_equity_usd=100_000.0,
        unrealized_pl_usd=unrealized,
        position_count=1,
    )


@pytest.fixture
def reader(day_pnl_repo: ClerkSqliteRepository) -> Iterator[SqliteEconomicProjectionReader]:
    """One read-only projection over ``day_pnl_repo``, closed with the test.

    Constructed after the seeding fixtures have committed; it holds its own
    ``mode=ro`` connection, so it must be closed like the sibling economic
    suite closes its readers.
    """
    projection = SqliteEconomicProjectionReader.from_repository(day_pnl_repo)
    yield projection
    projection.close()


def test_realized_counts_only_lots_closed_today_and_nets_reported_fees(
    day_pnl_repo: ClerkSqliteRepository,
    seeded_round_trip: None,
    reader: SqliteEconomicProjectionReader,
) -> None:
    pnl = day_pnl_at(reader, day_pnl_repo, observation=_observation(unrealized=25.0), now_ms=NOON)
    assert (pnl.day_start_ms, pnl.day_end_ms) == et_day_window_ms(NOON)
    assert pnl.realized_usd == pytest.approx(100.0)
    assert pnl.fee_usd == pytest.approx(0.05) and pnl.fee_fidelity == "reported"
    assert pnl.unrealized_usd == 25.0
    assert pnl.total_usd == pytest.approx(124.95)
    assert pnl.known
    assert pnl.execution_coverage == "complete"


def test_a_lot_closed_yesterday_is_not_todays_realized(
    day_pnl_repo: ClerkSqliteRepository,
    seeded_round_trip_closed_yesterday: None,
    reader: SqliteEconomicProjectionReader,
) -> None:
    """The window is what makes this a *day* P&L, not a lifetime one."""
    pnl = day_pnl_at(reader, day_pnl_repo, observation=_observation(unrealized=0.0), now_ms=NOON)
    assert pnl.day_start_ms > YESTERDAY_NOON
    assert pnl.realized_usd == pytest.approx(0.0)
    assert pnl.total_usd == pytest.approx(0.0)


def test_an_open_position_contributes_only_the_brokers_unrealized(
    day_pnl_repo: ClerkSqliteRepository,
    seeded_open_buy: None,
    reader: SqliteEconomicProjectionReader,
) -> None:
    """No lot has closed, so the day's whole P&L is the broker's own figure."""
    pnl = day_pnl_at(reader, day_pnl_repo, observation=_observation(unrealized=-40.0), now_ms=NOON)
    assert pnl.realized_usd == pytest.approx(0.0)
    assert pnl.total_usd == pytest.approx(-40.0)


def test_an_external_order_seen_today_makes_the_fact_unknown(
    day_pnl_repo: ClerkSqliteRepository,
    seeded_external_order_today: None,
    reader: SqliteEconomicProjectionReader,
) -> None:
    pnl = day_pnl_at(reader, day_pnl_repo, observation=_observation(unrealized=0.0), now_ms=NOON)
    assert pnl.external_orders_today == 1 and not pnl.known
    # The fixture's foreign order is filled (status="filled", filled_avg_price=50.0,
    # quantity=3.0), so the projection's own external_fill_exists check trips and its
    # verdict is "incomplete" — independent of, and carried alongside, `known`.
    assert pnl.execution_coverage == "incomplete"


def test_an_external_order_seen_yesterday_does_not(
    day_pnl_repo: ClerkSqliteRepository,
    seeded_external_order_yesterday: None,
    reader: SqliteEconomicProjectionReader,
) -> None:
    assert day_pnl_at(reader, day_pnl_repo, observation=_observation(unrealized=0.0), now_ms=NOON).known


def test_net_cash_spent_is_buys_less_sells_over_every_subject(
    seeded_round_trip: None, reader: SqliteEconomicProjectionReader
) -> None:
    assert reader.account_net_cash_spent_usd() == pytest.approx(1_000.0 - 1_100.0)


def test_unreported_fees_net_nothing_and_say_so(
    day_pnl_repo: ClerkSqliteRepository,
    seeded_round_trip_without_fees: None,
    reader: SqliteEconomicProjectionReader,
) -> None:
    pnl = day_pnl_at(reader, day_pnl_repo, observation=_observation(unrealized=0.0), now_ms=NOON)
    assert pnl.fee_usd == 0.0 and pnl.fee_fidelity == "not_reported"
