"""Preview transport and vendor-evidence refusal tests; no network or database."""

from __future__ import annotations

from datetime import date

import httpx
import pytest
from fastapi import FastAPI

from app.options_backtest.picking import ListedContract, Listing
from app.options_backtest.position_preview import PreviewUnavailable
from app.routers.options_position_preview import router
from app.schemas.http_errors import ErrorDetailResponse
from app.services import options_position_preview as service
from app.utils.session_anchors import MAX_TIMESTAMP_MS
from tests.options_backtest._market import at
from tests.options_backtest.test_position_preview import contract, request


def test_current_uses_timestamped_ask_and_never_falls_back_to_last_trade(monkeypatch: pytest.MonkeyPatch) -> None:
    call = contract("call", "500")
    listing = Listing("SPY", date(2026, 10, 9), (ListedContract(call, 100, True),), True)
    monkeypatch.setattr(service, "_listing", lambda *_: listing)
    row = {
        "ticker": "O:" + call.occ_symbol,
        "last_quote": {"bid": 2, "ask": 2.2, "last_updated": at(5) * 1_000_000},
        "last_trade": {"price": 9},
    }
    monkeypatch.setattr(service, "_snapshot", lambda *_: {"underlying": {"price": 500}, "contracts": [row]})
    result = service.current_preview(request(source="current"), now_ms=at(6))
    assert result.legs[0].premium_usd == "2.2"
    assert result.legs[0].price_at_ms == at(5)
    assert "ask" in result.legs[0].price_source
    row["last_quote"] = None
    with pytest.raises(PreviewUnavailable, match="two-sided quote"):
        service.current_preview(request(source="current"), now_ms=at(6))


@pytest.mark.parametrize(
    "quote",
    [
        {"bid": 3, "ask": 2, "last_updated": at(5) * 1_000_000},
        {"bid": 1, "ask": 2, "last_updated": at(9) * 1_000_000},
    ],
)
def test_bad_current_quotes_are_not_priced(monkeypatch: pytest.MonkeyPatch, quote: dict) -> None:
    call = contract("call", "500")
    monkeypatch.setattr(
        service, "_listing", lambda *_: Listing("SPY", date(2026, 10, 9), (ListedContract(call, 100, True),), True)
    )
    monkeypatch.setattr(
        service,
        "_snapshot",
        lambda *_: {"underlying": {"price": 500}, "contracts": [{"ticker": call.occ_symbol, "last_quote": quote}]},
    )
    with pytest.raises(PreviewUnavailable):
        service.current_preview(request(source="current"), now_ms=at(6))


@pytest.mark.asyncio
async def test_route_returns_data_refusal_and_validates_temporal_bounds() -> None:
    app = FastAPI()
    app.include_router(router)
    operation = app.openapi()["paths"]["/position-preview"]["post"]
    for code in ("409", "503"):
        assert operation["responses"][code]["content"]["application/json"]["schema"]["$ref"].endswith(
            "/ErrorDetailResponse"
        )

    class Missing:
        async def preview(self, _):
            raise PreviewUnavailable("No entry-minute price is stored")

    app.dependency_overrides[service.get_position_preview_service] = Missing
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        body = request().model_dump(mode="json")
        response = await client.post("/position-preview", json=body)
        assert response.status_code == 409
        ErrorDetailResponse.model_validate(response.json())
        assert response.json()["detail"] == "No entry-minute price is stored"
        body["trading_date_ms"] = MAX_TIMESTAMP_MS + 1
        assert (await client.post("/position-preview", json=body)).status_code == 422
        body.update(source="current", trading_date_ms=None, mode="replay")
        assert (await client.post("/position-preview", json=body)).status_code == 422


@pytest.mark.asyncio
async def test_route_publishes_retryable_failure_without_internal_error_text() -> None:
    app = FastAPI()
    app.include_router(router)

    class Unreadable:
        async def preview(self, _):
            raise RuntimeError("private-storage-path")

    app.dependency_overrides[service.get_position_preview_service] = Unreadable
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/position-preview", json=request().model_dump(mode="json"))
    assert response.status_code == 503
    body = ErrorDetailResponse.model_validate(response.json())
    assert "Retry" in body.detail
    assert "private-storage-path" not in body.detail
