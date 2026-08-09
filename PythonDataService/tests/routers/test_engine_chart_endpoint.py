"""Exact-evidence chart endpoint and strategy-recipe regression tests."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.engine.data.trade_bar import TradeBar
from app.lean_sidecar.trading_calendar import next_trading_day, session_open_ms_utc
from app.schemas.engine_chart import EngineChartRequest
from app.services.engine_chart_service import build_engine_chart, compute_strategy_indicator_results
from tests._helpers.lean_store import seed_store_day

DAY = date(2026, 1, 5)
FROM_MS = session_open_ms_utc(DAY)
TO_MS = session_open_ms_utc(next_trading_day(DAY))


@pytest.fixture
def chart_store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LEAN_DATA_ROOT", str(tmp_path / "no-reference-mount"))
    monkeypatch.setenv("LEAN_DATA_CACHE", str(tmp_path / "store"))
    seed_store_day(tmp_path / "store" / "polygon-adjusted", "SPY", DAY)


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
    assert result.data[-1].t == int(bars[-1].end_time.timestamp() * 1000)


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
