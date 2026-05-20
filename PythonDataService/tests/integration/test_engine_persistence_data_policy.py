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
