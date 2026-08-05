"""Shared content-addressed-identity errors for command domain modules.

``commands.py`` (local Start/Stop, #1376) and #1377's rebuilt ``enter.py``
both submit through :meth:`ClerkSqliteRepository.commit_first_transition`
and need the same three outcomes translated to the same exceptions — kept
here once rather than each domain module defining its own.
"""

from __future__ import annotations

from app.broker.alpaca.clerk.sqlite.models import CommandResource


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
