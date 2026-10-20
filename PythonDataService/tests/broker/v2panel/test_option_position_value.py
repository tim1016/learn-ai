"""Public pane parity with the canonical exit estimate, to the cent (#3368)."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from app.broker.alpaca.clerk.exit_estimate import exit_estimate
from app.broker.alpaca.clerk.fifo_pnl import LotWriteOff
from app.broker.alpaca.clerk.fills import FillRecord
from app.broker.alpaca.clerk.kept_option_quotes import KeptOptionQuote
from app.broker.alpaca.clerk.money import dollars
from app.broker.alpaca.clerk.option_effects import RecordedQuote
from app.broker.contract.models import OrderSide
from app.options.contract import OptionContract
from app.options.quotes import HeldQuote, QuoteDataType, QuoteSide
from app.schemas.broker_v2_panel import ChartBar, ChartHistoryResponse
from app.services.broker_v2_panel.chart_projection_service import build_live_chart
from app.services.broker_v2_panel.option_position_value import position_value_pane
from app.services.live_chart_window import CHART_FEED_NOT_EXPECTED, ChartWindowResult

START = 1_788_876_000_000 // 60_000 * 60_000
SYMBOLS = [OptionContract.of(root="SPY", expiry_date=date(2026, 9, 18), right="call" if i < 2 else "put", strike=580 + i * 5).occ_symbol for i in range(4)]


def _fill(index: int, quantity: int, at_ms: int = START - 1) -> FillRecord:
    return FillRecord(account_id="paper", sid="bot", intent_id="intent", order_ref=f"order-{index}", event_key=f"fill-{index}-{at_ms}",
                      symbol=SYMBOLS[index], side=OrderSide.BUY if quantity > 0 else OrderSide.SELL, quantity=abs(quantity),
                      fill_price=2, filled_at_ms=at_ms, fee=None, multiplier=100)


def _quote(at_ms: int, *, one_sided: bool = False) -> HeldQuote:
    return HeldQuote(bid=QuoteSide(Decimal("2.00"), at_ms - 100),
                     ask=None if one_sided else QuoteSide(Decimal("2.10"), at_ms - 100), data_type=QuoteDataType.LIVE, generation=1)


def _row(index: int, minute: int, *, one_sided: bool = False) -> KeptOptionQuote:
    at = START + minute * 60_000 + 1_000
    return KeptOptionQuote(seq=minute * 10 + index + 1, symbol=SYMBOLS[index], quote=RecordedQuote.of(_quote(at, one_sided=one_sided)),
                           kept_at_ms=at, minute_ms=START + minute * 60_000)


def _chart() -> ChartHistoryResponse:
    return ChartHistoryResponse(strategy_instance_id="bot", symbol="SPY", timeframe="1m", from_ms=START, to_ms=START + 180_000,
        bars=[ChartBar(start_ms=START + i * 60_000, end_ms=START + (i + 1) * 60_000, open="580", high="581", low="579", close="580", volume=1, source="ibkr") for i in range(3)],
        fill_markers=[], truncated=False, as_of_ms=START + 180_000,
        indicator_bars=[], indicator_bar_budget=0, indicator_bar_budget_satisfied=True)


@pytest.mark.parametrize(("quantities", "expected_cents"), [([2], -40000), ([2, -2], 2000), ([2, -2, -2, 2], 4000)])
def test_minute_value_equals_exit_estimate_for_long_covered_short_and_four_legs(quantities: list[int], expected_cents: int) -> None:
    fills = [_fill(i, qty) for i, qty in enumerate(quantities)]
    rows = [_row(i, 0) for i in range(len(quantities))]
    legs = {(SYMBOLS[i], 100): qty for i, qty in enumerate(quantities)}
    estimate = exit_estimate(legs, {row.symbol: row.quote.held for row in rows}, now_ms=START + 1_000,
                             quotes_from="Saved minute quotes", none_held="No saved minute quote")
    pane = position_value_pane(_chart(), fills, rows, symbol="SPY", through_ms=START + 180_000)
    assert estimate.net_close.amount_cents == expected_cents
    assert pane.kind == "value" and pane.points[0].value == dollars(expected_cents)
    assert pane.points[0].value == dollars(estimate.net_close.amount_cents)
    assert [point.value for point in pane.points[1:]] == [None, None]


def test_partial_closes_change_future_values_and_one_sided_minutes_are_gaps() -> None:
    fills = [_fill(0, 3), _fill(0, -1, START + 90_000)]
    rows = [_row(0, 0), _row(0, 1), _row(0, 2, one_sided=True)]
    pane = position_value_pane(_chart(), fills, rows, symbol="SPY", through_ms=START + 180_000)
    assert [point.value for point in pane.points] == ["-600.00", "-400.00", None]
    assert pane.fill_markers[0].symbol == SYMBOLS[0]
    # Missing one leg of a structure never publishes the other leg alone.
    pane = position_value_pane(_chart(), [_fill(0, 1), replace(_fill(1, -1), filled_at_ms=START)], rows, symbol="SPY", through_ms=START + 180_000)
    assert all(point.value is None for point in pane.points)


def test_flat_minutes_and_discharge_never_resurrect_an_old_position() -> None:
    fills = [replace(_fill(0, 3), ledger_sequence=1), replace(_fill(0, 2, START + 120_000), ledger_sequence=20)]
    discharge = LotWriteOff(symbol=SYMBOLS[0], exact_qty=Decimal(3), written_off_at_ms=START + 60_000, ledger_sequence=10, sid="bot")
    rows = [_row(0, minute) for minute in range(3)]
    pane = position_value_pane(_chart(), fills, rows, symbol="SPY", through_ms=START + 180_000, write_offs=[discharge])
    assert [point.value for point in pane.points] == ["-600.00", None, "-400.00"]
    # Account-wide quotes from another bot do not create a position before entry or after close.
    pane = position_value_pane(_chart(), [_fill(0, 1, START + 60_000), _fill(0, -1, START + 120_000)], rows, symbol="SPY", through_ms=START + 180_000)
    assert [point.value for point in pane.points] == [None, "-200.00", None]


def test_current_value_and_embedded_card_share_one_cut_without_a_completed_underlying_candle() -> None:
    through = START + 130_000
    live = build_live_chart(ChartWindowResult(bars=[], timeframe="1m", resolution="1m", feed=CHART_FEED_NOT_EXPECTED), (),
        strategy_instance_id="bot", symbol="SPY", window=(START, START + 180_000), now_ms=through)
    live = live.model_copy(update={"bars": _chart().bars[:2]})
    fills = [_fill(0, 3), _fill(0, -1, START + 125_000)]
    pane = position_value_pane(live, fills, [_row(0, i) for i in range(3)], symbol="SPY", through_ms=through)
    assert pane.points[-1].start_ms == START + 120_000
    assert pane.points[-1].value == "-400.00"
    assert pane.exit_estimate is not None and pane.exit_estimate.net_close.amount_usd == pane.points[-1].value
    assert len(live.bars) == 2
