"""Repository-domain data shapes — write inputs/outputs and read snapshots.

Split out of ``repository.py`` (corrective foundation slice, Scope E) purely
to keep that module under the file-size ceiling as new behavior (lease
renewal, operation claims, poison handling) accumulates — these are shapes,
not behavior; no method on any of these classes touches SQL, a lock, or a
connection.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TransitionInput:
    """Everything a caller supplies for one ``custody_transitions`` append.

    ``sequence``, ``prev_hash``, ``row_hash``, and ``recorded_at_ms`` are
    computed by the repository, never the caller.
    """

    transition_kind: str
    custody_owner: str
    execution_authority: str
    operation_state: str
    clerk_observed_at_ms: int
    summary_code: str
    facts_json: str
    strategy_instance_id: str | None = None
    run_id: str | None = None
    command_id: str | None = None
    effect_operation_id: str | None = None
    order_ref: str | None = None
    broker_order_id: str | None = None
    broker_state: str | None = None
    proof_reference: str | None = None
    source_event_at_ms: int | None = None
    facts_schema_version: int = 1


@dataclass(frozen=True)
class CommittedTransition:
    sequence: int
    row_hash: str
    prev_hash: str
    control_revision: int


@dataclass(frozen=True)
class ControlMetaSnapshot:
    schema_version: int
    account_id: str
    db_identity_token: str
    authority_generation: int
    control_revision: int
    created_at_ms: int
    last_open_at_ms: int


@dataclass(frozen=True)
class BotConfigResource:
    """Immutable strategy configuration persisted with a registration."""

    strategy_instance_id: str
    strategy_key: str
    display_name: str
    config_json: str
    config_hash: str
    created_at_ms: int


@dataclass(frozen=True)
class DecisionReceiptResource:
    """One durable strategy-decision receipt from the SQLite authority."""

    strategy_instance_id: str
    seq: int
    outcome: str
    symbol: str | None
    intent_id: str | None
    order_ref: str | None
    observed_at_ms: int
    facts_json: str


@dataclass(frozen=True)
class ExternalOrderResource:
    """A broker order outside every registered bot namespace.

    This account-scoped evidence intentionally has no strategy, run, order
    reference, or economic attribution.  Keeping it separate from ``fills``
    prevents an operator review of an external order from changing a bot's
    position or P&L.
    """

    external_order_id: str
    broker_order_id: str
    client_order_id: str
    symbol: str
    side: str
    qty: float
    price: float | None
    observed_at_ms: int
    acknowledged_at_ms: int | None
    ack_operator: str | None
    evidence_refs: tuple[str, ...]
    # These are assigned by the durable folds.  ``None`` is only valid for a
    # not-yet-appended observation value used to plan a transition.
    observation_sequence: int | None = None
    acknowledgement_sequence: int | None = None
    observation_recorded_at_ms: int | None = None
    acknowledgement_recorded_at_ms: int | None = None


@dataclass(frozen=True)
class CommandResource:
    """The durable command resource ``GET /commands/{command_id}`` returns (R2)."""

    command_id: str
    idempotency_key: str
    payload_hash: str
    kind: str
    strategy_instance_id: str
    run_id: str | None
    action: str
    intended_end_state: str | None
    state: str
    effect_operation_id: str | None
    receipt_id: str | None
    created_at_ms: int
    updated_at_ms: int


@dataclass(frozen=True)
class RunResource:
    run_id: str
    strategy_instance_id: str
    lifecycle_run_id: str
    state: str
    started_at_ms: int
    stopped_at_ms: int | None


@dataclass(frozen=True)
class EffectOperationResource:
    """A row of ``effect_operations`` — Clerk-owned ENTER/EXIT/... work (§7)."""

    effect_operation_id: str
    authority_generation: int
    idempotency_key: str
    command_id: str
    strategy_instance_id: str
    run_id: str | None
    kind: str
    state: str
    custody_owner: str
    created_at_ms: int
    updated_at_ms: int
    terminal_receipt_id: str | None


@dataclass(frozen=True)
class OrderResource:
    """A row of ``orders`` — one broker/client order identity (R7)."""

    order_ref: str
    effect_operation_id: str
    client_order_id: str
    broker_order_id: str | None
    role: str
    broker_state: str | None
    submitted_at_ms: int | None
    updated_at_ms: int


@dataclass(frozen=True)
class ExitSubmission:
    """Current resource snapshot for one EXIT effect operation."""

    command: CommandResource
    effect_operation_id: str | None
    entry_order_ref: str
    reducing_order_ref: str | None
    created: bool


@dataclass(frozen=True)
class CommandCreated:
    """A fresh idempotency key: the transition committed and the fold
    created the durable command resource in the same transaction."""

    command: CommandResource
    committed: CommittedTransition


@dataclass(frozen=True)
class CommandExistingSame:
    """Same key, same hash — transport retry or genuine re-request (R2).

    No new transition is appended; the existing resource is returned as-is,
    whatever its current state — there is no separate code path for "already
    requested" versus "already terminal", per R2's own closing rule.
    """

    command: CommandResource


@dataclass(frozen=True)
class CommandExistingConflict:
    """Same key, different hash — durable conflict, no effect (R2)."""

    command: CommandResource


@dataclass(frozen=True)
class OperationClaim:
    """A live, fenced claim on one ``effect_operations`` row (Scope D)."""

    effect_operation_id: str
    owner: str
    token: str
    claimed_at_ms: int
    expires_at_ms: int
