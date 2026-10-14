"""A run with no budget, for the suites that exercise a run once it exists (#3216).

A Deploy sets a budget aside or it does not start, so
``SqliteAlpacaClerkFacade.register_strategy_run`` refuses a binding that
carries no ``budget_consent`` (``DeployBudgetRequiredError``). The suites that
use the ``legacy_run_registration`` fixture prove what the Clerk does with a
run -- ENTER, EXIT, recovery, the sweep -- on a store that predates budgets,
which is what an earlier deployment's store still is and what
``submit_start_run`` still writes. They are not about how a run was funded,
and a budget there would need an envelope reading, a risk policy and cash that
none of them is about.

With the fixture, a binding with no consent starts its run as a Start did
before: the bot's sealed registration, then ``RUN_STARTED``. A binding with a
consent goes through the production path, untouched. Every refusal that
precedes the budget (the sealed account, the admission snapshot, a conflicting
registration) is the production method's own: it runs first.
"""

from __future__ import annotations

import pytest

from app.broker.alpaca.clerk.sqlite.commands import submit_start_run
from app.broker.alpaca.clerk.sqlite.hashchain import canonicalize
from app.broker.alpaca.clerk.sqlite.run_ownership import RunOwner
from app.broker.alpaca.clerk.sqlite.runtime import (
    DeployBudgetRequiredError,
    SqliteAlpacaClerkFacade,
    StrategyRegistrationConflictError,
    _strategy_display_name,
)
from app.services.bot_binding_repository import BrokerBotBinding
from app.services.bot_carryover import configuration_hash, immutable_configuration_payload


async def start_run_without_a_budget(
    facade: SqliteAlpacaClerkFacade, binding: BrokerBotBinding, *, run_owner: RunOwner | None,
) -> None:
    """Register ``binding``'s bot if it is new and start its run, with no budget behind it."""
    repo = facade.repository
    sid = binding.strategy_instance_id
    async with facade.intake:
        if repo.strategy_instance(sid) is None:
            repo.register_strategy_instance(
                strategy_instance_id=sid,
                symbol=facade._symbol_to_register(binding.action_plan),
                config_hash=configuration_hash(binding),
                strategy_key=binding.strategy_key,
                display_name=_strategy_display_name(binding.strategy_key),
                config_json=canonicalize(immutable_configuration_payload(binding)),
                exit_terms=binding.exit_terms,
            )
        facade._exit_terms[sid] = facade._sealed_exit_terms(binding)
        submission = submit_start_run(
            repo, account_id=facade.account_id, strategy_instance_id=sid, lifecycle_run_id=binding.run_id,
        )
        if submission.command.state != "succeeded":
            raise StrategyRegistrationConflictError(f"SQLite authority rejected lifecycle run {binding.run_id!r}")
        facade._hold_run(binding, run_owner)


@pytest.fixture
def legacy_run_registration(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let a binding with no consent start its run on a store that predates budgets (see the module)."""
    registered = SqliteAlpacaClerkFacade.register_strategy_run

    async def register_strategy_run(
        self: SqliteAlpacaClerkFacade, binding: BrokerBotBinding, *, admission_snapshot: object = None, run_owner: RunOwner | None = None,
    ) -> None:
        try:
            await registered(self, binding, admission_snapshot=admission_snapshot, run_owner=run_owner)
        except DeployBudgetRequiredError:
            await start_run_without_a_budget(self, binding, run_owner=run_owner)

    monkeypatch.setattr(SqliteAlpacaClerkFacade, "register_strategy_run", register_strategy_run)
