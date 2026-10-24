"""``GET /api/engine/strategy-registry``: every registered strategy and what the registry allows for it (#3508)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.routers.engine import router
from app.schemas.strategy_registry import CapabilityAnswer
from app.services.strategy_registry_capabilities import HARNESS_LIVE_CEILING_REASON, NO_RUNTIME_REASON

URL = "/api/engine/strategy-registry"


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    app = FastAPI()
    app.include_router(router, prefix="/api/engine")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


async def _entries(client: AsyncClient) -> dict[str, dict[str, Any]]:
    response = await client.get(URL)
    assert response.status_code == 200
    return {entry["strategy_key"]: entry for entry in response.json()}


async def test_every_registry_entry_is_listed_including_one_the_picker_hides(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    hidden = replace(_STRATEGY_REGISTRY["sma_crossover"], catalog_visible=False, description="   ")
    monkeypatch.setitem(_STRATEGY_REGISTRY, "compat_sma", hidden)

    entries = await _entries(client)

    assert set(entries) == set(_STRATEGY_REGISTRY)
    assert entries["compat_sma"]["catalog_visible"] is False
    assert entries["sma_crossover"]["catalog_visible"] is True
    assert entries["compat_sma"]["description"] == "", "a missing description stays missing for the reader to say so"


async def test_a_production_candidate_with_a_golden_declaration_is_allowed_every_action(client: AsyncClient) -> None:
    capabilities = (await _entries(client))["ema_crossover_signal"]["capabilities"]

    assert capabilities == {
        "sweep": {"supported": True, "reason": None},
        "golden": {"supported": True, "reason": None},
        "live": {"supported": True, "reason": None},
    }


async def test_a_production_candidate_without_a_golden_declaration_says_why_golden_is_not_offered(
    client: AsyncClient,
) -> None:
    capabilities = (await _entries(client))["sma_crossover"]["capabilities"]

    assert capabilities["sweep"]["supported"] is True
    assert capabilities["live"]["supported"] is True
    assert capabilities["golden"]["supported"] is False
    assert "No Golden Search declaration" in capabilities["golden"]["reason"]


async def test_the_operational_harness_is_refused_a_sweep_and_golden_and_capped_below_live(
    client: AsyncClient,
) -> None:
    entry = (await _entries(client))["deployment_validation"]

    assert entry["strategy_category"] == "operational_validation_harness"
    assert entry["experimental_notice"] is not None
    for action in ("sweep", "golden"):
        assert entry["capabilities"][action]["supported"] is False
        assert "operational harness" in entry["capabilities"][action]["reason"]
    assert entry["capabilities"]["live"] == {"supported": False, "reason": HARNESS_LIVE_CEILING_REASON}


async def test_a_strategy_with_no_signal_program_has_no_program_facts_and_no_runtime(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy = replace(_STRATEGY_REGISTRY["sma_crossover"], signal_program_contract=None)
    monkeypatch.setitem(_STRATEGY_REGISTRY, "legacy_sma", legacy)

    entry = (await _entries(client))["legacy_sma"]

    assert entry["program_version"] is None
    assert entry["rule_statement"] is None
    assert entry["validated_settings"] == {}
    assert entry["validated_symbols"] == []
    assert entry["capabilities"]["live"] == {"supported": False, "reason": NO_RUNTIME_REASON}
    assert entry["capabilities"]["sweep"]["supported"] is False


async def test_the_validated_corpus_point_and_units_are_the_programs_own(client: AsyncClient) -> None:
    contract = _STRATEGY_REGISTRY["ema_crossover_signal"].signal_program_contract
    assert contract is not None

    entry = (await _entries(client))["ema_crossover_signal"]

    assert entry["validated_settings"] == dict(contract.validated_settings)
    assert entry["validated_symbols"] == list(contract.validated_symbols)
    assert entry["parameter_units"] == dict(contract.parameter_units)
    assert entry["program_version"] == contract.program_version
    assert entry["rule_statement"] == contract.numerical_provenance.formula


@pytest.mark.parametrize(("supported", "reason"), [(True, "because"), (False, None)])
def test_an_answer_carries_a_reason_exactly_when_it_is_a_refusal(supported: bool, reason: str | None) -> None:
    with pytest.raises(ValidationError):
        CapabilityAnswer(supported=supported, reason=reason)
