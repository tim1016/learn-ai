"""HTTP-route tests for deploy-time strategy configuration (#1701).

Every registered tunable is editable at deploy time, validated by the
strategy's own registered `param_schema` — the same schema Engine Lab and
Strategy Lab already validate against. `symbol` is always deploy-authoritative
and is never a submittable tunable. The deploy receipt states which resolved
fields diverge from the strategy's registered defaults (informational only).

The ``deploy_app`` HTTP harness and its fakes live in ``conftest.py``, shared
with ``test_deploy_scoped_route.py`` / ``test_deploy_stale_proof_demotion.py``.
"""

from __future__ import annotations

import httpx
import pytest
from httpx import ASGITransport

from tests.broker.v2panel.conftest import _BODY
from tests.broker.v2panel.fixtures import ACCT


@pytest.mark.asyncio
async def test_deploy_with_no_parameters_resolves_registered_defaults(deploy_app) -> None:
    fast_app, registry = deploy_app

    async with httpx.AsyncClient(transport=ASGITransport(app=fast_app), base_url="http://test") as client:
        response = await client.post(f"/api/brokers/alpaca/accounts/{ACCT}/bots", json=_BODY)

    assert response.status_code == 201
    body = response.json()
    assert body["parameters"] == {"gap": 0.20, "rsi_min": 50.0, "rsi_max": 70.0}
    assert body["parameters_diverge_from_defaults"] == []
    assert registry.deploy_calls[0]["strategy_params"] == {"gap": 0.20, "rsi_min": 50.0, "rsi_max": 70.0}


@pytest.mark.asyncio
async def test_deploy_with_an_override_resolves_full_set_and_flags_divergence(deploy_app) -> None:
    fast_app, registry = deploy_app

    async with httpx.AsyncClient(transport=ASGITransport(app=fast_app), base_url="http://test") as client:
        response = await client.post(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots",
            json={**_BODY, "parameters": {"gap": 5.0}},
        )

    assert response.status_code == 201
    body = response.json()
    # The full resolved set, not a sparse diff: unset fields still carry
    # their registered defaults, so a later default change can never
    # silently alter this already-deployed instance's behavior.
    assert body["parameters"] == {"gap": 5.0, "rsi_min": 50.0, "rsi_max": 70.0}
    assert body["parameters_diverge_from_defaults"] == ["gap"]
    assert registry.deploy_calls[0]["strategy_params"]["gap"] == 5.0


@pytest.mark.asyncio
async def test_admission_preview_resolves_the_same_parameters_as_deploy(deploy_app) -> None:
    fast_app, _registry = deploy_app

    async with httpx.AsyncClient(transport=ASGITransport(app=fast_app), base_url="http://test") as client:
        response = await client.post(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots/admission",
            json={**_BODY, "parameters": {"gap": 5.0}},
        )

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_deploy_rejects_an_invalid_parameter_with_a_clear_message(deploy_app) -> None:
    fast_app, registry = deploy_app

    async with httpx.AsyncClient(transport=ASGITransport(app=fast_app), base_url="http://test") as client:
        response = await client.post(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots",
            # rsi_min must be < 100 (le=100.0) — this violates the same
            # schema Engine Lab and Strategy Lab validate against.
            json={**_BODY, "parameters": {"rsi_min": 200}},
        )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["message"] == "The submitted strategy parameters are invalid."
    assert "rsi_min" in detail["why"]
    assert registry.deploy_calls == []


@pytest.mark.asyncio
async def test_deploy_rejects_symbol_submitted_inside_parameters(deploy_app) -> None:
    fast_app, registry = deploy_app

    async with httpx.AsyncClient(transport=ASGITransport(app=fast_app), base_url="http://test") as client:
        response = await client.post(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots",
            # `symbol` is deploy-authoritative (the top-level `symbol` field)
            # and must never be accepted as a tunable, even if a registration
            # never declared it in its own hidden_params.
            json={**_BODY, "parameters": {"symbol": "AAPL"}},
        )

    assert response.status_code == 400
    assert "symbol" in response.json()["detail"]["why"]
    assert registry.deploy_calls == []


@pytest.mark.asyncio
async def test_deploy_rejects_an_unknown_parameter_name(deploy_app) -> None:
    fast_app, registry = deploy_app

    async with httpx.AsyncClient(transport=ASGITransport(app=fast_app), base_url="http://test") as client:
        response = await client.post(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots",
            json={**_BODY, "parameters": {"not_a_real_field": 1}},
        )

    assert response.status_code == 400
    assert registry.deploy_calls == []
