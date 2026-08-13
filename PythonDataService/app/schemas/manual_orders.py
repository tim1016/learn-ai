"""Typed transport contract for the SQLite manual market-order tracer."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.broker.alpaca.clerk.sqlite.manual_order_runtime import (
    ManualOrderCapability,
    ManualOrderPreview,
    ManualOrderUnavailable,
)
from app.broker.alpaca.clerk.sqlite.models import (
    ManualOrderLegResource,
    ManualOrderTicketResource,
)
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.contract.models import BrokerOrderLeg


class ManualOrderLegRequest(BaseModel):
    """One browser-minted stable leg identity and the intended instruction."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    leg_id: UUID
    instruction: BrokerOrderLeg


class ManualOrderPreviewRequest(BaseModel):
    """Preview never accepts operator identity; the server attributes that fact."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id: UUID
    leg: ManualOrderLegRequest


class ManualOrderSubmitRequest(BaseModel):
    """Confirmation for the exact previewed ticket and leg."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    leg: ManualOrderLegRequest
    preview_token: str = Field(min_length=64, max_length=128)


class ManualOrderUnavailableResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str
    message: str

    @classmethod
    def from_domain(cls, unavailable: ManualOrderUnavailable) -> ManualOrderUnavailableResponse:
        return cls(code=unavailable.code, message=unavailable.message)


class ManualOrderCapabilityResponse(BaseModel):
    """The policy answer shown before an operator may create exposure."""

    model_config = ConfigDict(frozen=True)

    available: bool
    unavailable: ManualOrderUnavailableResponse | None
    supported_order_shape: str = "BUY market DAY equity, one leg"

    @classmethod
    def from_domain(cls, capability: ManualOrderCapability) -> ManualOrderCapabilityResponse:
        return cls(
            available=capability.available,
            unavailable=(
                ManualOrderUnavailableResponse.from_domain(capability.unavailable)
                if capability.unavailable is not None
                else None
            ),
        )


class ManualOrderPreviewResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    capability: ManualOrderCapabilityResponse
    preview_token: str | None
    authority_generation: int | None
    db_identity_token: str | None
    control_revision: int | None
    subject_id: str | None

    @classmethod
    def from_domain(cls, preview: ManualOrderPreview) -> ManualOrderPreviewResponse:
        return cls(
            capability=ManualOrderCapabilityResponse.from_domain(preview.capability),
            preview_token=preview.preview_token,
            authority_generation=preview.authority_generation,
            db_identity_token=preview.db_identity_token,
            control_revision=preview.control_revision,
            subject_id=preview.subject_id,
        )


class ManualOrderCommandResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    command_id: str
    state: str
    action: str
    receipt_id: str | None


class ManualOrderEffectResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    effect_operation_id: str
    state: str
    kind: str
    terminal_receipt_id: str | None


class ManualOrderBrokerOrderResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    order_ref: str
    client_order_id: str
    broker_order_id: str | None
    broker_state: str | None


class ManualOrderLegResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    leg_id: str
    instruction_hash: str
    state: str
    command: ManualOrderCommandResponse | None
    effect: ManualOrderEffectResponse | None
    order: ManualOrderBrokerOrderResponse | None

    @classmethod
    def from_resource(
        cls,
        leg: ManualOrderLegResource,
        *,
        repo: ClerkSqliteRepository,
    ) -> ManualOrderLegResponse:
        command = repo.get_command(leg.command_id) if leg.command_id is not None else None
        effect = repo.effect_operation(leg.effect_operation_id) if leg.effect_operation_id is not None else None
        order = repo.order(leg.order_ref) if leg.order_ref is not None else None
        return cls(
            leg_id=leg.leg_id,
            instruction_hash=leg.instruction_hash,
            state=leg.state,
            command=(
                ManualOrderCommandResponse(
                    command_id=command.command_id,
                    state=command.state,
                    action=command.action,
                    receipt_id=command.receipt_id,
                )
                if command is not None
                else None
            ),
            effect=(
                ManualOrderEffectResponse(
                    effect_operation_id=effect.effect_operation_id,
                    state=effect.state,
                    kind=effect.kind,
                    terminal_receipt_id=effect.terminal_receipt_id,
                )
                if effect is not None
                else None
            ),
            order=(
                ManualOrderBrokerOrderResponse(
                    order_ref=order.order_ref,
                    client_order_id=order.client_order_id,
                    broker_order_id=order.broker_order_id,
                    broker_state=order.broker_state,
                )
                if order is not None
                else None
            ),
        )


class ManualOrderTicketResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    ticket_id: str
    subject_id: str
    state: str
    created_at_ms: int
    updated_at_ms: int
    legs: tuple[ManualOrderLegResponse, ...]

    @classmethod
    def from_resource(
        cls,
        ticket: ManualOrderTicketResource,
        *,
        repo: ClerkSqliteRepository,
    ) -> ManualOrderTicketResponse:
        legs = tuple(ManualOrderLegResponse.from_resource(leg, repo=repo) for leg in ticket.legs)
        return cls(
            ticket_id=ticket.ticket_id,
            subject_id=ticket.subject_id,
            state=ticket.state,
            created_at_ms=ticket.created_at_ms,
            updated_at_ms=ticket.updated_at_ms,
            legs=legs,
        )
