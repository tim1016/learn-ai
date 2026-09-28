"""One Activity period's money statement (PRD #2560): canonical parts, exact cents."""

from __future__ import annotations

from decimal import Decimal

from app.services.alpaca_fee_reconciliation import compose_period_statement


def test_net_is_the_displayed_parts_added_in_whole_cents() -> None:
    statement = compose_period_statement(
        realized_usd=12.345,  # half-to-even on the exact decimal reading: 12.34
        fees_usd=Decimal("0.41"),
        open_usd=-3.005,  # -3.00 (half-to-even), never float-rounded to -3.01
        prices_read=True,
        outside_activity=False,
    )
    assert statement.model_dump() == {
        "state": "ready",
        "detail": None,
        "realized_usd": "12.34",
        "fees_usd": "0.41",
        "open_usd": "-3.00",
        "net_usd": "8.93",
    }


def test_unread_prices_leave_open_and_net_unknown_never_zero() -> None:
    statement = compose_period_statement(
        realized_usd=1.0, fees_usd=Decimal("0.02"), open_usd=None, prices_read=False, outside_activity=False,
    )
    assert statement.state == "unavailable"
    assert (statement.realized_usd, statement.fees_usd, statement.open_usd, statement.net_usd) == (
        "1.00", "0.02", None, None,
    )
    assert statement.detail == (
        "Current prices are unavailable, so open gains and the net are not shown. Refresh to retry."
    )


def test_a_held_share_without_a_price_leaves_open_and_net_unknown() -> None:
    statement = compose_period_statement(
        realized_usd=0.0, fees_usd=Decimal("0"), open_usd=None, prices_read=True, outside_activity=False,
    )
    assert statement.state == "unavailable"
    assert statement.open_usd is None and statement.net_usd is None
    assert statement.detail == "A held share has no current price, so open gains and the net are not shown."


def test_unfinished_fee_evidence_leaves_fees_and_net_unknown() -> None:
    statement = compose_period_statement(
        realized_usd=-2.5, fees_usd=None, open_usd=0.0, prices_read=True, outside_activity=True,
    )
    assert statement.state == "unavailable"
    assert (statement.realized_usd, statement.fees_usd, statement.open_usd, statement.net_usd) == (
        "-2.50", None, "0.00", None,
    )
    assert statement.detail == "Fees for this period are not final yet, so fees and the net are not shown."


def test_a_ready_statement_says_when_outside_orders_are_left_out() -> None:
    statement = compose_period_statement(
        realized_usd=0.0, fees_usd=Decimal("0.01"), open_usd=0.0, prices_read=True, outside_activity=True,
    )
    assert statement.state == "ready"
    assert statement.net_usd == "-0.01"
    assert statement.detail == (
        "Orders placed outside the bots, and account charges not matched to a bot, are not counted here."
    )
