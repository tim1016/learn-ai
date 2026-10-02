"""Exact-evidence chart endpoint and strategy-recipe regression tests."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.config import settings
from app.data_lake.path_policy import lake_subpath
from app.data_lake.types import polygon_mode_for
from app.engine.data.trade_bar import TradeBar
from app.lean_sidecar.trading_calendar import next_trading_day, session_open_ms_utc
from app.schemas.engine_chart import EngineChartRequest, EngineStrategyViewRequest
from app.services.engine_chart_service import (
    build_engine_chart,
    build_engine_strategy_view,
    compute_strategy_indicator_results,
)
from tests._helpers.lake_fixture import seed_lake_daily, seed_lake_minute_day

pytestmark = pytest.mark.usefixtures("seeded_lake_catalog")

DAY = date(2026, 1, 5)
FROM_MS = session_open_ms_utc(DAY)
TO_MS = session_open_ms_utc(next_trading_day(DAY))


@pytest.fixture
def chart_store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Seed one SPY day in the lake's adjusted-mode root.

    #1893 retired the policy store this used to seed; the reader resolves
    the lake root for the request's adjustment mode now. The indicator and
    recipe assertions below are unchanged — they only need one day of bars
    wherever the reader looks.
    """
    write_root = tmp_path / "lean-data-writer"
    monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", str(write_root))
    adjusted_root = write_root / lake_subpath(polygon_mode_for(True))
    seed_lake_minute_day(adjusted_root, "SPY", DAY)
    seed_lake_daily(adjusted_root, "SPY", [DAY])


def _request(**overrides: object) -> EngineChartRequest:
    payload: dict[str, object] = {
        "strategy_name": "ema_crossover_signal",
        "parameters": {"symbol": "SPY"},
        "symbol": "SPY",
        "from_ms_utc": FROM_MS,
        "to_ms_utc": TO_MS,
        "adjusted": True,
        "session": "regular",
        "timespan": "minute",
        "multiplier": 15,
    }
    payload.update(overrides)
    return EngineChartRequest.model_validate(payload)


def test_service_resolves_strategy_recipe_from_validated_parameters(chart_store: None) -> None:
    response = build_engine_chart(
        _request(
            strategy_name="sma_crossover",
            parameters={"symbol": "SPY", "short_window": 3, "long_window": 8},
        )
    )

    assert [spec.id for spec in response.indicator_specs] == [
        "sma-length-3",
        "sma-length-8",
    ]
    assert all(spec.strategy_default for spec in response.indicator_specs)


def test_service_draws_the_configured_ema_lengths(chart_store: None) -> None:
    # The EMA lengths are parameters (#2696); the evidence chart must draw the
    # lines the program trades on, not the reference's 5/10.
    response = build_engine_chart(
        _request(parameters={"symbol": "SPY", "fast_period": 3, "slow_period": 20})
    )

    assert [spec.id for spec in response.indicator_specs] == [
        "ema-length-3",
        "ema-length-20",
        "rsi-length-14",
    ]


@pytest.mark.asyncio
async def test_endpoint_computes_indicators_on_the_returned_policy_store_bars(
    chart_store: None,
    client,
) -> None:
    response = await client.post("/api/engine/chart", json=_request().model_dump(mode="json"))

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["policy_key"] == "polygon-adjusted"
    assert len(payload["bars"]) == 26
    assert payload["bars"][0]["t"] == FROM_MS + 15 * 60_000
    assert [spec["id"] for spec in payload["indicator_specs"]] == [
        "ema-length-5",
        "ema-length-10",
        "rsi-length-14",
    ]
    bar_timestamps = [bar["t"] for bar in payload["bars"]]
    for result in payload["indicators"]:
        if isinstance(result["data"], list):
            assert [point["t"] for point in result["data"]] == bar_timestamps


@pytest.mark.asyncio
async def test_endpoint_applies_default_exclusions_and_user_indicators(chart_store: None, client) -> None:
    request = _request(
        excluded_strategy_indicator_ids=["ema-length-5"],
        indicators=[{"name": "sma", "params": {"length": 3}}],
    )

    response = await client.post("/api/engine/chart", json=request.model_dump(mode="json"))

    assert response.status_code == 200, response.text
    assert [spec["id"] for spec in response.json()["indicator_specs"]] == [
        "ema-length-10",
        "rsi-length-14",
        "sma-length-3",
    ]


def test_strategy_rsi_uses_canonical_wilders_value_and_bar_close_timestamp() -> None:
    et = ZoneInfo("America/New_York")
    start = datetime(2026, 1, 5, 9, 30, tzinfo=et)
    closes = [10, 11, 10, 11, 10, 11]
    bars = [
        TradeBar(
            symbol="SPY",
            time=start + timedelta(minutes=index),
            end_time=start + timedelta(minutes=index + 1),
            open=Decimal(close),
            high=Decimal(close),
            low=Decimal(close),
            close=Decimal(close),
            volume=1,
        )
        for index, close in enumerate(closes)
    ]

    result = compute_strategy_indicator_results(bars, [("rsi", {"length": 5})])[0]

    assert isinstance(result.data, list)
    assert [point.value for point in result.data[:-1]] == [None] * 5
    assert result.data[-1].value == pytest.approx(60.0, rel=0, abs=1e-9)
    assert result.data[-1].t == bars[-1].end_ms


@pytest.mark.asyncio
async def test_endpoint_deduplicates_numeric_equivalent_server_owned_indicator_ids(
    chart_store: None,
    client,
) -> None:
    request = _request(
        strategy_name="spy_strategy_b",
        parameters={},
        indicators=[{"name": "supertrend", "params": {"length": 10, "multiplier": 3}}],
    )

    response = await client.post("/api/engine/chart", json=request.model_dump(mode="json"))

    assert response.status_code == 200, response.text
    specs = response.json()["indicator_specs"]
    assert [spec["id"] for spec in specs].count("supertrend-length-10-multiplier-3") == 1


@pytest.fixture
def two_day_store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> date:
    """Two seeded SPY sessions: the run's warmup day, then its one evaluated day."""
    write_root = tmp_path / "lean-data-writer"
    monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", str(write_root))
    adjusted_root = write_root / lake_subpath(polygon_mode_for(True))
    second = next_trading_day(DAY)
    for day in (DAY, second):
        seed_lake_minute_day(adjusted_root, "SPY", day)
    seed_lake_daily(adjusted_root, "SPY", [DAY, second])
    return second


def test_a_backtests_strategy_view_draws_its_warmup_behind_its_own_decisions(two_day_store: date) -> None:
    """#2639 D13: Strategy Lab reads a run the way the bot page reads a bot."""
    evaluated_from = session_open_ms_utc(two_day_store)
    evaluated_to = session_open_ms_utc(next_trading_day(two_day_store))

    view = build_engine_strategy_view(
        EngineStrategyViewRequest(
            strategy_name="ema_crossover_signal",
            parameters={"symbol": "SPY", "rsi_min": 45, "rsi_max": 75},
            symbol="SPY",
            from_ms_utc=evaluated_from,
            to_ms_utc=evaluated_to,
            warmup_from_ms_utc=FROM_MS,
        )
    )

    phases = [candle.phase for candle in view.candles]
    assert phases == ["before_start"] * 26 + ["decision"] * 26
    assert all(candle.bar_close_ms <= evaluated_from for candle in view.candles[:26])
    assert view.run_started_at_ms == evaluated_from
    # The deployed band labels the default gate and its pane.
    assert view.declaration.gates[0].label == "RSI in 45–75"
    decided = view.candles[26:]
    assert {candle.outcome for candle in decided} <= {"no_action", "enter_intent", "exit_intent"}
    ready = [candle for candle in decided if candle.explanation.ready]
    assert ready and all(candle.gates["rsi_band"] is not None for candle in ready)
    assert view.settings["rsi_min"] == 45
