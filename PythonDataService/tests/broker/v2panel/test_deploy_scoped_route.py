"""Regression tests for the account-scoped deploy alias (§3, §5).

The deploy dialog POSTs to ``/api/brokers/{broker}/accounts/{account_id}/bots``
but only the unscoped ``/{broker}/bots`` route existed — the scoped form
404'd for the *correct* account (found live 2026-07-30, canary run). These
tests pin the scoped alias: correct account delegates to the bot runner,
mismatched account gets the documented typed 404.

The ``deploy_app`` HTTP harness and its fakes live in ``conftest.py``,
shared with ``test_deploy_stale_proof_demotion.py``.
"""

from __future__ import annotations

import httpx
import pytest
from httpx import ASGITransport

from app.broker.alpaca.clerk.models import (
    AccountFreezeState,
    ChannelHealth,
    ClerkStatus,
    HoldState,
)
from app.broker.contract.registry import get_broker_registry
from app.config import settings
from app.services.bot_runner import AdmittedBotStart, BotRunnerError
from app.services.broker_v2_panel import panel_deploy, panel_errors
from app.utils.timestamps import now_ms_utc
from tests.broker.v2panel.conftest import _BODY, _HEALTHY_POSTURE, _T0, account_snapshot
from tests.broker.v2panel.fixtures import ACCT, SID

# ema_crossover_signal is a sealed Signal Program (#1730); most tests below
# are about deploy routing, admission, or account-scoped gates, not the
# canary allowlist, so each explicitly enables the one pairing `_BODY`
# deploys under before submitting through the route.
_ALLOW_BODY_STRATEGY = frozenset({("ema_crossover_signal", ACCT)})


@pytest.mark.asyncio
async def test_deploy_scoped_correct_account_delegates(
    deploy_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fast_app, registry = deploy_app
    monkeypatch.setattr(
        "app.services.canary_admission.CANARY_ADMITTED_PROGRAM_ACCOUNT_PAIRS",
        _ALLOW_BODY_STRATEGY,
    )

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
    assert body["evidence_override"] is None
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
async def test_start_admission_preview_uses_the_request_specific_policy(
    deploy_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fast_app, _registry = deploy_app
    monkeypatch.setattr(
        "app.services.canary_admission.CANARY_ADMITTED_PROGRAM_ACCOUNT_PAIRS",
        _ALLOW_BODY_STRATEGY,
    )

    async with httpx.AsyncClient(transport=ASGITransport(app=fast_app), base_url="http://test") as client:
        response = await client.post(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots/admission",
            json=_BODY,
        )

    assert response.status_code == 200
    assert response.json()["reason_code"] == "START_ADMITTED"
    assert response.json()["strategy_instance_id"] == SID


@pytest.mark.asyncio
async def test_deploy_accepts_dotted_equity_symbol_supported_by_form(
    deploy_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fast_app, registry = deploy_app
    monkeypatch.setattr(
        "app.services.canary_admission.CANARY_ADMITTED_PROGRAM_ACCOUNT_PAIRS",
        _ALLOW_BODY_STRATEGY,
    )

    async with httpx.AsyncClient(transport=ASGITransport(app=fast_app), base_url="http://test") as client:
        response = await client.post(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots",
            json={**_BODY, "strategy_instance_id": "brk-b-validation", "symbol": " brk.b "},
        )

    assert response.status_code == 201
    assert response.json()["action_plan"]["on_enter"][0]["instrument"]["underlying"] == "BRK.B"
    assert registry.deploy_calls[0]["symbol"] == "BRK.B"


@pytest.mark.asyncio
async def test_deploy_submits_selected_validated_strategy(
    deploy_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fast_app, registry = deploy_app
    monkeypatch.setattr(
        "app.services.canary_admission.CANARY_ADMITTED_PROGRAM_ACCOUNT_PAIRS",
        _ALLOW_BODY_STRATEGY,
    )

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
async def test_deploy_view_is_closed_paper_only_contract(
    deploy_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """This is the full deploy-view contract test, so it exercises all three
    of `deploy_app`'s validated strategies -- not the canary allowlist --
    and explicitly enables every (program, account) pairing they deploy
    under (#1730), all sealed Signal Programs."""
    fast_app, _registry = deploy_app
    monkeypatch.setattr(
        "app.services.canary_admission.CANARY_ADMITTED_PROGRAM_ACCOUNT_PAIRS",
        frozenset(
            {
                ("ema_crossover_signal", ACCT),
                ("rsi_mean_reversion", ACCT),
                ("sma_crossover", ACCT),
            }
        ),
    )

    async with httpx.AsyncClient(transport=ASGITransport(app=fast_app), base_url="http://test") as client:
        resp = await client.get(f"/api/brokers/alpaca/accounts/{ACCT}/bots/deploy")

    assert resp.status_code == 200
    body = resp.json()
    assert body["account_mode"] == "paper"
    assert body["allowed_actions"] == ["deploy"]
    # Runtime-supported, human-validated evidence-only strategies are visible
    # and Paper-selectable (#1702): Paper gates on the human-validated flag
    # plus full Clerk custody proof, not on the behavioral verdict.
    assert [row["strategy_key"] for row in body["strategies"]] == [
        "ema_crossover_signal",
        "rsi_mean_reversion",
        "sma_crossover",
    ]
    strategy = body["strategies"][0]
    assert set(strategy) == {
        "strategy_key",
        "label",
        "explanation",
            "validation_case_symbol",
            "evidence_status",
            "paper_access_state",
            "selectable",
        "admissible_modes",
        "override_explanation",
        "blocked_explanation",
        "params_schema",
    }
    assert strategy["validation_case_symbol"] == "SPY"
    assert strategy["evidence_status"] == "accepted"
    assert strategy["paper_access_state"] == "enabled"
    assert strategy["selectable"] is True
    assert strategy["admissible_modes"] == ["dry_run", "paper"]
    assert strategy["override_explanation"] is None
    assert strategy["blocked_explanation"] is None
    # #1701: every registered tunable is present, seeded from its default;
    # `symbol` is deploy-authoritative and is never part of this schema.
    assert set(strategy["params_schema"]["properties"]) == {"gap", "rsi_min", "rsi_max"}
    assert "symbol" not in strategy["params_schema"]["properties"]
    assert [row["evidence_status"] for row in body["strategies"][1:]] == [
        "evidence_only",
        "evidence_only",
    ]
    assert all(row["selectable"] for row in body["strategies"][1:])
    assert all(row["admissible_modes"] == ["dry_run", "paper"] for row in body["strategies"][1:])
    assert all(row["override_explanation"] for row in body["strategies"][1:])
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
    monkeypatch.setattr(panel_deploy, "load_strategy_validation_entries", lambda _registry: [])

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


@pytest.mark.asyncio
async def test_evidence_only_strategy_requires_the_durable_override_for_paper(
    deploy_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operator decision 2026-08-24 (restoring the contract #1702 re-pointed
    at Live): an evidence-only strategy submitted for Paper without the
    durable override is a typed conflict — the override is what makes the
    Start-admission validation fact verifiable, so it cannot be omitted."""
    fast_app, registry = deploy_app
    # sma_crossover is a sealed Signal Program (#1730); this test is about
    # evidence-only Paper admission, not the canary allowlist, so explicitly
    # enable the one pairing it deploys under.
    monkeypatch.setattr(
        "app.services.canary_admission.CANARY_ADMITTED_PROGRAM_ACCOUNT_PAIRS",
        frozenset({("sma_crossover", ACCT)}),
    )

    async with httpx.AsyncClient(
        transport=ASGITransport(app=fast_app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots",
            json={
                **_BODY,
                "strategy_instance_id": "sma-paper-no-override",
                "strategy_key": "sma_crossover",
            },
        )

    assert response.status_code == 409
    assert response.json()["detail"]["message"] == (
        "This evidence-only strategy requires the durable evidence override for Paper deployment."
    )
    assert registry.deploy_calls == []


@pytest.mark.asyncio
async def test_evidence_only_strategy_deploys_to_paper_with_the_durable_override(
    deploy_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operator decision 2026-08-24: with the acknowledgement + reason
    recorded on the request, an evidence-only strategy deploys to Paper and
    the override rides the binding into the runner, where Start admission
    consumes it to verify the ``evidence_only`` validation fact."""
    fast_app, registry = deploy_app
    # sma_crossover is a sealed Signal Program (#1730); this test is about
    # evidence-only Paper admission, not the canary allowlist, so explicitly
    # enable the one pairing it deploys under.
    monkeypatch.setattr(
        "app.services.canary_admission.CANARY_ADMITTED_PROGRAM_ACCOUNT_PAIRS",
        frozenset({("sma_crossover", ACCT)}),
    )
    evidence_override = {
        "acknowledgement": "I_ACCEPT_EVIDENCE_ONLY_DEPLOYMENT_RISK",
        "reason": "Paper canary approved by the strategy owner.",
    }

    async with httpx.AsyncClient(
        transport=ASGITransport(app=fast_app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots",
            json={
                **_BODY,
                "strategy_instance_id": "sma-paper-override",
                "strategy_key": "sma_crossover",
                "evidence_override": evidence_override,
            },
        )

    assert response.status_code == 201
    assert response.json()["bot"]["strategy_key"] == "sma_crossover"
    recorded = registry.deploy_calls[0]["evidence_override"]
    assert recorded is not None
    assert recorded.acknowledgement == "I_ACCEPT_EVIDENCE_ONLY_DEPLOYMENT_RISK"
    assert recorded.reason == "Paper canary approved by the strategy owner."


@pytest.mark.asyncio
async def test_evidence_override_acknowledgement_and_reason_are_closed(deploy_app) -> None:
    fast_app, registry = deploy_app

    async with httpx.AsyncClient(
        transport=ASGITransport(app=fast_app),
        base_url="http://test",
    ) as client:
        wrong_ack = await client.post(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots",
            json={
                **_BODY,
                "strategy_key": "sma_crossover",
                "evidence_override": {
                    "acknowledgement": "I_ACCEPT_THE_RISK",
                    "reason": "Paper canary approved by the strategy owner.",
                },
            },
        )
        short_reason = await client.post(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots",
            json={
                **_BODY,
                "strategy_key": "sma_crossover",
                "evidence_override": {
                    "acknowledgement": "I_ACCEPT_EVIDENCE_ONLY_DEPLOYMENT_RISK",
                    "reason": "too short",
                },
            },
        )

    assert wrong_ack.status_code == 422
    assert short_reason.status_code == 422
    assert registry.deploy_calls == []


@pytest.mark.asyncio
async def test_deploy_refuses_instance_id_that_overflows_the_order_ref_cap(deploy_app) -> None:
    # Every order carries ``learn-ai/{sid}/v1:{intent_id}`` (35 fixed chars)
    # under the order_ref cap (60), so len(sid) > 25 must be a 422 at the
    # deploy boundary. Before this guard, such a bot deployed and ran fine,
    # then CRASHED with OrderRefTooLongError on its first order submission
    # (ceremony-spy-strategy-c-0824, 2026-08-24).
    fast_app, registry = deploy_app

    async with httpx.AsyncClient(
        transport=ASGITransport(app=fast_app),
        base_url="http://test",
    ) as client:
        too_long = await client.post(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots",
            json={**_BODY, "strategy_instance_id": "x" * 26},
        )

    assert too_long.status_code == 422
    assert "order_ref cap" in too_long.text
    assert registry.deploy_calls == []


@pytest.mark.asyncio
async def test_accepted_strategy_rejects_unnecessary_evidence_override(
    deploy_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fast_app, registry = deploy_app
    # ema_crossover_signal is a sealed Signal Program (#1730); this test is
    # about the unnecessary-override conflict, not the canary allowlist, so
    # explicitly enable the one pairing `_BODY` deploys under -- otherwise
    # the request would 409 on "not currently selectable" first.
    monkeypatch.setattr(
        "app.services.canary_admission.CANARY_ADMITTED_PROGRAM_ACCOUNT_PAIRS",
        _ALLOW_BODY_STRATEGY,
    )

    async with httpx.AsyncClient(
        transport=ASGITransport(app=fast_app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots",
            json={
                **_BODY,
                "evidence_override": {
                    "acknowledgement": "I_ACCEPT_EVIDENCE_ONLY_DEPLOYMENT_RISK",
                    "reason": "This should not be accepted for normal evidence.",
                },
            },
        )

    assert response.status_code == 409
    assert response.json()["detail"]["message"] == "An evidence override is not valid for Paper deployment."
    assert registry.deploy_calls == []


@pytest.mark.asyncio
async def test_pre_execution_service_failure_is_blocked_not_unknown(
    deploy_app,
    monkeypatch,
) -> None:
    fast_app, registry = deploy_app

    async def unavailable_clerk(*, symbol: str | None = None) -> ClerkStatus:
        raise panel_errors.PanelUnavailableError(
            "The Clerk is unavailable.",
            detail="No deployment was attempted.",
        )

    monkeypatch.setattr(panel_deploy, "clerk_status", unavailable_clerk)

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
    # ema_crossover_signal is a sealed Signal Program (#1730); this test is
    # about post-dispatch failure classification, not the canary allowlist,
    # so explicitly enable the one pairing `_BODY` deploys under -- the
    # preflight must admit the strategy for the runner to be dispatched at all.
    monkeypatch.setattr(
        "app.services.canary_admission.CANARY_ADMITTED_PROGRAM_ACCOUNT_PAIRS",
        _ALLOW_BODY_STRATEGY,
    )

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
    # ema_crossover_signal is a sealed Signal Program (#1730); this test is
    # about surfacing a runner-refused Start decision, not the canary
    # allowlist, so explicitly enable the one pairing `_BODY` deploys under
    # -- the preflight must admit the strategy for the runner to be
    # dispatched (and then refuse) at all.
    monkeypatch.setattr(
        "app.services.canary_admission.CANARY_ADMITTED_PROGRAM_ACCOUNT_PAIRS",
        _ALLOW_BODY_STRATEGY,
    )
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
    # This test is about the Clerk channel-health gate, not the strategy
    # gate or the canary allowlist -- admit one strategy so the strategy
    # gate is ready and channel health is the actual blocking reason.
    monkeypatch.setattr(
        "app.services.canary_admission.CANARY_ADMITTED_PROGRAM_ACCOUNT_PAIRS",
        _ALLOW_BODY_STRATEGY,
    )

    async def no_channel_status(*, symbol: str | None = None) -> ClerkStatus:
        return ClerkStatus(
            broker="alpaca",
            account_id=ACCT,
            hold=HoldState(active=False),
            outstanding_intents=0,
            observed_at_ms=_T0,
            channel_healths=None,
            operator_posture=_HEALTHY_POSTURE,
        )

    monkeypatch.setattr(panel_deploy, "clerk_status", no_channel_status)
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
    ("present_streams", "evidence_key", "expected_channel"),
    [
        (("market_data",), "missing_channels", "execution"),
        (("market_data", "execution"), "stale_channels", "market_data"),
    ],
)
async def test_deploy_requires_both_fresh_clerk_channels(
    deploy_app,
    monkeypatch,
    present_streams: tuple[str, ...],
    evidence_key: str,
    expected_channel: str,
) -> None:
    fast_app, registry = deploy_app
    # Built per-test, not in the parametrize list: those literals are
    # evaluated once at module import, so a freshness window shorter than
    # the suite's own runtime (it is now 45 s, derived from the hold-sync
    # cadence -- #1777 WP4) would age them into staleness mid-run and make
    # this test order-dependent.
    channels = [
        ChannelHealth(
            stream=stream,
            healthy=True,
            connected=True,
            observed_at_ms=0 if stream == expected_channel else now_ms_utc(),
        )
        for stream in present_streams
    ]

    async def incomplete_channel_status(*, symbol: str | None = None) -> ClerkStatus:
        return ClerkStatus(
            broker="alpaca",
            account_id=ACCT,
            hold=HoldState(active=False),
            outstanding_intents=0,
            observed_at_ms=now_ms_utc(),
            channel_healths=channels,
            operator_posture=_HEALTHY_POSTURE,
        )

    monkeypatch.setattr(panel_deploy, "clerk_status", incomplete_channel_status)
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
async def test_deploy_accepts_a_registry_defined_strategy_key_the_catalog_has_not_validated(
    deploy_app,
) -> None:
    """#1703: the retired enum is replaced by registry-validated keys, not a second enum.

    ``spy_strategy_a`` is a real, catalog-visible registry entry that this
    fixture's validation manifest does not mark validated (only
    ``ema_crossover_signal``, ``rsi_mean_reversion``, and ``sma_crossover``
    are, per ``deploy_app``). The wire boundary accepts it — it is a genuine
    registry key, not the 422 case above — and the request fails downstream
    with the ordinary "not currently accepted" conflict, exactly as an
    unvalidated ``ema_crossover_signal`` request would have failed before
    the enum was retired.
    """
    fast_app, registry = deploy_app

    async with httpx.AsyncClient(transport=ASGITransport(app=fast_app), base_url="http://test") as client:
        resp = await client.post(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots",
            json={**_BODY, "strategy_key": "spy_strategy_a"},
        )

    assert resp.status_code == 409
    assert resp.json()["detail"]["message"] == "The selected strategy is not currently accepted for Alpaca deployment."
    assert registry.deploy_calls == []


@pytest.mark.asyncio
async def test_carryover_is_refused_even_with_account_policy_and_explicit_opt_in(
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
        still_blocked = await client.post(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots",
            json=carryover_body,
        )

    assert blocked.status_code == 409
    assert still_blocked.status_code == 409
    assert registry.deploy_calls == []


@pytest.mark.asyncio
async def test_clerk_hold_authors_blocked_view_and_submission_remedy(
    deploy_app,
    monkeypatch,
) -> None:
    fast_app, registry = deploy_app
    # This test is about the Clerk exposure-hold gate, not the strategy gate
    # or the canary allowlist -- admit `_BODY`'s strategy so the strategy
    # gate is ready and the hold is the actual blocking reason.
    monkeypatch.setattr(
        "app.services.canary_admission.CANARY_ADMITTED_PROGRAM_ACCOUNT_PAIRS",
        _ALLOW_BODY_STRATEGY,
    )

    async def held_status(*, symbol: str | None = None) -> ClerkStatus:
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
            operator_posture=_HEALTHY_POSTURE,
        )

    monkeypatch.setattr(panel_deploy, "clerk_status", held_status)
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
    # This test is about the Clerk custody-freeze gate, not the strategy
    # gate or the canary allowlist -- admit one strategy so the strategy
    # gate is ready and the freeze is the actual blocking reason.
    monkeypatch.setattr(
        "app.services.canary_admission.CANARY_ADMITTED_PROGRAM_ACCOUNT_PAIRS",
        _ALLOW_BODY_STRATEGY,
    )

    async def frozen_status(*, symbol: str | None = None) -> ClerkStatus:
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
            operator_posture=_HEALTHY_POSTURE,
        )

    monkeypatch.setattr(panel_deploy, "clerk_status", frozen_status)
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
    # This test is about the account-posture gate, not the strategy gate or
    # the canary allowlist -- admit one strategy so the strategy gate is
    # ready and the trading block is the actual blocking reason.
    monkeypatch.setattr(
        "app.services.canary_admission.CANARY_ADMITTED_PROGRAM_ACCOUNT_PAIRS",
        _ALLOW_BODY_STRATEGY,
    )
    monkeypatch.setattr(
        get_broker_registry().resolve("alpaca"),
        "account",
        account_snapshot(trading_blocked=True),
    )

    async with httpx.AsyncClient(transport=ASGITransport(app=fast_app), base_url="http://test") as client:
        response = await client.get(f"/api/brokers/alpaca/accounts/{ACCT}/bots/deploy")

    assert response.status_code == 200
    view = response.json()
    assert view["eligibility"]["reason_code"] == "ALPACA_ACCOUNT_NOT_TRADABLE"
    assert view["allowed_actions"] == []
    assert registry.deploy_calls == []


@pytest.mark.asyncio
async def test_dry_run_admits_despite_clerk_hold_and_freeze_while_paper_stays_refused(
    deploy_app,
    monkeypatch,
) -> None:
    """#1702: Clerk custody freeze and exposure hold are "not applicable" to
    Dry Run — it makes no broker contact and holds no custody. Paper remains
    fail-closed under the exact same Clerk facts (regression)."""
    fast_app, registry = deploy_app

    async def frozen_and_held_status(*, symbol: str | None = None) -> ClerkStatus:
        return ClerkStatus(
            broker="alpaca",
            account_id=ACCT,
            hold=HoldState(
                active=True,
                reason_code="UNEXPLAINED_ORDER_HOLD",
                reason="An unattributed broker order requires operator review.",
                since_ms=_T0 - 1,
            ),
            freeze=AccountFreezeState(
                active=True,
                category="ACCOUNT_STATE_UNPROVABLE",
                explanation="Fresh order and exposure truth is unavailable.",
                next_step="Restore broker observation, then reconcile.",
                observed_at_ms=_T0,
            ),
            outstanding_intents=3,
            observed_at_ms=now_ms_utc(),
            channel_healths=[
                ChannelHealth(stream="market_data", healthy=True, connected=True, observed_at_ms=now_ms_utc()),
            ],
            operator_posture=_HEALTHY_POSTURE,
        )

    monkeypatch.setattr(panel_deploy, "clerk_status", frozen_and_held_status)
    async with httpx.AsyncClient(transport=ASGITransport(app=fast_app), base_url="http://test") as client:
        dry_run_response = await client.post(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots",
            json={**_BODY, "execution_mode": "dry_run"},
        )
        paper_response = await client.post(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots",
            json=_BODY,
        )

    assert dry_run_response.status_code == 201
    assert dry_run_response.json()["execution_mode"] == "dry_run"
    assert paper_response.status_code == 409
    assert paper_response.json()["detail"]["outcome"] == "conflict"
    assert len(registry.deploy_calls) == 1
    assert registry.deploy_calls[0]["mode"] == "dry_run"
