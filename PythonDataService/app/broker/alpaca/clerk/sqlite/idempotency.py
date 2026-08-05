"""Shared content-addressed-identity errors for command domain modules.

``commands.py`` (local Start/Stop, #1376) and ``enter.py`` (#1377) both
submit through :meth:`ClerkSqliteRepository.commit_first_transition` and
need the same three outcomes translated to the same exceptions — kept here
once rather than each domain module defining its own.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.broker.alpaca.clerk.sqlite.models import CommandResource, RunResource

if TYPE_CHECKING:
    from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository


class InvalidIdentityError(ValueError):
    """A caller-supplied identity component cannot safely form the
    colon-delimited idempotency key (#1376 review)."""


def reject_colon(field_name: str, value: str) -> None:
    if ":" in value:
        raise InvalidIdentityError(
            f"{field_name} must not contain ':' — it is embedded in a colon-delimited "
            f"idempotency key, got {value!r}"
        )


class DurableConflictError(Exception):
    """Same command identity, different payload — R2's durable conflict."""

    def __init__(self, command: CommandResource) -> None:
        self.command = command
        super().__init__(f"command {command.command_id} already exists with a different payload")


class UnknownStrategyInstanceError(Exception):
    """The target bot has no ``strategy_instances`` row — a typed domain
    not-found, never a raw SQLite foreign-key failure (open-pr-review-2026-08-05.md
    P2 "Unknown bot ID becomes raw SQLite 500"). Shared by every
    domain module that targets a bot (#1376's commands.py, #1377's enter.py)."""

    def __init__(self, strategy_instance_id: str) -> None:
        self.strategy_instance_id = strategy_instance_id
        super().__init__(f"unknown strategy instance {strategy_instance_id!r}")


def require_strategy_instance(repo: ClerkSqliteRepository, strategy_instance_id: str) -> None:
    """Called from inside a ``build_transition`` closure — i.e. already under
    the repository's write lock — never before it (see ``commands.py``'s
    ``submit_start_run`` docstring for why that ordering matters).

    Existence-only: does not reject a retired instance. Whether a retired bot
    may still be targeted is admission gating (R6, #1380), not this check.
    """
    if repo.strategy_instance(strategy_instance_id) is None:
        raise UnknownStrategyInstanceError(strategy_instance_id)


class NoActiveRunError(Exception):
    """The caller-supplied ``lifecycle_run_id`` does not match this bot's
    currently ``ACTIVE`` run (or there isn't one) — there is nothing to bind
    a content-addressed identity to, so no ``commands``/``effect_operations``
    row is written and no broker contact ever happens. Shared by every
    domain module that must respect the Start/Stop active-run fence
    (#1376's ``commands.py`` Stop, #1377's ``enter.py`` ENTER)."""

    def __init__(self, strategy_instance_id: str) -> None:
        self.strategy_instance_id = strategy_instance_id
        super().__init__(f"strategy instance {strategy_instance_id!r} has no matching active run")


def require_active_run(
    repo: ClerkSqliteRepository, strategy_instance_id: str, lifecycle_run_id: str
) -> RunResource:
    """Called from inside a ``build_transition`` closure, same rule as
    :func:`require_strategy_instance`. A stopped or stale caller must never
    reach a broker-facing or lifecycle-terminal transition — this is the one
    check that fences that, by requiring the caller's own
    ``lifecycle_run_id`` to match the run the repository considers active
    right now."""
    active = repo.active_run(strategy_instance_id)
    if active is None or active.lifecycle_run_id != lifecycle_run_id:
        raise NoActiveRunError(strategy_instance_id)
    return active
