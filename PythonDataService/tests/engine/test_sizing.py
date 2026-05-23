"""Parity tests for the SetHoldings sizing models.

``LeanSetHoldingsSizing`` must reproduce LEAN's ``SetHoldings`` share count
exactly (atol=0) against the golden fixture — 20 entries from a pinned LEAN
run. See docs/references/lean-set-holdings.md.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from app.engine.execution.sizing import (
    LEAN_FREE_PORTFOLIO_VALUE_PCT,
    LeanSetHoldingsSizing,
    SimpleFloorSizing,
)

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "golden" / "lean-set-holdings" / "entries.json"


def _load_entries() -> list[dict]:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def test_fixture_present_and_nonempty() -> None:
    entries = _load_entries()
    assert len(entries) == 20


def test_lean_sizing_reproduces_every_golden_entry() -> None:
    """Exact (atol=0) reproduction of LEAN's SetHoldings quantity."""
    sizing = LeanSetHoldingsSizing()
    mismatches: list[str] = []
    for e in _load_entries():
        qty = sizing.target_quantity(
            portfolio_value=Decimal(e["tpv"]),
            price=Decimal(e["price"]),
            target_fraction=Decimal(1),
            order_fee=Decimal(e["order_fee"]),
        )
        if qty != e["lean_qty"]:
            mismatches.append(f"tpv={e['tpv']} price={e['price']}: got {qty}, LEAN {e['lean_qty']}")
    assert not mismatches, "LEAN SetHoldings parity failed:\n" + "\n".join(mismatches)


def test_simple_floor_overbuys_vs_lean_on_the_fixture() -> None:
    """SimpleFloorSizing is NOT LEAN parity — it buys >= LEAN, and strictly
    more on at least one entry (the divergence the parity matrix surfaced)."""
    simple = SimpleFloorSizing()
    strictly_more = 0
    for e in _load_entries():
        qty = simple.target_quantity(
            portfolio_value=Decimal(e["tpv"]),
            price=Decimal(e["price"]),
            target_fraction=Decimal(1),
            order_fee=Decimal(e["order_fee"]),
        )
        assert qty >= e["lean_qty"]
        if qty > e["lean_qty"]:
            strictly_more += 1
    assert strictly_more > 0


def test_lean_free_portfolio_pct_is_lean_default() -> None:
    assert Decimal("0.0025") == LEAN_FREE_PORTFOLIO_VALUE_PCT
    assert LeanSetHoldingsSizing().free_portfolio_value_pct == Decimal("0.0025")


def test_lean_sizing_buffer_and_fee_reduce_quantity() -> None:
    """A worked example: $100,000 at $665.67 — naive floor is 150, LEAN 149."""
    pv, price = Decimal("100000"), Decimal("665.67")
    assert (
        SimpleFloorSizing().target_quantity(
            portfolio_value=pv, price=price, target_fraction=Decimal(1), order_fee=Decimal("1")
        )
        == 150
    )
    assert (
        LeanSetHoldingsSizing().target_quantity(
            portfolio_value=pv, price=price, target_fraction=Decimal(1), order_fee=Decimal("1")
        )
        == 149
    )


def test_partial_target_fraction_is_uncapped_by_the_buffer() -> None:
    """For a target well under 100%, the free-portfolio buffer never binds —
    the model sizes against target_fraction directly."""
    pv, price = Decimal("100000"), Decimal("100")
    qty = LeanSetHoldingsSizing().target_quantity(
        portfolio_value=pv, price=price, target_fraction=Decimal("0.5"), order_fee=Decimal(0)
    )
    assert qty == 500  # floor(100000 * 0.5 / 100)


@pytest.mark.parametrize("model", [SimpleFloorSizing(), LeanSetHoldingsSizing()])
def test_non_positive_price_raises(model) -> None:
    with pytest.raises(ValueError, match="price must be positive"):
        model.target_quantity(
            portfolio_value=Decimal("100000"),
            price=Decimal(0),
            target_fraction=Decimal(1),
            order_fee=Decimal(0),
        )


def test_lean_sizing_returns_zero_when_budget_below_one_share() -> None:
    qty = LeanSetHoldingsSizing().target_quantity(
        portfolio_value=Decimal("50"),
        price=Decimal("665.67"),
        target_fraction=Decimal(1),
        order_fee=Decimal("1"),
    )
    assert qty == 0
