"""HTTP endpoint seam test for POST /api/brokers/alpaca/orders (phase 2, S1).

Exercises the full write path over ASGITransport: router → Clerk → real
``AlpacaBroker`` → real ``AlpacaTradingClient`` → ``responses``-mocked Alpaca
REST. alpaca-py drives ``requests``, so ``responses`` (not respx) is the mock
that intercepts it. One accepted leg and one rejected (422) leg assert the
typed per-leg response shape and that a reject surfaces what/why, not a 500.
"""

from __future__ import annotations

import json
from collections.abc import Generator

import pytest
import responses
from httpx import ASGITransport, AsyncClient, Response

from app.broker.alpaca.broker import AlpacaBroker
from app.broker.alpaca.clerk import journal as journal_module
from app.broker.alpaca.clerk.clerk import (
    AlpacaClerk,
    reset_alpaca_clerk_for_testing,
    set_alpaca_clerk,
)
from app.broker.alpaca.client import AlpacaTradingClient
from app.broker.alpaca.config import AlpacaSettings, reset_alpaca_settings_for_testing
from app.config import settings
from app.main import app
from app.security.data_plane_control import CONTROL_SECRET_HEADER

_BASE = "https://paper-api.alpaca.markets"

_ACCOUNT_BODY = json.dumps(
    {
        "account_number": "PA-ENDPOINT",
        "status": "ACTIVE",
        "currency": "USD",
        "cash": "1000",
        "equity": "1000",
        "buying_power": "2000",
        "portfolio_value": "1000",
        "long_market_value": "0",
        "short_market_value": "0",
        "pattern_day_trader": False,
        "trading_blocked": False,
        "account_blocked": False,
        "created_at": "2024-01-01T00:00:00Z",
    }
)


def _accepted_order_body(client_order_id: str, symbol: str) -> str:
    return json.dumps(
        {
            "id": "broker-order-xyz",
            "client_order_id": client_order_id,
            "symbol": symbol,
            "asset_class": "us_equity",
            "side": "buy",
            "order_type": "market",
            "type": "market",
            "time_in_force": "day",
            "qty": "1",
            "filled_qty": "0",
            "status": "accepted",
            "submitted_at": "2026-07-22T14:30:00Z",
            "created_at": "2026-07-22T14:30:00Z",
            "updated_at": "2026-07-22T14:30:00Z",
        }
    )


@pytest.fixture
def _alpaca_clerk(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    monkeypatch.setenv("ALPACA_CLERK_DIR", str(tmp_path))
    journal_module.reset_clerk_settings_for_testing()
    reset_alpaca_settings_for_testing()
    settings = AlpacaSettings(api_key_id="k", api_secret_key="s", mode="paper")
    broker = AlpacaBroker(AlpacaTradingClient(settings=settings))
    set_alpaca_clerk(AlpacaClerk(read=broker, trade=broker))
    yield
    reset_alpaca_clerk_for_testing()
    journal_module.reset_clerk_settings_for_testing()
    reset_alpaca_settings_for_testing()


def _control_headers() -> dict[str, str]:
    """Supply the control-mutation secret when the test env configures one.

    Mirrors the real caller (the dev proxy attaches this header). When no secret
    is configured (CI's unauthenticated-allowed mode), the header is harmless.
    """
    secret = settings.DATA_PLANE_CONTROL_SECRET.strip()
    return {CONTROL_SECRET_HEADER: secret} if secret else {}


async def _post(body: dict) -> Response:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.post(
            "/api/brokers/alpaca/orders", json=body, headers=_control_headers()
        )


@responses.activate
async def test_accepted_leg_returns_acked_result(_alpaca_clerk: None) -> None:
    responses.add(responses.GET, f"{_BASE}/v2/account", body=_ACCOUNT_BODY, status=200)

    def _order_callback(request):
        payload = json.loads(request.body)
        return (
            200,
            {},
            _accepted_order_body(payload["client_order_id"], payload["symbol"]),
        )

    responses.add_callback(
        responses.POST, f"{_BASE}/v2/orders", callback=_order_callback
    )

    response = await _post(
        {"operator": "inkant", "legs": [{"symbol": "SPY", "side": "buy", "quantity": 1}]}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["broker"] == "alpaca"
    assert body["account_id"] == "PA-ENDPOINT"
    [leg] = body["results"]
    assert leg["status"] == "acked"
    assert leg["order"]["status"] == "accepted"
    # client_order_id echoed back equals the minted order_ref.
    assert leg["order"]["client_order_id"] == leg["order_ref"]
    assert leg["order_ref"].startswith("manual/inkant/v1:")


@responses.activate
async def test_rejected_leg_surfaces_what_why_not_500(_alpaca_clerk: None) -> None:
    responses.add(responses.GET, f"{_BASE}/v2/account", body=_ACCOUNT_BODY, status=200)
    responses.add(
        responses.POST,
        f"{_BASE}/v2/orders",
        body=json.dumps({"code": 42210000, "message": "insufficient buying power"}),
        status=422,
    )

    response = await _post(
        {"operator": "inkant", "legs": [{"symbol": "SPY", "side": "buy", "quantity": 1}]}
    )

    # The request succeeded (200); the leg failed with a typed what/why.
    assert response.status_code == 200
    [leg] = response.json()["results"]
    assert leg["status"] == "failed"
    assert leg["order"] is None
    assert "insufficient buying power" in leg["error"]["message"]
    assert leg["error"]["why"] == "HTTP 422"
    assert leg["order_ref"].startswith("manual/inkant/v1:")


async def test_missing_leg_is_rejected_at_boundary(_alpaca_clerk: None) -> None:
    response = await _post({"operator": "inkant", "legs": []})

    assert response.status_code == 422


async def test_operator_with_space_is_rejected_at_boundary_not_500(
    _alpaca_clerk: None,
) -> None:
    # A bad operator (space) becomes an invalid manual-namespace path segment.
    # It must be rejected as a Pydantic 422 at the endpoint boundary, never
    # allowed to reach the identity validator and surface as a raw 500.
    response = await _post(
        {
            "operator": "bad operator",
            "legs": [{"symbol": "SPY", "side": "buy", "quantity": 1}],
        }
    )

    assert response.status_code == 422


async def test_unconfigured_clerk_returns_503() -> None:
    reset_alpaca_clerk_for_testing()

    response = await _post(
        {"operator": "inkant", "legs": [{"symbol": "SPY", "side": "buy", "quantity": 1}]}
    )

    assert response.status_code == 503
    assert "not configured" in response.json()["detail"]["message"]
