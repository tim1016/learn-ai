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
``docs/architecture/alpaca-clerk-sqlite-pinned-contracts.md`` §3.d; #1377 adds
the dataclass here alongside the fold that consumes it
(``folds._fold_enter_accepted``). The evidence-fold kinds that follow it
(``ORDER_SUBMIT_UNCERTAIN``, ``ORDER_SUBMIT_FAILED``, ``ORDER_FILL_OBSERVED``)
only ever *update* an already-existing ``effect_operations``/``orders`` row —
none of their fields are needed to rebuild that row's identity — but §3d's
"no untyped snapshot bag" rule still applies to whatever they *do* carry
beyond outer transition columns, so those get typed dataclasses too.
``ORDER_SUBMIT_ACKED`` is the one exception: every field its fold reads
(``broker_order_id``, ``broker_state``, ``source_event_at_ms``) is already an
outer ``custody_transitions`` column, so its ``facts_json`` is legitimately
``{}`` — there is nothing left to type.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any

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


@dataclass(frozen=True)
class EnterAcceptedFacts:
    """§3d's ``ENTER_ACCEPTED`` row: command idempotency key/hash/kind/action,
    the decision id, the effect idempotency key/kind, and the complete
    immutable broker leg — everything ``_fold_enter_accepted`` needs to
    rebuild the ``commands``, ``effect_operations``, and ``orders`` rows from
    a finalized mirror line alone."""

    idempotency_key: str
    payload_hash: str
    kind: str
    action: str
    intended_end_state: str | None
    effect_idempotency_key: str
    effect_kind: str
    decision_id: str
    # Already-validated ``BrokerOrderLeg.model_dump(mode="json")`` — kept as a
    # plain dict, not re-typed as a nested ``BrokerOrderLeg`` field, because
    # ``asdict()`` (this module's uniform (de)serialization path) only
    # recurses into stdlib dataclasses, not Pydantic models; storing the
    # already-dumped, already-validated leg is the one shape that keeps every
    # facts dataclass here going through the same to/from_facts_json pair.
    leg: dict[str, Any]

    def to_facts_json(self) -> str:
        return canonicalize(asdict(self))

    @classmethod
    def from_facts_json(cls, facts_json: str) -> EnterAcceptedFacts:
        return cls(**json.loads(facts_json))


@dataclass(frozen=True)
class OrderSubmitUncertainFacts:
    """A lost/timed-out broker response — R4's ``why`` explanation."""

    why: str

    def to_facts_json(self) -> str:
        return canonicalize(asdict(self))

    @classmethod
    def from_facts_json(cls, facts_json: str) -> OrderSubmitUncertainFacts:
        return cls(**json.loads(facts_json))


@dataclass(frozen=True)
class OrderSubmitFailedFacts:
    """A definitive broker rejection, or an absence proven past the R4
    uncertainty grace window."""

    reason: str
    why: str

    def to_facts_json(self) -> str:
        return canonicalize(asdict(self))

    @classmethod
    def from_facts_json(cls, facts_json: str) -> OrderSubmitFailedFacts:
        return cls(**json.loads(facts_json))


@dataclass(frozen=True)
class OrderFillObservedFacts:
    """The evidence ``_fold_order_fill_observed`` needs beyond the outer
    transition row: Alpaca's REST-reported *cumulative* ``filled_quantity``
    for the order, from which the fold derives both the fill delta and its
    idempotent identity (see the fold's own docstring for why)."""

    symbol: str
    side: str
    cumulative_filled_quantity: float
    avg_price: float
    is_correction: bool

    def to_facts_json(self) -> str:
        return canonicalize(asdict(self))

    @classmethod
    def from_facts_json(cls, facts_json: str) -> OrderFillObservedFacts:
        return cls(**json.loads(facts_json))
