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

from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.services.broker_v2_panel import panel_deploy
from app.services.broker_v2_panel.strategy_catalog import GoldenValidationScope
from tests.broker.v2panel.conftest import _BODY
from tests.broker.v2panel.fixtures import ACCT

# ema_crossover_signal is a sealed Signal Program (#1730); every test below
# is about deploy-time parameter resolution/validation, not the canary
# allowlist, so each explicitly enables the one pairing `_BODY` deploys
# under before submitting through the route.
_ALLOW_BODY_STRATEGY = frozenset({("ema_crossover_signal", ACCT)})


def _golden_scope(symbol: str = "TSLA", gap: float = 0.75) -> GoldenValidationScope:
    parameters = _STRATEGY_REGISTRY["ema_crossover_signal"].param_schema(symbol=symbol).model_dump(mode="json")
    parameters["gap"] = gap
    return GoldenValidationScope(symbol=symbol, parameters=parameters)


@pytest.mark.asyncio
async def test_deploy_with_no_parameters_resolves_registered_defaults(
    deploy_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fast_app, registry = deploy_app
    monkeypatch.setattr(
        "app.services.canary_admission.CANARY_ADMITTED_PROGRAM_ACCOUNT_PAIRS",
        _ALLOW_BODY_STRATEGY,
    )

    async with httpx.AsyncClient(transport=ASGITransport(app=fast_app), base_url="http://test") as client:
        response = await client.post(f"/api/brokers/alpaca/accounts/{ACCT}/bots", json=_BODY)

    assert response.status_code == 201
    body = response.json()
    assert body["parameters"] == {"gap": 0.20, "gap_bps": 0.0, "rsi_min": 50.0, "rsi_max": 70.0}
    assert body["parameters_diverge_from_defaults"] == []
    assert registry.deploy_calls[0]["strategy_params"] == {"gap": 0.20, "gap_bps": 0.0, "rsi_min": 50.0, "rsi_max": 70.0}


@pytest.mark.asyncio
async def test_deploy_with_an_override_resolves_full_set_and_flags_divergence(
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
            json={**_BODY, "parameters": {"gap": 5.0}},
        )

    assert response.status_code == 201
    body = response.json()
    # The full resolved set, not a sparse diff: unset fields still carry
    # their registered defaults, so a later default change can never
    # silently alter this already-deployed instance's behavior.
    assert body["parameters"] == {"gap": 5.0, "gap_bps": 0.0, "rsi_min": 50.0, "rsi_max": 70.0}
    assert body["parameters_diverge_from_defaults"] == ["gap"]
    assert registry.deploy_calls[0]["strategy_params"]["gap"] == 5.0


@pytest.mark.asyncio
async def test_deploy_accepts_only_the_exact_current_golden_scope_for_broker_mode(
    deploy_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Golden review authorizes its ticker and full resolved parameters, not nearby choices."""
    fast_app, registry = deploy_app
    monkeypatch.setattr(
        "app.services.canary_admission.CANARY_ADMITTED_PROGRAM_ACCOUNT_PAIRS",
        _ALLOW_BODY_STRATEGY,
    )

    async def current_golden_scopes(symbol: str | None) -> dict[str, tuple[GoldenValidationScope, ...]]:
        scope = _golden_scope("SPY", gap=0.55) if symbol == "SPY" else _golden_scope()
        return {"ema_crossover_signal": (scope,)}

    monkeypatch.setattr(panel_deploy, "_current_golden_validation_scopes", current_golden_scopes)
    exact = _golden_scope()
    exact_parameters = {name: value for name, value in exact.parameters.items() if name != "symbol"}

    async with httpx.AsyncClient(transport=ASGITransport(app=fast_app), base_url="http://test") as client:
        view_response = await client.get(f"/api/brokers/alpaca/accounts/{ACCT}/bots/deploy")
        spy_view_response = await client.get(f"/api/brokers/alpaca/accounts/{ACCT}/bots/deploy?symbol=SPY")
        accepted = await client.post(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots",
            json={
                **_BODY,
                "strategy_instance_id": "golden-tsla-accepted",
                "symbol": "TSLA",
                "parameters": exact_parameters,
            },
        )
        rejected = await client.post(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots",
            json={
                **_BODY,
                "strategy_instance_id": "golden-tsla-modified",
                "symbol": "TSLA",
                "parameters": {**exact_parameters, "gap": 0.2},
            },
        )

    strategy = next(
        row for row in view_response.json()["strategies"] if row["strategy_key"] == "ema_crossover_signal"
    )
    assert strategy["validation_case_symbol"] == "TSLA"
    assert strategy["validation_case_parameters"] == exact_parameters
    assert strategy["golden_validation_scope"] is True
    spy_strategy = next(
        row for row in spy_view_response.json()["strategies"] if row["strategy_key"] == "ema_crossover_signal"
    )
    assert spy_strategy["validation_case_symbol"] == "SPY"
    assert spy_strategy["validation_case_parameters"]["gap"] == 0.55
    assert accepted.status_code == 201
    assert registry.deploy_calls[0]["strategy_params"] == exact_parameters
    assert rejected.status_code == 409
    assert rejected.json()["detail"]["message"] == (
        "Broker deployment must use the reviewed Golden Validation configuration."
    )


@pytest.mark.asyncio
async def test_admission_preview_resolves_the_same_parameters_as_deploy(
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
            json={**_BODY, "parameters": {"gap": 5.0}},
        )

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_deploy_rejects_an_invalid_parameter_with_a_clear_message(
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
async def test_deploy_rejects_symbol_submitted_inside_parameters(
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
            # `symbol` is deploy-authoritative (the top-level `symbol` field)
            # and must never be accepted as a tunable, even if a registration
            # never declared it in its own hidden_params.
            json={**_BODY, "parameters": {"symbol": "AAPL"}},
        )

    assert response.status_code == 400
    assert "symbol" in response.json()["detail"]["why"]
    assert registry.deploy_calls == []


@pytest.mark.asyncio
async def test_deploy_rejects_an_unknown_parameter_name(
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
            json={**_BODY, "parameters": {"not_a_real_field": 1}},
        )

    assert response.status_code == 400
    assert registry.deploy_calls == []
