"""Tests for the ShadowFillSimulator (PRD-C).

Pure-function fill-model dispatch over synthetic bars: deterministic, no I/O,
no randomness. Asserts on the synthesised ``shadow_sim`` ExecutionRow.
"""

from __future__ import annotations

from app.broker.ibkr.models import IbkrOrderSpec
from app.engine.live.divergence.bar_series_joiner import CanonicalBar
from app.engine.live.shadow_fill_simulator import (
    PendingFill,
    UnknownFillModel,
    simulate_shadow_fill,
)


def _order(action: str = "BUY", quantity: float = 10.0) -> IbkrOrderSpec:
    return IbkrOrderSpec(
        symbol="SPY",
        sec_type="STK",
        action=action,
        quantity=quantity,
        order_type="MKT",
        time_in_force="DAY",
        confirm_paper=True,
    )


def _bar(bar_close_ms: int, open_: float) -> CanonicalBar:
    return CanonicalBar(
        bar_close_ms=bar_close_ms, open=open_, high=open_, low=open_, close=open_, volume=100.0
    )


def test_next_bar_open_fills_at_next_bar_open_price() -> None:
    row = simulate_shadow_fill(
        _order(action="BUY", quantity=10),
        source_bar=_bar(1000, 100.0),
        next_bar=_bar(2000, 100.25),
        fill_model="NEXT_BAR_OPEN",
        account_id="SHADOW",
        strategy_instance_id="spy_vwap_reversion_1min",
    )

    assert not isinstance(row, PendingFill)
    assert row.fill_price == 100.25  # next bar's open
    assert row.fill_quantity == 10  # signed buy
    assert row.source_bar_close_ms == 1000
    assert row.fill_model == "NEXT_BAR_OPEN"
    assert row.execution_source == "shadow_sim"
    # exec_id is shadow-namespaced so it can never collide with a real IBKR execId.
    assert row.exec_id.startswith("shadow:")


def test_sell_preserves_negative_signed_quantity() -> None:
    row = simulate_shadow_fill(
        _order(action="SELL", quantity=10),
        source_bar=_bar(1000, 100.0),
        next_bar=_bar(2000, 100.0),
        fill_model="NEXT_BAR_OPEN",
        account_id="SHADOW",
        strategy_instance_id="inst",
    )
    assert row.fill_quantity == -10


def test_determinism_same_inputs_same_row() -> None:
    args = dict(
        source_bar=_bar(1000, 100.0),
        next_bar=_bar(2000, 100.25),
        fill_model="NEXT_BAR_OPEN",
        account_id="SHADOW",
        strategy_instance_id="inst",
    )
    a = simulate_shadow_fill(_order(), **args)
    b = simulate_shadow_fill(_order(), **args)
    assert a == b


def test_missing_next_bar_defers_with_pending_fill() -> None:
    result = simulate_shadow_fill(
        _order(),
        source_bar=_bar(1000, 100.0),
        next_bar=None,
        fill_model="NEXT_BAR_OPEN",
        account_id="SHADOW",
        strategy_instance_id="inst",
    )
    assert isinstance(result, PendingFill)
    assert result.source_bar_close_ms == 1000  # no fabricated fill price


def test_unknown_fill_model_raises() -> None:
    import pytest

    with pytest.raises(UnknownFillModel):
        simulate_shadow_fill(
            _order(),
            source_bar=_bar(1000, 100.0),
            next_bar=_bar(2000, 100.0),
            fill_model="VWAP_BAND_TOUCH",
            account_id="SHADOW",
            strategy_instance_id="inst",
        )
