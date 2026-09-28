"""Trader-facing fee API consumes the same custody projection as admission."""
from __future__ import annotations

from app.broker.alpaca.clerk.sqlite.fee_evidence import record_fee_evidence
from tests.broker.alpaca.clerk.sqlite.conftest import DAY_PNL_SID, NOON, YESTERDAY_NOON
from tests.broker.alpaca.clerk.sqlite.test_fee_evidence import _activity, _seed


async def test_account_api_reports_exact_fee_ownership_from_custody(day_pnl_repo, monkeypatch) -> None:
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
    from app.routers import brokers
    from app.services import alpaca_fee_reconciliation

    repo = day_pnl_repo
    _seed(repo)
    record_fee_evidence(repo, [_activity("fee", "FEE", YESTERDAY_NOON, -0.05)], checked_at_ms=NOON, history_complete=True)
    facade = SqliteAlpacaClerkFacade(account_mode="paper", repo=repo, read=object(), trade=object())
    monkeypatch.setattr(alpaca_fee_reconciliation, "active_sqlite_facade", lambda _: facade)
    monkeypatch.setattr(brokers, "_resolve_port", lambda _: object())
    app = FastAPI()
    app.include_router(brokers.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/brokers/alpaca/fees/attribution")
    assert response.status_code == 200
    result = response.json()
    assert result["known"] is True
    assert result["authority_revision"] == repo.control_meta_snapshot().control_revision
    assert result["rows"][0]["strategy_instance_id"] == DAY_PNL_SID
    assert result["rows"][0]["observed_usd"] == "0.05"
    assert result["rows"][0]["total_usd"] == "0.05"
    assert result["account_unattributed_usd"] == "0"


async def test_bot_fee_read_selects_its_synthetic_authority(day_pnl_repo, tmp_path, monkeypatch) -> None:
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
    from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
    from app.services import alpaca_fee_reconciliation, bot_runner
    from app.services.broker_v2_panel import panel_data_source
    from tests.broker.alpaca.clerk.sqlite.conftest import _clock_at

    record_fee_evidence(day_pnl_repo, [_activity("real-fee", "FEE", YESTERDAY_NOON, -500)], checked_at_ms=NOON, history_complete=True)
    real = SqliteAlpacaClerkFacade(account_mode="paper", repo=day_pnl_repo, read=object(), trade=object())
    simulated_repo = ClerkSqliteRepository.initialize(account_id="sim:dry", artifacts_root=tmp_path / "dry", clock=_clock_at(NOON))
    simulated = SqliteAlpacaClerkFacade(account_mode="paper", repo=simulated_repo, read=object(), trade=object(), authority_kind="synthetic")
    binding = SimpleNamespace(mode="dry_run", strategy_instance_id="dry")
    registry = SimpleNamespace(binding_for_control=lambda broker, sid: binding)

    @asynccontextmanager
    async def select(selected_registry, selected_binding):
        assert selected_registry is registry and selected_binding is binding
        yield simulated

    monkeypatch.setattr(alpaca_fee_reconciliation, "active_sqlite_facade", lambda _: real)
    monkeypatch.setattr(bot_runner, "get_bot_task_registry", lambda: registry)
    monkeypatch.setattr(panel_data_source, "_panel_authority_for_binding", select)
    try:
        view = await alpaca_fee_reconciliation.deployment_fee_attribution("dry")
        assert view.account_id == "sim:dry"
        assert view.known and view.rows == []
        assert view.account_unattributed_usd == "0"
    finally:
        simulated_repo.close()
