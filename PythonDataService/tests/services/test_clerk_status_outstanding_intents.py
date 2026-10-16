"""What counts as an order intent still without a known outcome (#3103).

The Deploy page's *intent custody* gate reads ``ClerkStatus.outstanding_intents``. A bull call spread that
filled left the account at one for the rest of the day, blocking every new deployment: the parent order
has no execution of its own (its legs carry the fills), so "filled with nothing filled" read as unresolved.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.broker.alpaca.clerk.sqlite.projection_models import ProjectedOrder
from app.services.sqlite_clerk_compat import _operation_requires_reconciliation


def _order(
    ref: str, *, state: str | None = "filled", filled: float | None = 1.0, symbol: str | None = "SPY261016C00771000",
    side: str | None = "buy",
) -> ProjectedOrder:
    return ProjectedOrder(
        order_ref=ref, client_order_id=ref, broker_order_id=ref, role="OTHER", broker_state=state,
        updated_at_ms=1, symbol=symbol, side=side, quantity=1.0, filled_quantity=filled,
    )


def _enter(*orders: ProjectedOrder) -> SimpleNamespace:
    return SimpleNamespace(state="in_progress", kind="ENTER", orders=orders)


def test_a_filled_single_leg_entry_with_its_fill_is_settled() -> None:
    assert _operation_requires_reconciliation(_enter(_order("a", filled=1.0))) is False


def test_a_filled_order_with_nothing_filled_is_still_unsettled() -> None:
    assert _operation_requires_reconciliation(_enter(_order("a", filled=0.0))) is True


def test_a_filled_several_leg_parent_is_settled_by_its_legs_fills() -> None:
    parent = _order("p", filled=0.0, symbol=None, side=None)
    legs = (_order("p~1", filled=1.0), _order("p~2", filled=1.0, side="sell"))

    assert _operation_requires_reconciliation(_enter(parent, *legs)) is False


def test_a_filled_several_leg_parent_whose_legs_show_no_fill_is_unsettled() -> None:
    parent = _order("p", filled=0.0, symbol=None, side=None)
    legs = (_order("p~1", filled=0.0), _order("p~2", filled=0.0, side="sell"))

    assert _operation_requires_reconciliation(_enter(parent, *legs)) is True


def test_a_filled_order_that_names_a_symbol_is_not_excused_by_a_neighbours_fill() -> None:
    lone = _order("a", filled=0.0)
    other = _order("b", filled=1.0)

    assert _operation_requires_reconciliation(_enter(lone, other)) is True
