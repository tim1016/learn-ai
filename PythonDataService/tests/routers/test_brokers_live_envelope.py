"""Router surface for the guarded loss-hold clear (ADR 0059 D4).

Transport-only assertions: the service tests
(``tests/services/test_alpaca_live_envelope.py``) cover the decision, so
this file only proves the route resolves the broker, the active runtime, and
the control-secret guard the same way every other mutating data-plane
control route does.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from app.broker.alpaca.clerk.active_authority import (
    ActiveClerkRuntime,
    set_active_clerk_runtime,
)
from app.config import settings
from app.security.data_plane_control import CONTROL_SECRET_HEADER
from tests.broker.v2panel.test_shadow_operator_surfaces import (
    shadow_app,  # noqa: F401 — the composed-shadow ASGI app fixture
)

_CLEAR_PATH = "/api/brokers/alpaca/live-envelope/loss-hold/clear"


async def _post(app: FastAPI, path: str, **kwargs: Any) -> httpx.Response:
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(path, **kwargs)


async def test_the_clear_reports_no_hold_over_http(
    shadow_app: tuple[FastAPI, ActiveClerkRuntime],  # noqa: F811 — the imported fixture
) -> None:
    app, _runtime = shadow_app

    response = await _post(app, _CLEAR_PATH)

    assert response.status_code == 200, response.json()
    assert response.json()["outcome"] == "no_hold"


async def test_an_unsupported_broker_is_404(
    shadow_app: tuple[FastAPI, ActiveClerkRuntime],  # noqa: F811 — the imported fixture
) -> None:
    app, _runtime = shadow_app

    response = await _post(app, "/api/brokers/ibkr/live-envelope/loss-hold/clear")

    assert response.status_code == 404
    assert response.json()["detail"]["reason"] == "live_envelope_unsupported_broker"


async def test_no_installed_runtime_is_503(
    shadow_app: tuple[FastAPI, ActiveClerkRuntime],  # noqa: F811 — the imported fixture
) -> None:
    app, _runtime = shadow_app
    # The fixture's own ``finally`` resets the active runtime to ``None``
    # anyway; clearing it here mid-test is what exercises the 503 path.
    set_active_clerk_runtime(None)

    response = await _post(app, _CLEAR_PATH)

    assert response.status_code == 503
    assert response.json()["detail"]["reason"] == "live_envelope_not_installed"


async def test_the_clear_is_refused_without_the_control_secret(
    shadow_app: tuple[FastAPI, ActiveClerkRuntime],  # noqa: F811 — the imported fixture
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same guard every mutating data-plane control route carries."""
    app, _runtime = shadow_app
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", "test-control-secret")
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)

    response = await _post(app, _CLEAR_PATH)

    assert response.status_code == 403
    assert CONTROL_SECRET_HEADER in response.json()["detail"]
