"""HTTP endpoint seam tests for the S6 clerk status + clear-hold routes (#1197).

Exercises the full path over ASGITransport: router → Clerk → real
``AlpacaBroker`` → real ``AlpacaTradingClient`` → ``responses``-mocked Alpaca
REST (alpaca-py drives ``requests``, so ``responses`` is the mock). Asserts:

- GET /clerk/status shape (hold + latest verdict + outstanding intents).
- A submit is refused with 409 + reason_code UNEXPLAINED_ORDER_HOLD while held.
- POST /clerk/clear-hold restores submission and returns the updated status.
- DELETE /orders (cancel) is still allowed under hold.

The hold is raised through the Clerk's own reconciliation path (a foreign order
at Alpaca), so nothing about the state is fabricated by the test.
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
    get_alpaca_clerk,
    reset_alpaca_clerk_for_testing,
    set_alpaca_clerk,
)
from app.broker.alpaca.client import AlpacaTradingClient
from app.broker.alpaca.config import AlpacaSettings, reset_alpaca_settings_for_testing
from app.config import settings
from app.main import app
from app.security.data_plane_control import CONTROL_SECRET_HEADER

_BASE = "https://paper-api.alpaca.markets"
_FOREIGN_ORDER_ID = "00000000-0000-4000-8000-000000000001"

_ACCOUNT_BODY = json.dumps(
    {
        "account_number": "PA-STATUS",
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


def _foreign_order_body() -> str:
    """One order at Alpaca whose client_order_id is NOT one of our namespaces."""
    return json.dumps(
        [
            {
                "id": _FOREIGN_ORDER_ID,
                "client_order_id": "someone-elses-order",
                "symbol": "SPY",
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
        ]
    )


@pytest.fixture
def _alpaca_clerk(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    monkeypatch.setenv("ALPACA_CLERK_DIR", str(tmp_path))
    journal_module.reset_clerk_settings_for_testing()
    reset_alpaca_settings_for_testing()
    alpaca_settings = AlpacaSettings(api_key_id="k", api_secret_key="s", mode="paper")
    broker = AlpacaBroker(AlpacaTradingClient(settings=alpaca_settings))
    set_alpaca_clerk(AlpacaClerk(read=broker, trade=broker))
    yield
    reset_alpaca_clerk_for_testing()
    journal_module.reset_clerk_settings_for_testing()
    reset_alpaca_settings_for_testing()


def _headers() -> dict[str, str]:
    secret = settings.DATA_PLANE_CONTROL_SECRET.strip()
    return {CONTROL_SECRET_HEADER: secret} if secret else {}


async def _get(path: str) -> Response:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.get(path, headers=_headers())


async def _post(path: str, body: dict) -> Response:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.post(path, json=body, headers=_headers())


async def _delete(path: str) -> Response:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.delete(path, headers=_headers())


async def _raise_hold_via_sweep() -> None:
    """Drive one reconciliation pass so a foreign order raises the hold."""
    clerk = get_alpaca_clerk()
    assert clerk is not None
    verdict = await clerk.reconcile_once()
    assert verdict == "unexplained_order"


@responses.activate
async def test_status_reports_clean_before_any_hold(_alpaca_clerk: None) -> None:
    responses.add(responses.GET, f"{_BASE}/v2/account", body=_ACCOUNT_BODY, status=200)

    response = await _get("/api/brokers/alpaca/clerk/status")

    assert response.status_code == 200
    body = response.json()
    assert body["broker"] == "alpaca"
    assert body["account_id"] == "PA-STATUS"
    assert body["hold"]["active"] is False
    assert body["outstanding_intents"] == 0


@responses.activate
async def test_status_reports_hold_after_unexplained_order(_alpaca_clerk: None) -> None:
    responses.add(responses.GET, f"{_BASE}/v2/account", body=_ACCOUNT_BODY, status=200)
    responses.add(
        responses.GET, f"{_BASE}/v2/orders", body=_foreign_order_body(), status=200
    )
    responses.add(responses.GET, f"{_BASE}/v2/positions", body="[]", status=200)
    await _raise_hold_via_sweep()

    response = await _get("/api/brokers/alpaca/clerk/status")

    assert response.status_code == 200
    body = response.json()
    assert body["hold"]["active"] is True
    assert body["hold"]["reason_code"] == "UNEXPLAINED_ORDER_HOLD"
    assert body["hold"]["reason"]
    assert body["latest_reconciliation"]["verdict"] == "unexplained_order"


@responses.activate
async def test_submit_returns_409_reason_code_while_held(_alpaca_clerk: None) -> None:
    responses.add(responses.GET, f"{_BASE}/v2/account", body=_ACCOUNT_BODY, status=200)
    responses.add(
        responses.GET, f"{_BASE}/v2/orders", body=_foreign_order_body(), status=200
    )
    responses.add(responses.GET, f"{_BASE}/v2/positions", body="[]", status=200)
    await _raise_hold_via_sweep()

    response = await _post(
        "/api/brokers/alpaca/orders",
        {"operator": "inkant", "legs": [{"symbol": "SPY", "side": "buy", "quantity": 1}]},
    )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["reason_code"] == "UNEXPLAINED_ORDER_HOLD"
    assert detail["message"]


@responses.activate
async def test_cancel_is_allowed_while_held(_alpaca_clerk: None) -> None:
    responses.add(responses.GET, f"{_BASE}/v2/account", body=_ACCOUNT_BODY, status=200)
    responses.add(
        responses.GET, f"{_BASE}/v2/orders", body=_foreign_order_body(), status=200
    )
    responses.add(responses.GET, f"{_BASE}/v2/positions", body="[]", status=200)
    await _raise_hold_via_sweep()
    responses.add(
        responses.DELETE, f"{_BASE}/v2/orders/{_FOREIGN_ORDER_ID}", body="", status=204
    )

    response = await _delete(f"/api/brokers/alpaca/orders/{_FOREIGN_ORDER_ID}")

    # Cancel reduces exposure and is never blocked by the hold.
    assert response.status_code == 200
    assert response.json()["status"] == "acked"


@responses.activate
async def test_clear_hold_restores_submission(_alpaca_clerk: None) -> None:
    responses.add(responses.GET, f"{_BASE}/v2/account", body=_ACCOUNT_BODY, status=200)
    responses.add(
        responses.GET, f"{_BASE}/v2/orders", body=_foreign_order_body(), status=200
    )
    responses.add(responses.GET, f"{_BASE}/v2/positions", body="[]", status=200)
    await _raise_hold_via_sweep()

    cleared = await _post(
        "/api/brokers/alpaca/clerk/clear-hold",
        {"operator": "ops", "reason": "Verified the account is safe."},
    )
    assert cleared.status_code == 200
    assert cleared.json()["hold"]["active"] is False

    # A subsequent submit lands (the foreign order no longer gates it).
    def _order_callback(request):
        payload = json.loads(request.body)
        return (
            200,
            {},
            json.dumps(
                {
                    "id": "broker-order-ok",
                    "client_order_id": payload["client_order_id"],
                    "symbol": payload["symbol"],
                    "asset_class": "us_equity",
                    "side": payload["side"],
                    "order_type": payload["type"],
                    "type": payload["type"],
                    "time_in_force": payload["time_in_force"],
                    "qty": payload["qty"],
                    "filled_qty": "0",
                    "status": "accepted",
                    "submitted_at": "2026-07-22T14:30:00Z",
                    "created_at": "2026-07-22T14:30:00Z",
                    "updated_at": "2026-07-22T14:30:00Z",
                }
            ),
        )

    responses.add_callback(
        responses.POST, f"{_BASE}/v2/orders", callback=_order_callback
    )
    submit = await _post(
        "/api/brokers/alpaca/orders",
        {"operator": "inkant", "legs": [{"symbol": "SPY", "side": "buy", "quantity": 1}]},
    )
    assert submit.status_code == 200
    assert submit.json()["results"][0]["status"] == "acked"


async def test_clear_hold_rejects_a_blank_audit_reason_at_the_boundary(
    _alpaca_clerk: None,
) -> None:
    response = await _post(
        "/api/brokers/alpaca/clerk/clear-hold",
        {"operator": "ops", "reason": "   "},
    )

    assert response.status_code == 422


async def test_status_unconfigured_clerk_returns_503() -> None:
    reset_alpaca_clerk_for_testing()

    response = await _get("/api/brokers/alpaca/clerk/status")

    assert response.status_code == 503
    assert "not configured" in response.json()["detail"]["message"]
