"""Tests for LEAN order-event pairing into round-trip BacktestTrade rows."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.lean_sidecar_persistence import (
    OpenLot,
    PairedTrade,
    compute_aggregates,
    finalize_open_lot_as_synthetic,
    pair_order_events,
)


def _filled_event(
    event_id: int,
    direction: str,
    ms_utc: int,
    fill_price: float,
    fill_qty: float,
    fee: float = 0.0,
) -> dict:
    return {
        "id": f"MyAlgorithm-{event_id}-2",
        "order_id": event_id,
        "order_event_id": 2,
        "direction": direction,
        "status": "filled",
        "ms_utc": ms_utc,
        "fill_price": fill_price,
        "fill_quantity": fill_qty,
        "quantity": fill_qty,
        "order_fee_amount": fee,
        "order_fee_currency": "USD",
    }


def test_pair_empty_events_returns_empty_list() -> None:
    trades, open_lot = pair_order_events([])
    assert trades == []
    assert open_lot is None


def test_pair_skips_non_filled_events() -> None:
    events = [
        {**_filled_event(1, "buy", 1_700_000_000_000, 100.0, 10), "status": "submitted"},
        _filled_event(1, "buy", 1_700_000_060_000, 100.0, 10, fee=0.5),
        _filled_event(2, "sell", 1_700_000_120_000, 101.0, 10, fee=0.5),
    ]
    trades, open_lot = pair_order_events(events)
    assert len(trades) == 1
    assert open_lot is None


def test_pair_single_round_trip() -> None:
    events = [
        _filled_event(1, "buy", 1_700_000_000_000, 100.0, 10, fee=0.5),
        _filled_event(2, "sell", 1_700_000_060_000, 101.0, 10, fee=0.5),
    ]
    trades, open_lot = pair_order_events(events)
    assert open_lot is None
    assert len(trades) == 1
    t = trades[0]
    assert t.trade_number == 1
    assert t.entry_ms_utc == 1_700_000_000_000
    assert t.exit_ms_utc == 1_700_000_060_000
    assert t.entry_price == pytest.approx(100.0)
    assert t.exit_price == pytest.approx(101.0)
    assert t.quantity == 10
    # pnl = (101 - 100) * 10 - 0.5 - 0.5 = 9.0
    assert t.pnl == pytest.approx(9.0)
    assert t.is_synthetic_exit is False


def test_pair_multiple_round_trips() -> None:
    events = [
        _filled_event(1, "buy", 1_700_000_000_000, 100.0, 10, fee=0.5),
        _filled_event(2, "sell", 1_700_000_060_000, 101.0, 10, fee=0.5),
        _filled_event(3, "buy", 1_700_000_120_000, 102.0, 10, fee=0.5),
        _filled_event(4, "sell", 1_700_000_180_000, 100.0, 10, fee=0.5),
    ]
    trades, open_lot = pair_order_events(events)
    assert open_lot is None
    assert len(trades) == 2
    assert [t.trade_number for t in trades] == [1, 2]
    assert trades[1].pnl == pytest.approx((100.0 - 102.0) * 10 - 1.0)


def test_pair_half_open_returns_open_lot() -> None:
    events = [
        _filled_event(1, "buy", 1_700_000_000_000, 100.0, 10, fee=0.5),
    ]
    trades, open_lot = pair_order_events(events)
    assert trades == []
    assert open_lot is not None
    assert open_lot.entry_ms_utc == 1_700_000_000_000
    assert open_lot.entry_price == pytest.approx(100.0)
    assert open_lot.quantity == 10
    assert open_lot.fees == [0.5]


def test_pair_raises_on_pyramiding() -> None:
    events = [
        _filled_event(1, "buy", 1_700_000_000_000, 100.0, 10),
        _filled_event(2, "buy", 1_700_000_060_000, 101.0, 10),  # second buy without sell
    ]
    with pytest.raises(NotImplementedError, match="Pyramiding not supported"):
        pair_order_events(events)


def test_pair_ignores_sell_without_open_lot() -> None:
    """Defensive: short selling not expected for current templates."""
    events = [
        _filled_event(1, "sell", 1_700_000_000_000, 100.0, 10),
    ]
    trades, open_lot = pair_order_events(events)
    assert trades == []
    assert open_lot is None


def test_finalize_open_lot_as_synthetic_uses_last_equity_point() -> None:
    open_lot = OpenLot(
        entry_ms_utc=1_700_000_000_000,
        entry_price=100.0,
        quantity=10,
        fees=[0.5],
    )
    equity_curve = [
        {"ms_utc": 1_700_000_000_000, "value": 100_000.0},
        {"ms_utc": 1_700_000_300_000, "value": 100_050.0},
        {"ms_utc": 1_700_000_600_000, "value": 100_090.0},
    ]
    trade = finalize_open_lot_as_synthetic(
        open_lot,
        equity_curve=equity_curve,
        starting_cash=100_000.0,
        trade_number=5,
    )
    assert trade.trade_number == 5
    assert trade.exit_ms_utc == 1_700_000_600_000
    assert trade.is_synthetic_exit is True
    assert trade.signal_reason == "EndOfAlgorithm:MTM (synthetic exit)"
    # exit_price reconstructed via portfolio-value identity:
    #   equity = cash_remaining + qty * exit_price
    #   cash_remaining = starting_cash - qty * entry_price - sum(fees)
    # => exit_price = (equity - starting_cash + qty * entry_price + sum(fees)) / qty
    #              = (100090 - 100000 + 10*100 + 0.5) / 10
    #              = 1090.5 / 10 = 109.05
    assert trade.exit_price == pytest.approx(109.05)
    # pnl = (109.05 - 100) * 10 - 0.5 = 90.5 - 0.5 = 90.0
    assert trade.pnl == pytest.approx(90.0)


def test_finalize_open_lot_raises_on_empty_equity_curve() -> None:
    open_lot = OpenLot(
        entry_ms_utc=1_700_000_000_000,
        entry_price=100.0,
        quantity=10,
        fees=[0.5],
    )
    with pytest.raises(ValueError, match="equity_curve is empty"):
        finalize_open_lot_as_synthetic(open_lot, [], 100_000.0, 1)


def test_compute_aggregates_empty_trades() -> None:
    agg = compute_aggregates(trades=[], starting_cash=100_000.0, total_fees=0.0)
    assert agg.total_trades == 0
    assert agg.winning_trades == 0
    assert agg.losing_trades == 0
    assert agg.total_pnl == pytest.approx(0.0)
    assert agg.final_equity == pytest.approx(100_000.0)
    assert agg.win_rate == pytest.approx(0.0)


def test_compute_aggregates_mixed_trades() -> None:
    trades = [
        PairedTrade(1, 0, 0, 100.0, 101.0, 10, pnl=10.0, signal_reason="x", is_synthetic_exit=False),
        PairedTrade(2, 0, 0, 100.0, 99.0, 10, pnl=-10.0, signal_reason="x", is_synthetic_exit=False),
        PairedTrade(3, 0, 0, 100.0, 102.0, 10, pnl=20.0, signal_reason="x", is_synthetic_exit=False),
    ]
    agg = compute_aggregates(trades=trades, starting_cash=100_000.0, total_fees=3.0)
    assert agg.total_trades == 3
    assert agg.winning_trades == 2
    assert agg.losing_trades == 1
    assert agg.total_pnl == pytest.approx(20.0)
    assert agg.final_equity == pytest.approx(100_000.0 + 20.0 - 3.0)
    assert agg.win_rate == pytest.approx(2 / 3)


# ---------------------------------------------------------------------------
# build_persist_payload tests
# ---------------------------------------------------------------------------


def _write_fixture_workspace(
    base: Path,
    run_id: str,
    *,
    order_events: list[dict],
    equity_curve: list[dict],
    statistics: dict | None = None,
) -> Path:
    """Build a minimal LEAN workspace with normalized/result.json."""
    ws = base / run_id
    (ws / "normalized").mkdir(parents=True)
    result = {
        "algorithm_id": "MyAlgorithm",
        "parser_version": "phase-3a-r1",
        "first_equity_ms_utc": equity_curve[0]["ms_utc"] if equity_curve else 0,
        "last_equity_ms_utc": equity_curve[-1]["ms_utc"] if equity_curve else 0,
        "total_equity_points": len(equity_curve),
        "total_order_events": len(order_events),
        "equity_curve": equity_curve,
        "order_events": order_events,
        "statistics": statistics or {},
        "runtime_statistics": {},
    }
    (ws / "normalized" / "result.json").write_text(json.dumps(result))
    return ws


def test_build_persist_payload_pairs_round_trip(tmp_path: Path) -> None:
    from app.services.lean_sidecar_persistence import build_persist_payload

    ws = _write_fixture_workspace(
        tmp_path,
        "ui_run_round_trip",
        order_events=[
            {
                "order_id": 1,
                "order_event_id": 1,
                "direction": "buy",
                "status": "submitted",
                "ms_utc": 1_700_000_000_000,
                "fill_price": 0.0,
                "fill_quantity": 0.0,
                "quantity": 10,
                "order_fee_amount": None,
            },
            {
                "order_id": 1,
                "order_event_id": 2,
                "direction": "buy",
                "status": "filled",
                "ms_utc": 1_700_000_060_000,
                "fill_price": 100.0,
                "fill_quantity": 10,
                "quantity": 10,
                "order_fee_amount": 0.5,
            },
            {
                "order_id": 2,
                "order_event_id": 1,
                "direction": "sell",
                "status": "submitted",
                "ms_utc": 1_700_000_540_000,
                "fill_price": 0.0,
                "fill_quantity": 0.0,
                "quantity": 10,
                "order_fee_amount": None,
            },
            {
                "order_id": 2,
                "order_event_id": 2,
                "direction": "sell",
                "status": "filled",
                "ms_utc": 1_700_000_600_000,
                "fill_price": 101.0,
                "fill_quantity": 10,
                "quantity": 10,
                "order_fee_amount": 0.5,
            },
        ],
        equity_curve=[
            {"ms_utc": 1_700_000_000_000, "value": 100_000.0},
            {"ms_utc": 1_700_000_600_000, "value": 100_009.0},
        ],
        statistics={"NetProfit": "9.00"},
    )

    payload = build_persist_payload(
        workspace_path=ws,
        run_id="ui_run_round_trip",
        starting_cash=100_000.0,
        symbol="SPY",
        algorithm_name="ema_crossover",
        start_date_ms=1_700_000_000_000,
        end_date_ms=1_700_000_600_000,
    )

    assert payload["lean_run_id"] == "ui_run_round_trip"
    assert payload["source"] == "lean-sidecar"
    assert payload["strategy_name"] == "ema_crossover"
    assert payload["symbol"] == "SPY"
    assert payload["starting_cash"] == 100_000.0
    assert payload["start_date_ms"] == 1_700_000_000_000
    assert payload["end_date_ms"] == 1_700_000_600_000
    assert payload["total_trades"] == 1
    assert payload["winning_trades"] == 1
    assert payload["total_pnl"] == pytest.approx(9.0)
    assert payload["total_fees"] == pytest.approx(1.0)
    assert payload["final_equity"] == pytest.approx(100_008.0)  # 100000 + 9 - 1
    assert len(payload["trades"]) == 1
    t = payload["trades"][0]
    assert t["entry_ms_utc"] == 1_700_000_060_000
    assert t["exit_ms_utc"] == 1_700_000_600_000
    assert t["entry_price"] == pytest.approx(100.0)
    assert t["exit_price"] == pytest.approx(101.0)
    assert t["pnl"] == pytest.approx(9.0)
    assert t["is_synthetic_exit"] is False
    assert "lean_statistics" in payload
    assert payload["lean_statistics"]["statistics"]["NetProfit"] == "9.00"
    assert payload["lean_statistics"]["parser_version"] == "phase-3a-r1"


def test_build_persist_payload_synthesizes_mtm_for_half_open(tmp_path: Path) -> None:
    from app.services.lean_sidecar_persistence import build_persist_payload

    ws = _write_fixture_workspace(
        tmp_path,
        "ui_run_half_open",
        order_events=[
            {
                "order_id": 1,
                "order_event_id": 1,
                "direction": "buy",
                "status": "filled",
                "ms_utc": 1_700_000_060_000,
                "fill_price": 100.0,
                "fill_quantity": 10,
                "quantity": 10,
                "order_fee_amount": 0.5,
            },
        ],
        equity_curve=[
            {"ms_utc": 1_700_000_000_000, "value": 100_000.0},
            {"ms_utc": 1_700_000_600_000, "value": 100_009.5},
        ],
    )

    payload = build_persist_payload(
        workspace_path=ws,
        run_id="ui_run_half_open",
        starting_cash=100_000.0,
        symbol="SPY",
        algorithm_name="trusted_default",
        start_date_ms=1_700_000_000_000,
        end_date_ms=1_700_000_600_000,
    )

    assert payload["total_trades"] == 1
    t = payload["trades"][0]
    assert t["is_synthetic_exit"] is True
    assert t["signal_reason"] == "EndOfAlgorithm:MTM (synthetic exit)"


def test_build_persist_payload_missing_normalized_result(tmp_path: Path) -> None:
    from app.services.lean_sidecar_persistence import build_persist_payload

    ws = tmp_path / "ui_run_crashed"
    ws.mkdir()
    # No normalized/result.json — simulate LEAN crash.

    payload = build_persist_payload(
        workspace_path=ws,
        run_id="ui_run_crashed",
        starting_cash=100_000.0,
        symbol="SPY",
        algorithm_name="ema_crossover",
        start_date_ms=1_700_000_000_000,
        end_date_ms=1_700_000_600_000,
    )

    assert payload["lean_run_id"] == "ui_run_crashed"
    assert payload["total_trades"] == 0
    assert payload["total_pnl"] == pytest.approx(0.0)
    assert payload["final_equity"] == pytest.approx(100_000.0)
    assert payload["trades"] == []
    assert "error" in payload["lean_statistics"]


def test_build_persist_payload_empty_order_events(tmp_path: Path) -> None:
    """Algorithm ran but produced no signals (warmup didn't complete in window)."""
    from app.services.lean_sidecar_persistence import build_persist_payload

    ws = _write_fixture_workspace(
        tmp_path,
        "ui_run_no_signals",
        order_events=[],
        equity_curve=[
            {"ms_utc": 1_700_000_000_000, "value": 100_000.0},
            {"ms_utc": 1_700_000_600_000, "value": 100_000.0},
        ],
    )

    payload = build_persist_payload(
        workspace_path=ws,
        run_id="ui_run_no_signals",
        starting_cash=100_000.0,
        symbol="SPY",
        algorithm_name="ema_crossover",
        start_date_ms=1_700_000_000_000,
        end_date_ms=1_700_000_600_000,
    )

    assert payload["total_trades"] == 0
    assert payload["total_pnl"] == pytest.approx(0.0)
    assert payload["trades"] == []


# ---------------------------------------------------------------------------
# persist_via_dotnet tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persist_via_dotnet_posts_payload_and_returns_id() -> None:
    import respx
    from httpx import Response

    from app.services.lean_sidecar_persistence import persist_via_dotnet

    with respx.mock:
        route = respx.post("http://backend/api/backtest-runs/persist-lean").mock(
            return_value=Response(200, json={"strategy_execution_id": 42})
        )

        payload: dict = {
            "lean_run_id": "ui_run_test",
            "source": "lean-sidecar",
            "strategy_name": "ema_crossover",
            "symbol": "SPY",
            "starting_cash": 100_000.0,
            "start_date_ms": 1_700_000_000_000,
            "end_date_ms": 1_700_000_600_000,
            "total_trades": 0,
            "winning_trades": 0,
            "losing_trades": 0,
            "total_pnl": 0.0,
            "total_fees": 0.0,
            "final_equity": 100_000.0,
            "win_rate": 0.0,
            "trades": [],
            "lean_statistics": {},
        }

        strategy_execution_id = await persist_via_dotnet(payload, base_url="http://backend")

    assert strategy_execution_id == 42
    assert route.called


@pytest.mark.asyncio
async def test_persist_via_dotnet_returns_none_on_http_error() -> None:
    import respx
    from httpx import Response

    from app.services.lean_sidecar_persistence import persist_via_dotnet

    with respx.mock:
        respx.post("http://backend/api/backtest-runs/persist-lean").mock(
            return_value=Response(500, json={"error": "boom"})
        )

        result = await persist_via_dotnet(
            {"lean_run_id": "ui_run_test", "source": "lean-sidecar", "trades": []},
            base_url="http://backend",
        )

    # Must not raise; persistence failure should not abort the LEAN run.
    assert result is None


@pytest.mark.asyncio
async def test_persist_via_dotnet_returns_none_on_connection_error() -> None:
    import respx
    from httpx import ConnectError

    from app.services.lean_sidecar_persistence import persist_via_dotnet

    with respx.mock:
        respx.post("http://backend/api/backtest-runs/persist-lean").mock(side_effect=ConnectError("connection refused"))

        result = await persist_via_dotnet(
            {"lean_run_id": "ui_run_test", "source": "lean-sidecar", "trades": []},
            base_url="http://backend",
        )

    assert result is None
