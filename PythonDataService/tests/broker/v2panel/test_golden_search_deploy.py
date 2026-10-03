"""Deploy offers a stock's READY Golden Search default first, and nothing else changes (#2696).

The default arrives already judged (``qualification_service.ready_defaults``
reads it from the research store); these tests give Deploy that answer and
check what the form offers, which reviewed case it shows, and that the exact
offered tuple deploys. The store-backed read is exercised in
``tests/routers/test_golden_qualifications_endpoints.py``.
"""

from __future__ import annotations

import dataclasses
import logging

import asyncpg
import httpx
import pytest
from httpx import ASGITransport

from app.config import settings
from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.research.golden_search.qualification_service import JudgedQualification, StatusName
from app.research.golden_search.qualifications import QualificationRow, params_sha256
from app.services.broker_v2_panel import panel_deploy
from app.services.broker_v2_panel.paper_deploy_service import _qualified_configuration
from app.services.broker_v2_panel.strategy_catalog import GoldenValidationScope
from tests.broker.v2panel.conftest import _BODY
from tests.broker.v2panel.fixtures import ACCT

PROGRAM = "ema_crossover_signal"
REGISTRATION = _STRATEGY_REGISTRY[PROGRAM]
CONTRACT = REGISTRATION.signal_program_contract
assert CONTRACT is not None
_ALLOW_BODY_STRATEGY = frozenset({(PROGRAM, ACCT)})
_REGISTRY_EXPLANATION_START = "This exact symbol and parameter configuration is covered by the registered"


def _point(symbol: str, **overrides: object) -> dict[str, object]:
    return REGISTRATION.param_schema.model_validate(
        {**CONTRACT.validated_settings, **overrides, "symbol": symbol}
    ).model_dump(mode="json")


def _judged(
    symbol: str = "SPY",
    *,
    status: StatusName = "ready",
    golden_run_id: int = 2,
    qualification_id: str = "gq-0123456789abcdef",
    **overrides: object,
) -> JudgedQualification:
    point = _point(symbol, **(overrides or {"gap": 0.15, "fast_period": 8, "slow_period": 21}))
    row = QualificationRow(
        id=qualification_id,
        program_key=PROGRAM,
        program_version=CONTRACT.program_version,
        parameter_schema_version=CONTRACT.parameter_schema_version,
        symbol=symbol,
        params=point,
        params_sha256=params_sha256(point),
        artifact_digest="1" * 64,
        wiring_digest="2" * 64,
        study_id="study-abcdef0123",
        golden_run_id=golden_run_id,
        golden_review_id=9,
        proof={},
        proof_sha256="3" * 64,
        research={},
        note="Approved after the final test.",
        approved_by="local:owner",
        created_at_ms=1_759_300_000_000,  # 2025-10-01 ET
    )
    return JudgedQualification(qualification=row, events=(), status=status, is_default=True)


def _scope(symbol: str, golden_run_id: int, **overrides: object) -> GoldenValidationScope:
    return GoldenValidationScope(symbol=symbol, parameters=_point(symbol, **overrides), golden_run_id=golden_run_id)


# ---------------------------------------------------------------------------
# The qualified preset
# ---------------------------------------------------------------------------
def test_qualified_configuration_prefers_the_ready_default_with_its_approval_and_study() -> None:
    default = _judged()

    preset = _qualified_configuration(PROGRAM, "SPY", {(PROGRAM, "SPY"): default})

    assert preset is not None
    assert preset.symbol == "SPY"
    assert preset.parameters == {
        name: value for name, value in default.qualification.params.items() if name != "symbol"
    }
    assert preset.golden_qualification_id == default.qualification.id
    assert "study study-ab" in preset.explanation
    assert "approved 2025-10-01" in preset.explanation


@pytest.mark.parametrize("status", ["stale", "revoked", "unverifiable"])
def test_a_default_that_is_not_ready_falls_back_to_the_registry_point(status: StatusName) -> None:
    preset = _qualified_configuration(PROGRAM, "SPY", {(PROGRAM, "SPY"): _judged(status=status)})

    assert preset is not None
    assert preset.golden_qualification_id is None
    assert preset.parameters == {name: value for name, value in _point("SPY").items() if name != "symbol"}
    assert preset.explanation.startswith(_REGISTRY_EXPLANATION_START)


def test_a_ready_default_deploy_would_refuse_falls_back_to_the_registry_point_and_says_why() -> None:
    """#2841: a READY tuple whose periods need more history than a bot can load was still offered, then always refused."""
    program = "sma_crossover"
    registration = _STRATEGY_REGISTRY[program]
    contract = registration.signal_program_contract
    assert contract is not None
    symbol = contract.validated_symbols[0]
    registry_point = registration.param_schema.model_validate({**contract.validated_settings, "symbol": symbol})
    approved = registry_point.model_copy(update={"short_window": 50, "long_window": 500}).model_dump(mode="json")
    default = JudgedQualification(
        qualification=dataclasses.replace(
            _judged(symbol).qualification,
            program_key=program,
            program_version=contract.program_version,
            parameter_schema_version=contract.parameter_schema_version,
            params=approved,
            params_sha256=params_sha256(approved),
        ),
        events=(),
        status="ready",
        is_default=True,
    )

    preset = _qualified_configuration(program, symbol, {(program, symbol): default})

    assert preset is not None
    assert preset.golden_qualification_id is None
    assert preset.parameters == registry_point.model_dump(mode="json", exclude={"symbol"})
    assert preset.explanation.startswith(_REGISTRY_EXPLANATION_START)
    assert f"The Golden Search default for {symbol} is not offered" in preset.explanation
    assert "39 days" in preset.explanation


def test_another_stocks_default_leaves_this_stock_on_the_registry_point() -> None:
    preset = _qualified_configuration(PROGRAM, "QQQ", {(PROGRAM, "SPY"): _judged()})

    assert preset is not None
    assert preset.symbol == "QQQ"
    assert preset.golden_qualification_id is None


def test_without_a_requested_stock_the_default_of_the_first_validated_stock_is_offered() -> None:
    first = CONTRACT.validated_symbols[0]

    preset = _qualified_configuration(PROGRAM, None, {(PROGRAM, first): _judged(first)})

    assert preset is not None
    assert preset.symbol == first
    assert preset.golden_qualification_id is not None


def test_a_default_can_offer_a_stock_outside_the_registry_corpus() -> None:
    preset = _qualified_configuration(PROGRAM, "MSFT", {(PROGRAM, "MSFT"): _judged("MSFT")})

    assert preset is not None
    assert preset.symbol == "MSFT"
    assert preset.golden_qualification_id is not None


# ---------------------------------------------------------------------------
# Golden scope selection
# ---------------------------------------------------------------------------
def test_the_defaults_case_leads_its_own_stock_and_nothing_else_moves() -> None:
    scopes = {
        PROGRAM: (
            _scope("SPY", 3, gap=0.55),
            _scope("TSLA", 4, gap=0.65),
            _scope("SPY", 2, gap=0.15, fast_period=8, slow_period=21),
        )
    }

    ordered = panel_deploy._prefer_default_scopes(scopes, {(PROGRAM, "SPY"): _judged(golden_run_id=2)})

    assert [(scope.symbol, scope.golden_run_id) for scope in ordered[PROGRAM]] == [("SPY", 2), ("SPY", 3), ("TSLA", 4)]


@pytest.mark.parametrize(
    "defaults",
    [{}, {(PROGRAM, "SPY"): _judged(golden_run_id=2, status="stale")}, {(PROGRAM, "SPY"): _judged(golden_run_id=99)}],
    ids=["no-default", "stale-default", "default-case-not-current"],
)
def test_without_a_ready_default_case_the_newest_scope_stays_first(defaults) -> None:
    scopes = {PROGRAM: (_scope("SPY", 3, gap=0.55), _scope("SPY", 2, gap=0.15))}

    ordered = panel_deploy._prefer_default_scopes(scopes, defaults)

    assert [scope.golden_run_id for scope in ordered[PROGRAM]] == [3, 2]


async def test_unreadable_defaults_degrade_to_the_registry_point_and_say_so(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(settings, "POSTGRES_URL", "")

    with caplog.at_level(logging.WARNING, logger=panel_deploy.logger.name):
        defaults = await panel_deploy._ready_golden_defaults()

    assert defaults == {}
    assert any(getattr(record, "action", None) == "golden_search_defaults_unavailable" for record in caplog.records)


async def test_a_dropped_research_connection_degrades_to_the_registry_point(monkeypatch: pytest.MonkeyPatch) -> None:
    async def dropped() -> object:
        raise asyncpg.InterfaceError("connection is closed")

    monkeypatch.setattr(panel_deploy.qualification_service, "ready_defaults", dropped)

    assert await panel_deploy._ready_golden_defaults() == {}


# ---------------------------------------------------------------------------
# The Deploy form and the deploy itself
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_deploy_offers_and_accepts_the_ready_default_tuple(deploy_app, monkeypatch: pytest.MonkeyPatch) -> None:
    fast_app, registry = deploy_app
    monkeypatch.setattr("app.services.canary_admission.CANARY_ADMITTED_PROGRAM_ACCOUNT_PAIRS", _ALLOW_BODY_STRATEGY)
    default = _judged(golden_run_id=2)

    async def ready_defaults() -> dict[tuple[str, str], JudgedQualification]:
        return {(PROGRAM, "SPY"): default}

    async def current_scopes(_symbol: str | None) -> dict[str, tuple[GoldenValidationScope, ...]]:
        # The newest accepted SPY case is someone else's; the default cites the older one.
        return {PROGRAM: (_scope("SPY", 3, gap=0.55), _scope("SPY", 2, gap=0.15, fast_period=8, slow_period=21))}

    monkeypatch.setattr(panel_deploy, "_ready_golden_defaults", ready_defaults)
    monkeypatch.setattr(panel_deploy, "_current_golden_validation_scopes", current_scopes)
    offered = {name: value for name, value in default.qualification.params.items() if name != "symbol"}

    async with httpx.AsyncClient(transport=ASGITransport(app=fast_app), base_url="http://test") as client:
        view = await client.get(f"/api/brokers/alpaca/accounts/{ACCT}/bots/deploy?symbol=SPY")
        deployed = await client.post(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots",
            json={**_BODY, "submission_key": "golden-default-spy", "parameters": offered},
        )

    assert view.status_code == 200, view.text
    strategy = next(row for row in view.json()["strategies"] if row["strategy_key"] == PROGRAM)
    assert strategy["qualified_configuration"]["golden_qualification_id"] == default.qualification.id
    assert strategy["qualified_configuration"]["parameters"] == offered
    assert strategy["validation_case_parameters"] == offered
    assert strategy["golden_validation_scope"] is True
    assert deployed.status_code == 201, deployed.text
    assert registry.deploy_calls[0]["strategy_params"] == offered
