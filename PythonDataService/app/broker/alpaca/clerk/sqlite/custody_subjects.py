"""Stable custody-subject identities shared by bot and manual Clerk work.

The subject is the account-scoped economic owner.  A bot's historical
``strategy_instance_id`` remains a compatibility projection, but it is no
longer the identity manual custody would have to impersonate.
"""

from __future__ import annotations

from dataclasses import dataclass

BOT_SUBJECT_PREFIX = "bot:"
MANUAL_OPERATOR_SUBJECT_PREFIX = "manual-operator:"


def bot_subject_id(strategy_instance_id: str) -> str:
    """Return the stable subject ID for one immutable strategy instance."""
    if not strategy_instance_id:
        raise ValueError("bot custody subject requires strategy_instance_id")
    return f"{BOT_SUBJECT_PREFIX}{strategy_instance_id}"


def manual_operator_subject_id(operator_id: str) -> str:
    """Return the stable account-local subject ID for the trusted operator."""
    if not operator_id:
        raise ValueError("manual custody subject requires operator_id")
    return f"{MANUAL_OPERATOR_SUBJECT_PREFIX}{operator_id}"


@dataclass(frozen=True)
class CustodySubject:
    """One durable actor eligible to own Clerk commands and attribution."""

    subject_id: str
    kind: str
    strategy_instance_id: str | None
    operator_id: str | None

    def validate(self) -> None:
        if self.kind == "BOT":
            if self.strategy_instance_id is None or self.operator_id is not None:
                raise ValueError("BOT custody subject requires only strategy_instance_id")
            if self.subject_id != bot_subject_id(self.strategy_instance_id):
                raise ValueError("BOT custody subject ID must match strategy_instance_id")
            return
        if self.kind == "MANUAL_OPERATOR":
            if self.operator_id is None or self.strategy_instance_id is not None:
                raise ValueError("MANUAL_OPERATOR custody subject requires only operator_id")
            if self.subject_id != manual_operator_subject_id(self.operator_id):
                raise ValueError("manual custody subject ID must match operator_id")
            return
        raise ValueError(f"unsupported custody subject kind {self.kind!r}")
