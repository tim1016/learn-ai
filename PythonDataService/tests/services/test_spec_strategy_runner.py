"""Tests for app.services.spec_strategy_runner.

Pure-mechanism tests: synthetic bars feed both a hand-coded reference and the
spec, exercise the OrderEvent capture path, and confirm the resulting
EngineTrade objects carry quantities and pnl. The synthetic-bar parity itself
(spec ≡ hand-coded) is already covered by tests/spec/test_spec_spy_ema_parity.py;
this file is about the capture mechanism, not strategy correctness.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
import respx

from app.engine.execution.order import Direction, OrderEvent
from app.engine.strategy.spec.tests._parity_helpers import (
    build_minute_bars,
    closes_for_spy_ema,
    fixture_path,
)
from app.services.spec_strategy_runner import (
    InMemoryDataReader,
    pair_engine_fills,
    run_spec_against_bars,
    run_spec_against_bars_and_persist,
)


def _event(
    *,
    order_id: int = 1,
    symbol: str = "SPY",
    time: datetime | None = None,
    fill_price: str = "100",
    fill_quantity: int = 10,
    direction: Direction = Direction.LONG,
    fee: str = "0",
    tag: str = "",
) -> OrderEvent:
    return OrderEvent(
        order_id=order_id,
        symbol=symbol,
        time=time or datetime(2025, 1, 6, 14, 30, tzinfo=UTC),
        fill_price=Decimal(fill_price),
        fill_quantity=fill_quantity,
        direction=direction,
        fee=Decimal(fee),
        tag=tag,
    )


class TestPairEngineFills:
    def test_empty_event_list_returns_empty_trades(self) -> None:
        assert pair_engine_fills([]) == []

    def test_long_then_short_pair_produces_one_trade(self) -> None:
        entry = _event(direction=Direction.LONG, fill_price="100", fill_quantity=10)
        exit_ = _event(
            order_id=2,
            time=datetime(2025, 1, 6, 14, 45, tzinfo=UTC),
            direction=Direction.SHORT,
            fill_price="101",
            fill_quantity=-10,
        )

        trades = pair_engine_fills([entry, exit_])

        assert len(trades) == 1
        t = trades[0]
        assert t.trade_number == 1
        assert t.entry_price == Decimal("100")
        assert t.exit_price == Decimal("101")
        assert t.quantity == Decimal("10")  # positive entry qty
        assert t.pnl == Decimal("10")  # (101-100) * 10
        assert t.is_synthetic_exit is False

    def test_long_then_flat_also_pairs_as_exit(self) -> None:
        # Some strategies emit Direction.FLAT (explicit liquidate) instead of
        # SHORT. The pairer must accept both as exit signals.
        entry = _event(direction=Direction.LONG, fill_price="100", fill_quantity=10)
        exit_ = _event(
            order_id=2,
            time=datetime(2025, 1, 6, 14, 45, tzinfo=UTC),
            direction=Direction.FLAT,
            fill_price="99",
            fill_quantity=-10,
        )

        trades = pair_engine_fills([entry, exit_])

        assert len(trades) == 1
        assert trades[0].pnl == Decimal("-10")

    def test_force_flat_tag_marks_synthetic_exit(self) -> None:
        entry = _event(direction=Direction.LONG, fill_price="100", fill_quantity=10)
        exit_ = _event(
            order_id=2,
            time=datetime(2025, 1, 6, 14, 45, tzinfo=UTC),
            direction=Direction.SHORT,
            fill_price="101",
            fill_quantity=-10,
            tag="ForceFlat",
        )

        trades = pair_engine_fills([entry, exit_])

        assert trades[0].is_synthetic_exit is True

    def test_fee_subtracted_from_pnl(self) -> None:
        entry = _event(direction=Direction.LONG, fill_price="100", fill_quantity=10, fee="0.50")
        exit_ = _event(
            order_id=2,
            time=datetime(2025, 1, 6, 14, 45, tzinfo=UTC),
            direction=Direction.SHORT,
            fill_price="101",
            fill_quantity=-10,
            fee="0.75",
        )

        trades = pair_engine_fills([entry, exit_])

        # Gross 10.00 - (0.50 + 0.75) = 8.75
        assert trades[0].pnl == Decimal("8.75")

    def test_pyramiding_raises(self) -> None:
        e1 = _event(direction=Direction.LONG)
        e2 = _event(direction=Direction.LONG, time=datetime(2025, 1, 6, 14, 45, tzinfo=UTC))

        with pytest.raises(NotImplementedError, match="Pyramiding"):
            pair_engine_fills([e1, e2])

    def test_unmatched_exit_raises(self) -> None:
        flat = _event(direction=Direction.FLAT, fill_quantity=-10)

        with pytest.raises(ValueError, match="Unmatched FLAT"):
            pair_engine_fills([flat])

    def test_open_position_at_end_raises(self) -> None:
        entry = _event(direction=Direction.LONG)

        with pytest.raises(ValueError, match="ended with an open LONG"):
            pair_engine_fills([entry])

    def test_multiple_round_trips(self) -> None:
        events = [
            _event(
                order_id=1,
                time=datetime(2025, 1, 6, 14, 30, tzinfo=UTC),
                direction=Direction.LONG,
                fill_price="100",
                fill_quantity=10,
            ),
            _event(
                order_id=2,
                time=datetime(2025, 1, 6, 14, 45, tzinfo=UTC),
                direction=Direction.SHORT,
                fill_price="102",
                fill_quantity=-10,
            ),
            _event(
                order_id=3,
                time=datetime(2025, 1, 6, 15, 0, tzinfo=UTC),
                direction=Direction.LONG,
                fill_price="103",
                fill_quantity=10,
            ),
            _event(
                order_id=4,
                time=datetime(2025, 1, 6, 15, 15, tzinfo=UTC),
                direction=Direction.SHORT,
                fill_price="101",
                fill_quantity=-10,
            ),
        ]

        trades = pair_engine_fills(events)

        assert len(trades) == 2
        assert trades[0].trade_number == 1
        assert trades[0].pnl == Decimal("20")  # 2 * 10
        assert trades[1].trade_number == 2
        assert trades[1].pnl == Decimal("-20")  # -2 * 10


class TestInMemoryDataReader:
    def test_filters_by_symbol(self) -> None:
        from datetime import date

        bars = build_minute_bars(closes_for_spy_ema(50))
        reader = InMemoryDataReader(bars=bars)

        spy_bars = list(reader.iter_bars("SPY", date(2020, 1, 1), date(2030, 12, 31)))
        wrong_bars = list(reader.iter_bars("QQQ", date(2020, 1, 1), date(2030, 12, 31)))

        # The synthetic helper uses symbol="TEST" so neither matches; just confirm
        # the symbol filter actually filters (no symbol => no bars).
        assert spy_bars == []
        assert wrong_bars == []

    def test_filters_by_date_range(self) -> None:
        from datetime import date

        bars = build_minute_bars(closes_for_spy_ema(100))
        reader = InMemoryDataReader(bars=bars)
        # Bars start at 2024-01-02 per START_TIME in _parity_helpers
        in_range = list(reader.iter_bars("TEST", date(2024, 1, 2), date(2024, 1, 5)))
        out_of_range = list(reader.iter_bars("TEST", date(2030, 1, 1), date(2030, 12, 31)))

        assert len(in_range) > 0
        assert out_of_range == []


class TestRunSpecAgainstBars:
    def test_runs_spec_and_captures_trades_from_synthetic_bars(self) -> None:
        bars = build_minute_bars(closes_for_spy_ema(2000))

        result = run_spec_against_bars(
            spec_path=fixture_path("spy_ema_crossover"),
            symbol="TEST",
            bars=bars,
            start_date=(2024, 1, 2),
            end_date=(2024, 12, 31),
        )

        # The synthetic SPY EMA generator is tuned to fire trades — confirm >= 1
        # so we know the runner actually exercised the strategy path.
        assert len(result.trades) >= 1
        # Quantities are populated from OrderEvent.fill_quantity (positive int → Decimal)
        for t in result.trades:
            assert t.quantity > 0
            assert t.entry_ms_utc < t.exit_ms_utc
            assert t.entry_price > 0
            assert t.exit_price > 0
        # No commissions configured → fees are zero
        assert result.total_fees == Decimal("0")
        # captured_events length is 2x trades (one LONG + one FLAT per round-trip)
        assert len(result.captured_events) == 2 * len(result.trades)


class TestRunSpecAgainstBarsAndPersist:
    @pytest.mark.asyncio
    async def test_persists_engine_run_after_capturing_trades(self) -> None:
        backend_url = "http://test-backend"
        bars = build_minute_bars(closes_for_spy_ema(2000))

        async with respx.mock(base_url=backend_url, assert_all_called=True) as mock:
            mock.post("/api/backtest-runs/persist-lean").mock(
                return_value=httpx.Response(200, json={"strategy_execution_id": 7})
            )

            result = await run_spec_against_bars_and_persist(
                spec_path=fixture_path("spy_ema_crossover"),
                symbol="TEST",
                bars=bars,
                start_date=(2024, 1, 2),
                end_date=(2024, 12, 31),
                starting_cash=Decimal("100000"),
                backend_url=backend_url,
                strategy_name="ema_crossover",
            )

            assert result.strategy_execution_id == 7
            assert len(result.trades) >= 1
