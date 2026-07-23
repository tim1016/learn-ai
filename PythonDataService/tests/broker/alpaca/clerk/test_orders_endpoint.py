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
_ORDER_ID = "11111111-1111-4111-8111-111111111111"
_FOREIGN_ORDER_ID = "22222222-2222-4222-8222-222222222222"
_REJECTED_ORDER_ID = "33333333-3333-4333-8333-333333333333"

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


def _accepted_order_body(payload: dict) -> str:
    """Echo the submitted body back as an accepted order (the Alpaca shape).

    Reflects the vendor fields the caller sent (``type``, ``time_in_force``,
    ``limit_price``) so a limit submit round-trips its own attributes.
    """
    return json.dumps(
        {
            "id": _ORDER_ID,
            "client_order_id": payload["client_order_id"],
            "symbol": payload["symbol"],
            "asset_class": "us_equity",
            "side": payload["side"],
            "order_type": payload["type"],
            "type": payload["type"],
            "time_in_force": payload["time_in_force"],
            "qty": payload["qty"],
            "filled_qty": "0",
            "limit_price": payload.get("limit_price"),
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


async def _delete(order_id: str) -> Response:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.delete(
            f"/api/brokers/alpaca/orders/{order_id}", headers=_control_headers()
        )


@responses.activate
async def test_accepted_leg_returns_acked_result(_alpaca_clerk: None) -> None:
    responses.add(responses.GET, f"{_BASE}/v2/account", body=_ACCOUNT_BODY, status=200)

    def _order_callback(request):
        payload = json.loads(request.body)
        return (200, {}, _accepted_order_body(payload))

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
async def test_accepted_limit_gtc_leg_sends_price_and_tif(_alpaca_clerk: None) -> None:
    responses.add(responses.GET, f"{_BASE}/v2/account", body=_ACCOUNT_BODY, status=200)

    sent: list[dict] = []

    def _order_callback(request):
        payload = json.loads(request.body)
        sent.append(payload)
        return (200, {}, _accepted_order_body(payload))

    responses.add_callback(
        responses.POST, f"{_BASE}/v2/orders", callback=_order_callback
    )

    response = await _post(
        {
            "operator": "inkant",
            "legs": [
                {
                    "symbol": "SPY",
                    "side": "sell",
                    "quantity": 2,
                    "order_type": "limit",
                    "limit_price": 999.99,
                    "time_in_force": "gtc",
                }
            ],
        }
    )

    assert response.status_code == 200
    [leg] = response.json()["results"]
    assert leg["status"] == "acked"
    assert leg["order"]["order_type"] == "limit"
    assert leg["order"]["limit_price"] == 999.99
    assert leg["order"]["time_in_force"] == "gtc"
    # The wire body carried the vendor-shaped price + tif.
    assert sent[0]["type"] == "limit"
    assert sent[0]["limit_price"] == "999.99"
    assert sent[0]["time_in_force"] == "gtc"


async def test_limit_leg_without_price_is_rejected_at_boundary(_alpaca_clerk: None) -> None:
    # An inconsistent leg (limit order, no limit_price) is a Pydantic 422 at the
    # transport boundary — never a 500, never reaches the broker.
    response = await _post(
        {
            "operator": "inkant",
            "legs": [
                {"symbol": "SPY", "side": "buy", "quantity": 1, "order_type": "limit"}
            ],
        }
    )

    assert response.status_code == 422


async def test_market_leg_with_price_is_rejected_at_boundary(_alpaca_clerk: None) -> None:
    response = await _post(
        {
            "operator": "inkant",
            "legs": [
                {
                    "symbol": "SPY",
                    "side": "buy",
                    "quantity": 1,
                    "order_type": "market",
                    "limit_price": 100.0,
                }
            ],
        }
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    "leg",
    [
        {
            "symbol": "SPY",
            "side": "buy",
            "quantity": 1,
            "order_type": "limit",
            "limit_price": 240.555,
        },
        {
            "symbol": "SPY",
            "side": "buy",
            "quantity": 0.5,
            "order_type": "limit",
            "limit_price": 240.5,
            "time_in_force": "gtc",
        },
    ],
)
async def test_unsupported_alpaca_order_leg_is_rejected_at_boundary(
    _alpaca_clerk: None,
    leg: dict[str, object],
) -> None:
    response = await _post({"operator": "inkant", "legs": [leg]})

    assert response.status_code == 422


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


@pytest.mark.parametrize("symbol", ["BTC/USD", "AAPL240621C00200000"])
async def test_non_equity_symbol_is_rejected_at_boundary(
    _alpaca_clerk: None, symbol: str
) -> None:
    response = await _post(
        {
            "operator": "inkant",
            "legs": [{"symbol": symbol, "side": "buy", "quantity": 1}],
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


# ── S3 cancel path (DELETE /api/brokers/alpaca/orders/{order_id}) ────────────


async def _submit_one() -> str:
    """Submit one accepted leg and return its broker-assigned order id."""

    def _order_callback(request):
        payload = json.loads(request.body)
        return (200, {}, _accepted_order_body(payload))

    responses.add_callback(
        responses.POST, f"{_BASE}/v2/orders", callback=_order_callback
    )
    response = await _post(
        {"operator": "inkant", "legs": [{"symbol": "SPY", "side": "buy", "quantity": 1}]}
    )
    assert response.status_code == 200
    return response.json()["results"][0]["order"]["order_id"]


@responses.activate
async def test_cancel_owned_order_returns_acked(_alpaca_clerk: None) -> None:
    responses.add(responses.GET, f"{_BASE}/v2/account", body=_ACCOUNT_BODY, status=200)
    order_id = await _submit_one()
    # Alpaca returns 204 (no body) on a successful cancel.
    responses.add(
        responses.DELETE, f"{_BASE}/v2/orders/{order_id}", body="", status=204
    )

    response = await _delete(order_id)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "acked"
    assert body["owned"] is True
    assert body["order_id"] == order_id
    assert body["order_ref"].startswith("manual/inkant/v1:")
    assert body["error"] is None


@responses.activate
async def test_cancel_unowned_order_still_acks_honestly(_alpaca_clerk: None) -> None:
    responses.add(responses.GET, f"{_BASE}/v2/account", body=_ACCOUNT_BODY, status=200)
    # A foreign order id never submitted through this clerk.
    responses.add(
        responses.DELETE, f"{_BASE}/v2/orders/{_FOREIGN_ORDER_ID}", body="", status=204
    )

    response = await _delete(_FOREIGN_ORDER_ID)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "acked"
    assert body["owned"] is False
    assert body["order_ref"] is None


@responses.activate
async def test_cancel_non_cancelable_order_surfaces_what_why_not_500(
    _alpaca_clerk: None,
) -> None:
    responses.add(responses.GET, f"{_BASE}/v2/account", body=_ACCOUNT_BODY, status=200)
    responses.add(
        responses.DELETE,
        f"{_BASE}/v2/orders/{_REJECTED_ORDER_ID}",
        body=json.dumps({"code": 42210000, "message": "order is not cancelable"}),
        status=422,
    )

    response = await _delete(_REJECTED_ORDER_ID)

    # The request succeeded (200); the cancel failed with a typed what/why.
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert body["owned"] is False
    assert "not cancelable" in body["error"]["message"]
    assert body["error"]["why"] == "HTTP 422"


async def test_cancel_unconfigured_clerk_returns_503() -> None:
    reset_alpaca_clerk_for_testing()

    response = await _delete(_REJECTED_ORDER_ID)

    assert response.status_code == 503
    assert "not configured" in response.json()["detail"]["message"]


async def test_cancel_invalid_order_id_is_rejected_at_boundary(_alpaca_clerk: None) -> None:
    response = await _delete("not-a-uuid")

    assert response.status_code == 422
