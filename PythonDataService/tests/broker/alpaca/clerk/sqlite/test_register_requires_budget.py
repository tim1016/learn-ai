"""A Deploy needs a budget, and a refused one leaves no bot behind (#3216).

New registration and budget consent fold in one custody write: a Deploy the
Clerk refuses leaves no registered, sealed, never-started instance to list.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.broker.alpaca.clerk.budgets import BudgetUnavailable
from app.broker.alpaca.clerk.live_envelope import OBSERVATION_MAX_AGE_MS, LiveEnvelopeGate
from app.broker.alpaca.clerk.sqlite import budget_commands
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.alpaca.clerk.sqlite.uncertainty import AdmissionBlockedError
from app.schemas.deployment_budget import DeployBudgetConsent
from app.services.bot_binding_repository import BrokerBotBinding
from tests.broker.alpaca.clerk.sqlite.conftest import NOON, _TestClock
from tests.broker.alpaca.clerk.sqlite.test_budget_commands import _gate, _new_budget_repo
from tests.broker.alpaca.clerk.sqlite.test_runtime import _binding, _Broker

SID = "deploy-under-test"


def _consent(*, cents: int = 50_000, world: str = "real_paper", risk_revision: int = 1) -> DeployBudgetConsent:
    return DeployBudgetConsent(
        committed_cents=cents, risk_revision=risk_revision, actor="server-owner", request_fingerprint="reviewed",
        world=world,  # type: ignore[arg-type]
    )


def _deploy_of(repo: ClerkSqliteRepository, consent: DeployBudgetConsent | None) -> BrokerBotBinding:
    binding = _binding().model_copy(update={"strategy_instance_id": SID, "sealed_account_id": repo.account_id})
    return binding.model_copy(update={"budget_consent": consent})


def _facade(
    repo: ClerkSqliteRepository, *, gate: LiveEnvelopeGate | None = None,
    quote: Callable[[str, int], object] = lambda symbol, now: SimpleNamespace(ask=100),
) -> SqliteAlpacaClerkFacade:
    broker = _Broker()
    return SqliteAlpacaClerkFacade(
        repo=repo, read=broker, trade=broker, account_mode="paper", live_envelope=gate, quote_source=quote,
    )


def _nothing_of_the_bot_is_on_the_books(repo: ClerkSqliteRepository) -> None:
    assert repo.strategy_instance(SID) is None
    assert repo.latest_run(SID) is None
    assert repo.deployment_budget(SID) is None
    assert SID not in {row["strategy_instance_id"] for row in repo.strategy_instances()}


@pytest.fixture
def budget_repo(tmp_path: Path):
    repo = _new_budget_repo(tmp_path)
    yield repo
    repo.close()


async def test_a_deploy_that_names_no_budget_is_refused_before_anything_is_registered(budget_repo) -> None:
    transitions = len(budget_repo.custody_transitions())

    with pytest.raises(BudgetUnavailable, match="needs a budget"):
        await _facade(budget_repo, gate=_gate()).register_strategy_run(_deploy_of(budget_repo, None))

    _nothing_of_the_bot_is_on_the_books(budget_repo)
    assert len(budget_repo.custody_transitions()) == transitions


@pytest.mark.parametrize(
    ("reason", "consent", "gate", "quote"),
    [
        pytest.param("the world changed", _consent(world="real_live"), _gate(), None, id="world-changed"),
        pytest.param("the cash authority changed", _consent(), None, None, id="no-cash-authority"),
        pytest.param("no fresh price", _consent(), _gate(), lambda symbol, now: None, id="no-price"),
        pytest.param("over the account's cash", _consent(cents=100_001), _gate(), None, id="budget-over-cash"),
        pytest.param(
            "reading stale", _consent(), _gate(observed_at_ms=NOON - OBSERVATION_MAX_AGE_MS - 1), None,
            id="cash-reading-stale",
        ),
        pytest.param("risk limits changed", _consent(risk_revision=2), _gate(), None, id="risk-limits-changed"),
    ],
)
async def test_a_deploy_the_clerk_refuses_leaves_no_registered_bot(
    budget_repo, reason: str, consent: DeployBudgetConsent, gate: LiveEnvelopeGate | None,
    quote: Callable[[str, int], object] | None,
) -> None:
    facade = _facade(budget_repo, gate=gate) if quote is None else _facade(budget_repo, gate=gate, quote=quote)
    transitions = len(budget_repo.custody_transitions())

    with pytest.raises((BudgetUnavailable, AdmissionBlockedError)):
        await facade.register_strategy_run(_deploy_of(budget_repo, consent))

    _nothing_of_the_bot_is_on_the_books(budget_repo)
    assert len(budget_repo.custody_transitions()) == transitions, reason


async def test_a_deploy_on_an_account_not_switched_to_budgets_leaves_no_registered_bot(tmp_path: Path) -> None:
    repo = ClerkSqliteRepository.initialize(account_id="BUDGET-PAPER", artifacts_root=tmp_path, clock=_TestClock(NOON))
    try:
        with pytest.raises(BudgetUnavailable, match="Switch this account to budgets"):
            await _facade(repo, gate=_gate()).register_strategy_run(_deploy_of(repo, _consent()))

        _nothing_of_the_bot_is_on_the_books(repo)
    finally:
        repo.close()


async def test_a_deploy_command_that_already_exists_is_refused_and_writes_nothing_more(budget_repo) -> None:
    facade = _facade(budget_repo, gate=_gate())
    binding = _deploy_of(budget_repo, _consent())
    await facade.register_strategy_run(binding)
    transitions = len(budget_repo.custody_transitions())

    with pytest.raises(BudgetUnavailable, match="already exists"):
        await facade.register_strategy_run(binding)

    assert len(budget_repo.custody_transitions()) == transitions
    assert budget_repo.deployment_budget(SID)["committed_cents"] == 50_000


async def test_a_new_deploy_cannot_refuse_funding_after_registering_its_bot(
    budget_repo, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second check after registration could refuse on a newly stale cash reading."""
    check_funding = budget_commands._require_funded
    checks = 0

    def fail_a_second_check(*args, **kwargs) -> None:
        nonlocal checks
        checks += 1
        if checks == 2:
            raise BudgetUnavailable("cash reading expired after registration")
        check_funding(*args, **kwargs)

    monkeypatch.setattr(budget_commands, "_require_funded", fail_a_second_check)
    await _facade(budget_repo, gate=_gate()).register_strategy_run(_deploy_of(budget_repo, _consent()))

    assert checks == 1
    assert budget_repo.strategy_instance(SID) is not None
    assert budget_repo.deployment_budget(SID) is not None


async def test_a_new_deploy_needs_no_database_read_after_its_budget_commits(
    budget_repo, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The run owner must be held even if a later exit-terms read would fail."""
    facade = _facade(budget_repo, gate=_gate())

    def fail_exit_terms(_strategy_instance_id: str):
        raise RuntimeError("exit terms read failed")

    monkeypatch.setattr(budget_repo, "exit_terms", fail_exit_terms)
    await facade.register_strategy_run(_deploy_of(budget_repo, _consent()))

    assert budget_repo.active_run(SID) is not None
    assert budget_repo.deployment_budget(SID) is not None
