"""Typed consent facts. Rebuild needs no browser, account feed or policy file."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from app.broker.alpaca.clerk.sqlite.hashchain import canonicalize
from app.schemas.account_authority import AuthorityKind


@dataclass(frozen=True)
class DeployCommittedFacts:
    idempotency_key: str
    payload_hash: str
    lifecycle_run_id: str
    world: AuthorityKind
    committed_cents: int
    configuration_hash: str
    exit_terms_hash: str
    risk_revision: int
    actor: str

    def __post_init__(self) -> None:
        if type(self.committed_cents) is not int or not 0 < self.committed_cents <= 2**63 - 1:
            raise ValueError("Deployment consent must be positive integer cents")
        if self.world not in {"real_paper", "real_live", "shadow", "synthetic"}:
            raise ValueError("Deployment consent must name a custody world")
        if type(self.risk_revision) is not int or self.risk_revision < 0:
            raise ValueError("Deployment consent must name the reviewed risk revision")
        if not self.actor or not self.lifecycle_run_id or not self.configuration_hash or not self.exit_terms_hash:
            raise ValueError("Deployment consent must preserve its actor, run, configuration and exit terms")

    def to_facts_json(self) -> str:
        return canonicalize(asdict(self))

    @classmethod
    def from_facts_json(cls, facts_json: str) -> DeployCommittedFacts:
        return cls(**json.loads(facts_json))
