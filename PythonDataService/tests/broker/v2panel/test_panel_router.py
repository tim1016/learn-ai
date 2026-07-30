"""HTTP seam tests for the broker-v2 panel router (S1).

Drives the account-scoped endpoints through the FastAPI HTTP surface with
journal fixtures on disk: panel-profile, catalog, panel, presented-action
execution (revision 409 + idempotency), and chart history preset validation.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from app.broker.alpaca.clerk import reset_alpaca_clerk_for_testing, set_alpaca_clerk
from app.broker.alpaca.clerk.journal import (
    OrderJournal,
    get_clerk_settings,
    reset_clerk_settings_for_testing,
)
from app.broker.alpaca.clerk.models import (
    ClerkStatus,
    HoldState,
    ReconciliationSummary,
)
from app.broker.contract.registry import (
    get_broker_registry,
    reset_broker_registry_for_testing,
)
from app.marketdata.feed import MarketDataBar
from app.routers.broker_v2_panel import router
from app.services.bot_runner import BotTaskRegistry, set_bot_task_registry
from app.services.broker_v2_panel.action_execution_service import (
    reset_idempotency_store_for_testing,
)
from tests.broker.v2panel.fixtures import (
    ACCT,
    SID,
    fill_entry,
    intent_entry,
    submit_acked_entry,
)

_ACCOUNT_ID = ACCT


class _FakeAccount:
    account_id = _ACCOUNT_ID


class _FakeReadPort:
    broker_id = "alpaca"

    async def get_account(self):
        return _FakeAccount()

    def capabilities(self):  # pragma: no cover - registry shape only
        raise NotImplementedError


_T0 = 1_700_000_000_000


class _HoldFeed:
    feed_id = "fake"

    async def stream_bars(self, symbol: str, *, use_rth: bool = True):
        yield MarketDataBar(
            symbol=symbol,
            start_ms=_T0,
            end_ms=_T0 + 60_000,
            open=Decimal("400"),
            high=Decimal("401"),
            low=Decimal("399"),
            close=Decimal("400.5"),
            volume=100,
            fetched_at_ms=_T0 + 500,
            feed_id="fake",
            session_phase="RTH",
        )
        await asyncio.Event().wait()

    def health(self):  # pragma: no cover
        raise NotImplementedError


class _FakeClerk:
    """Minimal clerk exposing only the panel/action read+control seams."""

    def __init__(self) -> None:
        self.reconciled = False
        self.cleared = False

    async def status(self) -> ClerkStatus:
        return ClerkStatus(
            broker="alpaca",
            account_id=_ACCOUNT_ID,
            hold=HoldState(active=False, reason_code=None, reason=None, since_ms=None),
            latest_reconciliation=ReconciliationSummary(
                verdict="clean", recorded_at_ms=1_000
            ),
            outstanding_intents=0,
            observed_at_ms=2_000,
            channel_healths=None,
        )

    async def reconcile_once(self) -> str:
        self.reconciled = True
        return "clean"

    async def clear_hold(self, *, operator: str, reason: str) -> ClerkStatus:
        self.cleared = True
        return await self.status()


@pytest.fixture
def api(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ALPACA_CLERK_DIR", str(tmp_path / "alpaca_clerk"))
    reset_clerk_settings_for_testing()
    reset_broker_registry_for_testing()
    reset_alpaca_clerk_for_testing()
    reset_idempotency_store_for_testing()
    get_broker_registry().register(_FakeReadPort())
    clerk = _FakeClerk()
    set_alpaca_clerk(clerk)

    # Deploy one bot so the roster is non-empty.
    registry = BotTaskRegistry(
        tmp_path / "live_state_root",
        feed_resolver=lambda: _HoldFeed(),
        boot_recovery_required=False,
    )
    set_bot_task_registry(registry)

    app = FastAPI()
    app.include_router(router)
    try:
        yield app, clerk, registry
    finally:
        set_bot_task_registry(None)
        set_alpaca_clerk(None)
        reset_broker_registry_for_testing()
        reset_clerk_settings_for_testing()
        reset_idempotency_store_for_testing()


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _write_journal(entries) -> None:
    journal = OrderJournal(account_id=_ACCOUNT_ID, root=get_clerk_settings().dir)
    for entry in entries:
        journal.append(entry)


async def _deploy_bot(registry: BotTaskRegistry) -> None:
    await registry.deploy(
        broker="alpaca", strategy_instance_id=SID, symbol="SPY", use_rth=True
    )


# ── §4 panel-profile ─────────────────────────────────────────────────────────


async def test_panel_profile_endpoint(api) -> None:
    app, _clerk, _registry = api
    async with _client(app) as client:
        response = await client.get("/api/brokers/alpaca/panel-profile")
    assert response.status_code == 200
    body = response.json()
    assert body["broker"] == "alpaca"
    assert body["fee_fidelity"] == "none"
    assert len(body["stations"]) == 6


async def test_panel_profile_unknown_broker_is_404(api) -> None:
    app, _clerk, _registry = api
    async with _client(app) as client:
        response = await client.get("/api/brokers/ibkr/panel-profile")
    assert response.status_code == 404


# ── §5 catalog (account-scoped) ──────────────────────────────────────────────


async def test_catalog_scoped_returns_roster(api) -> None:
    app, _clerk, registry = api
    await _deploy_bot(registry)
    _write_journal([fill_entry(sid=SID, intent="i1", ts_ms=1_000, qty=100, price=500.0)])

    async with _client(app) as client:
        response = await client.get(
            f"/api/brokers/alpaca/accounts/{_ACCOUNT_ID}/bots/catalog"
        )
    assert response.status_code == 200
    rows = response.json()
    assert [r["strategy_instance_id"] for r in rows] == [SID]
    assert rows[0]["exposure"] == {"SPY": 100.0}
    assert rows[0]["desired_state"] in ("RUNNING", "STOPPED")


async def test_catalog_account_mismatch_is_404(api) -> None:
    app, _clerk, registry = api
    await _deploy_bot(registry)
    async with _client(app) as client:
        response = await client.get(
            "/api/brokers/alpaca/accounts/WRONG-ACCT/bots/catalog"
        )
    assert response.status_code == 404


async def test_catalog_unscoped_alias_still_works(api) -> None:
    app, _clerk, registry = api
    await _deploy_bot(registry)
    async with _client(app) as client:
        response = await client.get("/api/brokers/alpaca/bots/catalog")
    assert response.status_code == 200


# ── §7 panel ─────────────────────────────────────────────────────────────────


async def test_panel_scoped_never_emits_paused(api) -> None:
    app, _clerk, registry = api
    await _deploy_bot(registry)
    _write_journal(
        [
            intent_entry(sid=SID, intent="i1", ts_ms=1_000),
            submit_acked_entry(sid=SID, intent="i1", ts_ms=1_100),
        ]
    )
    async with _client(app) as client:
        response = await client.get(
            f"/api/brokers/alpaca/accounts/{_ACCOUNT_ID}/bots/{SID}/panel"
        )
    assert response.status_code == 200
    body = response.json()
    assert body["health"]["desired_state"] in ("RUNNING", "STOPPED")
    assert body["health"]["desired_state"] != "PAUSED"
    assert len(body["rail"]["stations"]) == 6
    assert "revision" in body


# ── §11 presented-action execution ───────────────────────────────────────────


async def test_action_reconcile_now_applies(api) -> None:
    app, clerk, registry = api
    await _deploy_bot(registry)
    async with _client(app) as client:
        panel = await client.get(
            f"/api/brokers/alpaca/accounts/{_ACCOUNT_ID}/bots/{SID}/panel"
        )
        revision = panel.json()["revision"]
        response = await client.post(
            f"/api/brokers/alpaca/accounts/{_ACCOUNT_ID}/bots/{SID}/actions",
            json={
                "action_id": "reconcile_now",
                "revision": revision,
                "idempotency_key": "k1",
            },
        )
    assert response.status_code == 200
    assert response.json()["applied"] is True
    assert clerk.reconciled is True


async def test_action_stale_revision_is_409(api) -> None:
    app, _clerk, registry = api
    await _deploy_bot(registry)
    async with _client(app) as client:
        response = await client.post(
            f"/api/brokers/alpaca/accounts/{_ACCOUNT_ID}/bots/{SID}/actions",
            json={
                "action_id": "reconcile_now",
                "revision": 999_999_999,
                "idempotency_key": "k1",
            },
        )
    assert response.status_code == 409


async def test_action_idempotent_repost_is_noop(api) -> None:
    app, _clerk, registry = api
    await _deploy_bot(registry)
    async with _client(app) as client:
        panel = await client.get(
            f"/api/brokers/alpaca/accounts/{_ACCOUNT_ID}/bots/{SID}/panel"
        )
        revision = panel.json()["revision"]
        body = {
            "action_id": "reconcile_now",
            "revision": revision,
            "idempotency_key": "dup",
        }
        first = await client.post(
            f"/api/brokers/alpaca/accounts/{_ACCOUNT_ID}/bots/{SID}/actions", json=body
        )
        second = await client.post(
            f"/api/brokers/alpaca/accounts/{_ACCOUNT_ID}/bots/{SID}/actions", json=body
        )
    assert first.json()["applied"] is True
    assert second.json()["applied"] is False


async def test_action_identity_is_not_a_request_field(api) -> None:
    """The request schema forbids an operator/identity field (extra='forbid')."""
    app, _clerk, registry = api
    await _deploy_bot(registry)
    async with _client(app) as client:
        response = await client.post(
            f"/api/brokers/alpaca/accounts/{_ACCOUNT_ID}/bots/{SID}/actions",
            json={
                "action_id": "reconcile_now",
                "revision": 0,
                "idempotency_key": "k",
                "operator": "smuggled-identity",
            },
        )
    # Pydantic rejects the extra field at the boundary (422).
    assert response.status_code == 422


# ── §8 chart history preset validation ───────────────────────────────────────


async def test_history_unknown_preset_is_422(api) -> None:
    app, _clerk, registry = api
    await _deploy_bot(registry)
    async with _client(app) as client:
        response = await client.get(
            f"/api/brokers/alpaca/accounts/{_ACCOUNT_ID}/bots/{SID}/chart/history",
            params={"preset": "7D"},
        )
    assert response.status_code == 422
