"""Tests for app.services.engine_persistence."""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest
import respx

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
    def test_empty_trade_list_returns_zero_aggregates(self) -> None:
        agg = compute_aggregates([], starting_cash=Decimal("100000"))

        assert agg.total_trades == 0
        assert agg.winning_trades == 0
        assert agg.losing_trades == 0
        assert agg.total_pnl == Decimal("0")
        assert agg.final_equity == Decimal("100000")
        assert agg.win_rate == 0.0

    def test_single_winning_trade(self) -> None:
        agg = compute_aggregates([_trade(pnl="25")], starting_cash=Decimal("100000"))

        assert agg.total_trades == 1
        assert agg.winning_trades == 1
        assert agg.losing_trades == 0
        assert agg.total_pnl == Decimal("25")
        assert agg.final_equity == Decimal("100025")
        assert agg.win_rate == 1.0

    def test_mixed_wins_and_losses(self) -> None:
        trades = [
            _trade(n=1, pnl="50"),
            _trade(n=2, pnl="-20"),
            _trade(n=3, pnl="30"),
        ]
        agg = compute_aggregates(trades, starting_cash=Decimal("100000"))

        assert agg.total_trades == 3
        assert agg.winning_trades == 2
        assert agg.losing_trades == 1
        assert agg.total_pnl == Decimal("60")
        assert agg.final_equity == Decimal("100060")
        assert agg.win_rate == pytest.approx(2 / 3)

    def test_breakeven_trade_counted_neither_win_nor_loss(self) -> None:
        agg = compute_aggregates([_trade(pnl="0")], starting_cash=Decimal("100000"))

        assert agg.winning_trades == 0
        assert agg.losing_trades == 0
        assert agg.win_rate == 0.0

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
            start_date_ms=1_700_000_000_000,
            end_date_ms=1_700_001_000_000,
            trades=[_trade(pnl="10")],
        )

        assert payload["source"] == "engine"
        assert payload["lean_run_id"] is None
        assert payload["strategy_name"] == "ema_crossover"
        assert payload["symbol"] == "SPY"
        assert payload["starting_cash"] == 100_000.0
        assert payload["start_date_ms"] == 1_700_000_000_000
        assert payload["end_date_ms"] == 1_700_001_000_000
        assert payload["total_trades"] == 1
        assert payload["winning_trades"] == 1
        assert payload["losing_trades"] == 0
        assert payload["total_pnl"] == 10.0
        assert payload["final_equity"] == 100_010.0
        assert payload["total_fees"] == 0.0
        assert payload["win_rate"] == 1.0
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
            start_date_ms=1_700_000_000_000,
            end_date_ms=1_700_001_000_000,
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
            start_date_ms=1_700_000_000_000,
            end_date_ms=1_700_001_000_000,
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
    async def test_posts_to_persist_lean_and_returns_assigned_id(self) -> None:
        base_url = "http://test-backend"
        async with respx.mock(base_url=base_url, assert_all_called=True) as mock:
            route = mock.post("/api/backtest-runs/persist-lean").mock(
                return_value=httpx.Response(200, json={"strategy_execution_id": 42})
            )

            persisted_id = await persist_engine_run(
                base_url=base_url,
                strategy_name="ema_crossover",
                symbol="SPY",
                starting_cash=Decimal("100000"),
                start_date_ms=1_700_000_000_000,
                end_date_ms=1_700_001_000_000,
                trades=[_trade()],
            )

            assert persisted_id == 42
            sent = route.calls[0].request
            import json

            body = json.loads(sent.content)
            assert body["source"] == "engine"
            assert body["lean_run_id"] is None
            assert body["strategy_name"] == "ema_crossover"
            assert len(body["trades"]) == 1

    @pytest.mark.asyncio
    async def test_returns_none_when_backend_returns_500(self) -> None:
        base_url = "http://test-backend"
        async with respx.mock(base_url=base_url, assert_all_called=True) as mock:
            mock.post("/api/backtest-runs/persist-lean").mock(return_value=httpx.Response(500, json={"error": "boom"}))

            persisted_id = await persist_engine_run(
                base_url=base_url,
                strategy_name="ema_crossover",
                symbol="SPY",
                starting_cash=Decimal("100000"),
                start_date_ms=1_700_000_000_000,
                end_date_ms=1_700_001_000_000,
                trades=[],
            )

            assert persisted_id is None
