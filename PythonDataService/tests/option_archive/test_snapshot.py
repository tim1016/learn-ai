from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from app.data_lake.admission import LakeAdmissionError
from app.lean_sidecar.trading_calendar import session_window_for_date
from app.option_archive.snapshot import observe_chain, read_observation, save_observation, snapshot_path
from app.utils.background_loop import background_loop
from tests.options_backtest._lake import FRIDAY, raw_root
from tests.options_backtest._lake import fake_catalog as fake_catalog
from tests.options_backtest._lake import tmp_lake as tmp_lake

URL = "https://api.polygon.io/v3/snapshot/options/SPY"
CALL = "O:SPY250613C00500000"
PUT = "O:SPY250613P00500000"


async def background[T](work: Coroutine[Any, Any, T]) -> T:
    return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(work, background_loop()))


def row(ticker: str = CALL, **changes: Any) -> dict:
    return {"details": {"ticker": ticker}, "open_interest": 0, "implied_volatility": 0.2,
            "greeks": {"delta": 0.5, "gamma": 0.1, "theta": -0.03, "vega": 0,
                       "last_updated": 1_749_846_600_123_456_789}, **changes}


@respx.mock
async def test_complete_pages_preserve_zero_missing_and_nested_field_times(fake_catalog, tmp_lake: Path) -> None:
    now = session_window_for_date(FRIDAY).close_ms_utc + 1_800_000
    respx.get(URL, params={"limit": 250}).mock(return_value=httpx.Response(200, json={"status": "OK", "results": [row()], "next_url": URL + "?cursor=second"}))
    respx.get(URL + "?cursor=second").mock(return_value=httpx.Response(200, json={"status": "OK", "results": [row(PUT, implied_volatility=None, greeks={"delta": True})]}))
    observation = await background(observe_chain("SPY", FRIDAY, clock=lambda: now))
    assert observation.complete and len(observation.contracts) == 2, observation.reason
    call, put = observation.contracts
    assert call.open_interest.value == 0 and call.open_interest.vendor_time_ms is None
    assert call.delta.vendor_time_ms == 1_749_846_600_123 and call.vega.value == 0
    assert call.implied_volatility.vendor_time_ms is None
    assert put.implied_volatility.state == "unavailable" and put.delta.state == "invalid"
    assert put.gamma.state == "missing" and put.gamma.value is None
    saved = await background(save_observation(observation))
    assert saved.failure is None
    path = raw_root(tmp_lake) / str(snapshot_path(observation))
    assert (await read_observation(path)).observation_id == observation.observation_id
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(LakeAdmissionError):
        await read_observation(path)


@pytest.mark.parametrize("damage", ["loop", "foreign", "missing", "duplicate", "partial", "secret_cursor"])
@respx.mock
async def test_bad_traversals_are_saved_as_incomplete_with_reason(damage: str) -> None:
    body = {"status": "OK", "results": [row()]}
    if damage == "loop":
        body["next_url"] = URL
    elif damage == "foreign":
        body["next_url"] = "https://foreign.example/next"
    elif damage == "missing":
        body["next_url"] = URL + "?cursor=second"
        respx.get(URL + "?cursor=second").mock(return_value=httpx.Response(200, json={"status": "OK"}))
    elif damage == "duplicate":
        body["results"].append(row())
    elif damage == "partial":
        body["status"] = "PARTIAL"
    elif damage == "secret_cursor":
        body["next_url"] = URL + "?apiKey=credential"
    respx.get(URL, params={"limit": 250}).mock(return_value=httpx.Response(200, json=body))
    now = session_window_for_date(FRIDAY).close_ms_utc + 1_800_000
    observation = await background(observe_chain("SPY", FRIDAY, clock=lambda: now))
    assert not observation.complete and observation.reason
    assert "credential" not in observation.model_dump_json()
