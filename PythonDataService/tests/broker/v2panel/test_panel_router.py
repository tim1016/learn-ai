"""HTTP seam coverage for the active SQLite Broker V2 panel."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from app.broker.alpaca.clerk.active_authority import (
    ActiveClerkRuntime,
    set_active_clerk_runtime,
)
from app.broker.alpaca.clerk.sqlite.commands import submit_start_run
from app.broker.alpaca.clerk.sqlite.enter import accept_enter
from app.broker.alpaca.clerk.sqlite.exit import accept_exit
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.contract.models import BrokerOrderLeg
from app.broker.contract.registry import (
    get_broker_registry,
    reset_broker_registry_for_testing,
)
from app.routers import broker_v2_panel
from app.routers.broker_v2_panel import router
from app.schemas.broker_bots import BotStatusView
from app.schemas.broker_v2_panel import BotPanelLiveSnapshot, ChartLiveResponse
from app.services.bot_runner import set_bot_task_registry
from app.services.broker_v2_panel.action_execution_service import (
    reset_idempotency_store_for_testing,
)
from tests.broker.v2panel.fixtures import ACCT, SID

_T0 = 1_700_000_000_000


class _FakeAccount:
    account_id = ACCT


class _FakeBrokerPort:
    broker_id = "alpaca"

    async def get_account(self) -> _FakeAccount:
        return _FakeAccount()

    async def list_positions(self) -> list:
        return []

    async def list_orders(self, **_kwargs) -> list:
        return []

    async def list_activities(self, **_kwargs) -> list:
        return []

    async def submit(self, *_args, **_kwargs):  # pragma: no cover - not used
        raise AssertionError("panel read tests must not submit broker orders")

    async def cancel(self, _order_id: str) -> None:  # pragma: no cover - not used
        raise AssertionError("panel read tests must not cancel broker orders")

    async def get_order_by_client_order_id(self, _client_order_id: str):
        return None

    def capabilities(self) -> None:  # pragma: no cover - registry shape only
        raise NotImplementedError


class _FakeRegistry:
    def status(self, broker: str, sid: str) -> BotStatusView:
        assert broker == "alpaca"
        assert sid == SID
        return BotStatusView(
            strategy_instance_id=SID,
            strategy_key="deployment_validation",
            strategy_label="Deployment Validation",
            broker="alpaca",
            symbol="SPY",
            mode="trade",
            quantity=1,
            running=True,
            phase="ON_DUTY",
            desired_state="RUNNING",
            active_run_id="run-1",
            duty_outcome=None,
            binding_created_at_ms=_T0,
            last_transition_at_ms=_T0,
        )

    def list_bots(self, broker: str) -> list[BotStatusView]:
        return [self.status(broker, SID)]

    def binding_for_control(self, broker: str, sid: str) -> SimpleNamespace:
        self.status(broker, sid)
        return SimpleNamespace(
            strategy_instance_id=sid,
            run_id="run-1",
            symbol="SPY",
            use_rth=True,
            strategy_key="deployment_validation",
            sealed_program=None,
        )

    def dry_run_activity(self, broker: str, sid: str) -> list:
        self.status(broker, sid)
        return []

    def bindings_for_broker(self, broker: str) -> list:
        """No durable dry-run bindings — the catalog is the plain SQLite roster."""
        return []


@pytest.fixture()
def api(tmp_path: Path):
    reset_broker_registry_for_testing()
    reset_idempotency_store_for_testing()
    set_active_clerk_runtime(None)
    set_bot_task_registry(_FakeRegistry())  # type: ignore[arg-type]
    port = _FakeBrokerPort()
    get_broker_registry().register(port)  # type: ignore[arg-type]
    repo = ClerkSqliteRepository.initialize(account_id=ACCT, artifacts_root=tmp_path)
    repo.register_strategy_instance(
        strategy_instance_id=SID,
        symbol="SPY",
        config_hash="config-1",
        strategy_key="deployment_validation",
        display_name="Deployment Validation",
        # Production always writes the full binding dump here; the roster now
        # reads the bot's declared mode/quantity/carryover from it rather than
        # assuming `trade`.
        config_json=json.dumps({"mode": "trade", "quantity": 1, "carryover_policy": "FORBID"}),
    )
    submit_start_run(
        repo,
        account_id=ACCT,
        strategy_instance_id=SID,
        lifecycle_run_id="run-1",
    )
    facade = SqliteAlpacaClerkFacade(
        repo=repo,
        read=port,  # type: ignore[arg-type]
        trade=port,  # type: ignore[arg-type]
    )
    set_active_clerk_runtime(ActiveClerkRuntime(authority_kind="sqlite", clerk=facade))
    app = FastAPI()
    app.include_router(router)
    try:
        yield app, repo
    finally:
        set_active_clerk_runtime(None)
        set_bot_task_registry(None)
        repo.close()
        reset_broker_registry_for_testing()
        reset_idempotency_store_for_testing()


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_panel_profile_endpoint(api) -> None:
    app, _repo = api
    async with _client(app) as client:
        response = await client.get("/api/brokers/alpaca/panel-profile")

    assert response.status_code == 200, response.text
    assert response.json()["broker"] == "alpaca"
    assert len(response.json()["stations"]) == 6
    assert "clear_hold" not in response.json()["supported_action_ids"]
    assert "record_inventory_baseline" not in response.json()["supported_action_ids"]


async def test_catalog_scoped_returns_sqlite_roster(api) -> None:
    app, _repo = api
    async with _client(app) as client:
        response = await client.get(f"/api/brokers/alpaca/accounts/{ACCT}/bots/catalog")

    assert response.status_code == 200
    rows = response.json()
    assert [row["strategy_instance_id"] for row in rows] == [SID]
    assert rows[0]["strategy_key"] == "deployment_validation"
    assert rows[0]["desired_state"] == "RUNNING"


async def test_panel_scoped_uses_sqlite_projection(api) -> None:
    app, _repo = api
    async with _client(app) as client:
        response = await client.get(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/panel"
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["health"]["desired_state"] == "RUNNING"
    assert len(body["rail"]["stations"]) == 6
    assert "revision" in body
    assert {action["action_id"] for action in body["actions"]}.isdisjoint(
        {"clear_hold", "record_inventory_baseline"}
    )
    # "deployment_validation" is a registered Signal Program (issue #1730
    # Slice 5); this fixture's bot has no v2 seal at all (no sealed_program,
    # no build receipt for this instance's bytes), so the honest build
    # verdict is UNPROVEN, not a fabricated NOT_APPLICABLE -- see
    # prove_running_program_build's docstring in
    # app/services/signal_program_admission.py.
    assert body["sealed_program"] is None
    assert body["program_build"]["state"] == "UNPROVEN"
    assert body["program_build"]["program_key"] == "deployment_validation"


def _accept_enter_then_exit(repo: ClerkSqliteRepository) -> tuple[str, str]:
    """Two real, durable effect operations for SID with no broker contact."""
    entered = accept_enter(
        repo,
        account_id=ACCT,
        strategy_instance_id=SID,
        decision_id="dec-old",
        lifecycle_run_id="run-1",
        leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=1),
    )
    exited = accept_exit(
        repo,
        account_id=ACCT,
        strategy_instance_id=SID,
        decision_id="dec-new",
        lifecycle_run_id="run-1",
        entry_order_ref=entered.order_ref,
    )
    assert exited.effect_operation_id is not None
    return entered.effect_operation_id, exited.effect_operation_id


async def test_panel_selects_the_requested_older_transaction_not_the_newest(api) -> None:
    """#1729 AC #7: selecting an older transaction must render that
    transaction, not the newest unrelated one, through the real HTTP panel
    endpoint."""
    app, repo = api
    old_ref, newest_ref = _accept_enter_then_exit(repo)
    assert old_ref != newest_ref

    async with _client(app) as client:
        response = await client.get(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/panel",
            params={"transaction_ref": old_ref},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["rail"]["transaction_ref"] == old_ref
    assert body["rail"]["transaction_ref"] != newest_ref


async def test_panel_unresolvable_transaction_ref_is_explicit_absence_not_the_newest_operation(
    api,
) -> None:
    """#1729 AC #6/#7: a ``transaction_ref`` that names no durable transaction
    must render explicit, named absence — never silently substitute the
    bot's newest unrelated operation for it."""
    app, repo = api
    _old_ref, newest_ref = _accept_enter_then_exit(repo)

    async with _client(app) as client:
        response = await client.get(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/panel",
            params={"transaction_ref": "effect:never-existed"},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["rail"]["transaction_ref"] == "effect:never-existed"
    assert body["rail"]["transaction_ref"] != newest_ref
    assert all(station["state"] == "not_applicable" for station in body["rail"]["stations"])
    assert all(
        "effect:never-existed" in station["receipt"] for station in body["rail"]["stations"]
    )


async def test_live_snapshot_bootstrap_and_sse_share_one_versioned_document(
    api,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _repo = api
    async with _client(app) as client:
        panel_response = await client.get(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/panel"
        )
    panel = panel_response.json()
    snapshot = BotPanelLiveSnapshot(
        stream_epoch="epoch-new",
        surface_version=7,
        panel=panel,
        live_chart=ChartLiveResponse(
            strategy_instance_id=SID,
            symbol="SPY",
            trading_date_open_ms=_T0,
            trading_date_close_ms=_T0 + 60_000,
            resolution="5s",
            bars=[],
            fill_markers=[],
            overlay_notices=[],
            as_of_ms=_T0,
        ),
    )

    class _Hub:
        async def snapshot(self) -> BotPanelLiveSnapshot:
            return snapshot

        def subscribe(self) -> asyncio.Queue[BotPanelLiveSnapshot | None]:
            queue: asyncio.Queue[BotPanelLiveSnapshot | None] = asyncio.Queue(maxsize=2)
            queue.put_nowait(snapshot)
            queue.put_nowait(None)
            return queue

        def unsubscribe(self, _queue: asyncio.Queue[BotPanelLiveSnapshot | None]) -> None:
            return None

    async def get_hub(*_args: object, **_kwargs: object) -> _Hub:
        return _Hub()

    monkeypatch.setattr(broker_v2_panel, "get_or_start_live_projection_hub", get_hub)
    monkeypatch.setattr(
        broker_v2_panel, "retain_live_projection_hub", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        broker_v2_panel, "release_live_projection_hub", lambda *_args, **_kwargs: None
    )
    async with _client(app) as client:
        bootstrap = await client.get(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/live-snapshot",
            params={"resolution": "5s"},
        )
        stream = await client.get(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/live-stream",
            params={"resolution": "5s", "cursor": "epoch-old:99"},
        )

    assert bootstrap.status_code == 200
    assert bootstrap.json()["surface_version"] == 7
    assert stream.status_code == 200
    assert stream.headers["content-type"].startswith("text/event-stream")
    assert "event: reset" in stream.text
    assert "id: epoch-new:7" in stream.text


async def test_presented_action_executes_and_stale_repost_fails_closed(api) -> None:
    app, _repo = api
    async with _client(app) as client:
        panel = await client.get(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/panel"
        )
        action = next(
            item for item in panel.json()["actions"] if item["action_id"] == "reconcile_now"
        )
        request = {
            "action_id": "reconcile_now",
            "revision": panel.json()["revision"],
            "concurrency_token": action["concurrency_token"],
            "idempotency_key": "sqlite-reconcile",
        }
        first = await client.post(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/actions", json=request
        )
        second = await client.post(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/actions", json=request
        )

    assert first.status_code == 200
    assert first.json()["applied"] is True
    assert second.status_code == 409
    body = second.json()["detail"]
    assert body.keys() >= {
        "action_id",
        "outcome",
        "receipt_id",
        "recorded_at_ms",
        "message",
        "why",
        "reason_code",
    }
    assert body["action_id"] == "reconcile_now"
    assert body["outcome"] == "conflict"


async def test_live_chart_accepts_five_second_resolution(
    api,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _repo = api
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

    monkeypatch.setattr("app.routers.broker_v2_panel.ds.get_live_chart", live_chart)
    async with _client(app) as client:
        response = await client.get(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/chart/live",
            params={"resolution": "5s"},
        )

    assert response.status_code == 200
    assert response.json()["resolution"] == "5s"
    assert observed == ["5s"]


async def test_chart_contract_rejects_unknown_resolution_and_timeframe(api) -> None:
    app, _repo = api
    async with _client(app) as client:
        live = await client.get(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/chart/live",
            params={"resolution": "15s"},
        )
        history = await client.get(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/chart/history",
            params={"timeframe": "5m"},
        )

    assert live.status_code == 422
    assert history.status_code == 422
