"""Regression tests for the account-scoped deploy alias (§3, §5).

The deploy dialog POSTs to ``/api/brokers/{broker}/accounts/{account_id}/bots``
but only the unscoped ``/{broker}/bots`` route existed — the scoped form
404'd for the *correct* account (found live 2026-07-30, canary run). These
tests pin the scoped alias: correct account delegates to the bot runner,
mismatched account gets the documented typed 404.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from app.broker.alpaca.clerk.models import (
    AccountFreezeState,
    ChannelHealth,
    ClerkStatus,
    HoldState,
)
from app.broker.contract.registry import (
    get_broker_registry,
    reset_broker_registry_for_testing,
)
from app.config import settings
from app.routers.broker_v2_panel import router
from app.schemas.broker_bots import BotStatusView
from app.schemas.run_admission import RunAdmissionDecision
from app.schemas.strategy_validation import StrategyValidationEntry
from app.services.bot_runner import (
    AdmittedBotStart,
    BotRunnerError,
    set_bot_task_registry,
)
from app.services.broker_account_snapshot import (
    clear_broker_account_snapshot_cache_for_testing,
)
from app.services.broker_v2_panel import panel_data_source
from app.services.broker_v2_panel.paper_deploy_service import _strategy_views
from app.services.strategy_validation_manifest import (
    load_strategy_validation_entries,
    strategy_registry_seeds,
)
from app.utils.timestamps import now_ms_utc
from tests.broker.v2panel.fixtures import ACCT, SID

_T0 = 1_700_000_000_000


class _FakeAccount:
    account_id = ACCT
    account_mode = "paper"
    account_status = "ACTIVE"
    trading_blocked = False
    account_blocked = False


class _FakeReadPort:
    broker_id = "alpaca"

    async def get_account(self) -> _FakeAccount:
        return _FakeAccount()

    def capabilities(self) -> None:  # pragma: no cover
        raise NotImplementedError


class _FakeDeployRegistry:
    def __init__(self) -> None:
        self.deploy_calls: list[dict] = []

    def _decision(self, kwargs: dict) -> RunAdmissionDecision:
        return RunAdmissionDecision(
            operation="START",
            allowed=True,
            reason_code="START_ADMITTED",
            explanation="The Clerk and bot registry admit Start.",
            next_step=None,
            strategy_instance_id=kwargs["strategy_instance_id"],
            proposed_run_id="run-test",
            configuration_hash="a" * 64,
            account_id=ACCT,
            evaluated_at_ms=_T0,
            fact_ages_ms={"runtime": 0, "process": 0, "market_data": 0, "clerk": 0},
            evidence_refs=("test-admission",),
        )

    async def deploy_with_admission(self, **kwargs) -> AdmittedBotStart:
        self.deploy_calls.append(kwargs)
        bot = BotStatusView(
            strategy_instance_id=kwargs["strategy_instance_id"],
            strategy_key=kwargs["strategy_key"],
            broker=kwargs["broker"],
            symbol=kwargs["symbol"],
            mode=kwargs["mode"],
            quantity=kwargs["quantity"],
            running=True,
            phase="ON_DUTY",
            desired_state="RUNNING",
            active_run_id="run-test",
            duty_outcome=None,
            binding_created_at_ms=_T0,
            last_transition_at_ms=None,
        )
        return AdmittedBotStart(bot=bot, admission=self._decision(kwargs))

    async def preview_start_admission(self, **kwargs) -> RunAdmissionDecision:
        return self._decision(kwargs)


@pytest.fixture()
def deploy_app(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ALPACA_CLERK_DIR", str(tmp_path))
    clear_broker_account_snapshot_cache_for_testing()
    reset_broker_registry_for_testing()
    get_broker_registry().register(_FakeReadPort())  # type: ignore[arg-type]
    registry = _FakeDeployRegistry()
    set_bot_task_registry(registry)  # type: ignore[arg-type]

    async def clerk_status() -> ClerkStatus:
        observed_at_ms = now_ms_utc()
        return ClerkStatus(
            broker="alpaca",
            account_id=ACCT,
            hold=HoldState(active=False),
            outstanding_intents=0,
            observed_at_ms=observed_at_ms,
            channel_healths=[
                ChannelHealth(stream="market_data", healthy=True, observed_at_ms=observed_at_ms),
                ChannelHealth(stream="execution", healthy=True, observed_at_ms=observed_at_ms),
            ],
        )

    monkeypatch.setattr(panel_data_source, "_clerk_status", clerk_status)

    fast_app = FastAPI()
    fast_app.include_router(router)

    try:
        yield fast_app, registry
    finally:
        set_bot_task_registry(None)
        clear_broker_account_snapshot_cache_for_testing()
        reset_broker_registry_for_testing()


_BODY = {
    "strategy_instance_id": SID,
    "strategy_key": "deployment_validation",
    "symbol": "SPY",
    "sizing": {"preset": "custom", "quantity": 2},
}


def _accepted_deploy_entry() -> StrategyValidationEntry:
    return next(
        entry
        for entry in load_strategy_validation_entries(strategy_registry_seeds())
        if entry.strategy_key == "deployment_validation"
    )


@pytest.mark.asyncio
async def test_deploy_scoped_correct_account_delegates(deploy_app) -> None:
    fast_app, registry = deploy_app

    async with httpx.AsyncClient(transport=ASGITransport(app=fast_app), base_url="http://test") as client:
        resp = await client.post(f"/api/brokers/alpaca/accounts/{ACCT}/bots", json=_BODY)

    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "deployed"
    assert body["outcome"] == "success"
    assert body["receipt_id"].startswith("alpaca-paper-deploy:")
    assert body["recorded_at_ms"] == _T0
    assert body["account_id"] == ACCT
    assert body["execution_mode"] == "paper"
    assert body["sizing"] == {"preset": "custom", "quantity": 2}
    assert body["bot"]["strategy_instance_id"] == SID
    assert body["bot"]["mode"] == "trade"
    assert body["bot"]["quantity"] == 2
    assert body["admission"]["reason_code"] == "START_ADMITTED"
    assert body["action_plan"]["on_enter"][0]["instrument"]["underlying"] == "SPY"
    assert body["action_plan"]["on_exit"] == [{"kind": "close_leg", "entry_leg_id": "primary"}]
    assert body["next_action"]
    assert body["panel_path"].endswith(f"/{SID}")
    assert len(registry.deploy_calls) == 1
    call = registry.deploy_calls[0]
    assert call["broker"] == "alpaca"
    assert call["mode"] == "trade"
    assert call["quantity"] == 2
    assert call["use_rth"] is True


@pytest.mark.asyncio
async def test_dry_run_deploy_selects_zero_broker_write_runner_mode(deploy_app) -> None:
    fast_app, registry = deploy_app
    request = {**_BODY, "execution_mode": "dry_run"}

    async with httpx.AsyncClient(
        transport=ASGITransport(app=fast_app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots",
            json=request,
        )

    assert response.status_code == 201
    assert response.json()["execution_mode"] == "dry_run"
    assert response.json()["bot"]["mode"] == "dry_run"
    assert registry.deploy_calls[-1]["mode"] == "dry_run"


@pytest.mark.asyncio
async def test_dry_run_refuses_broker_exposure_carryover(deploy_app) -> None:
    fast_app, registry = deploy_app

    async with httpx.AsyncClient(
        transport=ASGITransport(app=fast_app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots",
            json={
                **_BODY,
                "execution_mode": "dry_run",
                "carryover_policy": "ALLOW",
            },
        )

    assert response.status_code == 422
    assert registry.deploy_calls == []


@pytest.mark.asyncio
async def test_start_admission_preview_uses_the_request_specific_policy(deploy_app) -> None:
    fast_app, _registry = deploy_app

    async with httpx.AsyncClient(transport=ASGITransport(app=fast_app), base_url="http://test") as client:
        response = await client.post(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots/admission",
            json=_BODY,
        )

    assert response.status_code == 200
    assert response.json()["reason_code"] == "START_ADMITTED"
    assert response.json()["strategy_instance_id"] == SID


@pytest.mark.asyncio
async def test_deploy_accepts_dotted_equity_symbol_supported_by_form(deploy_app) -> None:
    fast_app, registry = deploy_app

    async with httpx.AsyncClient(transport=ASGITransport(app=fast_app), base_url="http://test") as client:
        response = await client.post(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots",
            json={**_BODY, "strategy_instance_id": "brk-b-validation", "symbol": " brk.b "},
        )

    assert response.status_code == 201
    assert response.json()["action_plan"]["on_enter"][0]["instrument"]["underlying"] == "BRK.B"
    assert registry.deploy_calls[0]["symbol"] == "BRK.B"


@pytest.mark.asyncio
async def test_deploy_submits_selected_validated_strategy(deploy_app) -> None:
    fast_app, registry = deploy_app

    async with httpx.AsyncClient(transport=ASGITransport(app=fast_app), base_url="http://test") as client:
        response = await client.post(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots",
            json={
                **_BODY,
                "strategy_instance_id": "ema-paper-01",
                "strategy_key": "ema_crossover_signal",
            },
        )

    assert response.status_code == 201
    assert response.json()["bot"]["strategy_key"] == "ema_crossover_signal"
    assert registry.deploy_calls[0]["strategy_key"] == "ema_crossover_signal"


@pytest.mark.asyncio
async def test_deploy_scoped_account_mismatch_404(deploy_app) -> None:
    fast_app, registry = deploy_app

    async with httpx.AsyncClient(transport=ASGITransport(app=fast_app), base_url="http://test") as client:
        resp = await client.post("/api/brokers/alpaca/accounts/WRONGACCT/bots", json=_BODY)

    assert resp.status_code == 404
    assert "is not the account for broker" in resp.json()["detail"]["message"]
    assert resp.json()["detail"]["outcome"] == "blocked"
    assert registry.deploy_calls == []


@pytest.mark.asyncio
async def test_deploy_view_is_closed_paper_only_contract(deploy_app) -> None:
    fast_app, _registry = deploy_app

    async with httpx.AsyncClient(transport=ASGITransport(app=fast_app), base_url="http://test") as client:
        resp = await client.get(f"/api/brokers/alpaca/accounts/{ACCT}/bots/deploy")

    assert resp.status_code == 200
    body = resp.json()
    assert body["account_mode"] == "paper"
    assert body["allowed_actions"] == ["deploy"]
    assert [row["strategy_key"] for row in body["strategies"]] == [
        "deployment_validation",
        "ema_crossover_signal",
    ]
    strategy = body["strategies"][0]
    assert set(strategy) == {
        "strategy_key",
        "label",
        "explanation",
        "validation_case_symbol",
    }
    assert strategy["validation_case_symbol"] == "SPY"
    assert body["evaluated_at_ms"] > 0
    assert {check["gate_id"] for check in body["readiness_checks"]} == {
        "strategy.validation_accepted",
        "broker.account_posture",
        "clerk.custody_freeze",
        "clerk.exposure_hold",
        "clerk.intent_custody",
        "clerk.channel_health",
    }
    assert all(check["ready"] for check in body["readiness_checks"])
    assert body["execution_modes"] == [
        {
            "mode": "dry_run",
            "label": "Dry Run",
            "availability": "available",
            "explanation": (
                "Real market data and strategy decisions produce clearly simulated fills; "
                "the runner never calls the Clerk's broker-effect boundary."
            ),
        },
        {
            "mode": "paper",
            "label": "Paper",
            "availability": "available",
            "explanation": "Orders route only to the selected Alpaca paper account through the Clerk.",
        },
        {
            "mode": "live",
            "label": "Live",
            "availability": "planned",
            "explanation": "Live Alpaca execution is planned but is not connected to an admission or execution path.",
        },
    ]
    assert [row["preset"] for row in body["sizing_options"]] == [
        "safe_canary",
        "custom",
    ]
    assert "enter" in body["action_plan_explanation"].lower()
    assert "close" in body["action_plan_explanation"].lower()
    assert body["carryover_available"] is False
    assert body["carryover_label"]
    assert body["carryover_explanation"]


@pytest.mark.asyncio
async def test_deploy_requires_current_accepted_validation_provenance(
    deploy_app,
    monkeypatch,
) -> None:
    fast_app, registry = deploy_app
    monkeypatch.setattr(panel_data_source, "load_strategy_validation_entries", lambda _registry: [])

    async with httpx.AsyncClient(transport=ASGITransport(app=fast_app), base_url="http://test") as client:
        view_response = await client.get(f"/api/brokers/alpaca/accounts/{ACCT}/bots/deploy")
        deploy_response = await client.post(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots",
            json=_BODY,
        )

    view = view_response.json()
    assert view["strategies"] == []
    assert view["eligibility"]["reason_code"] == "STRATEGY_NOT_ACCEPTED_FOR_DEPLOY"
    strategy_gate = next(
        check for check in view["readiness_checks"] if check["gate_id"] == "strategy.validation_accepted"
    )
    assert strategy_gate["ready"] is False
    assert strategy_gate["authority"] == "Strategy validation current-state projection"
    assert deploy_response.status_code == 409
    assert deploy_response.json()["detail"]["outcome"] == "conflict"
    assert registry.deploy_calls == []


def test_deploy_rejects_manifest_proof_that_differs_from_accepted_snapshot() -> None:
    entry = _accepted_deploy_entry()
    changed = entry.model_copy(update={"settings_file_sha256": "0" * 64})

    assert _strategy_views([changed]) == ()


def test_deploy_reverifies_the_accepted_audit_copy_hash() -> None:
    entry = _accepted_deploy_entry()
    event = entry.current_flag_event
    assert event is not None
    bad_hash = "0" * 64
    changed_snapshot = event.evidence_snapshot.model_copy(update={"audit_copy_sha256": bad_hash})
    changed_event = event.model_copy(update={"evidence_snapshot": changed_snapshot})
    changed = entry.model_copy(
        update={
            "audit_copy_sha256": bad_hash,
            "current_flag_event": changed_event,
        }
    )

    assert _strategy_views([changed]) == ()


def test_deploy_rejects_accepted_event_with_gating_divergence() -> None:
    entry = _accepted_deploy_entry()
    event = entry.current_flag_event
    assert event is not None
    changed_event = event.model_copy(
        update={
            "behavioral_equivalence": event.behavioral_equivalence.model_copy(
                update={"gating_divergence_counts": {"DECISION_MISMATCH": 1}}
            )
        }
    )
    changed = entry.model_copy(update={"current_flag_event": changed_event})

    assert _strategy_views([changed]) == ()


@pytest.mark.asyncio
async def test_pre_execution_service_failure_is_blocked_not_unknown(
    deploy_app,
    monkeypatch,
) -> None:
    fast_app, registry = deploy_app

    async def unavailable_clerk() -> ClerkStatus:
        raise panel_data_source.PanelUnavailableError(
            "The Clerk is unavailable.",
            detail="No deployment was attempted.",
        )

    monkeypatch.setattr(panel_data_source, "_clerk_status", unavailable_clerk)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=fast_app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots",
            json=_BODY,
        )

    assert response.status_code == 503
    assert response.json()["detail"]["outcome"] == "blocked"
    assert response.json()["detail"]["receipt_id"] is None
    assert registry.deploy_calls == []


@pytest.mark.asyncio
async def test_failure_after_runner_dispatch_is_unknown(
    deploy_app,
    monkeypatch,
) -> None:
    fast_app, registry = deploy_app

    async def failed_dispatch(**_kwargs) -> AdmittedBotStart:
        raise BotRunnerError(
            "Runner response was lost.",
            detail="The deployment outcome cannot be proved.",
        )

    monkeypatch.setattr(registry, "deploy_with_admission", failed_dispatch)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=fast_app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots",
            json=_BODY,
        )

    assert response.status_code == 500
    assert response.json()["detail"]["outcome"] == "unknown"


@pytest.mark.asyncio
async def test_start_refusal_returns_the_execution_policy_decision(
    deploy_app,
    monkeypatch,
) -> None:
    fast_app, registry = deploy_app
    denied = registry._decision(_BODY).model_copy(
        update={
            "allowed": False,
            "reason_code": "MARKET_DATA_STALE",
            "explanation": "The required market-data feed is stale.",
            "next_step": "Restore fresh market data before Start.",
        }
    )

    async def refuse(**_kwargs) -> AdmittedBotStart:
        raise BotRunnerError(
            "Start admission was refused.",
            detail=denied.explanation,
            admission_decision=denied,
        )

    monkeypatch.setattr(registry, "deploy_with_admission", refuse)
    async with httpx.AsyncClient(transport=ASGITransport(app=fast_app), base_url="http://test") as client:
        response = await client.post(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots",
            json=_BODY,
        )

    assert response.status_code == 500
    assert response.json()["detail"]["outcome"] == "blocked"
    assert response.json()["detail"]["admission"]["reason_code"] == "MARKET_DATA_STALE"


@pytest.mark.asyncio
async def test_deploy_blocks_when_clerk_channel_health_is_unproven(
    deploy_app,
    monkeypatch,
) -> None:
    fast_app, registry = deploy_app

    async def no_channel_status() -> ClerkStatus:
        return ClerkStatus(
            broker="alpaca",
            account_id=ACCT,
            hold=HoldState(active=False),
            outstanding_intents=0,
            observed_at_ms=_T0,
            channel_healths=None,
        )

    monkeypatch.setattr(panel_data_source, "_clerk_status", no_channel_status)
    async with httpx.AsyncClient(transport=ASGITransport(app=fast_app), base_url="http://test") as client:
        response = await client.get(f"/api/brokers/alpaca/accounts/{ACCT}/bots/deploy")

    view = response.json()
    assert view["eligibility"]["reason_code"] == "CLERK_CHANNEL_UNHEALTHY"
    channel_gate = next(check for check in view["readiness_checks"] if check["gate_id"] == "clerk.channel_health")
    assert channel_gate["ready"] is False
    assert channel_gate["evidence"]["channel_count"] == 0
    assert registry.deploy_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("channels", "evidence_key", "expected_channel"),
    [
        (
            [ChannelHealth(stream="market_data", healthy=True, observed_at_ms=now_ms_utc())],
            "missing_channels",
            "execution",
        ),
        (
            [
                ChannelHealth(stream="market_data", healthy=True, observed_at_ms=0),
                ChannelHealth(stream="execution", healthy=True, observed_at_ms=now_ms_utc()),
            ],
            "stale_channels",
            "market_data",
        ),
    ],
)
async def test_deploy_requires_both_fresh_clerk_channels(
    deploy_app,
    monkeypatch,
    channels: list[ChannelHealth],
    evidence_key: str,
    expected_channel: str,
) -> None:
    fast_app, registry = deploy_app

    async def incomplete_channel_status() -> ClerkStatus:
        return ClerkStatus(
            broker="alpaca",
            account_id=ACCT,
            hold=HoldState(active=False),
            outstanding_intents=0,
            observed_at_ms=now_ms_utc(),
            channel_healths=channels,
        )

    monkeypatch.setattr(panel_data_source, "_clerk_status", incomplete_channel_status)
    async with httpx.AsyncClient(transport=ASGITransport(app=fast_app), base_url="http://test") as client:
        response = await client.get(f"/api/brokers/alpaca/accounts/{ACCT}/bots/deploy")

    channel_gate = next(
        check for check in response.json()["readiness_checks"] if check["gate_id"] == "clerk.channel_health"
    )
    assert channel_gate["ready"] is False
    assert channel_gate["evidence"][evidence_key] == expected_channel
    assert registry.deploy_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {**_BODY, "mode": "log_only"},
        {**_BODY, "strategy_key": "unknown"},
        {
            **_BODY,
            "sizing": {"preset": "safe_canary", "quantity": 2},
        },
    ],
)
async def test_deploy_rejects_semantics_outside_closed_contract(
    deploy_app,
    body: dict,
) -> None:
    fast_app, registry = deploy_app

    async with httpx.AsyncClient(transport=ASGITransport(app=fast_app), base_url="http://test") as client:
        resp = await client.post(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots",
            json=body,
        )

    assert resp.status_code == 422
    assert registry.deploy_calls == []


@pytest.mark.asyncio
async def test_carryover_requires_account_policy_and_explicit_deploy_opt_in(
    deploy_app,
    monkeypatch,
) -> None:
    fast_app, registry = deploy_app
    carryover_body = {**_BODY, "carryover_policy": "ALLOW"}

    async with httpx.AsyncClient(transport=ASGITransport(app=fast_app), base_url="http://test") as client:
        blocked = await client.post(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots",
            json=carryover_body,
        )
        monkeypatch.setattr(settings, "ALPACA_PAPER_CARRYOVER_ENABLED", True)
        accepted = await client.post(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots",
            json=carryover_body,
        )

    assert blocked.status_code == 409
    assert accepted.status_code == 201
    assert len(registry.deploy_calls) == 1
    assert registry.deploy_calls[0]["carryover_policy"] == "ALLOW"


@pytest.mark.asyncio
async def test_clerk_hold_authors_blocked_view_and_submission_remedy(
    deploy_app,
    monkeypatch,
) -> None:
    fast_app, registry = deploy_app

    async def held_status() -> ClerkStatus:
        return ClerkStatus(
            broker="alpaca",
            account_id=ACCT,
            hold=HoldState(
                active=True,
                reason_code="UNEXPLAINED_ORDER_HOLD",
                reason="An unattributed broker order requires operator review.",
                since_ms=_T0 - 1,
            ),
            outstanding_intents=0,
            observed_at_ms=_T0,
        )

    monkeypatch.setattr(panel_data_source, "_clerk_status", held_status)
    async with httpx.AsyncClient(transport=ASGITransport(app=fast_app), base_url="http://test") as client:
        view_response = await client.get(f"/api/brokers/alpaca/accounts/{ACCT}/bots/deploy")
        deploy_response = await client.post(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots",
            json=_BODY,
        )

    view = view_response.json()
    assert view["eligibility"]["reason_code"] == "UNEXPLAINED_ORDER_HOLD"
    assert view["allowed_actions"] == []
    assert deploy_response.status_code == 409
    detail = deploy_response.json()["detail"]
    assert detail["why"] == "An unattributed broker order requires operator review."
    assert detail["next_action"]
    assert registry.deploy_calls == []


@pytest.mark.asyncio
async def test_account_freeze_category_and_remedy_reach_deploy_unchanged(
    deploy_app,
    monkeypatch,
) -> None:
    fast_app, registry = deploy_app

    async def frozen_status() -> ClerkStatus:
        return ClerkStatus(
            broker="alpaca",
            account_id=ACCT,
            hold=HoldState(active=False),
            freeze=AccountFreezeState(
                active=True,
                category="ACCOUNT_STATE_UNPROVABLE",
                explanation="Fresh order and exposure truth is unavailable.",
                next_step="Restore broker observation, then reconcile.",
                observed_at_ms=_T0,
            ),
            outstanding_intents=0,
            observed_at_ms=_T0,
        )

    monkeypatch.setattr(panel_data_source, "_clerk_status", frozen_status)
    async with httpx.AsyncClient(transport=ASGITransport(app=fast_app), base_url="http://test") as client:
        response = await client.get(f"/api/brokers/alpaca/accounts/{ACCT}/bots/deploy")

    eligibility = response.json()["eligibility"]
    assert eligibility["reason_code"] == "ACCOUNT_STATE_UNPROVABLE"
    assert eligibility["explanation"] == "Fresh order and exposure truth is unavailable."
    assert eligibility["next_action"] == "Restore broker observation, then reconcile."
    assert registry.deploy_calls == []


@pytest.mark.asyncio
async def test_account_trading_block_authors_ineligible_deploy_view(
    deploy_app,
    monkeypatch,
) -> None:
    fast_app, registry = deploy_app
    monkeypatch.setattr(_FakeAccount, "trading_blocked", True)

    async with httpx.AsyncClient(transport=ASGITransport(app=fast_app), base_url="http://test") as client:
        response = await client.get(f"/api/brokers/alpaca/accounts/{ACCT}/bots/deploy")

    assert response.status_code == 200
    view = response.json()
    assert view["eligibility"]["reason_code"] == "ALPACA_ACCOUNT_NOT_TRADABLE"
    assert view["allowed_actions"] == []
    assert registry.deploy_calls == []
