"""Phase 2 integration: Python engine request/response carry data_policy.

PR B (2026-05-19) — both engines persist a canonical ``DataPolicy`` block
on every ``StrategyExecution`` row. This module pins the request/response
side of the contract: the Python ``EngineBacktestRequest`` accepts a
``data_policy`` block and synthesizes a default when the legacy shape is
used, and the ``EngineBacktestResponse`` echoes the post-normalization
value back to the caller.

The full engine run (which writes the row through the .NET ``/api/studies``
endpoint) is gated behind the longer integration test in
``test_lean_engine_polygon_parity.py``; this file only validates the
schema layer.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError


def _raw_policy() -> dict[str, object]:
    return {
        "source": "polygon",
        "symbol": "SPY",
        "adjusted": False,
        "session": "regular",
        "input_bars": {"timespan": "minute", "multiplier": 1},
        "strategy_bars": {"timespan": "minute", "multiplier": 15},
        "timestamp_policy": "bar_close_ms_utc",
        "timezone": "America/New_York",
        "provider_kind": "live",
        "fixture_id": None,
        "fixture_sha256": None,
    }


@pytest.mark.asyncio
async def test_engine_backtest_request_accepts_data_policy_block() -> None:
    """A request including a ``data_policy`` block is accepted as-is."""
    from app.routers.engine import EngineBacktestRequest

    req = EngineBacktestRequest(
        strategy_name="spy_ema_crossover",
        params={"symbol": "SPY"},
        from_date="2025-01-13",
        to_date="2025-01-17",
        resolution="minute",
        data_policy={
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
    )

    assert req.data_policy is not None
    assert req.data_policy.symbol == "SPY"
    assert req.data_policy.adjusted is True
    assert req.data_policy.strategy_bars.multiplier == 15


@pytest.mark.asyncio
async def test_engine_backtest_synthesizes_data_policy_from_legacy_fields() -> None:
    """A request without ``data_policy`` synthesizes it from symbol + resolution."""
    from app.routers.engine import EngineBacktestRequest

    req = EngineBacktestRequest(
        strategy_name="spy_ema_crossover",
        params={"symbol": "spy"},  # lowercase → uppercased by synthesizer
        from_date="2025-01-13",
        to_date="2025-01-17",
        resolution="minute",
    )

    assert req.data_policy is not None
    assert req.data_policy.symbol == "SPY"
    assert req.data_policy.adjusted is True  # PR B default
    assert req.data_policy.session == "regular"
    assert req.data_policy.input_bars.timespan == "minute"
    assert req.data_policy.input_bars.multiplier == 1


@pytest.mark.asyncio
async def test_engine_backtest_synthesizes_data_policy_for_daily_resolution() -> None:
    """Daily resolution maps to ``timespan='day'`` in the synthesized BarsSpec."""
    from app.routers.engine import EngineBacktestRequest

    req = EngineBacktestRequest(
        strategy_name="sma_crossover",
        params={"symbol": "AAPL"},
        from_date="2025-01-13",
        to_date="2025-01-17",
        resolution="daily",
    )

    assert req.data_policy is not None
    assert req.data_policy.symbol == "AAPL"
    assert req.data_policy.input_bars.timespan == "day"
    assert req.data_policy.strategy_bars.timespan == "day"


@pytest.mark.asyncio
async def test_engine_backtest_defers_data_policy_when_symbol_absent() -> None:
    """One-cycle legacy compat: empty ``params`` does NOT raise.

    Pre-PR-B clients POST ``params={}`` and rely on the strategy's
    registered default symbol (e.g., SPY) being resolved downstream.
    The synthesizer leaves ``data_policy=None`` in that case rather
    than raising — request validation must not break legacy callers
    before the strategy registry has a chance to fill in the default.
    Downstream (``_save_study_sync``, .NET persistence layer) handles
    the ``None`` case by either emitting ``null`` for ``dataPolicyJson``
    or synthesizing from the resolved symbol on the .NET side.
    """
    from app.routers.engine import EngineBacktestRequest

    req = EngineBacktestRequest(
        strategy_name="spy_ema_crossover",
        params={},
        from_date="2025-01-13",
        to_date="2025-01-17",
        resolution="minute",
    )

    assert req.data_policy is None


def test_compatibility_profile_rejects_adjusted_input() -> None:
    from app.routers.engine import EngineBacktestRequest

    policy = _raw_policy()
    policy["adjusted"] = True

    with pytest.raises(ValidationError, match="requires raw bars"):
        EngineBacktestRequest(
            strategy_name="ema_crossover_signal",
            params={"symbol": "SPY"},
            from_date="2025-01-13",
            to_date="2025-01-17",
            resolution="minute",
            compatibility_profile="us-equity-raw-ibkr-v1",
            data_policy=policy,
        )


def test_compatibility_profile_wires_lean_sizing_fees_and_stale_fills(tmp_path) -> None:
    from app.engine.data.lean_format import LeanMinuteDataReader
    from app.engine.execution.commission import IbkrEquityCommissionModel
    from app.engine.execution.execution_config import ExecutionConfig
    from app.engine.execution.sizing import LeanSetHoldingsSizing
    from app.routers.engine import EngineBacktestRequest, _build_backtest_engine

    request = EngineBacktestRequest(
        strategy_name="ema_crossover_signal",
        params={"symbol": "SPY"},
        from_date="2025-01-13",
        to_date="2025-01-17",
        resolution="minute",
        compatibility_profile="us-equity-raw-ibkr-v1",
        data_policy=_raw_policy(),
    )

    engine = _build_backtest_engine(
        reader=LeanMinuteDataReader(tmp_path),
        execution_config=ExecutionConfig(),
        request=request,
    )

    assert isinstance(engine.sizing_model, LeanSetHoldingsSizing)
    assert isinstance(engine.sizing_model.fee_model, IbkrEquityCommissionModel)
    assert isinstance(engine.fill_model.fee_model, IbkrEquityCommissionModel)
    assert engine.fill_model.fill_stale_signal_at_current_open is True


def test_compatibility_profile_pins_exact_shared_bar_fixture(tmp_path) -> None:
    from app.routers.engine import EngineBacktestRequest, _pin_compatibility_fixture

    trade_dir = tmp_path / "equity" / "usa" / "minute" / "spy"
    trade_dir.mkdir(parents=True)
    (trade_dir / "20250113_trade.zip").write_bytes(b"day-one")
    (trade_dir / "20250114_trade.zip").write_bytes(b"day-two")
    request = EngineBacktestRequest(
        strategy_name="ema_crossover_signal",
        params={"symbol": "SPY"},
        from_date="2025-01-13",
        to_date="2025-01-14",
        resolution="minute",
        compatibility_profile="us-equity-raw-ibkr-v1",
        data_policy=_raw_policy(),
    )

    _pin_compatibility_fixture(request, [tmp_path])

    assert request.data_policy is not None
    assert request.data_policy.provider_kind == "fixture"
    assert request.data_policy.fixture_id is not None
    assert request.data_policy.fixture_id.startswith("bar-store-v1-")
    assert request.data_policy.fixture_sha256 is not None
    assert len(request.data_policy.fixture_sha256) == 64
