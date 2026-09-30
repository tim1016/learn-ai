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
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE
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


async def test_every_loss_limit_string_on_settings_names_the_cent_the_rule_judged(risk_client: RiskClient) -> None:
    """#2612 review: one hold, one limit, one cent on the paragraph, the explanation and the refusal.

    A 1% limit on a $10,008.50 prior close is the float 100.08500000000001:
    the value the loss rule judged and the sealed hold carries. Every limit
    string on the Settings page renders that float through the money
    boundary, so all three read 100.09. An exact ``Decimal`` twin of the
    limit (100.085, half-even 100.08) once made the clear-hold refusal
    contradict the hold paragraph directly above it.
    """
    client, read, _ = risk_client
    read.cash = read.last_equity = 10_008.50
    read.unrealized = -200.0
    state = (await client.get(f"{routes.PREFIX}/risk-limits")).json()
    applied = await client.post(f"{routes.PREFIX}/risk-limits/apply", json={
        "expected_risk_revision": 0, "expected_selection_generation": state["selection_generation"],
        "loss_fraction": .01, "loss_usd": 1_000,
    })
    assert applied.status_code == 200, applied.text
    assert applied.json()["entry_state"] == "held"

    paragraph = (await client.get(f"{routes.PREFIX}/risk-limits")).json()["hold_loss_limit_usd"]
    hold = routes.get_active_clerk_runtime().sqlite_repository.active_uncertainty(
        scope="ACCOUNT_CLERK", reason_code=LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE, strategy_instance_id=None,
    )
    refused = await client.post(f"{routes.PREFIX}/risk-limits/clear-hold", json={
        "expected_risk_revision": 1, "expected_selection_generation": state["selection_generation"],
    })

    assert paragraph == "100.09"
    assert hold is not None and "against a loss limit of 100.09 USD" in hold["explanation"]
    assert refused.status_code == 409, refused.text
    assert "Day P&L -200.00 USD is still at or below the 100.09 USD loss limit" in refused.json()["detail"]["message"]


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


async def test_canonical_binary_noise_loss_cap_is_accepted_at_the_boundary(risk_client: RiskClient) -> None:
    """#2550: the schema's whole-cent check must agree with require_whole_cent_loss_cap,
    which tolerates up to 4 ULPs of binary noise around a whole-cent amount."""
    client, _read, _profile_id = risk_client
    state = (await client.get(f"{routes.PREFIX}/risk-limits")).json()
    body = {"expected_risk_revision": 0, "expected_selection_generation": state["selection_generation"],
        "loss_fraction": .1, "loss_usd": 99.89999999999999}
    result = await client.post(f"{routes.PREFIX}/risk-limits/apply", json=body)
    assert result.status_code == 200, result.text


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
    # A limit now exists, so the wait is on account evidence a re-read can settle.
    assert result.json()["limit_missing"] is False


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
    assert "account evidence is incomplete" in response.json()["detail"]
    assert repo.custody_transitions() == before
    assert gate.latest_observation() == observation


async def test_an_unapplied_limit_is_named_as_the_cause_not_stale_evidence(risk_client: RiskClient) -> None:
    """#2566 (H6): a Paper account with no limit said "Refresh account evidence",
    which no control could do. The missing limit is the cause, and Settings is
    where it is set, so the read names it in place."""
    client, _read, _ = risk_client
    response = await client.get(f"{routes.PREFIX}/risk-limits")
    assert response.status_code == 200, response.text
    state = response.json()
    assert state["entry_state"] == "unknown"
    assert state["limit_missing"] is True
    assert state["detail"] == (
        "No daily loss limit is set for this account, so new entries are refused. Set one below and apply it."
    )
    assert "Refresh" not in state["detail"]


async def test_readiness_names_a_missing_limit_before_any_observation(risk_client: RiskClient) -> None:
    """#2566 (H7): the shared readiness Deploy reads judged the observation first,
    so a never-set limit surfaced as stale evidence. A missing limit now wins;
    once a limit exists, an absent reading says when the next one comes."""
    from app.broker.alpaca.clerk.sqlite.account_risk import AccountRiskPolicy
    from app.broker.alpaca.clerk.sqlite.risk_admission import current_risk_readiness

    runtime = routes.get_active_clerk_runtime()
    repo, sync = runtime.sqlite_repository, runtime.envelope_sync
    missing = current_risk_readiness(repo, envelope=sync.envelope, now_ms=repo.clock())
    assert not missing.allowed and missing.limit_missing
    assert missing.detail.endswith("Set one in Settings.")

    sync.discard_observation()
    sync.apply_risk_policy(AccountRiskPolicy(revision=1, loss_fraction=.1, loss_usd=100, profile_id="p", profile_revision=1,
        actor="owner", applied_at_ms=repo.clock()), expected_revision=0)
    unobserved = current_risk_readiness(repo, envelope=sync.envelope, now_ms=repo.clock())
    assert not unobserved.allowed and not unobserved.limit_missing
    assert "Refresh" not in unobserved.detail
    assert unobserved.detail.endswith("The account is read again every 15 seconds.")
