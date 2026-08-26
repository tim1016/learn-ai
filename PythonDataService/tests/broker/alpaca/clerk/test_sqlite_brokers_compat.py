"""Existing Broker Desk routes fail closed under SQLite authority."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any, NoReturn

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from app.broker.alpaca.clerk.active_authority import (
    ActiveClerkRuntime,
    set_active_clerk_runtime,
)
from app.broker.alpaca.clerk.models import ChannelHealth
from app.broker.alpaca.clerk.sqlite.projection_errors import ProjectionReadError
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.alpaca.clerk.sqlite.uncertainty import raise_uncertainty
from app.broker.alpaca.clerk.stream_health import StreamHealthGate
from app.broker.contract.registry import (
    get_broker_registry,
    reset_broker_registry_for_testing,
)
from app.config import settings
from app.routers import brokers as brokers_router
from app.routers.brokers import router
from app.security.data_plane_control import CONTROL_SECRET_HEADER
from app.services.broker_account_snapshot import (
    clear_broker_account_snapshot_cache_for_testing,
)
from app.utils.timestamps import now_ms_utc


class _UnusedBroker:
    broker_id = "alpaca"

    async def list_orders(self, **_kwargs: Any) -> list:
        return []

    async def list_positions(self) -> list:
        return []

    async def cancel(self, _order_id: str) -> None:
        raise AssertionError("generic recovery must not contact the broker")


@pytest.fixture()
def sqlite_desk(tmp_path: Path):
    repo = ClerkSqliteRepository.initialize(
        account_id="PA-SQLITE-DESK",
        artifacts_root=tmp_path,
    )
    repo.register_strategy_instance(
        strategy_instance_id="spy-bot",
        symbol="SPY",
        config_hash="config-1",
    )
    raise_uncertainty(
        repo,
        strategy_instance_id="spy-bot",
        reason_code="ORDER_OUTCOME_UNKNOWN",
        headline="Order outcome remains unknown",
        explanation="The broker has not proven a terminal outcome.",
        operator_impact="Only this bot is blocked from new exposure.",
        next_step="Use the evidence-bound recovery actions.",
        evidence_refs=("order:one",),
    )
    broker = _UnusedBroker()
    facade = SqliteAlpacaClerkFacade(
        repo=repo,
        read=broker,  # type: ignore[arg-type]
        trade=broker,  # type: ignore[arg-type]
        stream_health=StreamHealthGate(
            market_data=lambda: ChannelHealth(
                stream="market_data",
                healthy=True, connected=True,
                reason="",
                observed_at_ms=10,
            ),
            execution=lambda: ChannelHealth(
                stream="execution",
                healthy=True, connected=True,
                reason="",
                observed_at_ms=11,
            ),
        ),
    )
    set_active_clerk_runtime(
        ActiveClerkRuntime(authority_kind="sqlite", clerk=facade)
    )
    app = FastAPI()
    app.include_router(router)
    try:
        yield app
    finally:
        set_active_clerk_runtime(None)
        repo.close()


@pytest.mark.asyncio
async def test_existing_reads_project_active_sqlite_authority(sqlite_desk: FastAPI) -> None:
    async with httpx.AsyncClient(
        transport=ASGITransport(app=sqlite_desk), base_url="http://test"
    ) as client:
        status = await client.get("/api/brokers/alpaca/clerk/status")
        diagnosis = await client.get(
            "/api/brokers/alpaca/clerk/custody-diagnosis"
        )

    assert status.status_code == 200
    assert status.json()["authority_kind"] == "real_paper"
    assert status.json()["channel_healths"] == [
        {
            "stream": "market_data",
            "healthy": True,
            "connected": True,
            "reason": "",
            "observed_at_ms": 10,
        },
        {
            "stream": "execution",
            "healthy": True,
            "connected": True,
            "reason": "",
            "observed_at_ms": 11,
        },
    ]
    assert diagnosis.status_code == 200
    assert diagnosis.json()["authority_kind"] == "real_paper"
    assert diagnosis.json()["in_sync"] is False
    assert diagnosis.json()["resolvable"] is False
    assert diagnosis.json()["divergences"][0]["kind"] == "needs_review"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/api/brokers/alpaca/clerk/status",
        "/api/brokers/alpaca/clerk/custody-diagnosis",
    ],
)
@pytest.mark.parametrize("projection_failure", ["read_error", "missing"])
async def test_existing_reads_map_sqlite_projection_failures_to_typed_unavailable(
    sqlite_desk: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    projection_failure: str,
) -> None:
    """Both projection failure modes must fail closed on both retained reads."""

    def unreadable_projection(**_kwargs: Any) -> NoReturn:
        raise ProjectionReadError("simulated malformed durable projection")

    def missing_projection(**_kwargs: Any) -> None:
        return None

    projection_reader = (
        unreadable_projection
        if projection_failure == "read_error"
        else missing_projection
    )
    monkeypatch.setattr(brokers_router, "sqlite_projection", projection_reader)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=sqlite_desk), base_url="http://test"
    ) as client:
        response = await client.get(path)

    assert response.status_code == 503
    assert response.json()["detail"] == {
        "reason": "sqlite_projection_unavailable",
        "message": "The Account Clerk order record could not be read safely.",
        "next_step": "Keep broker actions blocked and retry after the Clerk projection is repaired.",
    }


@pytest.mark.asyncio
async def test_generic_recovery_routes_are_absent_under_sqlite(
    sqlite_desk: FastAPI,
) -> None:
    async with httpx.AsyncClient(
        transport=ASGITransport(app=sqlite_desk), base_url="http://test"
    ) as client:
        headers = {CONTROL_SECRET_HEADER: settings.DATA_PLANE_CONTROL_SECRET}
        clear = await client.post(
            "/api/brokers/alpaca/clerk/clear-hold",
            json={"operator": "ops", "reason": "unsafe generic request"},
            headers=headers,
        )
        resolve = await client.post(
            "/api/brokers/alpaca/clerk/resolve",
            json={
                "reason": "unsafe generic request",
                "snapshot_version": "stale",
                "confirmation_token": "RESOLVE",
                "idempotency_key": "old-path",
            },
            headers=headers,
        )

    assert clear.status_code == resolve.status_code == 404


class _FakeAccount:
    account_id = "PA-SQLITE-DESK-CLEAN"
    account_mode = "paper"
    account_status = "ACTIVE"
    trading_blocked = False
    account_blocked = False


class _FakeAccountReadPort:
    broker_id = "alpaca"

    def __init__(self, account: _FakeAccount) -> None:
        self._account = account

    async def get_account(self) -> _FakeAccount:
        return self._account

    async def list_orders(self, **_kwargs: Any) -> list:
        return []

    async def list_positions(self) -> list:
        return []


@pytest.fixture()
def sqlite_desk_clean(tmp_path: Path) -> Iterator[FastAPI]:
    """A boot-selected SQLite authority with no active custody uncertainty."""
    repo = ClerkSqliteRepository.initialize(
        account_id="PA-SQLITE-DESK-CLEAN",
        artifacts_root=tmp_path,
    )
    broker = _UnusedBroker()
    facade = SqliteAlpacaClerkFacade(
        repo=repo,
        read=broker,  # type: ignore[arg-type]
        trade=broker,  # type: ignore[arg-type]
        stream_health=StreamHealthGate(
            market_data=lambda: ChannelHealth(
                stream="market_data", healthy=True, connected=True, reason="", observed_at_ms=now_ms_utc()
            ),
            execution=lambda: ChannelHealth(
                stream="execution", healthy=True, connected=True, reason="", observed_at_ms=now_ms_utc()
            ),
        ),
    )
    set_active_clerk_runtime(ActiveClerkRuntime(authority_kind="sqlite", clerk=facade))
    app = FastAPI()
    app.include_router(router)
    try:
        yield app
    finally:
        set_active_clerk_runtime(None)
        repo.close()
        reset_broker_registry_for_testing()
        clear_broker_account_snapshot_cache_for_testing()


@pytest.mark.asyncio
async def test_clerk_status_reports_healthy_posture_when_account_and_custody_are_clean(
    sqlite_desk_clean: FastAPI,
) -> None:
    get_broker_registry().register(_FakeAccountReadPort(_FakeAccount()))  # type: ignore[arg-type]

    async with httpx.AsyncClient(
        transport=ASGITransport(app=sqlite_desk_clean), base_url="http://test"
    ) as client:
        status = await client.get("/api/brokers/alpaca/clerk/status")

    assert status.status_code == 200
    posture = status.json()["operator_posture"]
    assert posture["condition"] is None
    assert posture["account_desk"] is None
    assert posture["fleet_roster"] is None


@pytest.mark.asyncio
async def test_clerk_status_reports_wrong_execution_mode_from_the_same_read(
    sqlite_desk_clean: FastAPI,
) -> None:
    account = _FakeAccount()
    account.account_mode = "live"
    get_broker_registry().register(_FakeAccountReadPort(account))  # type: ignore[arg-type]

    async with httpx.AsyncClient(
        transport=ASGITransport(app=sqlite_desk_clean), base_url="http://test"
    ) as client:
        status = await client.get("/api/brokers/alpaca/clerk/status")

    assert status.status_code == 200
    posture = status.json()["operator_posture"]
    assert posture["condition"]["id"] == "alpaca_account_wrong_execution_mode"
    # Terminal, not wait: a non-paper account is a static config problem that
    # fresh evidence can never resolve (2026-08-20 review).
    assert posture["account_desk"]["disposition"] == "terminal"
    assert posture["fleet_roster"]["disposition"] == "terminal"
    assert posture["account_desk"]["condition"]["id"] == posture["fleet_roster"]["condition"]["id"]


@pytest.mark.asyncio
async def test_clerk_status_degrades_to_evidence_unavailable_when_account_read_fails(
    sqlite_desk_clean: FastAPI,
) -> None:
    """No broker is registered — the account read fails, but the endpoint still 200s."""
    async with httpx.AsyncClient(
        transport=ASGITransport(app=sqlite_desk_clean), base_url="http://test"
    ) as client:
        status = await client.get("/api/brokers/alpaca/clerk/status")

    assert status.status_code == 200
    posture = status.json()["operator_posture"]
    assert posture["condition"]["id"] == "alpaca_account_evidence_unavailable"
    assert posture["condition"]["severity"] == "warning"


@pytest.mark.asyncio
async def test_clerk_status_reports_identity_mismatch_instead_of_the_wrong_accounts_facts(
    sqlite_desk_clean: FastAPI,
) -> None:
    """The registered broker port observes a different account than the one
    the active SQLite Clerk authority is bound to (e.g. credentials were
    repointed) — the wrong account's paper mode/status/block flags must
    never be attributed to this projection's account (2026-08-20 review)."""
    mismatched_account = _FakeAccount()
    mismatched_account.account_id = "PA-WRONG-ACCOUNT"
    get_broker_registry().register(_FakeAccountReadPort(mismatched_account))  # type: ignore[arg-type]

    async with httpx.AsyncClient(
        transport=ASGITransport(app=sqlite_desk_clean), base_url="http://test"
    ) as client:
        status = await client.get("/api/brokers/alpaca/clerk/status")

    assert status.status_code == 200
    assert status.json()["account_id"] == "PA-SQLITE-DESK-CLEAN"
    posture = status.json()["operator_posture"]
    assert posture["condition"]["id"] == "alpaca_account_identity_mismatch"
    assert posture["condition"]["severity"] == "blocking"
    assert posture["account_desk"]["disposition"] == "terminal"
    assert posture["fleet_roster"]["disposition"] == "terminal"


@pytest.mark.asyncio
async def test_clerk_status_degrades_to_evidence_unavailable_when_account_read_times_out(
    sqlite_desk_clean: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bound account read that times out must still 200 with the same
    evidence-unavailable posture as an outright read failure — the endpoint
    can never block UI polling for the account read's full budget."""

    async def timed_out_snapshot(_broker: str) -> Any:
        raise TimeoutError("simulated account read timeout")

    monkeypatch.setattr(brokers_router, "resolve_broker_account_snapshot", timed_out_snapshot)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=sqlite_desk_clean), base_url="http://test"
    ) as client:
        status = await client.get("/api/brokers/alpaca/clerk/status")

    assert status.status_code == 200
    posture = status.json()["operator_posture"]
    assert posture["condition"]["id"] == "alpaca_account_evidence_unavailable"
    assert posture["condition"]["severity"] == "warning"
