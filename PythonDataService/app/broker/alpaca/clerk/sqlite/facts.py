"""Typed transition facts — corrective foundation slice, Scope A2.

Every registered fold parses ``facts_json`` into one of these dataclasses,
never an untyped bag. Each one carries exactly the immutable inputs the fold
needs to rebuild an identical ``commands`` row that are *not* already present
as ``custody_transitions`` columns (``strategy_instance_id``, ``run_id``,
``command_id`` are transition columns already; ``idempotency_key``,
``payload_hash``, ``kind``, ``action``, ``intended_end_state`` are not, so
they must round-trip through facts). A finalized mirror line — payload plus
this facts string — is therefore sufficient to reconstruct the command
resource on rebuild without a live database or a caller closure.

``ENTER_ACCEPTED`` facts are pinned in
``docs/architecture/alpaca-clerk-sqlite-pinned-contracts.md`` §3.d but are not
defined here: no command flow appends that transition kind in this slice —
issue #1377's rebuild adds the dataclass alongside the fold that consumes it.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from app.broker.alpaca.clerk.sqlite.hashchain import canonicalize

FACTS_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class RunStartedFacts:
    idempotency_key: str
    payload_hash: str
    kind: str
    action: str
    intended_end_state: str
    lifecycle_run_id: str
    operator_reason: str | None = None

    def to_facts_json(self) -> str:
        return canonicalize(asdict(self))

    @classmethod
    def from_facts_json(cls, facts_json: str) -> RunStartedFacts:
        return cls(**json.loads(facts_json))


@dataclass(frozen=True)
class RunStoppedFacts:
    idempotency_key: str
    payload_hash: str
    kind: str
    action: str
    intended_end_state: str
    lifecycle_run_id: str
    operator_reason: str | None = None

    def to_facts_json(self) -> str:
        return canonicalize(asdict(self))

    @classmethod
    def from_facts_json(cls, facts_json: str) -> RunStoppedFacts:
        return cls(**json.loads(facts_json))


@dataclass(frozen=True)
class CommandRejectedFacts:
    idempotency_key: str
    payload_hash: str
    kind: str
    action: str
    intended_end_state: str
    reason_code: str
    operator_reason: str | None = None

    def to_facts_json(self) -> str:
        return canonicalize(asdict(self))

    @classmethod
    def from_facts_json(cls, facts_json: str) -> CommandRejectedFacts:
        return cls(**json.loads(facts_json))
