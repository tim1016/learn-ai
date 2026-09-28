"""The trader applies risk immediately without mutating saved profile identity."""
from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.broker.alpaca.clerk.active_runtime import ActiveClerkRuntime
from app.broker.alpaca.clerk.live_envelope import LiveEnvelopeGate
from app.broker.alpaca.clerk.sqlite.live_envelope_sync import LiveEnvelopeSync
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker_configuration.service import BrokerConfigurationService
from app.routers import broker_configuration as routes
from tests.broker.alpaca.clerk.sqlite.conftest import NOON, _TestClock, complete_fee_evidence
from tests.broker.alpaca.clerk.sqlite.test_live_envelope_sync import _Read
from tests.broker_configuration.conftest import paper_profile

RiskClient = tuple[AsyncClient, _Read, str]


@pytest.fixture
async def risk_client(service: BrokerConfigurationService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[RiskClient]:
    from app.config import settings
    from app.main import app

    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", "risk-test-control")

    profile = paper_profile(service)
    service.acknowledge_effective(profile_id=profile.profile.profile_id, revision=1,
        account_id="PA-RISK", expected_selection_generation=0)
    repo = ClerkSqliteRepository.initialize(account_id="PA-RISK", artifacts_root=tmp_path / "custody", clock=_TestClock(NOON))
    complete_fee_evidence(repo)
    read = _Read(unrealized=-150)
    sync = LiveEnvelopeSync(repo=repo, read=read, envelope=LiveEnvelopeGate(values=None, custody_is_simulated=False))
    runtime = ActiveClerkRuntime(authority_kind="sqlite", envelope_sync=sync, _sqlite_repository=repo, account_id=repo.account_id)
    app.dependency_overrides[routes.get_broker_configuration_service] = lambda: service
    monkeypatch.setattr(routes, "get_active_clerk_runtime", lambda: runtime)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers={"X-Data-Plane-Control-Secret": "risk-test-control"}) as client:
            yield client, read, profile.profile.profile_id
    finally:
        app.dependency_overrides.pop(routes.get_broker_configuration_service, None)
        await runtime.close()


async def test_apply_is_immediate_receipt_is_effective_and_old_hash_unchanged(risk_client: RiskClient, service: BrokerConfigurationService) -> None:
    client, read, profile_id = risk_client
    original = service.read_revision(profile_id, 1)
    response = await client.get(f"{routes.PREFIX}/risk-limits")
    assert response.status_code == 200, response.text
    state = response.json()
    assert state["entry_state"] == "unknown"
    body = {"expected_risk_revision": 0, "expected_selection_generation": state["selection_generation"], "loss_fraction": .1, "loss_usd": 100}
    result = await client.post(f"{routes.PREFIX}/risk-limits/apply", json=body)
    assert result.status_code == 200, result.text
    assert result.json()["risk_revision"] == 1
    assert result.json()["entry_state"] == "held"
    assert result.json()["hold_policy_revision"] == 1
    assert service.read_revision(profile_id, 1) == original
    conflict = await client.post(f"{routes.PREFIX}/risk-limits/apply", json=body)
    assert conflict.status_code == 409
    raised = await client.post(f"{routes.PREFIX}/risk-limits/apply", json={**body, "expected_risk_revision": 1, "loss_usd": 200})
    assert raised.json()["entry_state"] == "held"
    clear_body = {"expected_risk_revision": 2, "expected_selection_generation": state["selection_generation"]}
    refused = await client.post(f"{routes.PREFIX}/risk-limits/clear-hold", json=clear_body)
    assert refused.status_code == 409
    read.unrealized = -99
    cleared = await client.post(f"{routes.PREFIX}/risk-limits/clear-hold", json=clear_body)
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["entry_state"] == "ready"


async def test_draft_is_inert_stale_configuration_and_fractional_cents_refuse(risk_client: RiskClient, service: BrokerConfigurationService) -> None:
    client, _read, profile_id = risk_client
    response = await client.get(f"{routes.PREFIX}/risk-limits")
    assert response.status_code == 200, response.text
    state = response.json()
    service.create_revision(profile_id, expected_revision=1, credential_slot="alpaca_paper_primary", endpoint_mode="paper", live_envelope=None)
    assert (await client.get(f"{routes.PREFIX}/risk-limits")).json() == state
    body = {"expected_risk_revision": 0, "expected_selection_generation": state["selection_generation"] - 1, "loss_fraction": .1, "loss_usd": 100}
    assert (await client.post(f"{routes.PREFIX}/risk-limits/apply", json=body)).status_code == 409
    assert (await client.post(f"{routes.PREFIX}/risk-limits/apply", json={**body, "loss_usd": 100.001})).status_code == 422
    assert (await client.get(f"{routes.PREFIX}/risk-limits")).json()["risk_revision"] == 0


async def test_failed_observation_reports_effective_but_unknown(risk_client: RiskClient) -> None:
    client, read, _profile_id = risk_client
    response = await client.get(f"{routes.PREFIX}/risk-limits")
    assert response.status_code == 200, response.text
    state = response.json()
    read.fail = True
    result = await client.post(f"{routes.PREFIX}/risk-limits/apply", json={"expected_risk_revision": 0,
        "expected_selection_generation": state["selection_generation"], "loss_fraction": .1, "loss_usd": 100})
    assert result.status_code == 200, result.text
    assert result.json()["risk_revision"] == 1
    assert result.json()["entry_state"] == "unknown"


async def test_risk_read_rejudges_new_fee_evidence_without_writing_a_hold(risk_client: RiskClient) -> None:
    from tests.broker.alpaca.clerk.sqlite.test_fee_evidence import _activity

    client, read, _ = risk_client
    read.unrealized = 0
    state = (await client.get(f"{routes.PREFIX}/risk-limits")).json()
    applied = await client.post(f"{routes.PREFIX}/risk-limits/apply", json={
        "expected_risk_revision": 0, "expected_selection_generation": state["selection_generation"],
        "loss_fraction": .1, "loss_usd": 100,
    })
    assert applied.json()["entry_state"] == "ready"
    runtime = routes.get_active_clerk_runtime()
    repo, gate = runtime.sqlite_repository, runtime.envelope_sync.envelope
    incomplete = _activity("new-unknown-fee", "FEE", NOON, -.05).model_copy(update={"occurred_at_ms": None})
    complete_fee_evidence(repo, (incomplete,))
    before, observation = repo.custody_transitions(), gate.latest_observation()
    response = await client.get(f"{routes.PREFIX}/risk-limits")
    assert response.status_code == 200
    assert response.json()["entry_state"] == "unknown"
    assert "loss evidence is incomplete" in response.json()["detail"]
    assert repo.custody_transitions() == before
    assert gate.latest_observation() == observation
