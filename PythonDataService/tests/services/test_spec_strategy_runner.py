"""Tests for app.services.spec_strategy_runner.

Pure-mechanism tests: synthetic bars feed both a hand-coded reference and the
spec, exercise the OrderEvent capture path, and confirm the resulting
EngineTrade objects carry quantities and pnl. The synthetic-bar parity itself
(spec ≡ hand-coded) is already covered by tests/spec/test_spec_spy_ema_parity.py;
this file is about the capture mechanism, not strategy correctness.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

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

    @pytest.mark.parametrize(
        ("exit_direction", "exit_price", "entry_fee", "exit_fee", "expected_pnl"),
        [
            # (101-100) * 10
            pytest.param(Direction.SHORT, "101", "0", "0", "10", id="long-then-short-pair-produces-one-trade"),
            # Some strategies emit Direction.FLAT (explicit liquidate) instead of
            # SHORT. The pairer must accept both as exit signals.
            pytest.param(Direction.FLAT, "99", "0", "0", "-10", id="long-then-flat-also-pairs-as-exit"),
            # Gross 10.00 - (0.50 + 0.75) = 8.75
            pytest.param(Direction.SHORT, "101", "0.50", "0.75", "8.75", id="fee-subtracted-from-pnl"),
        ],
    )
    def test_a_long_entry_and_its_exit_pair_into_one_trade_with_its_pnl(
        self, exit_direction: Direction, exit_price: str, entry_fee: str, exit_fee: str, expected_pnl: str
    ) -> None:
        entry = _event(direction=Direction.LONG, fill_price="100", fill_quantity=10, fee=entry_fee)
        exit_ = _event(
            order_id=2,
            time=datetime(2025, 1, 6, 14, 45, tzinfo=UTC),
            direction=exit_direction,
            fill_price=exit_price,
            fill_quantity=-10,
            fee=exit_fee,
        )

        trades = pair_engine_fills([entry, exit_])

        assert len(trades) == 1, "the entry and its exit pair into one trade"
        t = trades[0]
        assert t.trade_number == 1, "the first trade is number 1"
        assert t.entry_price == Decimal("100"), "the trade enters at the entry fill price"
        assert t.exit_price == Decimal(exit_price), "the trade exits at the exit fill price"
        assert t.quantity == Decimal("10"), "the quantity is the positive entry qty"
        assert t.pnl == Decimal(expected_pnl), "pnl is gross of the price move less both fees"
        assert t.is_synthetic_exit is False, "a paired exit is not a synthetic exit"

    def test_pyramiding_raises(self) -> None:
        e1 = _event(direction=Direction.LONG)
        e2 = _event(direction=Direction.LONG, time=datetime(2025, 1, 6, 14, 45, tzinfo=UTC))

        with pytest.raises(NotImplementedError, match="Pyramiding"):
            pair_engine_fills([e1, e2])

    @pytest.mark.parametrize(
        ("direction", "fill_quantity", "reason"),
        [
            pytest.param(Direction.FLAT, -10, "Unmatched FLAT", id="unmatched-exit"),
            pytest.param(Direction.LONG, 10, "ended with an open LONG", id="open-position-at-end"),
        ],
    )
    def test_an_unpaired_fill_raises(self, direction: Direction, fill_quantity: int, reason: str) -> None:
        unpaired = _event(direction=direction, fill_quantity=fill_quantity)

        with pytest.raises(ValueError, match=reason):
            pair_engine_fills([unpaired])

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
    @pytest.mark.parametrize(
        ("closes", "symbol", "from_date", "to_date", "has_bars"),
        [
            # The synthetic helper uses symbol="TEST" so neither SPY nor QQQ matches; just confirm
            # the symbol filter actually filters (no symbol => no bars).
            pytest.param(50, "SPY", date(2020, 1, 1), date(2030, 12, 31), False, id="filters-by-symbol-spy"),
            pytest.param(50, "QQQ", date(2020, 1, 1), date(2030, 12, 31), False, id="filters-by-symbol-qqq"),
            # Bars start at 2024-01-02 per START_TIME in _parity_helpers
            pytest.param(100, "TEST", date(2024, 1, 2), date(2024, 1, 5), True, id="filters-by-date-range-in-range"),
            pytest.param(
                100, "TEST", date(2030, 1, 1), date(2030, 12, 31), False, id="filters-by-date-range-out-of-range"
            ),
        ],
    )
    def test_iter_bars_returns_only_the_symbol_and_dates_asked_for(
        self, closes: int, symbol: str, from_date: date, to_date: date, has_bars: bool
    ) -> None:
        reader = InMemoryDataReader(bars=build_minute_bars(closes_for_spy_ema(closes)))

        selected = list(reader.iter_bars(symbol, from_date, to_date))

        assert (len(selected) > 0) == has_bars, "bars come back only for the matching symbol inside the date range"


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


class TestRunSpecAgainstBarsStartingCash:
    def test_starting_cash_propagates_to_engine_sizing_and_defaults_to_the_spec(self) -> None:
        """starting_cash must flow into the engine, not just the persist payload.

        Run the same bars at 100k and 200k starting cash and assert that the
        quantities differ — at 200k the engine can afford twice as many shares,
        so at least one trade must have a larger quantity. If starting_cash were
        ignored (P1 bug), both runs would produce identical quantities.

        When starting_cash is None, the spec's own set_cash default is used (100 000).
        """
        bars = build_minute_bars(closes_for_spy_ema(2000))

        result_100k = run_spec_against_bars(
            spec_path=fixture_path("spy_ema_crossover"),
            symbol="TEST",
            bars=bars,
            start_date=(2024, 1, 2),
            end_date=(2024, 12, 31),
            starting_cash=Decimal("100000"),
        )
        result_200k = run_spec_against_bars(
            spec_path=fixture_path("spy_ema_crossover"),
            symbol="TEST",
            bars=bars,
            start_date=(2024, 1, 2),
            end_date=(2024, 12, 31),
            starting_cash=Decimal("200000"),
        )
        result_default = run_spec_against_bars(
            spec_path=fixture_path("spy_ema_crossover"),
            symbol="TEST",
            bars=bars,
            start_date=(2024, 1, 2),
            end_date=(2024, 12, 31),
            # starting_cash omitted → None → spec default of 100 000
        )

        assert len(result_100k.trades) >= 1, "need at least one trade to compare sizing"
        assert len(result_100k.trades) == len(result_200k.trades), (
            "trade count changed — sizing difference must only affect quantities"
        )

        # At least one trade must have a strictly larger quantity at 200k.
        assert any(
            t200.quantity > t100.quantity for t100, t200 in zip(result_100k.trades, result_200k.trades, strict=True)
        ), "200k run produced no larger quantities than 100k run — starting_cash not applied"

        assert len(result_100k.trades) == len(result_default.trades), (
            "an omitted starting_cash changed the trade count against an explicit 100 000"
        )
        for number, (t_explicit, t_default) in enumerate(zip(result_100k.trades, result_default.trades, strict=True)):
            assert t_explicit.quantity == t_default.quantity, (
                f"trade {number}: an omitted starting_cash must size like an explicit 100 000"
            )


class TestRunSpecAgainstBarsAndPersist:
    @pytest.mark.asyncio
    async def test_persists_engine_run_after_capturing_trades(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.research.backtest_runs.service import SaveOutcome
        from app.services import engine_persistence

        bars = build_minute_bars(closes_for_spy_ema(2000))
        written: list[dict] = []

        async def fake_persist(payload: dict) -> SaveOutcome:
            written.append(payload)
            return SaveOutcome(status="saved", run_id=7)

        monkeypatch.setattr(engine_persistence, "persist_run_payload", fake_persist)

        result = await run_spec_against_bars_and_persist(
            spec_path=fixture_path("spy_ema_crossover"),
            symbol="TEST",
            bars=bars,
            start_date=(2024, 1, 2),
            end_date=(2024, 12, 31),
            starting_cash=Decimal("100000"),
            strategy_name="ema_crossover",
        )

        assert result.strategy_execution_id == 7
        assert len(result.trades) >= 1
        [payload] = written
        assert payload["source"] == "engine" and payload["strategy_name"] == "ema_crossover"
