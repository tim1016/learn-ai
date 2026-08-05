"""Shared idempotency-key safety primitives for every command domain module.

Every SQLite Clerk command (operator lifecycle in ``commands.py``, strategy
decisions in ``enter.py``, and future EXIT/CANCEL modules) embeds
caller-supplied identity components into a colon-delimited idempotency key
(pinned contracts doc §3a). ``reject_colon`` and ``DurableConflictError`` are
the one shared guard and the one shared conflict signal every such module
uses — not re-declared per module.
"""

from __future__ import annotations

from app.broker.alpaca.clerk.sqlite.repository import CommandResource


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
