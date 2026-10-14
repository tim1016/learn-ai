"""Account-scoped Paper-access review and confirmation routes."""

from __future__ import annotations

import httpx
import pytest
from httpx import ASGITransport

from app.broker.contract.registry import get_broker_registry
from app.services.broker_account_snapshot import clear_broker_account_snapshot_cache_for_testing
from app.services.canary_admission import active_canary_pairings, plan_canary_activation
from tests.broker.v2panel.conftest import account_snapshot
from tests.broker.v2panel.fixtures import ACCT


@pytest.mark.asyncio
async def test_prepare_is_read_only_and_confirm_enables_only_the_reviewed_pairing(
    deploy_app,
) -> None:
    fast_app, _registry = deploy_app
    strategy_key = "ema_crossover_signal"
    base = f"/api/brokers/alpaca/accounts/{ACCT}/strategies/{strategy_key}/paper-access"

    async with httpx.AsyncClient(
        transport=ASGITransport(app=fast_app),
        base_url="http://test",
    ) as client:
        before = await client.get(f"/api/brokers/alpaca/accounts/{ACCT}/bots/deploy")
        prepared = await client.post(
            f"{base}/plan",
            json={"reason": "Enable Paper access from the Alpaca Deploy page."},
        )

        assert before.status_code == 200
        assert prepared.status_code == 200
        plan = prepared.json()
        assert plan["program_key"] == strategy_key
        assert plan["account_id"] == ACCT
        assert active_canary_pairings() == frozenset()
        before_states = {
            strategy["strategy_key"]: strategy["paper_access_state"]
            for strategy in before.json()["strategies"]
        }
        assert before_states[strategy_key] == "available"
        # Evidence-only rows are also offered the review (operator decision
        # 2026-08-24) — the pairing review records their durable override.
        assert all(state == "available" for key, state in before_states.items() if key != strategy_key)

        confirmed = await client.post(
            f"{base}/confirm",
            json={
                "plan": plan,
                "confirmation_token": plan["confirmation_token"],
            },
        )
        after = await client.get(f"/api/brokers/alpaca/accounts/{ACCT}/bots/deploy")

    assert confirmed.status_code == 201
    assert confirmed.json()["action"] == "activated"
    assert active_canary_pairings() == frozenset({(strategy_key, ACCT)})
    states = {
        strategy["strategy_key"]: strategy["paper_access_state"]
        for strategy in after.json()["strategies"]
    }
    assert states[strategy_key] == "enabled"
    assert all(state == "available" for key, state in states.items() if key != strategy_key)


@pytest.mark.asyncio
async def test_confirm_refuses_a_plan_posted_through_a_different_strategy_url(
    deploy_app,
) -> None:
    fast_app, _registry = deploy_app
    ema_base = f"/api/brokers/alpaca/accounts/{ACCT}/strategies/ema_crossover_signal/paper-access"
    sma_base = f"/api/brokers/alpaca/accounts/{ACCT}/strategies/sma_crossover/paper-access"

    async with httpx.AsyncClient(
        transport=ASGITransport(app=fast_app),
        base_url="http://test",
    ) as client:
        prepared = await client.post(
            f"{ema_base}/plan",
            json={"reason": "Review the exact EMA pairing."},
        )
        plan = prepared.json()
        response = await client.post(
            f"{sma_base}/confirm",
            json={
                "plan": plan,
                "confirmation_token": plan["confirmation_token"],
            },
        )

    assert prepared.status_code == 200
    assert response.status_code == 409
    assert active_canary_pairings() == frozenset()


@pytest.mark.asyncio
async def test_prepare_preserves_account_scope_validation(deploy_app) -> None:
    fast_app, _registry = deploy_app

    async with httpx.AsyncClient(
        transport=ASGITransport(app=fast_app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/brokers/alpaca/accounts/not-this-account/strategies/"
            "ema_crossover_signal/paper-access/plan",
            json={"reason": "This request must not escape account scope."},
        )

    assert response.status_code == 404
    assert active_canary_pairings() == frozenset()


#: The broker spells its account in lower case; a deep link typed in upper case
#: still validates (account-number case is immaterial), and must not leave a
#: pairing the Deploy view and Start admission can never find.
_BROKER_SPELLING = "pa-0001"
_ROUTE_SPELLING = "PA-0001"
_STRATEGY = "ema_crossover_signal"


def _broker_names_its_account(account_id: str) -> None:
    get_broker_registry().resolve("alpaca").account = account_snapshot(account_id=account_id)  # type: ignore[attr-defined]
    clear_broker_account_snapshot_cache_for_testing()


@pytest.mark.asyncio
async def test_a_pairing_confirmed_through_another_casing_is_recorded_and_shown_under_the_brokers_spelling(
    deploy_app,
) -> None:
    fast_app, _registry = deploy_app
    _broker_names_its_account(_BROKER_SPELLING)
    account = f"/api/brokers/alpaca/accounts/{_ROUTE_SPELLING}"

    async with httpx.AsyncClient(transport=ASGITransport(app=fast_app), base_url="http://test") as client:
        prepared = await client.post(
            f"{account}/strategies/{_STRATEGY}/paper-access/plan", json={"reason": "Enable Paper access."},
        )
        plan = prepared.json()
        confirmed = await client.post(
            f"{account}/strategies/{_STRATEGY}/paper-access/confirm",
            json={"plan": plan, "confirmation_token": plan["confirmation_token"]},
        )
        view = await client.get(f"{account}/bots/deploy")

    assert prepared.status_code == 200
    assert plan["account_id"] == _BROKER_SPELLING
    assert confirmed.status_code == 201
    assert active_canary_pairings() == frozenset({(_STRATEGY, _BROKER_SPELLING)})
    states = {row["strategy_key"]: row["paper_access_state"] for row in view.json()["strategies"]}
    assert states[_STRATEGY] == "enabled"


@pytest.mark.asyncio
async def test_a_plan_reviewed_under_the_routes_other_casing_is_not_confirmed(deploy_app) -> None:
    fast_app, _registry = deploy_app
    _broker_names_its_account(_BROKER_SPELLING)
    stale = plan_canary_activation(
        program_key=_STRATEGY, account_id=_ROUTE_SPELLING, actor="local:test-operator", reason="Enable Paper access.",
    )

    async with httpx.AsyncClient(transport=ASGITransport(app=fast_app), base_url="http://test") as client:
        response = await client.post(
            f"/api/brokers/alpaca/accounts/{_ROUTE_SPELLING}/strategies/{_STRATEGY}/paper-access/confirm",
            json={"plan": stale.model_dump(mode="json"), "confirmation_token": stale.confirmation_token},
        )

    assert response.status_code == 409
    assert response.json()["detail"]["why"] == "the reviewed Paper-access plan does not match this strategy and account"
    assert active_canary_pairings() == frozenset()
