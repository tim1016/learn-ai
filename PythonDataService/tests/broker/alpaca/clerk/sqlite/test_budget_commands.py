"""Real custody transactions: one consent, one run, disjoint spending, replay."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.live_envelope import AccountObservation, LiveEnvelopeGate
from app.broker.alpaca.clerk.sealed_ledger import canonical_sha256
from app.broker.alpaca.clerk.sqlite.account_risk import AccountRiskPolicy, append_risk_policy
from app.broker.alpaca.clerk.sqlite.budget_commands import submit_budgeted_deploy
from app.broker.alpaca.clerk.sqlite.budget_projection import BudgetUnavailable
from app.broker.alpaca.clerk.sqlite.commands import submit_stop_run
from app.broker.alpaca.clerk.sqlite.enter import accept_enter
from app.broker.alpaca.clerk.sqlite.fee_evidence import record_fee_evidence
from app.broker.alpaca.clerk.sqlite.idempotency import DurableConflictError
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty import AdmissionBlockedError
from app.broker.contract.models import BrokerOrderLeg
from app.schemas.exit_terms import ExitTerms
from tests.broker.alpaca.clerk.sqlite.conftest import NOON, _TestClock

TERMS = ExitTerms(band_multiple=2.0, spread_cap_bps=100.0, exit_allowance_bps=5.0, provenance="deployed")


def _new_budget_repo(tmp_path: Path) -> ClerkSqliteRepository:
    repo = ClerkSqliteRepository.initialize(account_id="BUDGET-PAPER", artifacts_root=tmp_path, clock=_TestClock(NOON))
    append_risk_policy(repo, policy=AccountRiskPolicy(1, .1, 100, "profile", 1, "owner", NOON), expected_revision=0)
    record_fee_evidence(repo, [], checked_at_ms=NOON)
    for sid in ("a", "b"):
        repo.register_strategy_instance(strategy_instance_id=sid, symbol="SPY", config_hash=f"seal-{sid}", exit_terms=TERMS)
    return repo


@pytest.fixture
def budget_repo(tmp_path: Path):
    repo = _new_budget_repo(tmp_path)
    yield repo
    repo.close()


def _gate(cash: float = 1000) -> LiveEnvelopeGate:
    gate = LiveEnvelopeGate(values=None, custody_is_simulated=False)
    gate.publish(AccountObservation(
        observed_at_ms=NOON, broker_cash_usd=cash, cash_available_usd=cash,
        last_equity_usd=cash, unrealized_pl_usd=0, position_count=0, risk_revision=1,
    ))
    return gate


def _deploy(repo: ClerkSqliteRepository, sid: str = "a", cents: int = 100_000):
    return submit_budgeted_deploy(
        repo, strategy_instance_id=sid, lifecycle_run_id=f"run-{sid}", world="real_paper",
        committed_cents=cents, configuration_hash=f"seal-{sid}", exit_terms_hash=canonical_sha256(TERMS.model_dump(mode="json")),
        risk_revision=1, actor="owner", envelope=_gate(), minimum_position_cost=Decimal("100.01"),
    )


def test_duplicate_deploy_returns_same_pending_and_launched_command(budget_repo) -> None:
    first = _deploy(budget_repo)
    retry = _deploy(budget_repo)
    assert first.created and not retry.created
    assert first.command.command_id == retry.command.command_id
    assert retry.command.state == "accepted"
    budget_repo.record_deploy_launched(strategy_instance_id="a", lifecycle_run_id="run-a")
    assert _deploy(budget_repo).command.state == "succeeded"
    assert len([row for row in budget_repo.custody_transitions() if row["transition_kind"] == "DEPLOY_COMMITTED"]) == 1


def test_response_loss_then_changed_amount_is_a_conflict(budget_repo) -> None:
    _deploy(budget_repo, cents=60_000)
    with pytest.raises(DurableConflictError):
        _deploy(budget_repo, cents=70_000)
    assert budget_repo.deployment_budget("a")["committed_cents"] == 60_000


def test_two_concurrent_deploys_cannot_both_claim_the_pool(budget_repo) -> None:
    def deploy(sid: str) -> bool:
        try:
            return _deploy(budget_repo, sid=sid, cents=60_000).created
        except BudgetUnavailable:
            return False

    with ThreadPoolExecutor(max_workers=2) as workers:
        outcomes = list(workers.map(deploy, ["a", "b"]))
    assert sum(outcomes) == 1
    assert budget_repo.account_budget(cash=1000, seen_before_ms=NOON).unreserved_cents == 40_000


def test_failed_start_releases_but_retry_never_restarts(budget_repo) -> None:
    _deploy(budget_repo)
    submit_stop_run(budget_repo, account_id=budget_repo.account_id, strategy_instance_id="a", lifecycle_run_id="run-a", operator_reason="startup_failed", clock=budget_repo.clock)
    retry = _deploy(budget_repo)
    assert not retry.created and retry.command.state == "failed"
    assert budget_repo.active_run("a") is None
    assert budget_repo.account_budget(cash=1000, seen_before_ms=NOON).unreserved_cents == 100_000
    with pytest.raises(BudgetUnavailable):
        budget_repo.record_deploy_launched(strategy_instance_id="a", lifecycle_run_id="run-a")


def test_pending_enter_does_not_make_budget_available_to_sibling(budget_repo) -> None:
    _deploy(budget_repo)
    accepted = accept_enter(
        budget_repo, account_id=budget_repo.account_id, strategy_instance_id="a",
        decision_id="buy", lifecycle_run_id="run-a", leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=6),
        reference_price=100, envelope=_gate(),
    )
    assert accepted.created
    projection = budget_repo.account_budget(cash=1000, seen_before_ms=NOON)
    assert projection.available == 0
    assert projection.deployments[0].free == Decimal("399.99")
    with pytest.raises(BudgetUnavailable):
        _deploy(budget_repo, sid="b", cents=60_000)
    with pytest.raises(AdmissionBlockedError):
        accept_enter(
            budget_repo, account_id=budget_repo.account_id, strategy_instance_id="a", decision_id="too-much",
            lifecycle_run_id="run-a", leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=4), reference_price=100, envelope=_gate(),
        )


def test_stop_retains_order_claim_and_mirror_rebuild_keeps_it(tmp_path: Path) -> None:
    budget_repo = _new_budget_repo(tmp_path)
    _deploy(budget_repo)
    accept_enter(
        budget_repo, account_id=budget_repo.account_id, strategy_instance_id="a", decision_id="buy",
        lifecycle_run_id="run-a", leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=6), reference_price=100, envelope=_gate(),
    )
    submit_stop_run(budget_repo, account_id=budget_repo.account_id, strategy_instance_id="a", lifecycle_run_id="run-a", clock=budget_repo.clock)
    expected = budget_repo.account_budget(cash=1000, seen_before_ms=NOON)
    assert expected.available == Decimal("399.99")
    database = budget_repo.db_path
    budget_repo.close()
    database.rename(database.with_suffix(".saved"))
    rebuilt = ClerkSqliteRepository.rebuild_from_mirror(account_id="BUDGET-PAPER", artifacts_root=tmp_path, clock=_TestClock(NOON))
    try:
        assert rebuilt.account_budget(cash=1000, seen_before_ms=NOON) == expected
        assert rebuilt.deployment_budget("a")["released_at_ms"] == NOON
    finally:
        rebuilt.close()
