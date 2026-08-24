"""Account-scoped Paper-access review and confirmation routes."""

from __future__ import annotations

import httpx
import pytest
from httpx import ASGITransport

from app.services.canary_admission import active_canary_pairings
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
