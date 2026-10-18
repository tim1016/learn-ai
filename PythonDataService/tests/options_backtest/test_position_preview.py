"""Hand-worked expiry dollars and canonical replay evidence for the setup desk."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from types import SimpleNamespace

import pytest

from app.options.contract import OptionContract
from app.options_backtest.data import Captured
from app.options_backtest.market import MinuteBar
from app.options_backtest.picking import ListedContract, Listing
from app.options_backtest.position_preview import PreviewUnavailable, expiry_view, historical_preview, resolved_leg
from app.schemas.options_position_preview import PositionPreviewRequest
from tests.options_backtest._market import D, at
from tests.options_backtest.setup_fixtures import leg, setup_body

DAY = date(2026, 10, 9)


def contract(right: str, strike: str, day: date = DAY) -> OptionContract:
    return OptionContract.of(root="SPY", expiry_date=day, right=right, strike=D(strike))


def priced(side: str, right: str, strike: str, premium: str, count: int = 1):
    return resolved_leg(f"{side}_{right}", side, count, contract(right, strike), D(premium), at(5), "test close")


def test_expiry_credit_spread_exact_knots_extremes_and_breakeven() -> None:
    rows = [priced("buy", "call", "503", "1"), priced("sell", "call", "498", "4")]
    prices, curves, risk = expiry_view(rows, D("500"))
    assert D(risk.entry_debit_usd) == D("-300")
    assert D(risk.max_profit_usd) == D("300")
    assert D(risk.max_loss_usd) == D("200")
    assert [D(value) for value in risk.breakevens_usd] == [D("501")]
    values = dict(zip(map(D, prices), map(D, curves[0].pnl_usd), strict=True))
    assert values[D("498")] == D("300")
    assert values[D("501")] == D(0)
    assert values[D("503")] == D("-200")
    assert len(curves) == 3


def test_put_quantity_and_unlimited_are_not_chart_window_bounds() -> None:
    _, _, long = expiry_view([priced("buy", "put", "500", "2", 3)], D("500"))
    assert D(long.max_loss_usd) == D("600")
    assert D(long.max_profit_usd) == D("149400")
    _, _, short = expiry_view([priced("sell", "call", "500", "2", 3)], D("500"))
    assert short.max_loss_usd is None
    assert D(short.max_profit_usd) == D("600")


def test_mixed_expiries_and_flat_replay_do_not_invent_a_common_payoff() -> None:
    rows = [priced("buy", "call", "500", "2"), priced("buy", "put", "500", "2")]
    rows[1] = rows[1].model_copy(update={"expiry_ms": contract("put", "500", date(2026, 10, 16)).expiry_ms})
    assert expiry_view(rows, D("500")) == ([], [], None)
    _, curves, risk = expiry_view([], D("500"), cash_adjustment=D("23.45"))
    assert set(map(D, curves[0].pnl_usd)) == {D("23.45")}
    assert D(risk.max_profit_usd) == D("23.45")
    assert D(risk.max_loss_usd) == 0


def data():
    call = contract("call", "500")
    bars = tuple(MinuteBar(at(i), D("500"), D("500"), D("500"), D("500")) for i in range(1, 391))
    option = tuple(MinuteBar(at(i), D("2"), D("2"), D("2"), D("2")) for i in range(1, 391))
    listing = Listing("SPY", DAY, (ListedContract(call, 100, True),), True)

    def capture(value):
        return Captured(value, "a" * 64)

    return SimpleNamespace(
        underlying_day=lambda _: capture(bars),
        listing=lambda _: capture(listing),
        option_day=lambda *_: capture(option),
    )


def request(**changes):
    return PositionPreviewRequest.model_validate(
        {
            "setup": setup_body(slippage_percent="0", fees={"kind": "per_contract", "per_contract_usd": "1"}),
            "source": "historical",
            "trading_date_ms": at(5),
            **changes,
        }
    )


def test_history_picker_uses_only_entry_minute() -> None:
    market = data()
    result = historical_preview(request(), market)
    assert result.legs[0].symbol == contract("call", "500").occ_symbol
    assert result.legs[0].price_at_ms == at(5)
    assert D(result.legs[0].premium_usd) == D(2)
    original = market.option_day
    market.option_day = lambda *args: replace(
        original(*args), value=tuple(bar for bar in original(*args).value if bar.close_ms != at(5))
    )
    with pytest.raises(PreviewUnavailable, match="no trade in the entry minute"):
        historical_preview(request(), market)


def test_replay_waits_for_fill_and_retains_realized_fees() -> None:
    pending = historical_preview(request(mode="replay", replay_at_ms=at(5)), data())
    assert pending.legs == []
    held = historical_preview(request(mode="replay", replay_at_ms=at(6)), data())
    assert len(held.legs) == 1
    assert held.legs[0].price_at_ms == at(6)
    assert D(held.risk.max_loss_usd) == D("201")
    closed = historical_preview(request(mode="replay", replay_at_ms=at(345)), data())
    assert closed.legs == []
    assert set(map(D, closed.curves[0].pnl_usd)) == {D("-2")}
    assert any(row.kind == "close_filled" for row in closed.events)


def test_unfilled_momentum_leg_never_appears_as_held() -> None:
    body = setup_body(legs=[leg(momentum={"kind": "percent_up", "value": "25"})])
    result = historical_preview(request(setup=body, mode="replay", replay_at_ms=at(30)), data())
    assert result.legs == []
    assert all(row.kind != "entry_filled" for row in result.events)


def test_replay_cash_adjustment_moves_the_canonical_breakeven() -> None:
    _, _, risk = expiry_view([priced("buy", "call", "500", "2")], D("500"), cash_adjustment=D("-1"))
    assert [D(value) for value in risk.breakevens_usd] == [D("502.01")]
    assert D(risk.max_loss_usd) == D("201")
