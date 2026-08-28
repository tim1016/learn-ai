"""GET /api/data-lake/backfill-defaults — the spec constants a browser cannot derive (#1838).

Mirrors tests/routers/test_data_lake_backfill_job.py's flag-off/flag-on app
construction. The route touches no catalog and no Polygon credential, so
there is nothing to fake beyond the pinned-digest module constant.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import app.routers.data_lake as data_lake_router
from app.data_lake.types import MAX_SYMBOL_LENGTH, MAX_TRADING_RANGE_DAYS
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
    }


async def test_reports_an_absent_pin_as_null_not_an_empty_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An empty string would compose a spec that fails deep inside ensure_data's
    # Phase 0; null lets the UI say backfill is unavailable up front.
    monkeypatch.setattr(data_lake_router, "PINNED_LEAN_IMAGE_DIGEST", None)

    status_code, body = await _get_defaults(_make_app(include_data_lake=True))

    assert status_code == 200
    assert body["lean_image_digest"] is None
