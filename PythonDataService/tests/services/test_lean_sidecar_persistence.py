"""Tests for LEAN order-event pairing into round-trip BacktestTrade rows."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.models.responses import LeanStatisticsResponse
from app.services.lean_sidecar_persistence import (
    OpenLot,
    PairedTrade,
    _algorithm_name_for_run,
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


def test_pair_raises_on_sell_without_open_lot() -> None:
    """Defensive: short selling not expected for current templates; treat as corrupt.

    The caller (build_persist_payload) catches ValueError and returns a failed-run
    payload, so the LEAN run result is not lost — it just records as a failed
    persistence row rather than crashing the caller.
    """
    events = [
        _filled_event(1, "sell", 1_700_000_000_000, 100.0, 10),
    ]
    with pytest.raises(ValueError, match="no matching open lot"):
        pair_order_events(events)


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
    # pnl values here already net out all entry/exit fees (matching pair_order_events
    # semantics). final_equity = starting_cash + total_pnl — do NOT subtract total_fees
    # again; that would double-count fees that are already embedded in each t.pnl.
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
    assert agg.final_equity == pytest.approx(100_000.0 + 20.0)
    assert agg.win_rate == pytest.approx(2 / 3)


# ---------------------------------------------------------------------------
# _algorithm_name_for_run tests
# ---------------------------------------------------------------------------


def test_algorithm_name_custom_source_overrides_template() -> None:
    """When algorithm_source is set, label is always 'user_provided'."""
    assert _algorithm_name_for_run("trusted_default", "print('hello')") == "user_provided"


def test_algorithm_name_template_only_returns_template() -> None:
    """No algorithm_source; use the template name verbatim."""
    assert _algorithm_name_for_run("ema_crossover", None) == "ema_crossover"


def test_algorithm_name_trusted_default_template_no_source() -> None:
    """Trusted-default template without custom source keeps its name."""
    assert _algorithm_name_for_run("trusted_default", None) == "trusted_default"


def test_algorithm_name_both_none_returns_user_provided() -> None:
    """Defensive: both None → fall back to 'user_provided'."""
    assert _algorithm_name_for_run(None, None) == "user_provided"


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
    # pnl=9.0 already nets the $1 of fees (pair_order_events: pnl = gross - entry_fee - exit_fee).
    # final_equity = starting_cash + total_pnl; do NOT subtract total_fees again.
    assert payload["final_equity"] == pytest.approx(100_009.0)  # 100000 + 9
    assert len(payload["trades"]) == 1
    t = payload["trades"][0]
    assert t["entry_ms_utc"] == 1_700_000_060_000
    assert t["exit_ms_utc"] == 1_700_000_600_000
    assert t["entry_price"] == pytest.approx(100.0)
    assert t["exit_price"] == pytest.approx(101.0)
    assert t["pnl"] == pytest.approx(9.0)
    assert t["is_synthetic_exit"] is False
    # The persisted ``lean_statistics`` shape now matches the engine
    # path's ``LeanStatisticsResponse`` ({portfolio, trade, runtime}),
    # not the raw flat dict the sidecar used to emit. The frontend's
    # LEAN-stats dashboard reads ``.portfolio`` and crashed previously
    # on the flat shape; see PR description for both-bugs context.
    assert "lean_statistics" in payload
    stats = payload["lean_statistics"]
    assert set(stats.keys()) == {"portfolio", "trade", "runtime"}
    assert stats["portfolio"]["start_equity"] == pytest.approx(100_000.0)
    assert stats["portfolio"]["end_equity"] == pytest.approx(100_009.0)
    assert stats["trade"]["total_number_of_trades"] == 1
    assert stats["runtime"]["total_orders"] == 1


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
    # Failed runs persist the canonical-shape-all-zeros dict, NOT the
    # legacy ``{"error": ..., "workspace_path": ...}``. The failure
    # signal lives in ``total_trades=0`` above; the diagnostic
    # ``error`` string lands in the PythonDataService log instead.
    stats = payload["lean_statistics"]
    assert set(stats.keys()) == {"portfolio", "trade", "runtime"}
    assert stats["portfolio"]["start_equity"] == 0.0
    assert stats["trade"]["total_number_of_trades"] == 0
    assert stats["runtime"]["total_orders"] == 0


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


def test_build_persist_payload_corrupt_json_returns_failed_payload(tmp_path: Path) -> None:
    """Corrupt result.json must not raise — returns a failed-run payload."""
    from app.services.lean_sidecar_persistence import build_persist_payload

    ws = tmp_path / "ui_run_corrupt"
    (ws / "normalized").mkdir(parents=True)
    (ws / "normalized" / "result.json").write_text("{ this is not valid JSON }")

    payload = build_persist_payload(
        workspace_path=ws,
        run_id="ui_run_corrupt",
        starting_cash=100_000.0,
        symbol="SPY",
        algorithm_name="ema_crossover",
        start_date_ms=1_700_000_000_000,
        end_date_ms=1_700_000_600_000,
    )

    assert payload["lean_run_id"] == "ui_run_corrupt"
    assert payload["total_trades"] == 0
    assert payload["trades"] == []
    # Canonical-shape failure payload — see test_build_persist_payload_missing_normalized_result.
    stats = payload["lean_statistics"]
    assert set(stats.keys()) == {"portfolio", "trade", "runtime"}
    assert stats["trade"]["total_number_of_trades"] == 0


def test_build_persist_payload_pyramiding_returns_failed_payload(tmp_path: Path) -> None:
    """Pyramiding in order events must not raise — returns a failed-run payload."""
    from app.services.lean_sidecar_persistence import build_persist_payload

    ws = _write_fixture_workspace(
        tmp_path,
        "ui_run_pyramiding",
        order_events=[
            # Two consecutive buys without an intervening sell — pyramiding.
            _filled_event(1, "buy", 1_700_000_000_000, 100.0, 10, fee=0.5),
            _filled_event(2, "buy", 1_700_000_060_000, 101.0, 10, fee=0.5),
        ],
        equity_curve=[
            {"ms_utc": 1_700_000_000_000, "value": 100_000.0},
            {"ms_utc": 1_700_000_600_000, "value": 102_000.0},
        ],
    )

    payload = build_persist_payload(
        workspace_path=ws,
        run_id="ui_run_pyramiding",
        starting_cash=100_000.0,
        symbol="SPY",
        algorithm_name="ema_crossover",
        start_date_ms=1_700_000_000_000,
        end_date_ms=1_700_000_600_000,
    )

    assert payload["lean_run_id"] == "ui_run_pyramiding"
    assert payload["total_trades"] == 0
    assert payload["trades"] == []
    # Canonical-shape failure payload — see test_build_persist_payload_missing_normalized_result.
    stats = payload["lean_statistics"]
    assert set(stats.keys()) == {"portfolio", "trade", "runtime"}
    assert stats["trade"]["total_number_of_trades"] == 0


# ---------------------------------------------------------------------------
# persist_via_dotnet tests
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# PR B P1 fix — manifest-forwarded brokerage_policy + data_policy_json
# ---------------------------------------------------------------------------


def _stub_manifest_dict(brokerage_policy: str = "interactive_brokers") -> dict:
    """A minimal manifest-shaped dict (as the backfill CLI loads it from disk).

    Mirrors the canonical ``RunManifest`` -> JSON serialization. The fields
    we care about for the persist payload are ``brokerage_policy`` and
    ``data_policy``; everything else is filler.
    """
    return {
        "brokerage_policy": brokerage_policy,
        "data_policy": {
            "source": "polygon",
            "symbol": "SPY",
            "adjusted": True,
            "session": "regular",
            "input_bars": {"timespan": "minute", "multiplier": 1},
            "strategy_bars": {"timespan": "minute", "multiplier": 15},
            "timestamp_policy": "bar_close_ms_utc",
            "timezone": "America/New_York",
            "provider_kind": "live",
            "fixture_id": None,
            "fixture_sha256": None,
        },
    }


def test_build_persist_payload_forwards_brokerage_and_data_policy_from_manifest_dict(
    tmp_path: Path,
) -> None:
    """The persist payload must carry the manifest's brokerage_policy +
    data_policy_json so the .NET row is the truthful record."""
    from app.services.lean_sidecar_persistence import build_persist_payload

    ws = _write_fixture_workspace(
        tmp_path,
        "ui_run_with_manifest",
        order_events=[
            _filled_event(1, "buy", 1_700_000_060_000, 100.0, 10, fee=0.5),
            _filled_event(2, "sell", 1_700_000_600_000, 101.0, 10, fee=0.5),
        ],
        equity_curve=[
            {"ms_utc": 1_700_000_000_000, "value": 100_000.0},
            {"ms_utc": 1_700_000_600_000, "value": 100_009.0},
        ],
    )

    payload = build_persist_payload(
        workspace_path=ws,
        run_id="ui_run_with_manifest",
        starting_cash=100_000.0,
        symbol="SPY",
        algorithm_name="ema_crossover",
        start_date_ms=1_700_000_000_000,
        end_date_ms=1_700_000_600_000,
        manifest=_stub_manifest_dict(brokerage_policy="interactive_brokers"),
    )

    assert payload["brokerage_policy"] == "interactive_brokers"
    assert payload["commission_per_order"] == 0.0
    assert payload["data_policy_json"] is not None
    parsed_dp = json.loads(payload["data_policy_json"])
    assert parsed_dp["source"] == "polygon"
    assert parsed_dp["symbol"] == "SPY"
    assert parsed_dp["adjusted"] is True
    assert parsed_dp["strategy_bars"]["multiplier"] == 15


def test_build_persist_payload_without_manifest_emits_none_brokerage(
    tmp_path: Path,
) -> None:
    """Without a manifest the payload's brokerage_policy is None.

    The .NET service preserves NULL on the row rather than fabricating
    ``algorithm_default``, which would corrupt compare-view gating for
    Interactive Brokers reconciliation runs.
    """
    from app.services.lean_sidecar_persistence import build_persist_payload

    ws = _write_fixture_workspace(
        tmp_path,
        "ui_run_legacy_no_manifest",
        order_events=[
            _filled_event(1, "buy", 1_700_000_060_000, 100.0, 10, fee=0.5),
            _filled_event(2, "sell", 1_700_000_600_000, 101.0, 10, fee=0.5),
        ],
        equity_curve=[
            {"ms_utc": 1_700_000_000_000, "value": 100_000.0},
            {"ms_utc": 1_700_000_600_000, "value": 100_009.0},
        ],
    )

    payload = build_persist_payload(
        workspace_path=ws,
        run_id="ui_run_legacy_no_manifest",
        starting_cash=100_000.0,
        symbol="SPY",
        algorithm_name="ema_crossover",
        start_date_ms=1_700_000_000_000,
        end_date_ms=1_700_000_600_000,
    )

    assert payload["brokerage_policy"] is None
    assert payload["data_policy_json"] is None
    assert payload["commission_per_order"] == 0.0


def test_build_persist_payload_failed_run_with_manifest_still_forwards_brokerage(
    tmp_path: Path,
) -> None:
    """Even when LEAN crashed before producing output, the manifest's
    brokerage_policy must flow through so the row's compare-view gating
    is accurate for the failed-run audit trail."""
    from app.services.lean_sidecar_persistence import build_persist_payload

    ws = tmp_path / "ui_run_crashed_with_manifest"
    ws.mkdir()
    # No normalized/result.json — simulate LEAN crash.

    payload = build_persist_payload(
        workspace_path=ws,
        run_id="ui_run_crashed_with_manifest",
        starting_cash=100_000.0,
        symbol="SPY",
        algorithm_name="ema_crossover",
        start_date_ms=1_700_000_000_000,
        end_date_ms=1_700_000_600_000,
        manifest=_stub_manifest_dict(brokerage_policy="algorithm_default"),
    )

    assert payload["total_trades"] == 0
    assert payload["brokerage_policy"] == "algorithm_default"
    assert payload["data_policy_json"] is not None


# ---------------------------------------------------------------------------
# _normalized_to_lean_statistics_response tests — Bug B fix coverage
#
# The sidecar persistence path historically wrote a flat
# ``{statistics, runtime_statistics, parser_version, workspace_path}``
# dict into ``StrategyExecution.LeanStatisticsJson``. The frontend's
# ``LeanStatistics`` interface expects the engine path's canonical
# ``{portfolio, trade, runtime}`` shape (``LeanStatisticsResponse``).
# This helper bridges the two so both engines persist the same shape
# and the LEAN-stats dashboard renders without crashing.
# ---------------------------------------------------------------------------


def test_normalized_to_lean_statistics_response_full_mapping() -> None:
    from app.services.lean_sidecar_persistence import _normalized_to_lean_statistics_response

    # Synthetic LEAN STATISTICS:: dict covering every mapped key.
    statistics = {
        "Net Profit": "-4.657%",
        "Compounding Annual Return": "-12.345%",
        "Sharpe Ratio": "-1.072",
        "Sortino Ratio": "-0.834",
        "Probabilistic Sharpe Ratio": "12.5%",
        "Drawdown": "5.234%",
        "Drawdown Recovery": "42",
        "Alpha": "0.123",
        "Beta": "0.987",
        "Information Ratio": "-0.456",
        "Tracking Error": "8.7%",
        "Treynor Ratio": "-4.123",
        "Annual Standard Deviation": "15.6%",
        "Annual Variance": "0.0243",
        "Win Rate": "52.5%",
        "Loss Rate": "47.5%",
        "Expectancy": "0.123",
        "Profit-Loss Ratio": "1.45",
        "Portfolio Turnover": "21.4%",
    }
    trades = [
        PairedTrade(
            1,
            1_700_000_000_000,
            1_700_000_600_000,
            100.0,
            101.0,
            10,
            pnl=9.0,
            signal_reason="x",
            is_synthetic_exit=False,
        ),
        PairedTrade(
            2,
            1_700_000_700_000,
            1_700_001_300_000,
            100.0,
            99.0,
            10,
            pnl=-11.0,
            signal_reason="x",
            is_synthetic_exit=False,
        ),
    ]

    resp = _normalized_to_lean_statistics_response(
        normalized_statistics=statistics,
        paired_trades=trades,
        starting_cash=100_000.0,
        total_fees=2.0,
    )

    # Portfolio percent / ratio parsing.
    assert resp.portfolio.total_net_profit == pytest.approx(-0.04657)
    assert resp.portfolio.compounding_annual_return == pytest.approx(-0.12345)
    assert resp.portfolio.sharpe_ratio == pytest.approx(-1.072)
    assert resp.portfolio.sortino_ratio == pytest.approx(-0.834)
    assert resp.portfolio.probabilistic_sharpe_ratio == pytest.approx(0.125)
    assert resp.portfolio.drawdown == pytest.approx(0.05234)
    assert resp.portfolio.drawdown_recovery == 42
    assert resp.portfolio.alpha == pytest.approx(0.123)
    assert resp.portfolio.beta == pytest.approx(0.987)
    assert resp.portfolio.tracking_error == pytest.approx(0.087)
    assert resp.portfolio.win_rate == pytest.approx(0.525)
    assert resp.portfolio.portfolio_turnover == pytest.approx(0.214)
    # Derived equity from paired trades.
    assert resp.portfolio.start_equity == pytest.approx(100_000.0)
    assert resp.portfolio.end_equity == pytest.approx(100_000.0 + 9.0 - 11.0)

    # Trade-level aggregates.
    assert resp.trade.total_number_of_trades == 2
    assert resp.trade.number_of_winning_trades == 1
    assert resp.trade.number_of_losing_trades == 1
    assert resp.trade.total_profit_loss == pytest.approx(-2.0)
    assert resp.trade.total_profit == pytest.approx(9.0)
    assert resp.trade.total_loss == pytest.approx(-11.0)
    assert resp.trade.profit_factor == pytest.approx(9.0 / 11.0)
    assert resp.trade.total_fees == pytest.approx(2.0)
    assert resp.trade.max_consecutive_winning_trades == 1
    assert resp.trade.max_consecutive_losing_trades == 1

    # Runtime stats derived from portfolio.
    assert resp.runtime.equity == pytest.approx(resp.portfolio.end_equity)
    assert resp.runtime.fees == pytest.approx(2.0)
    assert resp.runtime.net_profit == pytest.approx(-2.0)
    assert resp.runtime.total_orders == 2


def test_normalized_to_lean_statistics_response_missing_keys() -> None:
    """A sparse STATISTICS dict (only some keys) must not raise; absent
    fields default to 0.0 on the response."""
    from app.services.lean_sidecar_persistence import _normalized_to_lean_statistics_response

    resp = _normalized_to_lean_statistics_response(
        normalized_statistics={"Sharpe Ratio": "1.5"},
        paired_trades=[],
        starting_cash=50_000.0,
        total_fees=0.0,
    )
    assert resp.portfolio.sharpe_ratio == pytest.approx(1.5)
    assert resp.portfolio.sortino_ratio == 0.0
    assert resp.portfolio.alpha == 0.0
    assert resp.portfolio.beta == 0.0
    assert resp.portfolio.start_equity == pytest.approx(50_000.0)
    assert resp.portfolio.end_equity == pytest.approx(50_000.0)
    assert resp.trade.total_number_of_trades == 0
    assert resp.runtime.total_orders == 0


def test_normalized_to_lean_statistics_response_dollar_with_commas() -> None:
    """``_parse_dollar`` strips ``$`` and commas. While the production
    helper only uses dollar parsing reflexively (none of the mapped
    portfolio fields are dollar-valued in LEAN), the parser must be
    robust to that shape for forward compatibility."""
    from app.services.lean_sidecar_persistence import _parse_dollar

    assert _parse_dollar("$95,343.16") == pytest.approx(95_343.16)
    assert _parse_dollar("$1,000,000.00") == pytest.approx(1_000_000.0)
    assert _parse_dollar("-$1,234.56") == pytest.approx(-1_234.56)
    assert _parse_dollar("") == 0.0
    assert _parse_dollar(None) == 0.0
    assert _parse_dollar("not a number") == 0.0


def test_normalized_to_lean_statistics_response_parses_engine_shape() -> None:
    """``LeanStatisticsResponse.model_validate`` round-trips the helper
    output — the dict the .NET row receives must be parseable as the
    canonical shape, so the frontend's ``portfolio?.trade?.runtime``
    guard passes."""
    from app.services.lean_sidecar_persistence import _normalized_to_lean_statistics_response

    resp = _normalized_to_lean_statistics_response(
        normalized_statistics={"Net Profit": "10.0%", "Sharpe Ratio": "1.5"},
        paired_trades=[
            PairedTrade(1, 1, 2, 100.0, 110.0, 1, pnl=10.0, signal_reason="x", is_synthetic_exit=False),
        ],
        starting_cash=100_000.0,
        total_fees=0.5,
    )
    dumped = resp.model_dump(mode="json")
    reparsed = LeanStatisticsResponse.model_validate(dumped)
    assert reparsed.portfolio.total_net_profit == pytest.approx(0.10)
    assert reparsed.runtime.fees == pytest.approx(0.5)


def test_failed_run_payload_emits_canonical_shape(tmp_path: Path) -> None:
    """Smoke test for Bug B's failed-run regression: the failed payload's
    ``lean_statistics`` must be parseable as ``LeanStatisticsResponse``
    (canonical shape, all zeros) — not a flat ``{error, workspace_path}``."""
    from app.services.lean_sidecar_persistence import build_persist_payload

    ws = tmp_path / "ui_run_failed_shape_check"
    ws.mkdir()  # no normalized/result.json

    payload = build_persist_payload(
        workspace_path=ws,
        run_id="ui_run_failed_shape_check",
        starting_cash=100_000.0,
        symbol="SPY",
        algorithm_name="ema_crossover",
        start_date_ms=1_700_000_000_000,
        end_date_ms=1_700_000_600_000,
    )

    stats = payload["lean_statistics"]
    parsed = LeanStatisticsResponse.model_validate(stats)
    assert parsed.portfolio.start_equity == 0.0
    assert parsed.portfolio.end_equity == 0.0
    assert parsed.trade.total_number_of_trades == 0
    assert parsed.runtime.total_orders == 0


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
