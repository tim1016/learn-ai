"""HTTP coverage for SQLite-backed bot evidence."""

from __future__ import annotations

import json
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
# Alpaca's stored spelling and the fleet's canonical route spelling (#2221).
_ACCOUNT_NUMBER = "PA3KWXU1C4C3"
_CANONICAL_ROUTE_ACCOUNT = "pa3kwxu1c4c3"


class _FakeAccount:
    def __init__(self, account_id: str) -> None:
        self.account_id = account_id


class _FakeReadPort:
    broker_id = "alpaca"

    def __init__(self, account_id: str = ACCT) -> None:
        self._account_id = account_id

    async def get_account(self) -> _FakeAccount:
        return _FakeAccount(self._account_id)

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


def _activate_sqlite(tmp_path: Path, account_id: str = ACCT) -> ClerkSqliteRepository:
    repo = ClerkSqliteRepository.initialize(account_id=account_id, artifacts_root=tmp_path)
    repo.register_strategy_instance(
        strategy_instance_id=SID,
        symbol="SPY",
        config_hash="config-1",
    )
    submit_start_run(
        repo,
        account_id=account_id,
        strategy_instance_id=SID,
        lifecycle_run_id="run-1",
    )
    facade = SqliteAlpacaClerkFacade(
        account_mode="paper",
        repo=repo,
        read=_FakeReadPort(account_id),  # type: ignore[arg-type]
        trade=_FakeReadPort(account_id),  # type: ignore[arg-type]
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


async def _read_account_number_evidence(
    app: FastAPI, tmp_path: Path, route_account: str
) -> httpx.Response:
    """Read evidence from an authority custodying Alpaca's uppercase account number."""
    reset_broker_registry_for_testing()
    get_broker_registry().register(_FakeReadPort(_ACCOUNT_NUMBER))  # type: ignore[arg-type]
    repo = _activate_sqlite(tmp_path, _ACCOUNT_NUMBER)
    try:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.get(
                f"/api/brokers/alpaca/accounts/{route_account}/bots/{SID}/evidence"
            )
    finally:
        repo.close()


@pytest.mark.asyncio
async def test_evidence_reads_the_account_number_authority_on_the_canonical_route(
    app_and_tmp,
) -> None:
    """The route's case never decides custody (#2221).

    The page and its audit entry both record the route's own spelling.
    """
    app, tmp_path = app_and_tmp

    response = await _read_account_number_evidence(app, tmp_path, _CANONICAL_ROUTE_ACCOUNT)

    assert response.status_code == 200, response.text
    assert response.json()["account_id"] == _CANONICAL_ROUTE_ACCOUNT
    assert response.json()["entries"]
    audit_log = tmp_path / "accounts" / _CANONICAL_ROUTE_ACCOUNT / "evidence_audit.jsonl"
    (audit_line,) = audit_log.read_text(encoding="utf-8").splitlines()
    assert json.loads(audit_line)["account_id"] == _CANONICAL_ROUTE_ACCOUNT


@pytest.mark.asyncio
async def test_evidence_refuses_a_foreign_route_account_with_a_typed_404(app_and_tmp) -> None:
    app, tmp_path = app_and_tmp

    response = await _read_account_number_evidence(app, tmp_path, "pa9other0000")

    assert response.status_code == 404, response.text
    assert response.json()["detail"]["message"] == (
        "Account 'pa9other0000' is not the account for broker 'alpaca'."
    )
