"""HTTP coverage for SQLite-backed bot evidence."""

from __future__ import annotations

from pathlib import Path

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
from app.broker.alpaca.clerk.sqlite.folds import DEFAULT_FOLD_REGISTRY
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.alpaca.config import reset_alpaca_settings_for_testing
from app.broker.contract.models import BrokerOrderLeg
from app.broker.contract.registry import (
    get_broker_registry,
    reset_broker_registry_for_testing,
)
from app.routers.broker_v2_panel import router
from app.schemas.broker_bots import BotStatusView
from app.services.bot_runner import set_bot_task_registry
from app.services.broker_v2_panel.evidence_service import (
    _SQLITE_CUSTODY_COPY,
    _SQLITE_OPERATION_STATE_COPY,
    _SQLITE_TRANSITION_COPY,
)
from tests.broker.v2panel.fixtures import ACCT, OTHER_SID, SID

_T0 = 1_700_000_000_000


class _FakeAccount:
    account_id = ACCT


class _FakeReadPort:
    broker_id = "alpaca"

    async def get_account(self) -> _FakeAccount:
        return _FakeAccount()

    def capabilities(self) -> None:  # pragma: no cover
        raise NotImplementedError


def _make_status(sid: str) -> BotStatusView:
    return BotStatusView(
        strategy_instance_id=sid,
        broker="alpaca",
        symbol="SPY",
        mode="log_only",
        quantity=1,
        running=True,
        phase="ON_DUTY",
        desired_state="RUNNING",
        active_run_id=None,
        duty_outcome=None,
        binding_created_at_ms=_T0,
        last_transition_at_ms=None,
    )


class _FakeRegistry:
    def list_bots(self, broker: str) -> list[BotStatusView]:
        return [_make_status(SID)]

    def status(self, broker: str, sid: str) -> BotStatusView:
        from app.services.bot_runner import BotRunnerError

        if sid not in (SID, OTHER_SID):
            raise BotRunnerError(f"Unknown bot {sid}", detail=None)
        return _make_status(sid)


@pytest.fixture()
def app_and_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ALPACA_API_KEY_ID", "key")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "secret")
    monkeypatch.setenv("ALPACA_CLERK_DIR", str(tmp_path))
    reset_alpaca_settings_for_testing()
    reset_broker_registry_for_testing()
    set_active_clerk_runtime(None)
    get_broker_registry().register(_FakeReadPort())  # type: ignore[arg-type]
    set_bot_task_registry(_FakeRegistry())

    app = FastAPI()
    app.include_router(router)
    try:
        yield app, tmp_path
    finally:
        set_bot_task_registry(None)
        set_active_clerk_runtime(None)
        reset_broker_registry_for_testing()
        reset_alpaca_settings_for_testing()


def _activate_sqlite(tmp_path: Path) -> ClerkSqliteRepository:
    repo = ClerkSqliteRepository.initialize(account_id=ACCT, artifacts_root=tmp_path)
    repo.register_strategy_instance(
        strategy_instance_id=SID,
        symbol="SPY",
        config_hash="config-1",
    )
    submit_start_run(
        repo,
        account_id=ACCT,
        strategy_instance_id=SID,
        lifecycle_run_id="run-1",
    )
    facade = SqliteAlpacaClerkFacade(
        repo=repo,
        read=_FakeReadPort(),  # type: ignore[arg-type]
        trade=_FakeReadPort(),  # type: ignore[arg-type]
    )
    set_active_clerk_runtime(ActiveClerkRuntime(authority_kind="sqlite", clerk=facade))
    return repo


def test_sqlite_evidence_copy_covers_closed_fold_and_state_vocabularies() -> None:
    assert set(_SQLITE_TRANSITION_COPY) == DEFAULT_FOLD_REGISTRY.registered_kinds
    assert set(_SQLITE_OPERATION_STATE_COPY) == {
        "accepted",
        "failed",
        "in_progress",
        "rejected",
        "succeeded",
        "unknown",
    }
    assert set(_SQLITE_CUSTODY_COPY) == {"ACCOUNT_CLERK"}


@pytest.mark.asyncio
async def test_evidence_uses_active_sqlite_timeline_with_opaque_stable_cursor(
    app_and_tmp,
) -> None:
    app, tmp_path = app_and_tmp
    repo = _activate_sqlite(tmp_path)
    try:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            first = await client.get(
                f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/evidence",
                params={"page_size": 1},
            )
            assert first.status_code == 200
            first_body = first.json()
            assert isinstance(first_body["next_cursor"], str)
            assert first_body["entries"][0]["operation_ref"]
            assert first_body["entries"][0]["clerk_observed_at_ms"]
            assert first_body["entries"][0]["recorded_at_ms"]

            second = await client.get(
                f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/evidence",
                params={"page_size": 1, "cursor": first_body["next_cursor"]},
            )

        assert second.status_code == 200
        assert {
            first_body["entries"][0]["seq"],
            second.json()["entries"][0]["seq"],
        } == {1, 2}
    finally:
        repo.close()


@pytest.mark.asyncio
async def test_sqlite_evidence_filters_selected_effect_operation(app_and_tmp) -> None:
    app, tmp_path = app_and_tmp
    repo = _activate_sqlite(tmp_path)
    accepted = accept_enter(
        repo,
        account_id=ACCT,
        strategy_instance_id=SID,
        decision_id="decision-1",
        lifecycle_run_id="run-1",
        leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=1),
    )
    try:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(
                f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/evidence",
                params={"transaction_ref": accepted.effect_operation_id},
            )

        assert response.status_code == 200
        body = response.json()
        assert body["total_entries"] > 0
        assert {entry["operation_ref"] for entry in body["entries"]} == {
            accepted.effect_operation_id
        }
    finally:
        repo.close()
