"""Today's money statement (PRD #2560): a day figure in exact cents.

The change in open gains runs from the prior regular-session close, never
from each lot's cost -- lifetime unrealized P&L is not a day figure.
"""

from __future__ import annotations

from decimal import Decimal

from app.services.account_activity import compose_today_statement

_SINCE = 1_788_811_200_000
_NOW = 1_789_000_000_000


def _compose(**parts: object):
    base: dict[str, object] = dict(
        since_ms=_SINCE,
        observed_at_ms=_NOW,
        realized_usd=Decimal(0),
        fees_usd=Decimal("0"),
        start_open_usd=Decimal(0),
        open_usd=Decimal(0),
        prices_read=True,
        outside_activity=False,
    )
    base.update(parts)
    return compose_today_statement(**base)  # type: ignore[arg-type]


def test_net_is_the_displayed_parts_added_in_whole_cents() -> None:
    statement = _compose(
        realized_usd=Decimal("12.345"),  # half-to-even on the exact decimal reading: 12.34
        fees_usd=Decimal("0.41"),
        start_open_usd=Decimal("1.005"),
        open_usd=Decimal("-2.0"),  # change -3.005 -> -3.00 (half-to-even), never float-rounded to -3.01
    )
    assert statement.model_dump() == {
        "state": "ready",
        "detail": None,
        "since_ms": _SINCE,
        "observed_at_ms": _NOW,
        "realized_usd": "12.34",
        "fees_usd": "0.41",
        "open_change_usd": "-3.00",
        "net_usd": "8.93",
    }


def test_a_lot_carried_overnight_counts_only_its_move_since_the_close() -> None:
    # Bought at 400, closed last night at 402, worth 405 now: today moved it +3,
    # not the +5 it has gained since it was bought.
    statement = _compose(start_open_usd=Decimal("2.0"), open_usd=Decimal("5.0"))
    assert (statement.open_change_usd, statement.net_usd) == ("3.00", "3.00")


def test_unread_prices_leave_the_change_and_net_unknown_never_zero() -> None:
    statement = _compose(realized_usd=Decimal("1.0"), fees_usd=Decimal("0.02"), open_usd=None, prices_read=False)
    assert statement.state == "unavailable"
    assert (statement.realized_usd, statement.fees_usd, statement.open_change_usd, statement.net_usd) == (
        "1.00", "0.02", None, None,
    )
    assert statement.detail == (
        "Current prices could not be read, so the change in open gains and the net are not shown. Refresh to retry."
    )


def test_a_book_flat_at_both_ends_needs_no_price() -> None:
    statement = _compose(realized_usd=Decimal("1.0"), fees_usd=Decimal("0.02"), prices_read=False)
    assert (statement.state, statement.open_change_usd, statement.net_usd) == ("ready", "0.00", "0.98")


def test_a_share_held_now_without_a_price_leaves_the_change_unknown() -> None:
    statement = _compose(open_usd=None)
    assert statement.state == "unavailable"
    assert statement.open_change_usd is None and statement.net_usd is None
    assert statement.detail == (
        "A share held now has no current price, so the change in open gains and the net are not shown."
    )


def test_a_share_held_at_the_close_without_its_closing_price_leaves_the_change_unknown() -> None:
    statement = _compose(realized_usd=Decimal("0.5"), start_open_usd=None)
    assert statement.state == "unavailable"
    assert (statement.realized_usd, statement.open_change_usd, statement.net_usd) == ("0.50", None, None)
    assert statement.detail == (
        "A share held at the last close has no closing price here, "
        "so the change in open gains and the net are not shown."
    )


def test_unfinished_fee_evidence_leaves_fees_and_net_unknown() -> None:
    statement = _compose(realized_usd=Decimal("-2.5"), fees_usd=None, outside_activity=True)
    assert statement.state == "unavailable"
    assert (statement.realized_usd, statement.fees_usd, statement.open_change_usd, statement.net_usd) == (
        "-2.50", None, "0.00", None,
    )
    assert statement.detail == "Today's fees are not final yet, so fees and the net are not shown."


def test_a_ready_statement_says_when_outside_orders_are_left_out() -> None:
    statement = _compose(fees_usd=Decimal("0.01"), outside_activity=True)
    assert statement.state == "ready"
    assert statement.net_usd == "-0.01"
    assert statement.detail == (
        "Orders placed outside the bots, and account charges not matched to a bot, are not counted here."
    )
