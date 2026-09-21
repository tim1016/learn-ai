"""GET /api/data-lake/backfill-defaults — the spec constants a browser cannot derive (#1838).

Mirrors tests/routers/test_data_lake_backfill_job.py's flag-off/flag-on app
construction. The route touches no catalog and no Polygon credential, so
there is nothing to fake beyond the pinned-digest module constant.
"""

from __future__ import annotations

import time
from datetime import date

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import app.routers.data_lake as data_lake_router
from app.data_lake.polygon_fetcher import POLYGON_HISTORY_YEARS, polygon_history_floor
from app.data_lake.types import (
    MAX_SYMBOL_LENGTH,
    MAX_TRADING_RANGE_DAYS,
    calendar_anchor_ms_to_trading_date,
    trading_date_at_ms,
    trading_date_to_calendar_anchor_ms,
)
from app.routers.data_lake import router as data_lake_router_instance

pytestmark = pytest.mark.asyncio


def _make_app(*, include_data_lake: bool) -> FastAPI:
    app = FastAPI()
    if include_data_lake:
        app.include_router(data_lake_router_instance)
    return app


async def _get_defaults(app: FastAPI) -> tuple[int, dict]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/data-lake/backfill-defaults")
    return response.status_code, (response.json() if response.status_code == 200 else {})


async def test_route_404_when_flag_off() -> None:
    status_code, _ = await _get_defaults(_make_app(include_data_lake=False))
    assert status_code == 404


async def test_publishes_the_pinned_digest_and_the_shared_caps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(data_lake_router, "PINNED_LEAN_IMAGE_DIGEST", "sha256:pinned")

    status_code, body = await _get_defaults(_make_app(include_data_lake=True))

    assert status_code == 200
    assert body == {
        "market": "usa",
        "lean_image_digest": "sha256:pinned",
        # Same constants DataRunSpec's validator and GET /coverage enforce —
        # the form rejects an over-wide window instead of learning about it
        # from a 422.
        "max_trading_range_days": MAX_TRADING_RANGE_DAYS,
        "max_symbol_length": MAX_SYMBOL_LENGTH,
        "provider_history_start_ms": trading_date_to_calendar_anchor_ms(
            polygon_history_floor(trading_date_at_ms(int(time.time() * 1000)))
        ),
    }


async def test_history_floor_is_strictly_inside_the_provider_window() -> None:
    """The regression behind #2241.

    ``MAX_TRADING_RANGE_DAYS`` is ``5 * 366``, padded for leap years, so a
    window composed as ``today - (cap - 4)`` starts exactly five years back
    — and five years to the day is the one day a five-year plan refuses
    (probed 2026-09-21: 2021-09-21 answered 403, 2021-09-22 answered 200).
    Because ``provider_entitlement_error`` is globally fatal in the backfill
    worker, that single day aborted every run before a bar was written. The
    published floor must therefore sit *after* the anniversary, never on it.
    """
    status_code, body = await _get_defaults(_make_app(include_data_lake=True))
    today = trading_date_at_ms(int(time.time() * 1000))

    assert status_code == 200
    floor = calendar_anchor_ms_to_trading_date(body["provider_history_start_ms"])
    assert floor > today.replace(year=today.year - POLYGON_HISTORY_YEARS)
    # And still inside the request-validation cap, so a window that honours
    # the floor cannot be refused by the catalog for being over-wide.
    assert (today - floor).days + 1 <= MAX_TRADING_RANGE_DAYS


async def test_history_floor_survives_a_leap_day_anniversary() -> None:
    # 2028-02-29 has no anniversary in 2023; falling back to the 28th keeps
    # the floor a real date instead of raising inside a request.
    assert polygon_history_floor(date(2028, 2, 29)) == date(2023, 3, 2)


async def test_reports_an_absent_pin_as_null_not_an_empty_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An empty string would compose a spec that fails deep inside ensure_data's
    # Phase 0; null lets the UI say backfill is unavailable up front.
    monkeypatch.setattr(data_lake_router, "PINNED_LEAN_IMAGE_DIGEST", None)

    status_code, body = await _get_defaults(_make_app(include_data_lake=True))

    assert status_code == 200
    assert body["lean_image_digest"] is None
