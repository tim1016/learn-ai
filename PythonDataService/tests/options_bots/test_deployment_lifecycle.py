"""Owner HTTP commands through the real registry, daily rules and synthetic Clerk.

Only market observations, the primary account's read and research storage are
substituted. Approval proves archived lake bytes; deployment, budgets, orders,
fills, recovery fences and rule progress use their production implementations.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI

from app.broker.alpaca.clerk.active_authority import close_synthetic_clerk_runtimes, get_clerk_runtime
from app.broker.alpaca.clerk.kept_option_quotes import KeptOptionQuotes
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.lean_sidecar.trading_calendar import (
    CALENDAR_IDENTITY,
    CALENDAR_PACKAGE_VERSION,
    current_trading_session_window,
)
from app.options.contract import OptionContract
from app.options.quotes import HeldQuote, QuoteDataType, QuoteSide
from app.options.resolver import FeedState
from app.options_bots import golden, runtime
from app.options_bots.golden import GoldenOptionsSetups, SourceRun
from app.options_bots.golden_proof import build_proof
from app.options_bots.journal import OptionsBotJournal
from app.research.golden_search.proof import BlobStore
from app.routers import golden_option_setups, options_bots
from app.schemas.bot_end import BotEndInput
from app.schemas.deployment_budget import DeployBudgetConsent
from app.services import options_bot_deploy
from app.services.bot_binding_repository import BrokerBotBinding
from app.services.broker_v2_panel import bot_custody, panel_deploy, panel_scope
from tests._helpers.bot_runner.custody import _registry
from tests._helpers.bot_runner.doubles import _FakeFeed
from tests._helpers.fake_lake_catalog import FakeCatalog
from tests._helpers.market_quotes import publish_quotes
from tests.broker.v2panel.test_budget_deploy import authority as authority
from tests.options_backtest._lake import fake_catalog as fake_catalog
from tests.options_backtest._lake import tmp_lake as tmp_lake
from tests.options_backtest._market import D, at
from tests.options_bots.test_golden import MemoryGoldenStore
from tests.options_bots.test_golden_proof import _run
from tests.options_bots.test_runtime import Clock, Market


@respx.mock
async def test_approved_setup_deploys_once_through_http_and_the_actual_registry(
    authority: tuple[Any, Any, Any], fake_catalog: FakeCatalog, tmp_lake: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _primary_repo, primary, _risk = authority
    source = await _run(fake_catalog, tmp_lake)
    module = GoldenOptionsSetups(
        MemoryGoldenStore(),
        source=lambda _id: asyncio.sleep(0, result=SourceRun("Tested session", source, CALENDAR_IDENTITY, CALENDAR_PACKAGE_VERSION)),
        prove=partial(build_proof, blobs=BlobStore(tmp_path / "proof")),
    )
    clock = Clock(at(4))
    feed = _FakeFeed([], mode="hold", observed_at_ms=clock())
    registry = _registry(tmp_path / "bots", feed, now_ms=clock)

    class LiveObservations(Market):
        def quote_feed(self) -> FeedState:
            return FeedState(1, None)

        def quote(self, _contract: OptionContract) -> HeldQuote:
            return HeldQuote(QuoteSide(D("1.99"), clock()), QuoteSide(D("2.00"), clock()), QuoteDataType.LIVE, 1)

    class ObservedDuty(runtime.OptionsBotDuty):
        def __init__(self, binding: BrokerBotBinding, clerk: SqliteAlpacaClerkFacade, journal: OptionsBotJournal) -> None:
            super().__init__(binding, clerk, journal, clock=clock, qualify=module.ready,
                             market_factory=lambda window: LiveObservations(window, clock))

    async def account(_broker: str, _account: str) -> str:
        return primary.selected_account_id

    monkeypatch.setattr(panel_scope, "validate_account", account)
    monkeypatch.setattr(options_bot_deploy, "get_bot_task_registry", lambda: registry)
    monkeypatch.setattr(bot_custody, "get_bot_task_registry", lambda: registry)
    monkeypatch.setattr(panel_scope, "get_bot_task_registry", lambda: registry)
    monkeypatch.setattr(panel_deploy, "get_bot_task_registry", lambda: registry)
    monkeypatch.setattr(options_bot_deploy, "get_ready_setup", module.ready)
    monkeypatch.setattr(golden, "get_ready_setup", module.ready)
    monkeypatch.setattr(options_bot_deploy, "now_ms_utc", clock)
    monkeypatch.setattr("app.services.broker_v2_panel.bot_end_panel.now_ms_utc", clock)
    monkeypatch.setattr(runtime, "OptionsBotDuty", ObservedDuty)
    app = FastAPI()
    app.include_router(golden_option_setups.router, prefix="/api/research/golden-option-setups")
    app.include_router(options_bots.router)
    app.dependency_overrides[golden_option_setups.service] = lambda: module
    base = f"/api/brokers/alpaca/accounts/{primary.selected_account_id}/options-bots"
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            approved = await client.post("/api/research/golden-option-setups/approve", json={
                "run_id": 123, "note": "Approve this exact completed test", "idempotency_key": "approve-lifecycle",
            })
            assert approved.status_code == 200, approved.text
            body = {
                "golden_setup_id": approved.json()["id"], "execution_mode": "dry_run",
                "budget": {"amount_usd": "10000", "risk_revision": 0},
                "order_terms": {"gap_limit_percent": "8", "entry_wait_seconds": 5,
                                "cost_limit_percent": "10", "expiry_close_lead_minutes": 15},
                "exit_terms": {"band_multiple": 2, "spread_cap_bps": 50, "exit_allowance_bps": 20},
            }
            review = await client.post(base + "/review", json=body)
            assert review.status_code == 200, review.text
            assert review.json()["allowed"], review.text
            body["budget"]["review_token"] = review.json()["budget"]["review_token"]
            clock.now = at(5) + 1
            late_review = await client.post(base + "/review", json=body)
            assert late_review.status_code == 409, late_review.text
            assert "next eligible entry" in late_review.json()["detail"]["message"]
            late_deploy = await client.post(base + "/deploy", json={**body, "submission_key": "late-cutoff-deploy"})
            assert late_deploy.status_code == 409, late_deploy.text
            assert "next eligible entry" in late_deploy.json()["detail"]["message"]
            unclaimed = await client.get(base + "/deploy-submissions/late-cutoff-deploy")
            assert unclaimed.status_code == 404, unclaimed.text
            assert len(registry.bindings_for_broker("alpaca")) == 0
            resolve_consent = options_bot_deploy.budget_deploy.resolve_options_session_consent

            def consent_crosses_entry(*args: Any, **kwargs: Any) -> DeployBudgetConsent:
                consent = resolve_consent(*args, **kwargs)
                clock.now = at(5) + 1
                feed._observed_at_ms = clock()
                return consent

            clock.now = at(5) - 1
            with monkeypatch.context() as crossing:
                crossing.setattr(options_bot_deploy.budget_deploy, "resolve_options_session_consent", consent_crosses_entry)
                crossed = await client.post(base + "/deploy", json={**body, "submission_key": "crossed-entry-deploy"})
            assert crossed.status_code == 409, crossed.text
            assert "next eligible entry" in crossed.json()["detail"]["message"]
            assert len(registry.bindings_for_broker("alpaca")) == 0
            clock.now = at(4)
            feed._observed_at_ms = clock()
            body["submission_key"] = "lifecycle-deploy"
            deployed = await client.post(base + "/deploy", json=body)
            assert deployed.status_code == 200, deployed.text
            assert deployed.json()["status"] == "deployed", deployed.text
            sid = deployed.json()["strategy_instance_id"]
            desired = registry._desired_repo(sid).read()
            session = current_trading_session_window(clock())
            assert desired is not None and desired.end is not None and session is not None
            assert desired.end.end_at_ms == session.close_ms_utc - 45 * 60_000
            assert desired.end.end_action == "SELL"
            await registry.edit_bot_end("alpaca", sid, BotEndInput(end_at_ms=None, end_action="SELL"), updated_by="owner")
            assert registry.bot_end("alpaca", sid).default_end_at_ms == session.close_ms_utc - 45 * 60_000
            await registry.edit_bot_end("alpaca", sid, BotEndInput(
                end_at_ms=desired.end.end_at_ms, end_action=desired.end.end_action,
            ), updated_by="owner")
            replay = await client.post(base + "/deploy", json=body)
            assert replay.status_code == 200, replay.text
            assert replay.json()["run_id"] == deployed.json()["run_id"]
            assert len(registry.bindings_for_broker("alpaca")) == 1
            active = get_clerk_runtime(f"sim:{sid}")
            assert active is not None
            assert active.sqlite_repository.deployment_budget(sid)["committed_cents"] == 1_000_000
            async def advance(minute: int) -> None:
                current = get_clerk_runtime(f"sim:{sid}")
                assert current is not None
                while clock() < at(minute) + runtime.MINUTE_GRACE_MS:
                    clock.now = min(clock() + 10_000, at(minute) + runtime.MINUTE_GRACE_MS)
                    current.sqlite_repository.renew_execution_lease()
                publish_quotes(clock(), SPY=500)
                await current.envelope_sync.tick()

            async def session_until(predicate: Callable[[dict[str, Any]], bool]) -> dict[str, Any]:
                async with asyncio.timeout(4):
                    while True:
                        response = await client.get(base + f"/{sid}/session")
                        assert response.status_code == 200, response.text
                        state = response.json()
                        if predicate(state):
                            return state
                        await asyncio.sleep(0.05)

            await advance(5)
            try:
                async with asyncio.timeout(4):
                    while not active.sqlite_repository.attributed_positions_for_strategy(sid):
                        await asyncio.sleep(0.05)
            except TimeoutError:
                journal = OptionsBotJournal(registry.artifacts_root / "live_state" / sid)
                try:
                    pytest.fail(f"No confirmed entry: {journal.status()}; {journal.decisions()}; {registry.status('alpaca', sid).duty_outcome}")
                finally:
                    journal.close()
            assert active.sqlite_repository.attributed_positions_for_strategy(sid) == {"SPY261009C00500000": 1.0}
            entered = await session_until(lambda state: bool(state["legs"]) and state["legs"][0]["held_contracts"] == 1)
            assert D(entered["legs"][0]["average_fill_price"]) == D("2")
            KeptOptionQuotes.of_account(artifacts_root=registry.artifacts_root, account_id=f"sim:{sid}").keep_all(
                {"SPY261009C00500000": HeldQuote(QuoteSide(D("1.99"), clock()), QuoteSide(D("2.00"), clock()), QuoteDataType.LIVE, 1)},
                kept_at_ms=clock(),
            )
            await active.envelope_sync.tick()
            original_run = deployed.json()["run_id"]
            managed = registry._bots[sid]
            managed.task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await managed.task
            interrupted = await client.get(base + f"/{sid}/session")
            assert interrupted.json()["resume_allowed"]
            review_resume = await client.post(base + f"/{sid}/resume-review", json={"amount_usd": "10000", "risk_revision": 0})
            assert review_resume.status_code == 200, review_resume.text
            resume_body = {"idempotency_key": "resume-lifecycle", "budget": {
                "amount_usd": "10000", "risk_revision": 0, "review_token": review_resume.json()["review_token"],
            }}
            resumed = await client.post(base + f"/{sid}/resume", json=resume_body)
            assert resumed.status_code == 200, resumed.text
            assert resumed.json()["running"]
            assert resumed.json()["active_run_id"] != original_run
            repeated_resume = await client.post(base + f"/{sid}/resume", json=resume_body)
            assert repeated_resume.json()["active_run_id"] == resumed.json()["active_run_id"]
            retained = await session_until(lambda state: state["legs"][0]["held_contracts"] == 1)
            assert retained["legs"][0]["entries_filled"] == 1
            revoked = await client.post("/api/research/golden-option-setups/" + approved.json()["id"] + "/revoke", json={
                "reason": "Block further entries while preserving daily exits", "idempotency_key": "revoke-lifecycle",
            })
            assert revoked.status_code == 200, revoked.text
            await advance(60)
            closed = await session_until(lambda state: bool(state["legs"]) and state["legs"][0]["held_contracts"] == 0)
            assert closed["pending_decisions"] == 0
            current = get_clerk_runtime(f"sim:{sid}")
            assert current is not None
            with current.sqlite_repository.write_fence() as conn:
                assert conn.execute("SELECT COUNT(*) FROM orders WHERE role='ENTRY'").fetchone()[0] == 1
                assert conn.execute("SELECT COUNT(*) FROM orders WHERE role='REDUCING'").fetchone()[0] == 1
            assert current.sqlite_repository.deployment_budget(sid)["committed_cents"] == 1_000_000
    finally:
        await registry.stop_all()
        await close_synthetic_clerk_runtimes()
