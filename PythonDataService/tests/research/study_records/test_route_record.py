"""The route that answers a request from the caller's own bars records its runs: Realized vs IV (#3520).

The records are read back from the temporary artifact root the test conftest points the default root at. Indicator
reliability reads admitted lake bars, so its completed, refused and failed records are driven through the real lake in
``tests/routers/test_indicator_reliability_endpoint.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import numpy as np
import pytest
from httpx import ASGITransport

from app.main import app
from app.research.artifact.root import default_artifacts_root
from app.research.study_records.store import StudyRecordStore

RVIV = "/api/edge/realized-vs-iv/series"


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http


def the_records(study: Any) -> list[Any]:
    store = StudyRecordStore(study, root=default_artifacts_root())
    return [store.load(record_id) for record_id, _ in store.config_paths()]


def daily_bars(n: int, *, seed: int = 42, start_ms: int = 1_700_000_000_000) -> list[dict[str, Any]]:
    rng = np.random.default_rng(seed)
    closes = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.01, size=n)))
    return [
        {
            "ts": start_ms + i * 86_400_000,
            "open": float(c * 0.999),
            "high": float(c * 1.005),
            "low": float(c * 0.995),
            "close": float(c),
            "volume": 1_000_000.0,
        }
        for i, c in enumerate(closes)
    ]


def rviv_request(bars: list[dict[str, Any]], **changes: Any) -> dict[str, Any]:
    return {"symbol": "SPY", "bar_size": "1d", "tenor_days": 30, "windows": [5, 10], "bars": bars, **changes}


# ── Realized vs IV ──────────────────────────────────────────────────────────────────────────────


async def test_a_realized_vs_iv_run_is_recorded_with_the_bars_it_ran_on_and_a_digest_of_them(client: httpx.AsyncClient) -> None:
    bars = daily_bars(60)

    response = await client.post(RVIV, json=rviv_request(list(reversed(bars))))

    assert response.status_code == 200, response.text
    ((config, stored),) = the_records("realized-vs-iv")
    assert config.status == "completed" and config.job_id is None
    assert config.inputs["bars"] == {"count": 60, "first_ts_ms": bars[0]["ts"], "last_ts_ms": bars[-1]["ts"]}
    assert "iv_series" not in config.inputs and config.inputs["iv_series_supplied"] is False
    assert config.inputs["windows"] == [5, 10] and config.inputs["tenor_days"] == 30
    assert config.data_identity is not None and len(config.data_identity) == 64
    assert config.headline is not None and config.headline.startswith("60 bars; IV from ")
    assert stored.result is not None
    assert stored.result["ts"] == response.json()["ts"], "the stored series is the one the page was served"
    assert stored.result["bars"] == bars, "the chart's bars are stored in time order, as the series is"


async def test_a_second_run_on_the_same_data_has_the_same_data_identity_and_on_changed_data_a_new_one(
    client: httpx.AsyncClient,
) -> None:
    await client.post(RVIV, json=rviv_request(daily_bars(60)))
    await client.post(RVIV, json=rviv_request(daily_bars(60)))
    await client.post(RVIV, json=rviv_request(daily_bars(60, seed=7)))

    records = the_records("realized-vs-iv")
    identities_by_data: dict[float, set[str | None]] = {}
    for config, stored in records:
        identities_by_data.setdefault(stored.result["bars"][0]["close"], set()).add(config.data_identity)
    assert len(records) == 3, "every run is its own record"
    assert sorted(len(identities) for identities in identities_by_data.values()) == [1, 1], "the same bars, one identity"
    assert len({identity for identities in identities_by_data.values() for identity in identities}) == 2


async def test_a_realized_vs_iv_request_the_study_refuses_is_recorded_as_failed(client: httpx.AsyncClient) -> None:
    response = await client.post(RVIV, json=rviv_request([]))

    assert response.status_code == 400
    ((config, stored),) = the_records("realized-vs-iv")
    assert config.status == "failed" and config.status_detail == "bars must not be empty"
    assert config.data_identity is None and stored.result is None
    assert config.inputs["bars"]["count"] == 0


async def test_a_request_refused_before_the_study_runs_is_not_a_run_and_leaves_no_record(client: httpx.AsyncClient) -> None:
    response = await client.post(RVIV, json=rviv_request(daily_bars(10), bar_size="5m"))

    assert response.status_code == 422
    assert the_records("realized-vs-iv") == []


async def test_a_realized_vs_iv_run_names_the_record_it_was_prefilled_from(client: httpx.AsyncClient) -> None:
    await client.post(RVIV, json=rviv_request(daily_bars(60)))
    ((first, _),) = the_records("realized-vs-iv")

    await client.post(RVIV, json=rviv_request(daily_bars(60, seed=3), rerun_of=first.record_id))

    again = [config for config, _ in the_records("realized-vs-iv") if config.record_id != first.record_id]
    assert [config.rerun_of for config in again] == [first.record_id]
