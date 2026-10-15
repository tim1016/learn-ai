"""Golden research reads keep driver connection details out of their HTTP answers."""

from __future__ import annotations

from types import ModuleType

import asyncpg
import httpx
import pytest
from httpx import ASGITransport

from app.main import app
from app.routers import golden_qualifications, golden_validation


@pytest.mark.parametrize(
    ("route_module", "path"),
    [
        (golden_qualifications, "/api/research/golden-qualifications"),
        (golden_qualifications, "/api/research/golden-qualifications/defaults"),
        (golden_qualifications, "/api/research/golden-qualifications/gq-missing"),
        (golden_qualifications, "/api/research/golden-qualifications/gq-missing/deploy-offer"),
        (golden_validation, "/api/research/golden-validations"),
    ],
)
async def test_research_reads_answer_store_outages_without_driver_details(
    monkeypatch: pytest.MonkeyPatch, route_module: ModuleType, path: str
) -> None:
    async def unreachable(*_args: object, **_kwargs: object) -> None:
        raise asyncpg.ConnectionDoesNotExistError("connection to 10.9.8.7:5432 was closed in the middle of operation")

    monkeypatch.setattr(route_module, "with_connection", unreachable)

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(path)

    assert response.status_code == 503, response.text
    assert set(response.json()["detail"]) == {"code", "message"}
    assert response.json()["detail"]["code"] == "RESEARCH_STORE_UNAVAILABLE"
    assert "10.9.8.7" not in response.text and "5432" not in response.text


async def test_qualification_id_with_nul_is_rejected_before_store_access(monkeypatch: pytest.MonkeyPatch) -> None:
    async def unexpected_store_access(*_args: object, **_kwargs: object) -> None:
        pytest.fail("a NUL qualification id reached the store")

    monkeypatch.setattr(golden_qualifications, "with_connection", unexpected_store_access)

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/research/golden-qualifications/gq-%00/deploy-offer")

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "INVALID_QUALIFICATION_ID"
