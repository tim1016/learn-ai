"""Tests for app.services.engine_persistence."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest

from app.services import engine_persistence
from app.services.engine_persistence import (
    EngineTrade,
    build_engine_persist_payload,
    compute_aggregates,
    persist_engine_run,
)


def _trade(
    *,
    n: int = 1,
    entry_ms: int = 1_700_000_060_000,
    exit_ms: int = 1_700_000_120_000,
    entry_price: str = "100",
    exit_price: str = "101",
    quantity: str = "10",
    pnl: str = "10",
    signal_reason: str = "EMA exit",
    is_synthetic_exit: bool = False,
) -> EngineTrade:
    return EngineTrade(
        trade_number=n,
        entry_ms_utc=entry_ms,
        exit_ms_utc=exit_ms,
        entry_price=Decimal(entry_price),
        exit_price=Decimal(exit_price),
        quantity=Decimal(quantity),
        pnl=Decimal(pnl),
        signal_reason=signal_reason,
        is_synthetic_exit=is_synthetic_exit,
    )


class TestComputeAggregates:
    @pytest.mark.parametrize(
        ("pnls", "total_trades", "winning_trades", "losing_trades", "total_pnl", "final_equity", "win_rate"),
        [
            pytest.param((), 0, 0, 0, Decimal("0"), Decimal("100000"), 0.0, id="empty-trade-list-returns-zero-aggregates"),
            pytest.param(("25",), 1, 1, 0, Decimal("25"), Decimal("100025"), 1.0, id="single-winning-trade"),
            pytest.param(("50", "-20", "30"), 3, 2, 1, Decimal("60"), Decimal("100060"), 2 / 3, id="mixed-wins-and-losses"),
            pytest.param(("0",), 1, 0, 0, Decimal("0"), Decimal("100000"), 0.0, id="breakeven-trade-counted-neither-win-nor-loss"),
        ],
    )
    def test_aggregates_of_a_trade_list(
        self,
        pnls: tuple[str, ...],
        total_trades: int,
        winning_trades: int,
        losing_trades: int,
        total_pnl: Decimal,
        final_equity: Decimal,
        win_rate: float,
    ) -> None:
        trades = [_trade(n=n, pnl=pnl) for n, pnl in enumerate(pnls, start=1)]
        agg = compute_aggregates(trades, starting_cash=Decimal("100000"))

        assert agg.total_trades == total_trades, "every trade is counted"
        assert agg.winning_trades == winning_trades, "only a positive pnl wins"
        assert agg.losing_trades == losing_trades, "only a negative pnl loses"
        assert agg.total_pnl == total_pnl, "total pnl is the sum of the trades' pnl"
        assert agg.final_equity == final_equity, "final equity is the starting cash plus total pnl"
        assert agg.win_rate == win_rate, "win rate is winners over trades"

    def test_total_fees_does_not_double_subtract_from_final_equity(self) -> None:
        # Per the module docstring: per-trade pnl is already net of fees,
        # so final_equity = starting_cash + total_pnl (NOT minus total_fees again).
        agg = compute_aggregates(
            [_trade(pnl="100")],
            starting_cash=Decimal("100000"),
            total_fees=Decimal("5"),
        )

        assert agg.final_equity == Decimal("100100")


class TestBuildEnginePersistPayload:
    def test_payload_shape_for_engine_source(self) -> None:
        payload = build_engine_persist_payload(
            strategy_name="ema_crossover",
            symbol="SPY",
            starting_cash=Decimal("100000"),
            start_date=date(2023, 11, 14),
            end_date=date(2023, 11, 14),
            trades=[_trade(pnl="10")],
        )

        assert payload["source"] == "engine"
        assert payload["lean_run_id"] is None
        assert payload["strategy_name"] == "ema_crossover"
        assert payload["symbol"] == "SPY"
        assert payload["starting_cash"] == 100_000.0
        assert payload["start_date"] == "2023-11-14"
        assert payload["end_date"] == "2023-11-14"
        assert payload["total_trades"] == 1
        assert payload["winning_trades"] == 1
        assert payload["losing_trades"] == 0
        assert payload["total_pnl"] == 10.0
        assert payload["final_equity"] == 100_010.0
        assert payload["total_fees"] == 0.0
        assert payload["win_rate"] == 1.0
        assert json.loads(payload["metric_documentation_json"]) == [
            {
                "contract_id": "platform-sharpe-v1",
                "metric_id": "sharpe",
                "producer": "platform",
                "variant_id": "sharpe.platform.v1",
            },
            {
                "contract_id": "platform-results-statistics-v1",
                "metric_id": "sortino",
                "producer": "platform",
                "variant_id": "sortino.platform.v1",
            },
            {
                "contract_id": "platform-results-statistics-v1",
                "metric_id": "maximum_drawdown",
                "producer": "platform",
                "variant_id": "maximum_drawdown.platform.v1",
            },
            {
                "contract_id": "platform-results-statistics-v1",
                "metric_id": "profit_factor",
                "producer": "platform",
                "variant_id": "profit_factor.platform.v1",
            },
        ]
        assert len(payload["trades"]) == 1
        assert payload["trades"][0]["trade_number"] == 1
        assert payload["trades"][0]["entry_price"] == 100.0
        assert payload["trades"][0]["quantity"] == 10.0
        assert payload["trades"][0]["pnl"] == 10.0
        assert payload["trades"][0]["is_synthetic_exit"] is False

    def test_payload_includes_extra_statistics(self) -> None:
        payload = build_engine_persist_payload(
            strategy_name="ema_crossover",
            symbol="SPY",
            starting_cash=Decimal("100000"),
            start_date=date(2023, 11, 14),
            end_date=date(2023, 11, 14),
            trades=[],
            extra_statistics={"engine_version": "spec-v2", "fill_mode": "signal_bar_close"},
        )

        assert payload["lean_statistics"] == {
            "engine_version": "spec-v2",
            "fill_mode": "signal_bar_close",
        }

    def test_payload_with_zero_trades(self) -> None:
        payload = build_engine_persist_payload(
            strategy_name="ema_crossover",
            symbol="SPY",
            starting_cash=Decimal("100000"),
            start_date=date(2023, 11, 14),
            end_date=date(2023, 11, 14),
            trades=[],
        )

        assert payload["total_trades"] == 0
        assert payload["winning_trades"] == 0
        assert payload["losing_trades"] == 0
        assert payload["total_pnl"] == 0.0
        assert payload["final_equity"] == 100_000.0
        assert payload["trades"] == []
        assert payload["win_rate"] == 0.0


class TestPersistEngineRun:
    @pytest.mark.asyncio
    async def test_writes_the_payload_and_returns_the_assigned_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        written: list[dict] = []

        async def fake_persist(payload: dict) -> int:
            written.append(payload)
            return 42

        monkeypatch.setattr(engine_persistence, "persist_run_payload", fake_persist)

        persisted_id = await persist_engine_run(
            strategy_name="ema_crossover",
            symbol="SPY",
            starting_cash=Decimal("100000"),
            start_date=date(2023, 11, 14),
            end_date=date(2023, 11, 14),
            trades=[_trade()],
        )

        assert persisted_id == 42
        [body] = written
        assert body["source"] == "engine"
        assert body["lean_run_id"] is None
        assert body["strategy_name"] == "ema_crossover"
        assert len(body["trades"]) == 1

    @pytest.mark.asyncio
    async def test_returns_none_when_persistence_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def failed_persist(payload: dict) -> None:
            return None

        monkeypatch.setattr(engine_persistence, "persist_run_payload", failed_persist)

        persisted_id = await persist_engine_run(
            strategy_name="ema_crossover",
            symbol="SPY",
            starting_cash=Decimal("100000"),
            start_date=date(2023, 11, 14),
            end_date=date(2023, 11, 14),
            trades=[],
        )

        assert persisted_id is None
