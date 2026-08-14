"""Typed transport contract for the SQLite manual market-order tracer."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.broker.alpaca.clerk.sqlite.manual_order_cancellation import (
    ManualOrderCancellationSubmission,
)
from app.broker.alpaca.clerk.sqlite.manual_order_runtime import (
    ManualOrderCapability,
    ManualOrderPreview,
    ManualOrderUnavailable,
)
from app.broker.alpaca.clerk.sqlite.models import (
    ManualOrderCancellationResource,
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
    legs: tuple[ManualOrderLegRequest, ...] = Field(
        min_length=1,
        max_length=8,
        json_schema_extra={"minItems": 1, "maxItems": 8},
    )


class ManualOrderSubmitRequest(BaseModel):
    """Confirmation for the exact previewed ticket and leg."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    legs: tuple[ManualOrderLegRequest, ...] = Field(
        min_length=1,
        max_length=8,
        json_schema_extra={"minItems": 1, "maxItems": 8},
    )
    preview_token: str = Field(min_length=64, max_length=128)


class ManualOrderCancelRequest(BaseModel):
    """One stable browser-generated cancellation identity for a Clerk order ref."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cancel_request_id: UUID


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
    supported_order_shape: str = "BUY or SELL market/limit DAY/GTC equity, one to eight ordered legs"

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


class ManualOrderCancellationResponse(BaseModel):
    """Durable cancellation receipt; `UNKNOWN` never claims broker success."""

    model_config = ConfigDict(frozen=True)

    order_ref: str
    cancel_request_id: str
    state: str
    message: str
    impact: str
    next_action: str
    command: ManualOrderCommandResponse
    effect: ManualOrderEffectResponse

    @staticmethod
    def _copy_for_state(state: str) -> tuple[str, str, str]:
        copies = {
            "ACCEPTED": (
                "The Clerk accepted this cancellation request.",
                "The manual order remains active until exact broker evidence is recorded.",
                "Refresh this ticket for the Clerk's current cancellation receipt.",
            ),
            "UNKNOWN": (
                "The cancellation outcome is not yet known.",
                "The Clerk will not create another cancellation while exact broker evidence is unresolved.",
                "Refresh this ticket while the Clerk reconciles the cancellation by its exact order reference.",
            ),
            "SUCCEEDED": (
                "Exact broker evidence confirmed the manual order was canceled.",
                "The manual order is terminal and no longer leaves cancellation work outstanding.",
                "No further cancellation action is required.",
            ),
            "FAILED": (
                "The cancellation could not be completed because the manual order was already terminal.",
                "The original manual order has a terminal outcome and cannot be canceled again.",
                "Review the refreshed ticket receipt for the terminal order outcome.",
            ),
        }
        return copies.get(
            state,
            (
                "The Clerk recorded the cancellation request.",
                "The cancellation receipt is retained as durable account evidence.",
                "Refresh this ticket for the current receipt.",
            ),
        )

    @classmethod
    def from_resource(
        cls,
        cancellation: ManualOrderCancellationResource,
        *,
        repo: ClerkSqliteRepository,
    ) -> ManualOrderCancellationResponse:
        command = repo.get_command(cancellation.command_id)
        effect = repo.effect_operation(cancellation.effect_operation_id)
        if command is None or effect is None:
            raise RuntimeError("manual cancellation lost its durable command or effect")
        message, impact, next_action = cls._copy_for_state(cancellation.state)
        return cls(
            order_ref=cancellation.order_ref,
            cancel_request_id=cancellation.cancel_request_id,
            state=cancellation.state,
            message=message,
            impact=impact,
            next_action=next_action,
            command=ManualOrderCommandResponse(
                command_id=command.command_id,
                state=command.state,
                action=command.action,
                receipt_id=command.receipt_id,
            ),
            effect=ManualOrderEffectResponse(
                effect_operation_id=effect.effect_operation_id,
                state=effect.state,
                kind=effect.kind,
                terminal_receipt_id=effect.terminal_receipt_id,
            ),
        )

    @classmethod
    def from_domain(
        cls,
        submission: ManualOrderCancellationSubmission,
    ) -> ManualOrderCancellationResponse:
        message, impact, next_action = cls._copy_for_state(submission.cancellation.state)
        return cls(
            order_ref=submission.cancellation.order_ref,
            cancel_request_id=submission.cancellation.cancel_request_id,
            state=submission.cancellation.state,
            message=message,
            impact=impact,
            next_action=next_action,
            command=ManualOrderCommandResponse(
                command_id=submission.command.command_id,
                state=submission.command.state,
                action=submission.command.action,
                receipt_id=submission.command.receipt_id,
            ),
            effect=ManualOrderEffectResponse(
                effect_operation_id=submission.effect.effect_operation_id,
                state=submission.effect.state,
                kind=submission.effect.kind,
                terminal_receipt_id=submission.effect.terminal_receipt_id,
            ),
        )


class ManualOrderBrokerOrderResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    order_ref: str
    client_order_id: str
    broker_order_id: str | None
    broker_state: str | None


class ManualOrderLegResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    leg_id: str
    sequence_index: int
    instruction_hash: str
    instruction: BrokerOrderLeg | None
    state: str
    command: ManualOrderCommandResponse | None
    effect: ManualOrderEffectResponse | None
    order: ManualOrderBrokerOrderResponse | None
    cancellation: ManualOrderCancellationResponse | None

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
        cancellation = repo.manual_order_cancellation(order_ref=leg.order_ref) if leg.order_ref is not None else None
        return cls(
            leg_id=leg.leg_id,
            sequence_index=leg.sequence_index,
            instruction_hash=leg.instruction_hash,
            instruction=(BrokerOrderLeg.model_validate(leg.instruction) if leg.instruction is not None else None),
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
            cancellation=(
                ManualOrderCancellationResponse.from_resource(cancellation, repo=repo)
                if cancellation is not None
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
