"""``GET /api/engine/data/availability`` names unreadable sessions and files (#2499).

Since #2445/#2489 the availability report knows about files that are on disk
but that their reader cannot decode — a backfill does not repair them. The
endpoint's response model dropped both fields, so a caller saw
``is_complete=false`` with an empty ``missing_days`` and nothing to act on.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

import app.routers.engine as engine_router
from app.lean_sidecar.trading_calendar import expected_sessions
from app.main import app
from tests._helpers.lean_store import seed_store_day

WINDOW = (date(2024, 12, 2), date(2024, 12, 6))
PROBED = date(2024, 12, 3)


@pytest.fixture
def minute_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    for day in expected_sessions(*WINDOW):
        seed_store_day(tmp_path, "SPY", day)
    monkeypatch.setattr(engine_router, "_resolve_lean_data_roots", lambda *, adjusted: [tmp_path])
    return tmp_path


def _minute_zip(root: Path, day: date) -> Path:
    return root / "equity" / "usa" / "minute" / "spy" / f"{day.strftime('%Y%m%d')}_trade.zip"


async def _get_availability(**params: str) -> dict:
    query = {"symbol": "SPY", "start": WINDOW[0].isoformat(), "end": WINDOW[1].isoformat()}
    query.update(params)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/engine/data/availability", params=query)
    assert response.status_code == 200, response.text
    return response.json()


async def test_an_unreadable_minute_zip_is_reported_with_its_path(minute_root: Path) -> None:
    damaged = _minute_zip(minute_root, PROBED)
    damaged.write_bytes(b"not a zip")

    body = await _get_availability()

    assert body["is_complete"] is False
    assert body["missing_days"] == []
    assert body["unreadable_days"] == [PROBED.isoformat()]
    assert len(body["unreadable_files"]) == 1
    assert body["unreadable_files"][0]["path"] == str(damaged)
    assert body["unreadable_files"][0]["reason"].startswith("BadZipFile")


async def test_a_readable_window_reports_no_unreadable_days(minute_root: Path) -> None:
    body = await _get_availability()

    assert body["is_complete"] is True
    assert body["unreadable_days"] == []
    assert body["unreadable_files"] == []


async def test_an_unreadable_daily_history_is_reported_with_its_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    daily_zip = tmp_path / "equity" / "usa" / "daily" / "spy.zip"
    daily_zip.parent.mkdir(parents=True)
    daily_zip.write_bytes(b"not a zip")
    monkeypatch.setattr(engine_router, "_resolve_lean_data_roots", lambda *, adjusted: [tmp_path])

    body = await _get_availability(resolution="daily")

    assert body["is_complete"] is False
    # The daily reader aborts on an undecodable history, so every scheduled
    # session is unreadable and none is merely "missing".
    assert body["unreadable_days"] == [day.isoformat() for day in expected_sessions(*WINDOW)]
    assert body["missing_days"] == []
    assert [file["path"] for file in body["unreadable_files"]] == [str(daily_zip)]
    assert body["unreadable_files"][0]["reason"].startswith("BadZipFile")
