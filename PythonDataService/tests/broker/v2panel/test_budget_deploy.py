"""Trader money review uses custody facts and immutable consent, not UI math."""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.broker.alpaca.clerk.live_envelope import AccountObservation, LiveEnvelopeGate
from app.broker.alpaca.clerk.sealed_ledger import canonical_sha256
from app.broker.alpaca.clerk.sqlite.account_risk import AccountRiskPolicy, append_risk_policy
from app.broker.alpaca.clerk.sqlite.budget_authority import commit_budget_authority_cutover
from app.broker.alpaca.clerk.sqlite.budget_commands import submit_budgeted_deploy
from app.broker.alpaca.clerk.sqlite.budget_projection import BudgetUnavailable
from app.broker.alpaca.clerk.sqlite.fee_evidence import record_fee_evidence
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.schemas.broker_bots import AlpacaPaperDeployRequest
from app.schemas.deployment_budget import DeploymentBudgetInput
from app.schemas.exit_terms import ExitTermsInput
from app.services.broker_v2_panel import budget_deploy
from tests.broker.alpaca.clerk.sqlite.conftest import NOON, _TestClock


def _request(**updates) -> AlpacaPaperDeployRequest:
    return AlpacaPaperDeployRequest(
        strategy_instance_id="review-a", strategy_key="deployment_validation", symbol="SPY",
        exit_terms=ExitTermsInput(band_multiple=2, spread_cap_bps=100, exit_allowance_bps=5),
        **updates,
    )


@pytest.fixture
def authority(tmp_path, monkeypatch):
    repo = ClerkSqliteRepository.initialize(account_id="BUDGET-PAPER", artifacts_root=tmp_path, clock=_TestClock(NOON))
    policy = AccountRiskPolicy(1, .1, 100, "profile", 1, "owner", NOON)
    commit_budget_authority_cutover(repo, actor="owner", reviewed_token="empty-account", stop_receipt="no-runs")
    append_risk_policy(repo, policy=policy, expected_revision=0)
    record_fee_evidence(repo, [], checked_at_ms=NOON, history_complete=True)
    observation = AccountObservation(observed_at_ms=NOON, broker_cash_usd=1000, cash_available_usd=1000, last_equity_usd=1000, unrealized_pl_usd=0, position_count=0, risk_revision=1)
    snapshot = SimpleNamespace(observation=observation, policy=policy, hold=None)
    runtime = SimpleNamespace(selected_account_id=repo.account_id, account_authority_kind="real_paper", sqlite_repository=repo, envelope_sync=SimpleNamespace(risk_snapshot=lambda: snapshot))
    monkeypatch.setattr(budget_deploy, "get_active_clerk_runtime", lambda: runtime)
    monkeypatch.setattr(budget_deploy, "get_clerk_runtime", lambda account_id: None)
    monkeypatch.setattr(budget_deploy, "get_market_liveness_store", lambda: SimpleNamespace(top_of_book=lambda **kwargs: SimpleNamespace(ask=100.001, observed_at_ms=NOON)))
    monkeypatch.setattr(budget_deploy, "get_broker_configuration_service", lambda: SimpleNamespace(owner=lambda: SimpleNamespace(owner_id="server-owner")))
    yield repo, runtime, snapshot
    repo.close()


def test_preview_shortcuts_are_server_authored_and_round_at_boundary(authority) -> None:
    preview = budget_deploy.preview_budget("BUDGET-PAPER", _request(), resolved_parameters={})
    assert preview.state == "ready"
    assert preview.minimum_budget_usd == "100.02"
    assert {row.key: row.amount_usd for row in preview.shortcuts} == {"quarter": "250.00", "half": "500.00", "all": "1000.00", "position_headroom": "120.02"}
    assert preview.review_token is None


def test_confirmation_binds_amount_effective_parameters_and_risk(authority) -> None:
    request = _request(budget=DeploymentBudgetInput(amount_usd="500", risk_revision=1))
    preview = budget_deploy.preview_budget("BUDGET-PAPER", request, resolved_parameters={"period": 5})
    reviewed = request.model_copy(update={"budget": request.budget.model_copy(update={"review_token": preview.review_token})})
    assert budget_deploy.resolve_consent("BUDGET-PAPER", reviewed, resolved_parameters={"period": 5}).actor == "server-owner"
    with pytest.raises(BudgetUnavailable):
        budget_deploy.resolve_consent("BUDGET-PAPER", reviewed, resolved_parameters={"period": 6})
    changed = reviewed.model_copy(update={"budget": reviewed.budget.model_copy(update={"amount_usd": "600.00"})})
    with pytest.raises(BudgetUnavailable):
        budget_deploy.resolve_consent("BUDGET-PAPER", changed, resolved_parameters={"period": 5})
    authority[2].policy = replace(authority[2].policy, revision=2)
    assert budget_deploy.preview_budget("BUDGET-PAPER", reviewed, resolved_parameters={}).state == "unavailable"


def test_dry_run_never_copies_parent_cash_or_risk(authority) -> None:
    authority[2].observation = None
    request = _request(execution_mode="dry_run", budget=DeploymentBudgetInput(amount_usd="2000", risk_revision=0))
    preview = budget_deploy.preview_budget("BUDGET-PAPER", request, resolved_parameters={})
    assert preview.state == "ready" and preview.world == "synthetic"
    assert preview.custody_account_id == "sim:review-a" and preview.unreserved_usd is None
    assert [item.key for item in preview.shortcuts] == ["position_headroom"]


def test_recovery_returns_committed_outcome_even_after_evidence_expires(authority) -> None:
    repo, _, snapshot = authority
    request = _request(budget=DeploymentBudgetInput(amount_usd="500", risk_revision=1))
    terms = request.exit_terms.seal()
    repo.register_strategy_instance(strategy_instance_id=request.strategy_instance_id, symbol="SPY", config_hash="seal", exit_terms=terms)
    gate = LiveEnvelopeGate(values=None, custody_is_simulated=False)
    gate.publish(snapshot.observation)
    submit_budgeted_deploy(repo, strategy_instance_id=request.strategy_instance_id, lifecycle_run_id="run", world="real_paper", committed_cents=50_000, configuration_hash="seal", exit_terms_hash=canonical_sha256(terms.model_dump(mode="json")), risk_revision=1, actor="owner", envelope=gate, minimum_position_cost=Decimal(100), request_fingerprint=budget_deploy.request_fingerprint(request, custody_account_id=repo.account_id, world="real_paper"))
    snapshot.observation = None
    receipt = budget_deploy.command_receipt(repo.account_id, request.strategy_instance_id, request)
    assert receipt.status == "pending" and receipt.committed_usd == "500.00"
    altered = request.model_copy(update={"symbol": "QQQ"})
    with pytest.raises(BudgetUnavailable):
        budget_deploy.command_receipt(repo.account_id, request.strategy_instance_id, altered)


def test_fractional_cent_consent_is_rejected_at_wire_boundary() -> None:
    with pytest.raises(ValidationError):
        DeploymentBudgetInput(amount_usd="100.001", risk_revision=0)
