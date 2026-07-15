"""Wire contract and decode boundary for Account Clerk RPC.

The Unix-socket server and its bot-side client share this module so transport
validation stays independent from Clerk orchestration and broker ownership.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

ACCOUNT_CLERK_RPC_SCHEMA_VERSION: Final = 1
ACCOUNT_CLERK_RPC_NORMAL_TIMEOUT_S: Final = 30.0
ACCOUNT_CLERK_RPC_RECOVERY_TIMEOUT_S: Final = 120.0

AccountClerkRpcOperation = Literal[
    "submit",
    "cancel_namespace",
    "recovery_flatten",
    "recovery_flatten_batch",
    "operator_adjustment",
    "drain_events",
]
WRITE_OPERATIONS = frozenset(
    {
        "submit",
        "cancel_namespace",
        "recovery_flatten",
        "recovery_flatten_batch",
        "operator_adjustment",
    }
)
AccountClerkRpcServerErrorCode = Literal[
    "ACCOUNT_CLERK_REJECTED",
    "ACCOUNT_CLERK_CANCEL_NAMESPACE_UNCERTAIN",
    "ACCOUNT_CLERK_INTERNAL_ERROR",
]


class AccountClerkRpcSuccessEnvelope(BaseModel):
    """Versioned success response. ``payload`` stays operation-specific."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = ACCOUNT_CLERK_RPC_SCHEMA_VERSION
    outcome: Literal["success"] = "success"
    payload: dict[str, object]


class AccountClerkRpcErrorEnvelope(BaseModel):
    """Versioned failure response with a stable category and safe reason."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = ACCOUNT_CLERK_RPC_SCHEMA_VERSION
    outcome: Literal["error"] = "error"
    reason_code: AccountClerkRpcServerErrorCode
    reason: str | None = Field(default=None, min_length=1, max_length=128)


class AccountClerkRpcGenerationHandshake(BaseModel):
    """First frame on every Clerk socket connection before a request is read."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = ACCOUNT_CLERK_RPC_SCHEMA_VERSION
    frame_type: Literal["generation_handshake"] = "generation_handshake"
    account_id: str = Field(min_length=1)
    served_generation: int = Field(ge=1)


@dataclass(frozen=True)
class AccountClerkRpcRequestIdentity:
    """Identity retained by ambiguous failures for an idempotent retry."""

    intent_id: str | None
    order_ref: str | None


class AccountClerkRpcError(RuntimeError):
    """Base error for all typed Account Clerk RPC outcomes."""

    def __init__(
        self,
        *,
        reason_code: str,
        operation: str,
        request_identity: AccountClerkRpcRequestIdentity,
    ) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.operation = operation
        self.intent_id = request_identity.intent_id
        self.order_ref = request_identity.order_ref


class AccountClerkRpcUnavailableError(AccountClerkRpcError):
    """The Clerk socket could not complete a transport exchange."""

    def __init__(
        self,
        *,
        reason: str,
        operation: str,
        request_identity: AccountClerkRpcRequestIdentity,
    ) -> None:
        self.reason = reason
        super().__init__(
            reason_code=f"ACCOUNT_CLERK_UNAVAILABLE:{reason}",
            operation=operation,
            request_identity=request_identity,
        )


class AccountClerkRpcTimeoutError(AccountClerkRpcUnavailableError):
    """The bounded Clerk request expired; its intent identity remains available."""

    def __init__(self, *, operation: str, request_identity: AccountClerkRpcRequestIdentity) -> None:
        super().__init__(
            reason="TIMEOUT",
            operation=operation,
            request_identity=request_identity,
        )


class AccountClerkRpcGenerationMismatchError(AccountClerkRpcUnavailableError):
    """The socket belongs to a Clerk fenced by a newer durable generation."""

    def __init__(
        self,
        *,
        expected_generation: int,
        served_generation: int,
        operation: str,
        request_identity: AccountClerkRpcRequestIdentity,
    ) -> None:
        self.expected_generation = expected_generation
        self.served_generation = served_generation
        super().__init__(
            reason="GENERATION_MISMATCH",
            operation=operation,
            request_identity=request_identity,
        )


class AccountClerkRpcMalformedResponseError(AccountClerkRpcError):
    """The Clerk answered, but not with a supported versioned envelope."""

    def __init__(self, *, operation: str, request_identity: AccountClerkRpcRequestIdentity) -> None:
        super().__init__(
            reason_code="ACCOUNT_CLERK_PROTOCOL_ERROR:MALFORMED_RESPONSE",
            operation=operation,
            request_identity=request_identity,
        )


class AccountClerkRpcRejectedError(AccountClerkRpcError):
    """The Clerk deliberately rejected a request before accepting its outcome."""

    def __init__(
        self,
        *,
        reason: str,
        operation: str,
        request_identity: AccountClerkRpcRequestIdentity,
    ) -> None:
        self.reason = reason
        super().__init__(
            reason_code="ACCOUNT_CLERK_REJECTED",
            operation=operation,
            request_identity=request_identity,
        )


class AccountClerkRpcInternalError(AccountClerkRpcError):
    """The Clerk had an unexpected server-side failure without exposing details."""

    def __init__(self, *, operation: str, request_identity: AccountClerkRpcRequestIdentity) -> None:
        super().__init__(
            reason_code="ACCOUNT_CLERK_INTERNAL_ERROR",
            operation=operation,
            request_identity=request_identity,
        )


class AccountClerkRpcCancelNamespaceUncertainError(AccountClerkRpcError):
    """The Clerk recorded cancellation uncertainty rather than a terminal receipt."""

    def __init__(self, *, operation: str, request_identity: AccountClerkRpcRequestIdentity) -> None:
        super().__init__(
            reason_code="ACCOUNT_CLERK_CANCEL_NAMESPACE_UNCERTAIN",
            operation=operation,
            request_identity=request_identity,
        )


class _AccountClerkRpcRequestRejected(ValueError):
    """Safe server-side rejection for malformed or unsupported requests."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def request_operation(request: Mapping[str, object]) -> AccountClerkRpcOperation:
    """Return a supported operation or reject the untrusted request frame."""

    operation = request.get("operation")
    if operation not in (
        "submit",
        "cancel_namespace",
        "recovery_flatten",
        "recovery_flatten_batch",
        "operator_adjustment",
        "drain_events",
    ):
        raise _AccountClerkRpcRequestRejected("UNKNOWN_OPERATION")
    return cast(AccountClerkRpcOperation, operation)


def request_timeout_s(operation: AccountClerkRpcOperation) -> float:
    return (
        ACCOUNT_CLERK_RPC_RECOVERY_TIMEOUT_S
        if operation in ("recovery_flatten", "recovery_flatten_batch")
        else ACCOUNT_CLERK_RPC_NORMAL_TIMEOUT_S
    )


def request_identity(request: Mapping[str, object]) -> AccountClerkRpcRequestIdentity:
    intent = request.get("intent")
    if not isinstance(intent, Mapping):
        return AccountClerkRpcRequestIdentity(intent_id=None, order_ref=None)
    intent_id = intent.get("intent_id")
    order_ref = intent.get("order_ref")
    return AccountClerkRpcRequestIdentity(
        intent_id=intent_id if isinstance(intent_id, str) else None,
        order_ref=order_ref if isinstance(order_ref, str) else None,
    )


def decode_request(line: bytes) -> dict[str, object]:
    if not line:
        raise _AccountClerkRpcRequestRejected("EMPTY_REQUEST")
    payload = json.loads(line)
    if not isinstance(payload, dict):
        raise _AccountClerkRpcRequestRejected("INVALID_REQUEST")
    return cast(dict[str, object], payload)


def decode_generation_handshake(
    line: bytes,
    *,
    operation: str,
    request_identity: AccountClerkRpcRequestIdentity,
) -> AccountClerkRpcGenerationHandshake:
    try:
        if not line:
            raise ValueError("Account Clerk RPC generation handshake is empty")
        payload = json.loads(line)
        if not isinstance(payload, dict):
            raise TypeError("Account Clerk RPC generation handshake is not an object")
        return AccountClerkRpcGenerationHandshake.model_validate(payload)
    except (json.JSONDecodeError, TypeError, ValidationError, ValueError) as exc:
        raise AccountClerkRpcMalformedResponseError(
            operation=operation,
            request_identity=request_identity,
        ) from exc


def decode_response(
    line: bytes,
    *,
    operation: str,
    request_identity: AccountClerkRpcRequestIdentity,
) -> dict[str, object]:
    try:
        payload = json.loads(line)
        if not isinstance(payload, dict):
            raise TypeError("Account Clerk RPC response is not an object")
        outcome = payload.get("outcome")
        if outcome == "success":
            return AccountClerkRpcSuccessEnvelope.model_validate(payload).payload
        if outcome != "error":
            raise ValueError("Account Clerk RPC response has no supported outcome")
        error = AccountClerkRpcErrorEnvelope.model_validate(payload)
    except (json.JSONDecodeError, TypeError, ValidationError, ValueError) as exc:
        raise AccountClerkRpcMalformedResponseError(
            operation=operation,
            request_identity=request_identity,
        ) from exc

    if error.reason_code == "ACCOUNT_CLERK_REJECTED":
        raise AccountClerkRpcRejectedError(
            reason=error.reason or "REJECTION_REASON_MISSING",
            operation=operation,
            request_identity=request_identity,
        )
    if error.reason_code == "ACCOUNT_CLERK_CANCEL_NAMESPACE_UNCERTAIN":
        raise AccountClerkRpcCancelNamespaceUncertainError(
            operation=operation,
            request_identity=request_identity,
        )
    raise AccountClerkRpcInternalError(operation=operation, request_identity=request_identity)


def rejected_envelope(reason: str) -> AccountClerkRpcErrorEnvelope:
    return AccountClerkRpcErrorEnvelope(reason_code="ACCOUNT_CLERK_REJECTED", reason=reason)


def request_object(request: Mapping[str, object], key: str) -> dict[str, object]:
    value = request.get(key)
    if not isinstance(value, dict):
        raise _AccountClerkRpcRequestRejected("INVALID_REQUEST")
    return value


def required_string(request: Mapping[str, object], key: str) -> str:
    value = request.get(key)
    if not isinstance(value, str) or not value:
        raise _AccountClerkRpcRequestRejected("INVALID_REQUEST")
    return value


def required_nonnegative_int(request: Mapping[str, object], key: str) -> int:
    value = request.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise _AccountClerkRpcRequestRejected("INVALID_REQUEST")
    return value


def optional_string(request: Mapping[str, object], key: str) -> str | None:
    value = request.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise _AccountClerkRpcRequestRejected("INVALID_REQUEST")
    return value
