"""``GET /api/brokers/alpaca/live-verdict`` is transport over the pure composer."""

from __future__ import annotations

from pathlib import Path
from typing import NoReturn

import pytest
from httpx import ASGITransport, AsyncClient

from app.broker.alpaca.clerk.active_authority import (
    ActiveClerkRuntime,
    ClerkStartupFailure,
    set_active_clerk_runtime,
)
from app.broker.alpaca.clerk.shadow_receipt import (
    ShadowReceipt,
    ShadowReceiptSession,
    ShadowReceiptStore,
)
from app.broker.alpaca.config import reset_alpaca_settings_for_testing
from app.broker.contract.errors import BrokerAccountModeDisagreement
from app.broker.contract.registry import (
    get_broker_registry,
    reset_broker_registry_for_testing,
)
from app.config import settings
from app.main import app
from app.security.data_plane_control import CONTROL_SECRET_HEADER


def _headers() -> dict[str, str]:
    secret = settings.DATA_PLANE_CONTROL_SECRET.strip()
    return {CONTROL_SECRET_HEADER: secret} if secret else {}


@pytest.fixture(autouse=True)
def _reset() -> None:
    reset_alpaca_settings_for_testing()
    set_active_clerk_runtime(None)
    yield
    set_active_clerk_runtime(None)
    reset_alpaca_settings_for_testing()


@pytest.fixture(autouse=True)
def _clean_registry() -> None:
    reset_broker_registry_for_testing()
    yield
    reset_broker_registry_for_testing()


async def test_paper_settings_serve_a_paper_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPACA_API_KEY_ID", "k")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "s")
    monkeypatch.setenv("ALPACA_MODE", "paper")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/brokers/alpaca/live-verdict", headers=_headers())

    assert response.status_code == 200
    body = response.json()
    assert body["configured_mode"] == "paper"
    assert body["final_verdict"] == "paper"
    assert body["headline"]


async def test_live_settings_with_refused_clerk_serve_live_unarmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in {
        "ALPACA_API_KEY_ID": "k", "ALPACA_API_SECRET_KEY": "s", "ALPACA_MODE": "live",
        "ALPACA_LIVE_LOSS_FRACTION": "0.02", "ALPACA_LIVE_LOSS_USD": "500",
        "ALPACA_LIVE_SHADOW_SESSIONS": "5", "ALPACA_LIVE_ARMING_MAX_SESSIONS": "20",
        "ALPACA_LIVE_XH_ENTRY_BPS": "10", "ALPACA_LIVE_XH_EXIT_BPS": "10",
    }.items():
        monkeypatch.setenv(name, value)
    set_active_clerk_runtime(
        ActiveClerkRuntime(
            authority_kind="unavailable",
            account_id="9LIVE0001",
            startup_failure=ClerkStartupFailure(
                reason_code="LIVE_ACCOUNT_REFUSED", account_id="9LIVE0001",
                scope="ACCOUNT_CLERK", impact="x", recovery="y", observed_at_ms=1,
            ),
        )
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/brokers/alpaca/live-verdict", headers=_headers())

    assert response.status_code == 200
    body = response.json()
    assert body["final_verdict"] == "live-unarmed"
    assert body["observed_account_id"] == "9LIVE0001"
    assert body["armed_instance_count"] == 0


async def test_shadow_authority_with_a_receipt_serves_shadow_complete(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for name, value in {
        "ALPACA_API_KEY_ID": "k", "ALPACA_API_SECRET_KEY": "s", "ALPACA_MODE": "live",
        "ALPACA_CLERK_DIR": str(tmp_path),
        "ALPACA_LIVE_LOSS_FRACTION": "0.02", "ALPACA_LIVE_LOSS_USD": "500",
        "ALPACA_LIVE_SHADOW_SESSIONS": "5", "ALPACA_LIVE_ARMING_MAX_SESSIONS": "20",
        "ALPACA_LIVE_XH_ENTRY_BPS": "10", "ALPACA_LIVE_XH_EXIT_BPS": "10",
    }.items():
        monkeypatch.setenv(name, value)
    ShadowReceiptStore(tmp_path).append(
        ShadowReceipt.create(
            live_account_id="9LIVE0001",
            strategy_instance_id="ema-shadow-1",
            configured_signal_hash="a" * 64,
            twin_account_id="PA-TEST",
            twin_strategy_instance_id="ema-paper-1",
            required_sessions=1,
            sessions=(
                ShadowReceiptSession(session_open_ms=1_000, shadow_run_id="run-1", reconciliation_sha256="b" * 64),
            ),
            written_at_ms=1_700_000_000_000,
        )
    )
    set_active_clerk_runtime(
        ActiveClerkRuntime(
            authority_kind="shadow", account_id="shadow:9LIVE0001", account_authority_kind="shadow"
        )
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/brokers/alpaca/live-verdict", headers=_headers())

    assert response.status_code == 200
    body = response.json()
    assert body["clerk_authority"] == "shadow"
    assert body["shadow_state"] == "complete"


async def test_invalid_settings_serve_unknown_not_500(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPACA_API_KEY_ID", "k")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "s")
    monkeypatch.setenv("ALPACA_MODE", "live")  # no ALPACA_LIVE_* → invalid

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/brokers/alpaca/live-verdict", headers=_headers())

    assert response.status_code == 200
    assert response.json()["configured_mode"] == "unconfigured"


async def test_other_brokers_are_404() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/brokers/ibkr/live-verdict", headers=_headers())

    assert response.status_code == 404


class _ModeDisagreementReadPort:
    """A minimal fake read port whose account read always disagrees on mode."""

    broker_id = "alpaca"

    async def get_account(self) -> NoReturn:
        raise BrokerAccountModeDisagreement(
            "The configured Alpaca mode and the observed account disagree.",
            broker="alpaca",
            detail="ALPACA_MODE='live' but the account number begins with 'PA'",
        )


async def test_account_route_names_a_mode_disagreement() -> None:
    get_broker_registry().register(_ModeDisagreementReadPort())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/brokers/alpaca/account", headers=_headers())

    assert response.status_code == 409
    assert response.json()["detail"]["reason_code"] == "LIVE_MODE_DISAGREEMENT"
