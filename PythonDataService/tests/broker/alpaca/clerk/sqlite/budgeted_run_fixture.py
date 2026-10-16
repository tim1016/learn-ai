"""A real budgeted Clerk Deploy for repository-level behavior tests."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

from app.broker.alpaca.clerk.et_day import et_day_window_ms
from app.broker.alpaca.clerk.exit_terms import seal_exit_terms
from app.broker.alpaca.clerk.live_envelope import AccountObservation, LiveEnvelopeGate
from app.broker.alpaca.clerk.sealed_ledger import canonical_sha256
from app.broker.alpaca.clerk.sqlite.account_risk import AccountRiskPolicy, append_risk_policy
from app.broker.alpaca.clerk.sqlite.budget_authority import commit_budget_authority_cutover
from app.broker.alpaca.clerk.sqlite.budget_commands import submit_budgeted_deploy
from app.broker.alpaca.clerk.sqlite.commands import CommandSubmission
from app.broker.alpaca.clerk.sqlite.day_pnl import day_pnl_window_start_ms, risk_fill_sequence
from app.broker.alpaca.clerk.sqlite.enter import EnterSubmission, accept_enter, submit_enter
from app.broker.alpaca.clerk.sqlite.fee_evidence import record_fee_evidence
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.marketdata.feed import MarketDataBar
from app.schemas.account_authority import AuthorityKind
from app.schemas.exit_terms import ExitTerms
from app.services.source_bar_ledger import RetainedSourceBar, SourceBarLedger

_TERMS = ExitTerms(band_multiple=2.0, spread_cap_bps=100.0, exit_allowance_bps=5.0, provenance="deployed")
_CASH = 1_000_000_000.0
_BUDGET_CENTS = 100_000_000


def retain_budgeted_test_bar(
    repo: ClerkSqliteRepository, *, root: Path, run_id: str = "run-1", symbol: str = "SPY",
) -> RetainedSourceBar:
    """Retain a priced, closed decision minute for a real budgeted market ENTER."""
    end_ms = repo.clock() // 60_000 * 60_000
    ledger = SourceBarLedger(artifacts_root=root, account_id=repo.account_id)
    try:
        return ledger.append(MarketDataBar(
            feed_id="fixture", symbol=symbol, start_ms=end_ms - 60_000, end_ms=end_ms,
            open=Decimal("100"), high=Decimal("100"), low=Decimal("100"), close=Decimal("100"),
            volume=1, fetched_at_ms=end_ms, session_phase="RTH",
        ), run_id=run_id)
    finally:
        ledger.close()


def budgeted_test_envelope(repo: ClerkSqliteRepository) -> LiveEnvelopeGate:
    """One fresh account read for a repository test's next admitted action."""
    policy = repo.account_risk_policy()
    assert policy is not None
    now = repo.clock()
    simulated = repo.account_id.startswith(("sim:", "shadow:"))
    day_start = et_day_window_ms(now)[0] if simulated else day_pnl_window_start_ms(now)
    gate = LiveEnvelopeGate(values=None, custody_is_simulated=simulated)
    gate.publish(
        AccountObservation(
            observed_at_ms=now,
            broker_cash_usd=_CASH,
            cash_available_usd=_CASH,
            last_equity_usd=_CASH,
            equity_usd=_CASH,
            position_count=0,
            risk_revision=policy.revision,
            risk_fill_sequence=risk_fill_sequence(repo),
            risk_cash_flow_evidence_complete=True,
            risk_cash_flow_window_start_ms=day_start,
            risk_equity_window_start_ms=day_start,
            simulation_session_start_ms=day_start if simulated else None,
        )
    )
    return gate


def accept_budgeted_test_enter(repo: ClerkSqliteRepository, **kwargs: Any) -> EnterSubmission:
    """Supply a fresh budget-authority reading to a direct repository ENTER test."""
    if repo.budget_authority_version() == 2 and repo.account_risk_policy() is not None:
        if not repo.account_id.startswith(("sim:", "shadow:")):
            record_fee_evidence(repo, [], checked_at_ms=repo.clock(), history_complete=True)
        kwargs.setdefault("envelope", budgeted_test_envelope(repo))
        kwargs.setdefault("reference_price", 100.0)
    return accept_enter(repo, **kwargs)


async def submit_budgeted_test_enter(repo: ClerkSqliteRepository, **kwargs: Any) -> EnterSubmission:
    """Supply a fresh budget-authority reading to a direct asynchronous ENTER test."""
    if repo.budget_authority_version() == 2 and repo.account_risk_policy() is not None:
        if not repo.account_id.startswith(("sim:", "shadow:")):
            record_fee_evidence(repo, [], checked_at_ms=repo.clock(), history_complete=True)
        kwargs.setdefault("envelope", budgeted_test_envelope(repo))
        kwargs.setdefault("reference_price", 100.0)
    return await submit_enter(repo, **kwargs)


def submit_budgeted_test_run(
    repo: ClerkSqliteRepository,
    *,
    account_id: str,
    strategy_instance_id: str,
    lifecycle_run_id: str,
    clock: object | None = None,
    committed_cents: int = _BUDGET_CENTS,
    world: AuthorityKind | None = None,
) -> CommandSubmission:
    """Use the production budget gate while supplying one explicit test account reading."""
    if account_id != repo.account_id:
        raise ValueError("test Deploy account must be the repository account")
    if clock is not None and clock is not repo.clock:
        raise ValueError("test Deploy clock must be the repository clock")
    if repo.budget_authority_version() != 2:
        commit_budget_authority_cutover(repo, actor="test-owner", reviewed_token="reviewed", stop_receipt="no-active-runs")
    policy = repo.account_risk_policy()
    if policy is None:
        append_risk_policy(
            repo,
            policy=AccountRiskPolicy(1, 0.1, 100_000_000.0, "test-profile", 1, "test-owner", repo.clock()),
            expected_revision=0,
        )
        policy = repo.account_risk_policy()
    assert policy is not None
    if not repo.account_id.startswith(("sim:", "shadow:")) and repo._fee_evidence_checked_at_ms is None:
        record_fee_evidence(repo, [], checked_at_ms=repo.clock(), history_complete=True)
    instance = repo.strategy_instance(strategy_instance_id)
    if instance is None:
        raise ValueError(f"test Deploy needs a registered strategy instance: {strategy_instance_id}")
    terms = repo.exit_terms(strategy_instance_id)
    if terms is None:
        terms = seal_exit_terms(repo, strategy_instance_id, _TERMS)
    simulated = repo.account_id.startswith(("sim:", "shadow:"))
    resolved_world: AuthorityKind = world or (
        "synthetic" if repo.account_id.startswith("sim:") else "shadow" if simulated else "real_paper"
    )
    return submit_budgeted_deploy(
        repo,
        strategy_instance_id=strategy_instance_id,
        lifecycle_run_id=lifecycle_run_id,
        world=resolved_world,
        committed_cents=committed_cents,
        configuration_hash=str(instance["config_hash"]),
        exit_terms_hash=canonical_sha256(terms.model_dump(mode="json")),
        risk_revision=policy.revision,
        actor="test-owner",
        envelope=budgeted_test_envelope(repo),
        minimum_position_cost=Decimal("0"),
    )
