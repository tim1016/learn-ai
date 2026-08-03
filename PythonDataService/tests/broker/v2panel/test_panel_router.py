"""HTTP seam tests for the broker-v2 panel router (S1).

Drives the account-scoped endpoints through the FastAPI HTTP surface with
journal fixtures on disk: panel-profile, catalog, panel, presented-action
execution (revision 409 + idempotency), and chart history preset validation.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
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
    AccountFreezeState,
    ClerkCustodySnapshot,
    ClerkStatus,
    CustodyCountFact,
    CustodyExposureFact,
    HoldState,
    ReconciliationSummary,
)
from app.broker.contract.registry import (
    get_broker_registry,
    reset_broker_registry_for_testing,
)
from app.marketdata.feed import FeedHealth, MarketDataBar
from app.routers import broker_v2_panel
from app.routers.broker_v2_panel import router
from app.schemas.broker_v2_panel import BotPanelLiveSnapshot, ChartLiveResponse
from app.services.bot_runner import BotTaskRegistry, set_bot_task_registry
from app.services.broker_v2_panel import panel_data_source
from app.services.broker_v2_panel.action_execution_service import (
    reset_idempotency_store_for_testing,
)
from app.utils.timestamps import now_ms_utc
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

    def health(self) -> FeedHealth:
        observed_at_ms = now_ms_utc()
        return FeedHealth(
            connected=True,
            stale=False,
            last_bar_ms=_T0,
            reason="",
            active_subscription_count=0,
            observed_at_ms=observed_at_ms,
        )


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

    async def custody_snapshot(self, strategy_instance_id: str) -> ClerkCustodySnapshot:
        observed_at_ms = now_ms_utc()
        return ClerkCustodySnapshot(
            broker="alpaca",
            account_id=_ACCOUNT_ID,
            strategy_instance_id=strategy_instance_id,
            clerk_generation="clerk-test-generation",
            journal_sequence=7,
            reconciliation_state="clean",
            reconciliation_fresh=True,
            reconciled_at_ms=observed_at_ms,
            exposure=CustodyExposureFact(state="zero", positions={}),
            working_orders=CustodyCountFact(state="zero", count=0),
            pending_orders=CustodyCountFact(state="zero", count=0),
            terminal_orders=CustodyCountFact(state="zero", count=0),
            unresolved_effects=CustodyCountFact(state="zero", count=0),
            hold=HoldState(active=False),
            freeze=AccountFreezeState(),
            reason_code="CLERK_CUSTODY_PROVEN",
            evidence_refs=("alpaca-clerk-journal:PA-TEST:7",),
            observed_at_ms=observed_at_ms,
        )

    @asynccontextmanager
    async def start_admission_snapshot(self, strategy_instance_id: str):
        yield await self.custody_snapshot(strategy_instance_id)

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


async def test_authority_facts_endpoint_keeps_process_and_custody_separate(api) -> None:
    app, _clerk, registry = api
    await _deploy_bot(registry)

    async with _client(app) as client:
        response = await client.get(
            f"/api/brokers/alpaca/accounts/{_ACCOUNT_ID}/bots/{SID}/authority-facts"
        )

    assert response.status_code == 200
    body = response.json()
    assert body["process"]["strategy_instance_id"] == SID
    assert body["process"]["state"] == "RUNNING"
    assert body["clerk"]["strategy_instance_id"] == SID
    assert body["clerk"]["exposure"] == {"state": "zero", "positions": {}}
    assert body["clerk"]["reason_code"] == "CLERK_CUSTODY_PROVEN"

    await registry.stop("alpaca", SID)


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
    assert rows[0]["row_action"]["action_id"] == "stop"
    assert rows[0]["row_action"]["enabled"] is True


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


async def test_live_snapshot_bootstrap_and_sse_share_one_versioned_document(
    api,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _clerk, registry = api
    await _deploy_bot(registry)
    panel = await panel_data_source.get_panel("alpaca", _ACCOUNT_ID, SID)
    chart = ChartLiveResponse(
        strategy_instance_id=SID,
        symbol="SPY",
        trading_date_open_ms=_T0,
        trading_date_close_ms=_T0 + 60_000,
        resolution="5s",
        bars=[],
        fill_markers=[],
        overlay_notices=[],
        as_of_ms=_T0,
    )
    snapshot = BotPanelLiveSnapshot(
        stream_epoch="epoch-new",
        surface_version=7,
        panel=panel,
        live_chart=chart,
    )

    class _Hub:
        async def snapshot(self) -> BotPanelLiveSnapshot:
            return snapshot

        def subscribe(self):
            queue: asyncio.Queue[BotPanelLiveSnapshot | None] = asyncio.Queue(maxsize=1)
            queue.put_nowait(snapshot)
            return queue

        def unsubscribe(self, _queue) -> None:
            return None

    async def get_hub(*_args, **_kwargs):
        return _Hub()

    monkeypatch.setattr(broker_v2_panel, "get_or_start_live_projection_hub", get_hub)
    async with _client(app) as client:
        bootstrap = await client.get(
            f"/api/brokers/alpaca/accounts/{_ACCOUNT_ID}/bots/{SID}/live-snapshot",
            params={"resolution": "5s"},
        )

    assert bootstrap.status_code == 200
    assert bootstrap.json()["surface_version"] == 7
    assert bootstrap.json()["panel"]["market_pulse"]["headline"]

    response = await broker_v2_panel.stream_live_snapshot_scoped(
        "alpaca",
        _ACCOUNT_ID,
        SID,
        resolution="5s",
        cursor="epoch-old:99",
    )
    iterator = response.body_iterator
    assert "event: reset" in await anext(iterator)
    event = await anext(iterator)
    assert "id: epoch-new:7" in event
    assert "event: snapshot" in event
    await iterator.aclose()


# ── §11 presented-action execution ───────────────────────────────────────────


async def test_action_reconcile_now_applies(api) -> None:
    app, clerk, registry = api
    await _deploy_bot(registry)
    async with _client(app) as client:
        panel = await client.get(
            f"/api/brokers/alpaca/accounts/{_ACCOUNT_ID}/bots/{SID}/panel"
        )
        action = next(item for item in panel.json()["actions"] if item["action_id"] == "reconcile_now")
        response = await client.post(
            f"/api/brokers/alpaca/accounts/{_ACCOUNT_ID}/bots/{SID}/actions",
            json={
                "action_id": "reconcile_now",
                "revision": panel.json()["revision"],
                "concurrency_token": action["concurrency_token"],
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
                "concurrency_token": "stale-token",
                "idempotency_key": "k1",
            },
        )
    assert response.status_code == 409
    assert response.json()["detail"]["receipt_id"] is None


async def test_action_idempotent_repost_is_noop(api) -> None:
    app, _clerk, registry = api
    await _deploy_bot(registry)
    async with _client(app) as client:
        panel = await client.get(
            f"/api/brokers/alpaca/accounts/{_ACCOUNT_ID}/bots/{SID}/panel"
        )
        action = next(item for item in panel.json()["actions"] if item["action_id"] == "reconcile_now")
        body = {
            "action_id": "reconcile_now",
            "revision": panel.json()["revision"],
            "concurrency_token": action["concurrency_token"],
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


async def test_completed_stop_replay_survives_current_disabled_state(api) -> None:
    app, _clerk, registry = api
    await _deploy_bot(registry)
    async with _client(app) as client:
        panel = await client.get(
            f"/api/brokers/alpaca/accounts/{_ACCOUNT_ID}/bots/{SID}/panel"
        )
        action = next(
            item
            for item in panel.json()["actions"]
            if item["action_id"] == "stop"
        )
        body = {
            "action_id": "stop",
            "revision": panel.json()["revision"],
            "concurrency_token": action["concurrency_token"],
            "idempotency_key": "stop-replay",
        }
        first = await client.post(
            f"/api/brokers/alpaca/accounts/{_ACCOUNT_ID}/bots/{SID}/actions",
            json=body,
        )
        second = await client.post(
            f"/api/brokers/alpaca/accounts/{_ACCOUNT_ID}/bots/{SID}/actions",
            json=body,
        )

    assert first.status_code == 200
    assert first.json()["applied"] is True
    assert second.status_code == 200
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


# ── §8 chart interval and history preset validation ─────────────────────────


async def test_live_chart_accepts_five_second_resolution(
    api: tuple[FastAPI, _FakeClerk, BotTaskRegistry],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _clerk, registry = api
    await _deploy_bot(registry)
    observed: list[str] = []

    async def live_chart(
        broker: str,
        account_id: str,
        sid: str,
        *,
        resolution: str,
    ) -> dict[str, object]:
        observed.append(resolution)
        return {
            "strategy_instance_id": sid,
            "symbol": "SPY",
            "trading_date_open_ms": _T0,
            "trading_date_close_ms": _T0 + 60_000,
            "resolution": resolution,
            "bars": [],
            "fill_markers": [],
            "overlay_notices": [],
            "as_of_ms": _T0,
        }

    monkeypatch.setattr(
        "app.routers.broker_v2_panel.ds.get_live_chart",
        live_chart,
    )
    async with _client(app) as client:
        response = await client.get(
            f"/api/brokers/alpaca/accounts/{_ACCOUNT_ID}/bots/{SID}/chart/live",
            params={"resolution": "5s"},
        )

    assert response.status_code == 200
    assert response.json()["resolution"] == "5s"
    assert observed == ["5s"]


async def test_live_chart_rejects_unknown_resolution(
    api: tuple[FastAPI, _FakeClerk, BotTaskRegistry],
) -> None:
    app, _clerk, registry = api
    await _deploy_bot(registry)
    async with _client(app) as client:
        response = await client.get(
            f"/api/brokers/alpaca/accounts/{_ACCOUNT_ID}/bots/{SID}/chart/live",
            params={"resolution": "15s"},
        )

    assert response.status_code == 422


async def test_history_unknown_preset_is_422(api) -> None:
    app, _clerk, registry = api
    await _deploy_bot(registry)
    async with _client(app) as client:
        response = await client.get(
            f"/api/brokers/alpaca/accounts/{_ACCOUNT_ID}/bots/{SID}/chart/history",
            params={"preset": "7D"},
        )
    assert response.status_code == 422
