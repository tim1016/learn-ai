"""Effective account loss policy in the custody transition/mirror authority.

Configuration drafts do not enter this module. An explicit Apply appends one
immutable policy, fenced by its reviewed revision. The same repository writer
coordinates Apply, observation publication, ENTER and guarded hold clearing.
ExitTerms and the historical profile/arming payloads are never rewritten.
"""
from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from app.broker.alpaca.clerk.sqlite.hashchain import canonicalize
from app.broker.alpaca.clerk.sqlite.models import TransitionInput

if TYPE_CHECKING:
    from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository

SCHEMA_V19_STATEMENTS = (
    "CREATE TABLE IF NOT EXISTS account_risk_policy ("
    "id INTEGER PRIMARY KEY CHECK (id = 1), revision INTEGER NOT NULL, "
    "policy_json TEXT NOT NULL)",
)


@dataclass(frozen=True)
class AccountRiskPolicy:
    revision: int
    loss_fraction: float
    loss_usd: float
    profile_id: str
    profile_revision: int
    actor: str
    applied_at_ms: int

    def __post_init__(self) -> None:
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError("Risk revision must be a positive integer")
        if not math.isfinite(self.loss_fraction) or not 0 < self.loss_fraction < 1:
            raise ValueError("Loss fraction must be between zero and one")
        if not math.isfinite(self.loss_usd) or self.loss_usd <= 0:
            raise ValueError("Loss cap must be positive and finite")
        if not self.profile_id or not self.actor or self.profile_revision < 1:
            raise ValueError("Risk policy requires its effective profile and operator")
        if type(self.applied_at_ms) is not int or self.applied_at_ms < 0:
            raise ValueError("Risk policy timestamp must be integer UTC milliseconds")

    def to_facts_json(self) -> str:
        return canonicalize(asdict(self))

    @classmethod
    def from_facts_json(cls, raw: str) -> AccountRiskPolicy:
        return cls(**json.loads(raw))


class RiskRevisionConflict(ValueError):
    """The reviewed risk policy changed; refresh and review before applying."""


def read_account_risk_policy(conn: sqlite3.Connection) -> AccountRiskPolicy | None:
    row = conn.execute("SELECT policy_json FROM account_risk_policy WHERE id = 1").fetchone()
    return None if row is None else AccountRiskPolicy.from_facts_json(row["policy_json"])


def fold_account_risk_policy(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    policy = AccountRiskPolicy.from_facts_json(payload["facts_json"])
    previous = read_account_risk_policy(conn)
    expected = 1 if previous is None else previous.revision + 1
    if policy.revision != expected:
        raise RiskRevisionConflict("Risk policy history is not consecutive")
    conn.execute(
        "INSERT INTO account_risk_policy VALUES (1, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET revision=excluded.revision, policy_json=excluded.policy_json",
        (policy.revision, policy.to_facts_json()),
    )


def append_risk_policy(
    repo: ClerkSqliteRepository, *, policy: AccountRiskPolicy, expected_revision: int,
) -> None:
    """Caller holds the custody coordinator across publication and this append."""
    current = repo.account_risk_policy()
    if (0 if current is None else current.revision) != expected_revision:
        raise RiskRevisionConflict("Risk limits changed since review. Reload and review again.")
    if policy.revision != expected_revision + 1:
        raise RiskRevisionConflict("Risk policy must advance exactly one revision")
    repo.append_transition(TransitionInput(
        transition_kind="ACCOUNT_RISK_LIMITS_APPLIED",
        custody_owner="CLERK", execution_authority="NONE",
        operation_state="APPLIED", clerk_observed_at_ms=repo.clock(),
        summary_code="ACCOUNT_RISK_LIMITS_APPLIED",
        facts_json=policy.to_facts_json(),
    ))
