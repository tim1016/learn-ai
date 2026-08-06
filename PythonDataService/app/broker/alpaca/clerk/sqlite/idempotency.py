"""Shared content-addressed-identity errors for command domain modules.

``commands.py`` (local Start/Stop, #1376), ``enter.py`` (#1377), and
``exit.py`` (#1379) all submit through
:meth:`ClerkSqliteRepository.commit_first_transition` and need the same
three outcomes translated to the same exceptions — kept here once rather
than each domain module defining its own.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.broker.alpaca.clerk.sqlite.models import CommandResource, OrderResource, RunResource

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


class UnknownEntryOrderError(Exception):
    """EXIT's targeted ``entry_order_ref`` does not exist, does not belong
    to this bot, is not an ``ENTRY``-role order, or is already under an
    active (non-terminal) EXIT — a typed domain not-found/conflict, never a
    raw SQLite foreign-key failure. #1379's analog of
    :class:`UnknownStrategyInstanceError`."""

    def __init__(self, entry_order_ref: str) -> None:
        self.entry_order_ref = entry_order_ref
        super().__init__(f"unknown, foreign, or already-exiting entry order {entry_order_ref!r}")


def require_owned_entry_order(
    repo: ClerkSqliteRepository, *, strategy_instance_id: str, entry_order_ref: str
) -> OrderResource:
    """Called from inside a ``build_transition`` closure, same rule as
    :func:`require_strategy_instance` — must run under the write lock so a
    concurrent EXIT accept can never link the same order between this check
    and the accept it gates.

    Rejects: the order doesn't exist, belongs to a different bot, isn't
    ``ENTRY``-role (targeting a REDUCING order, or a foreign role, makes no
    sense for a fresh EXIT), or is already owned by a different, still-live
    EXIT (a second EXIT decision racing the first would otherwise create
    conflicting live custody) — this last case is intentionally
    conservative rather than a full conflicting-concurrent-operation policy,
    which is R6/#1380 territory.
    """
    order = repo.order(entry_order_ref)
    if order is None:
        raise UnknownEntryOrderError(entry_order_ref)
    effect = repo.effect_operation(order.effect_operation_id)
    if effect is None or effect.strategy_instance_id != strategy_instance_id:
        raise UnknownEntryOrderError(entry_order_ref)
    if order.role != "ENTRY":
        raise UnknownEntryOrderError(entry_order_ref)
    if repo.active_exit_for_order(entry_order_ref) is not None:
        raise UnknownEntryOrderError(entry_order_ref)
    return order
