from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from app.data_lake.admission import LakeAdmissionError
from app.lean_sidecar.trading_calendar import session_open_ms_utc, session_window_for_date
from app.option_archive.planner import UNDERLYINGS
from app.option_archive.service import OptionArchiveService
from app.option_archive.settings import ArchiveSettings
from app.option_archive.snapshot import (
    SnapshotOutsideWindowError,
    observe_chain,
    read_observation,
    save_observation,
    snapshot_path,
)
from app.option_archive.state import ArchiveState, ArchiveStateDoc
from app.utils.background_loop import background_loop
from tests._helpers.fake_lake_catalog import FakeCatalog
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
async def test_bad_traversals_are_saved_as_incomplete_with_reason(damage: str, fake_catalog: FakeCatalog, tmp_lake: Path) -> None:
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
    saved = await background(save_observation(observation))
    assert saved.failure is None
    assert await read_observation(raw_root(tmp_lake) / snapshot_path(observation)) == observation


@respx.mock
async def test_evening_window_follows_early_close_and_a_later_observation_never_fills_a_missed_evening(
    fake_catalog: FakeCatalog, tmp_lake: Path,
) -> None:
    half_day, missed_day, next_day = date(2026, 11, 27), date(2026, 11, 30), date(2026, 12, 1)
    config = ArchiveSettings(ENABLED=True)
    early_close = session_window_for_date(half_day).close_ms_utc
    current = early_close + config.SNAPSHOT_DELAY_MS - 1
    state = ArchiveState(tmp_lake / "state.json")
    def initialize(doc: ArchiveStateDoc) -> None:
        doc.baseline_ms = doc.snapshot_activation_ms = session_open_ms_utc(half_day)
    state.update(initialize)
    service = OptionArchiveService(config=config, state=state, clock=lambda: current)
    requests: list[tuple[str, int]] = []

    def snapshot(request: httpx.Request) -> httpx.Response:
        symbol = request.url.path.rsplit("/", 1)[-1]
        requests.append((symbol, current))
        return httpx.Response(200, json={"status": "OK", "results": [row(f"O:{symbol}261204C00500000")]})

    respx.get(url__regex=r"https://api\.polygon\.io/v3/snapshot/options/.*").mock(side_effect=snapshot)
    assert not service.snapshot_due(current)
    with pytest.raises(SnapshotOutsideWindowError):
        await background(service.snapshot_pass())
    assert requests == []
    current += 1
    assert service.snapshot_due(current)
    await background(service.snapshot_pass())
    assert requests == [(symbol, current) for symbol in UNDERLYINGS]
    assert all(state.read().snapshots[symbol][session_open_ms_utc(half_day)].complete for symbol in UNDERLYINGS)

    # No late HTTP request or observation is allowed to relabel the missed
    # session as observed.
    current = session_window_for_date(missed_day).close_ms_utc + config.SNAPSHOT_DELAY_MS + config.SNAPSHOT_WINDOW_MS
    with pytest.raises(SnapshotOutsideWindowError):
        await background(service.snapshot_pass())
    assert len(requests) == len(UNDERLYINGS)
    assert all(item.snapshot_missed_evenings == 1 for item in service.health().underlyings)

    current = session_window_for_date(next_day).close_ms_utc + config.SNAPSHOT_DELAY_MS
    restarted = OptionArchiveService(config=config, state=ArchiveState(state.path), clock=lambda: current)
    assert restarted.snapshot_due(current)
    await background(restarted.snapshot_pass())
    assert all(item.snapshot_session_ms == session_open_ms_utc(next_day) and item.snapshot_missed_evenings == 1
               for item in restarted.health().underlyings)
    assert requests[-len(UNDERLYINGS):] == [(symbol, current) for symbol in UNDERLYINGS]
    assert len(requests) == 2 * len(UNDERLYINGS)
    for symbol in UNDERLYINGS:
        assert set(restarted.state.read().snapshots[symbol]) == {session_open_ms_utc(half_day), session_open_ms_utc(next_day)}


@pytest.mark.parametrize(("edge", "due", "missed"), [("before", False, 0), ("open", True, 0), ("last", True, 0), ("cutoff", False, 1)])
def test_snapshot_due_and_missed_evening_agree_at_the_window_edges(edge: str, due: bool, missed: int, tmp_path: Path) -> None:
    """The window is half-open at ``close + delay`` through ``close + delay + window``."""
    config = ArchiveSettings(ENABLED=True)
    day = date(2026, 10, 19)
    start = session_window_for_date(day).close_ms_utc + config.SNAPSHOT_DELAY_MS
    current = {"before": start - 1, "open": start, "last": start + config.SNAPSHOT_WINDOW_MS - 1,
               "cutoff": start + config.SNAPSHOT_WINDOW_MS}[edge]
    state = ArchiveState(tmp_path / "state.json")
    def initialize(doc: ArchiveStateDoc) -> None:
        doc.baseline_ms, doc.snapshot_activation_ms = session_open_ms_utc(date(2026, 10, 16)), session_open_ms_utc(day)
    state.update(initialize)
    service = OptionArchiveService(config=config, state=state, clock=lambda: current)
    assert service.snapshot_due(current) is due
    assert all(item.snapshot_missed_evenings == missed for item in service.health().underlyings)
