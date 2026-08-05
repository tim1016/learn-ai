"""Response models for the SQLite Alpaca Clerk command endpoints (#1376).

Backend-authored resource shapes only — the frontend derives no verb or
availability from these (R11); it renders ``state`` and ``disabled_tooltip``
as given.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from app.broker.alpaca.clerk.sqlite.repository import CommandResource


class CommandResponse(BaseModel):
    """The durable command resource (R2). Mirrors ``CommandResource`` 1:1,
    plus a backend-authored ``disabled_tooltip`` for a non-terminal command
    so a future UI slice needs no extra round trip to render "already
    requested"."""

    model_config = ConfigDict(frozen=True)

    command_id: str
    idempotency_key: str
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
    disabled_tooltip: str | None

    @classmethod
    def from_resource(cls, resource: CommandResource) -> CommandResponse:
        non_terminal = resource.state in ("reserved", "accepted", "in_progress", "unknown")
        tooltip = (
            f"A {resource.action.lower()} has already been requested for this bot."
            if non_terminal
            else None
        )
        return cls(
            command_id=resource.command_id,
            idempotency_key=resource.idempotency_key,
            kind=resource.kind,
            strategy_instance_id=resource.strategy_instance_id,
            run_id=resource.run_id,
            action=resource.action,
            intended_end_state=resource.intended_end_state,
            state=resource.state,
            effect_operation_id=resource.effect_operation_id,
            receipt_id=resource.receipt_id,
            created_at_ms=resource.created_at_ms,
            updated_at_ms=resource.updated_at_ms,
            disabled_tooltip=tooltip,
        )


class StartRunRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    lifecycle_run_id: str
    operator_reason: str | None = None


class StopRunRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    operator_reason: str | None = None


class DurableConflictResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    reason: str
    existing_command: CommandResponse
