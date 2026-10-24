"""The route that answers a request records its runs: Indicator reliability (#3520).

The records are read back from the temporary artifact root the test conftest points the default root at. Realized vs IV
runs as a job over admitted lake bars, so its records are driven through the real lake in
``tests/jobs/test_realized_vs_iv_job.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import numpy as np
import pytest
from httpx import ASGITransport

import app.routers.indicator_reliability as reliability_router
from app.main import app
from app.research.artifact.root import default_artifacts_root
from app.research.study_records.store import StudyRecordStore

RELIABILITY = "/api/research/indicator-reliability"
_ET = ZoneInfo("America/New_York")


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http


def the_records(study: Any) -> list[Any]:
    store = StudyRecordStore(study, root=default_artifacts_root())
    return [store.load(record_id) for record_id, _ in store.config_paths()]


# ── Indicator reliability ───────────────────────────────────────────────────────────────────────


def rth_minute_bars(first_day: date, last_day: date) -> list[dict[str, Any]]:
    """One bar per regular-session minute (09:30-15:59 ET) of every weekday from ``first_day`` to ``last_day``."""
    rng = np.random.default_rng(11)
    bars: list[dict[str, Any]] = []
    price = 100.0
    day = first_day
    while day <= last_day:
        if day.weekday() < 5:
            open_ms = int(datetime(day.year, day.month, day.day, 9, 30, tzinfo=_ET).astimezone(UTC).timestamp() * 1000)
            for minute in range(390):
                price *= float(np.exp(rng.normal(0, 0.0005)))
                bars.append(
                    {
                        "timestamp": open_ms + minute * 60_000,
                        "open": price,
                        "high": price * 1.0003,
                        "low": price * 0.9997,
                        "close": price,
                        "volume": 1_000.0,
                    }
                )
        day += timedelta(days=1)
    return bars


RELIABILITY_REQUEST = {
    "symbol": "AAPL",
    "from_date": "2024-03-04",
    "to_date": "2024-03-06",
    "indicator_name": "rsi",
    "indicator_params": {"length": 14},
    "horizons": [1, 5],
}


async def test_an_indicator_reliability_run_is_recorded_with_its_response(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    bars = rth_minute_bars(date(2024, 2, 26), date(2024, 3, 6))
    monkeypatch.setattr(reliability_router, "fetch_bars_chunked", lambda *_, **__: bars)
    monkeypatch.setattr(reliability_router, "RANDOM_SIMULATIONS", 5)  # the record is under test, not the baseline's precision

    response = await client.post(RELIABILITY, json=RELIABILITY_REQUEST)

    assert response.status_code == 200, response.text
    ((config, stored),) = the_records("indicator-reliability")
    assert config.status == "completed"
    assert config.inputs["indicator_name"] == "rsi" and config.inputs["horizons"] == [1, 5]
    assert config.headline is not None and config.headline.startswith("Best horizon ")
    assert stored.result is not None
    assert stored.result["best_horizon"] == response.json()["best_horizon"]
    assert [row["horizon"] for row in stored.result["results"]] == [1, 5]


async def test_an_indicator_reliability_run_that_finds_too_little_data_is_recorded_as_failed(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(reliability_router, "fetch_bars_chunked", lambda *_, **__: [])

    response = await client.post(RELIABILITY, json=RELIABILITY_REQUEST)

    assert response.status_code == 400
    ((config, stored),) = the_records("indicator-reliability")
    assert config.status == "failed" and (config.status_detail or "").startswith("Insufficient data: got 0 bars")
    assert stored.result is None


async def test_empty_horizons_are_recorded_as_the_defaults_the_run_used(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The handler fills in the default horizons itself; the record says what ran, not what was posted."""
    monkeypatch.setattr(reliability_router, "fetch_bars_chunked", lambda *_, **__: [])

    await client.post(RELIABILITY, json={**RELIABILITY_REQUEST, "horizons": []})

    ((config, _),) = the_records("indicator-reliability")
    assert config.inputs["horizons"] == [1, 5, 10, 15, 30]
