"""Real custody transactions: one consent, one run, disjoint spending, replay."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.broker.alpaca.clerk.budgets import BudgetUnavailable, ReleaseAtStop
from app.broker.alpaca.clerk.live_envelope import AccountObservation, LiveEnvelopeGate
from app.broker.alpaca.clerk.money import MoneyInputError
from app.broker.alpaca.clerk.sealed_ledger import canonical_sha256
from app.broker.alpaca.clerk.sqlite import budget_projection
from app.broker.alpaca.clerk.sqlite.account_risk import AccountRiskPolicy, append_risk_policy
from app.broker.alpaca.clerk.sqlite.budget_authority import commit_budget_authority_cutover
from app.broker.alpaca.clerk.sqlite.budget_commands import submit_budgeted_deploy
from app.broker.alpaca.clerk.sqlite.commands import submit_stop_run
from app.broker.alpaca.clerk.sqlite.database_verification import verify_database
from app.broker.alpaca.clerk.sqlite.day_pnl import day_pnl_window_start_ms
from app.broker.alpaca.clerk.sqlite.economic_projection import EconomicProjectionError
from app.broker.alpaca.clerk.sqlite.enter import accept_enter
from app.broker.alpaca.clerk.sqlite.facts import RunStoppedFacts
from app.broker.alpaca.clerk.sqlite.fee_evidence import FeeEvidenceFacts, record_fee_evidence
from app.broker.alpaca.clerk.sqlite.idempotency import DurableConflictError
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty import AdmissionBlockedError
from app.broker.alpaca.regulatory_fees import RateNotPinnedError
from app.broker.contract.models import BrokerOrderLeg
from app.schemas.exit_terms import ExitTerms
from tests.broker.alpaca.clerk.sqlite.conftest import NOON, _TestClock

TERMS = ExitTerms(band_multiple=2.0, spread_cap_bps=100.0, exit_allowance_bps=5.0, provenance="deployed")


def _new_budget_repo(tmp_path: Path) -> ClerkSqliteRepository:
    repo = ClerkSqliteRepository.initialize(account_id="BUDGET-PAPER", artifacts_root=tmp_path, clock=_TestClock(NOON))
    commit_budget_authority_cutover(repo, actor="owner", reviewed_token="empty-account", stop_receipt="no-runs")
    append_risk_policy(repo, policy=AccountRiskPolicy(1, .1, 100, "profile", 1, "owner", NOON), expected_revision=0)
    record_fee_evidence(repo, [], checked_at_ms=NOON, history_complete=True)
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
        equity_usd=cash, risk_cash_flow_evidence_complete=True,
        risk_cash_flow_window_start_ms=day_pnl_window_start_ms(NOON),
        risk_equity_window_start_ms=day_pnl_window_start_ms(NOON),
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
    # #2555: what the Stop released, and what stayed claimed then, are its own facts.
    stop = next(row for row in budget_repo.custody_transitions() if row["transition_kind"] == "RUN_STOPPED")
    facts = RunStoppedFacts.from_facts_json(stop["facts_json"])
    assert (facts.released_cents, facts.held_cents) == (39_999, 60_001)
    assert expected.deployments[0].release == ReleaseAtStop(released_cents=39_999, held_cents=60_001)
    # The Stop's fold keeps them on the budget row, where every money read takes them.
    assert _release_on_budget_row(budget_repo, "a") == (NOON, 39_999, 60_001)
    database = budget_repo.db_path
    budget_repo.close()
    database.rename(database.with_suffix(".saved"))
    rebuilt = ClerkSqliteRepository.rebuild_from_mirror(account_id="BUDGET-PAPER", artifacts_root=tmp_path, clock=_TestClock(NOON))
    try:
        # Fee freshness is the rebuilt process's own producer read; it adds nothing.
        assert not record_fee_evidence(rebuilt, [], checked_at_ms=NOON, history_complete=True)
        assert rebuilt.account_budget(cash=1000, seen_before_ms=NOON) == expected
        assert _release_on_budget_row(rebuilt, "a") == (NOON, 39_999, 60_001)
        verify_database(rebuilt.db_path, expected_account_id="BUDGET-PAPER")
    finally:
        rebuilt.close()


def _release_on_budget_row(repo: ClerkSqliteRepository, sid: str) -> tuple[int | None, int | None, int | None]:
    row = repo.deployment_budget(sid)
    assert row is not None
    return row["released_at_ms"], row["released_cents"], row["held_cents"]


def _stop_facts(repo: ClerkSqliteRepository) -> RunStoppedFacts:
    stop = next(row for row in repo.custody_transitions() if row["transition_kind"] == "RUN_STOPPED")
    return RunStoppedFacts.from_facts_json(stop["facts_json"])


def _unvaluable(failure: Exception):
    def value_release(*_args: object, **_kwargs: object) -> ReleaseAtStop:
        raise failure

    return value_release


def _validation_error() -> ValidationError:
    with pytest.raises(ValidationError) as caught:
        FeeEvidenceFacts.model_validate_json("{}")
    return caught.value


@pytest.mark.parametrize("failure", [
    pytest.param(lambda: BudgetUnavailable("Fee evidence is unresolved."), id="unresolved-fee-evidence"),
    pytest.param(lambda: EconomicProjectionError("SQLite fill has invalid side 'x'"), id="corrupt-fill-row"),
    pytest.param(lambda: MoneyInputError("not a finite amount"), id="bad-money-value"),
    pytest.param(lambda: RateNotPinnedError(["sec"]), id="unpinned-fee-rate"),
    pytest.param(_validation_error, id="corrupt-fee-evidence"),
])
def test_a_stop_always_commits_when_what_it_releases_cannot_be_valued(
    budget_repo, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, failure,
) -> None:
    """#2555 review: every Stop now values its release, and a corrupt fill row
    or fee-evidence record raised out of the operator's Stop, the sweep's
    retirement and boot recovery alike. A Stop is never refused for money: it
    commits without amounts, and the reason is logged with its traceback."""
    _deploy(budget_repo)
    monkeypatch.setattr(budget_projection, "value_release", _unvaluable(failure()))

    with caplog.at_level(logging.WARNING):
        submit_stop_run(budget_repo, account_id=budget_repo.account_id, strategy_instance_id="a",
                        lifecycle_run_id="run-a", clock=budget_repo.clock)

    assert budget_repo.active_run("a") is None
    assert _stop_facts(budget_repo).released_cents is None
    assert _release_on_budget_row(budget_repo, "a") == (NOON, None, None)
    logged = [record for record in caplog.records if getattr(record, "action", None) == "stop_release_unvalued"]
    assert len(logged) == 1 and logged[0].exc_info is not None
    assert (logged[0].run_id, logged[0].account_id) == ("a:run-a", "BUDGET-PAPER")


async def test_a_clerk_restart_records_what_each_bot_it_stops_released(tmp_path: Path) -> None:
    """#2555 review: boot recovery stops every running bot before this process
    has read any fee evidence, and fee freshness lives in memory, so a Stop
    that demanded fresh evidence recorded no release for any of them. A Stop
    spends nothing: it values its release from the evidence already recorded
    (owner decision 2026-09-29)."""
    from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
    from tests.broker.alpaca.clerk.sqlite.test_reconcile import _FakeRead, _FakeTrade

    before_restart = _new_budget_repo(tmp_path)
    _deploy(before_restart, cents=60_000)
    before_restart.record_deploy_launched(strategy_instance_id="a", lifecycle_run_id="run-a")
    before_restart.close()

    restarted = ClerkSqliteRepository.open(account_id="BUDGET-PAPER", artifacts_root=tmp_path, clock=_TestClock(NOON + 600_000))
    try:
        assert not restarted.account_id.startswith(("sim:", "shadow:"))
        await SqliteAlpacaClerkFacade(account_mode="paper", repo=restarted, read=_FakeRead(), trade=_FakeTrade()).recover()

        assert restarted.active_run("a") is None
        facts = _stop_facts(restarted)
        assert facts.operator_reason == "service_restart_recovery"
        assert (facts.released_cents, facts.held_cents) == (60_000, 0)
        assert _release_on_budget_row(restarted, "a") == (NOON + 600_000, 60_000, 0)
    finally:
        restarted.close()


def test_a_money_read_never_searches_the_journal_for_a_stops_release(budget_repo) -> None:
    """#2555 review: each stopped bot's release was read back by searching the
    whole custody journal, once per deployment, on every money read -- account
    money, budget, ENTER admission and Deploy preview -- under the writer. The
    commitments are read from their own rows, which carry the release."""
    _deploy(budget_repo, cents=30_000)
    submit_stop_run(budget_repo, account_id=budget_repo.account_id, strategy_instance_id="a", lifecycle_run_id="run-a", clock=budget_repo.clock)
    _deploy(budget_repo, sid="b", cents=30_000)
    statements: list[str] = []
    with budget_repo.write_fence() as conn:
        conn.set_trace_callback(statements.append)
        try:
            budget = budget_repo.account_budget(cash=1000, seen_before_ms=NOON)
        finally:
            conn.set_trace_callback(None)

    assert [item.release for item in budget.deployments] == [ReleaseAtStop(30_000, 0), None]
    commitments = [statement for statement in statements if "deployment_budgets" in statement]
    assert commitments and not [statement for statement in commitments if "custody_transitions" in statement]


def test_a_v21_database_takes_its_recorded_releases_onto_its_budget_rows(tmp_path: Path) -> None:
    """Schema v22 (#2555 review): the release columns arrive filled from the
    Stops already recorded, so an upgraded file reads what a rebuild would."""
    import sqlite3

    budget_repo = _new_budget_repo(tmp_path)
    _deploy(budget_repo)
    submit_stop_run(budget_repo, account_id=budget_repo.account_id, strategy_instance_id="a", lifecycle_run_id="run-a", clock=budget_repo.clock)
    database = budget_repo.db_path
    budget_repo.close()
    conn = sqlite3.connect(database)
    try:
        # The v21 shape: the Stop's facts carry the amounts, the row has no columns for them.
        conn.execute("ALTER TABLE deployment_budgets DROP COLUMN held_cents")
        conn.execute("ALTER TABLE deployment_budgets DROP COLUMN released_cents")
        conn.execute("UPDATE control_meta SET schema_version = 21 WHERE id = 1")
        conn.commit()
    finally:
        conn.close()

    upgraded = ClerkSqliteRepository.open(account_id="BUDGET-PAPER", artifacts_root=tmp_path, clock=_TestClock(NOON))
    try:
        assert _release_on_budget_row(upgraded, "a") == (NOON, 100_000, 0)
        verify_database(upgraded.db_path, expected_account_id="BUDGET-PAPER")
    finally:
        upgraded.close()


def test_a_stop_that_records_no_release_keeps_the_facts_bytes_every_earlier_stop_has() -> None:
    """Hash-chained schema evolution (#2555): a Stop of a run with no budget,
    or one whose money could not be valued, is byte-identical to every Stop
    recorded before releases were, so an old chain replays and verifies."""
    unvalued = RunStoppedFacts(idempotency_key="k", payload_hash="h", kind="operator_lifecycle", action="STOP",
                               intended_end_state="STOPPED", lifecycle_run_id="r")
    earlier = '{"action":"STOP","idempotency_key":"k","intended_end_state":"STOPPED","kind":"operator_lifecycle","lifecycle_run_id":"r","operator_reason":null,"payload_hash":"h"}'
    assert unvalued.to_facts_json() == earlier
    assert RunStoppedFacts.from_facts_json(earlier) == unvalued
    valued = replace(unvalued, released_cents=39_999, held_cents=60_001)
    assert RunStoppedFacts.from_facts_json(valued.to_facts_json()) == valued
    with pytest.raises(ValueError, match="both"):
        RunStoppedFacts.from_facts_json(earlier[:-1] + ',"released_cents":1}')


def test_a_stop_that_recorded_no_release_replays_verifies_and_stays_unrecorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#2555: a Stop whose release could not be valued records the bytes every
    earlier Stop has; that chain rebuilds, verifies and reads the same budget,
    its release unrecorded rather than invented."""
    budget_repo = _new_budget_repo(tmp_path)
    _deploy(budget_repo)
    with monkeypatch.context() as patched:
        patched.setattr(budget_projection, "value_release", _unvaluable(BudgetUnavailable("Fee evidence is unresolved.")))
        submit_stop_run(budget_repo, account_id=budget_repo.account_id, strategy_instance_id="a", lifecycle_run_id="run-a", clock=budget_repo.clock)
    assert _stop_facts(budget_repo).released_cents is None
    expected = budget_repo.account_budget(cash=1000, seen_before_ms=NOON)
    assert expected.deployments[0].release is None and not expected.deployments[0].active
    database = budget_repo.db_path
    budget_repo.close()
    database.rename(database.with_suffix(".saved"))
    rebuilt = ClerkSqliteRepository.rebuild_from_mirror(account_id="BUDGET-PAPER", artifacts_root=tmp_path, clock=_TestClock(NOON))
    try:
        record_fee_evidence(rebuilt, [], checked_at_ms=NOON, history_complete=True)
        assert rebuilt.account_budget(cash=1000, seen_before_ms=NOON) == expected
        assert _release_on_budget_row(rebuilt, "a") == (NOON, None, None)
        verify_database(rebuilt.db_path, expected_account_id="BUDGET-PAPER")
    finally:
        rebuilt.close()


def test_a_budgeted_run_cannot_bypass_money_by_omitting_envelope(budget_repo) -> None:
    _deploy(budget_repo)
    with pytest.raises(AdmissionBlockedError):
        accept_enter(
            budget_repo, account_id=budget_repo.account_id, strategy_instance_id="a",
            decision_id="without-money-authority", lifecycle_run_id="run-a",
            leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=1),
        )
    assert budget_repo.account_budget(cash=1000, seen_before_ms=NOON).available == 0


async def test_runtime_registers_budget_before_launch_and_never_relaunches_retry(budget_repo) -> None:
    from types import SimpleNamespace

    from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
    from app.schemas.deployment_budget import DeployBudgetConsent
    from app.services.bot_carryover import configuration_hash
    from tests.broker.alpaca.clerk.sqlite.test_runtime import _binding, _Broker

    binding = _binding().model_copy(update={"strategy_instance_id": "runtime-budget", "sealed_account_id": budget_repo.account_id})
    original_hash = configuration_hash(binding)
    binding = binding.model_copy(update={"budget_consent": DeployBudgetConsent(
        committed_cents=50_000, risk_revision=1, actor="server-owner", request_fingerprint="reviewed", world="real_paper",
    )})
    assert configuration_hash(binding) == original_hash
    broker = _Broker()
    facade = SqliteAlpacaClerkFacade(
        repo=budget_repo, read=broker, trade=broker, account_mode="paper", live_envelope=_gate(),
        quote_source=lambda symbol, now: SimpleNamespace(ask=100),
    )
    await facade.register_strategy_run(binding)
    committed = budget_repo.deployment_budget(binding.strategy_instance_id)
    assert committed["committed_cents"] == 50_000 and committed["launched_at_ms"] is None
    assert not broker.submissions
    with pytest.raises(BudgetUnavailable):
        await facade.register_strategy_run(binding)
    await facade.record_deployment_launch(binding)
    assert budget_repo.deployment_budget(binding.strategy_instance_id)["launched_at_ms"] == NOON
    await facade.stop_strategy_run(strategy_instance_id=binding.strategy_instance_id, run_id=binding.run_id, reason="owner_stop")
    with pytest.raises(BudgetUnavailable):
        await facade.register_strategy_run(binding)
    assert budget_repo.active_run(binding.strategy_instance_id) is None
    assert not broker.submissions
