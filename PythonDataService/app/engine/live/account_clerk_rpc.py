"""Bounded, versioned Unix-socket RPC for the Account Clerk authority boundary."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.broker.ibkr.models import IbkrOrderEvent
from app.engine.live.account_artifacts import append_account_event
from app.engine.live.account_clerk import (
    AccountClerk,
    AccountClerkBrokerAckReceipt,
    AccountClerkIntentRejected,
    AccountClerkRecoveryFlattenReceipt,
    account_clerk_socket_path,
)
from app.engine.live.account_owner import AccountOwnerSubmitIntent

logger = logging.getLogger(__name__)

ACCOUNT_CLERK_RPC_SCHEMA_VERSION: Final = 1
ACCOUNT_CLERK_RPC_NORMAL_TIMEOUT_S: Final = 30.0
ACCOUNT_CLERK_RPC_RECOVERY_TIMEOUT_S: Final = 120.0

AccountClerkRpcOperation = Literal["submit", "recovery_flatten", "drain_events"]
AccountClerkRpcServerErrorCode = Literal[
    "ACCOUNT_CLERK_REJECTED",
    "ACCOUNT_CLERK_INTERNAL_ERROR",
]

# Deliberately a module seam: timeout tests replace it with a barrier-controlled
# context manager, while production always uses asyncio.timeout.
_request_timeout = asyncio.timeout


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

    def __init__(
        self,
        *,
        operation: str,
        request_identity: AccountClerkRpcRequestIdentity,
    ) -> None:
        super().__init__(
            reason="TIMEOUT",
            operation=operation,
            request_identity=request_identity,
        )


class AccountClerkRpcMalformedResponseError(AccountClerkRpcError):
    """The Clerk answered, but not with a supported versioned envelope."""

    def __init__(
        self,
        *,
        operation: str,
        request_identity: AccountClerkRpcRequestIdentity,
    ) -> None:
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

    def __init__(
        self,
        *,
        operation: str,
        request_identity: AccountClerkRpcRequestIdentity,
    ) -> None:
        super().__init__(
            reason_code="ACCOUNT_CLERK_INTERNAL_ERROR",
            operation=operation,
            request_identity=request_identity,
        )


class _AccountClerkRpcRequestRejected(ValueError):
    """Safe server-side rejection for malformed or unsupported requests."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class AccountClerkRpcClient:
    """Bot-side client: enqueue intents; it never holds a broker adapter."""

    def __init__(self, *, artifacts_root, account_id: str) -> None:
        self._socket_path = account_clerk_socket_path(artifacts_root, account_id)

    async def submit(self, intent: AccountOwnerSubmitIntent) -> AccountClerkBrokerAckReceipt:
        payload = await self._request({"operation": "submit", "intent": intent.model_dump(mode="json")})
        try:
            return AccountClerkBrokerAckReceipt.model_validate(payload["broker_acked"])
        except (KeyError, ValidationError, TypeError) as exc:
            raise AccountClerkRpcMalformedResponseError(
                operation="submit",
                request_identity=_request_identity({"intent": intent.model_dump(mode="json")}),
            ) from exc

    async def submit_recovery_flatten(self, intent: AccountOwnerSubmitIntent) -> AccountClerkRecoveryFlattenReceipt:
        """Ask the Clerk to flatten the calling bot's own namespace."""

        request = {
            "operation": "recovery_flatten",
            "actor": "bot",
            "actor_strategy_instance_id": intent.strategy_instance_id,
            "actor_run_id": intent.run_id,
            "actor_bot_order_namespace": intent.bot_order_namespace,
            "intent": intent.model_dump(mode="json"),
        }
        payload = await self._request(request)
        try:
            return AccountClerkRecoveryFlattenReceipt.model_validate(payload["recovery_flatten"])
        except (KeyError, ValidationError, TypeError) as exc:
            raise AccountClerkRpcMalformedResponseError(
                operation="recovery_flatten",
                request_identity=_request_identity(request),
            ) from exc

    async def submit_operator_recovery_flatten(
        self,
        intent: AccountOwnerSubmitIntent,
    ) -> AccountClerkRecoveryFlattenReceipt:
        """Run the explicit operator cure for a retired namespace."""

        request = {
            "operation": "recovery_flatten",
            "actor": "operator",
            "intent": intent.model_dump(mode="json"),
        }
        payload = await self._request(request)
        try:
            return AccountClerkRecoveryFlattenReceipt.model_validate(payload["recovery_flatten"])
        except (KeyError, ValidationError, TypeError) as exc:
            raise AccountClerkRpcMalformedResponseError(
                operation="recovery_flatten",
                request_identity=_request_identity(request),
            ) from exc

    async def drain_events(self, *, bot_order_namespace: str) -> list[IbkrOrderEvent]:
        request = {"operation": "drain_events", "bot_order_namespace": bot_order_namespace}
        payload = await self._request(request)
        try:
            return [IbkrOrderEvent.model_validate(event) for event in payload["events"]]
        except (KeyError, ValidationError, TypeError) as exc:
            raise AccountClerkRpcMalformedResponseError(
                operation="drain_events",
                request_identity=_request_identity(request),
            ) from exc

    async def _request(self, request: dict[str, object]) -> dict[str, object]:
        operation = _request_operation(request)
        request_identity = _request_identity(request)
        if not self._socket_path.exists():
            raise AccountClerkRpcUnavailableError(
                reason="SOCKET_MISSING",
                operation=operation,
                request_identity=request_identity,
            )

        writer: asyncio.StreamWriter | None = None
        try:
            async with _request_timeout(_request_timeout_s(operation)):
                try:
                    reader, writer = await asyncio.open_unix_connection(str(self._socket_path))
                except OSError as exc:
                    raise AccountClerkRpcUnavailableError(
                        reason="SOCKET_CONNECT_FAILED",
                        operation=operation,
                        request_identity=request_identity,
                    ) from exc
                writer.write((json.dumps(request) + "\n").encode())
                await writer.drain()
                line = await reader.readline()
        except TimeoutError as exc:
            raise AccountClerkRpcTimeoutError(
                operation=operation,
                request_identity=request_identity,
            ) from exc
        except (ConnectionError, OSError) as exc:
            raise AccountClerkRpcUnavailableError(
                reason="SOCKET_CONNECTION_LOST",
                operation=operation,
                request_identity=request_identity,
            ) from exc
        finally:
            if writer is not None:
                writer.close()
                await writer.wait_closed()

        if not line:
            raise AccountClerkRpcUnavailableError(
                reason="EMPTY_RESPONSE",
                operation=operation,
                request_identity=request_identity,
            )
        return _decode_response(
            line,
            operation=operation,
            request_identity=request_identity,
        )


class AccountClerkRpcServer:
    """Clerk-process RPC server; the broker stays exclusively behind this seam."""

    def __init__(self, clerk: AccountClerk) -> None:
        self._clerk = clerk
        self._server: asyncio.AbstractServer | None = None
        self._socket_path = account_clerk_socket_path(clerk._artifacts_root, clerk._account_id)
        self._events_by_namespace: dict[str, list[IbkrOrderEvent]] = {}
        self._intents_by_order_ref: dict[str, AccountOwnerSubmitIntent] = {}
        set_callback = getattr(clerk._broker, "set_broker_callback_sink", None)
        if callable(set_callback):
            set_callback(self._record_broker_event)

    async def start(self) -> None:
        self._socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self._socket_path.exists():
            self._socket_path.unlink()
        self._server = await asyncio.start_unix_server(self._handle, path=str(self._socket_path))
        self._socket_path.chmod(0o600)

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        if self._socket_path.exists():
            self._socket_path.unlink()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        operation = "unknown"
        try:
            request = _decode_request(await reader.readline())
            operation = _request_operation(request)
            response: AccountClerkRpcSuccessEnvelope | AccountClerkRpcErrorEnvelope = await self._dispatch(request)
        except _AccountClerkRpcRequestRejected as exc:
            response = _rejected_envelope(exc.reason)
        except AccountClerkIntentRejected as exc:
            response = _rejected_envelope(exc.reason)
        except (json.JSONDecodeError, ValidationError):
            response = _rejected_envelope("INVALID_REQUEST")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(
                "Account Clerk RPC server failure",
                extra={
                    "rpc_operation": operation,
                    "account_id": self._clerk._account_id,
                    "reason_code": "ACCOUNT_CLERK_INTERNAL_ERROR",
                    "error_type": type(exc).__name__,
                },
            )
            response = AccountClerkRpcErrorEnvelope(reason_code="ACCOUNT_CLERK_INTERNAL_ERROR")

        try:
            writer.write((response.model_dump_json() + "\n").encode())
            await writer.drain()
        except (ConnectionError, OSError):
            logger.info(
                "Account Clerk RPC client disconnected before response",
                extra={"rpc_operation": operation, "account_id": self._clerk._account_id},
            )
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                logger.info(
                    "Account Clerk RPC socket closed after client disconnect",
                    extra={"rpc_operation": operation, "account_id": self._clerk._account_id},
                )

    async def _dispatch(
        self,
        request: dict[str, object],
    ) -> AccountClerkRpcSuccessEnvelope | AccountClerkRpcErrorEnvelope:
        operation = _request_operation(request)
        if operation == "submit":
            intent = AccountOwnerSubmitIntent.model_validate(_request_object(request, "intent"))
            recorded, broker_acked = await self._clerk.submit_intent(intent)
            self._intents_by_order_ref[intent.order_ref] = intent
            return AccountClerkRpcSuccessEnvelope(
                payload={
                    "recorded": recorded.model_dump(mode="json"),
                    "broker_acked": broker_acked.model_dump(mode="json"),
                }
            )
        if operation == "recovery_flatten":
            intent = AccountOwnerSubmitIntent.model_validate(_request_object(request, "intent"))
            actor = request.get("actor")
            if actor not in ("bot", "operator"):
                raise _AccountClerkRpcRequestRejected("INVALID_RECOVERY_ACTOR")
            recovery = await self._clerk.submit_recovery_flatten(
                intent,
                actor=actor,
                actor_strategy_instance_id=_optional_string(request, "actor_strategy_instance_id"),
                actor_run_id=_optional_string(request, "actor_run_id"),
                actor_bot_order_namespace=_optional_string(request, "actor_bot_order_namespace"),
            )
            self._intents_by_order_ref[intent.order_ref] = intent
            return AccountClerkRpcSuccessEnvelope(
                payload={"recovery_flatten": recovery.model_dump(mode="json")}
            )
        namespace = _required_string(request, "bot_order_namespace")
        events = self._events_by_namespace.pop(namespace, [])
        return AccountClerkRpcSuccessEnvelope(
            payload={"events": [event.model_dump(mode="json") for event in events]}
        )

    def _record_broker_event(self, event: IbkrOrderEvent) -> None:
        order_ref = event.order_ref
        if order_ref is None or ":" not in order_ref:
            return
        namespace, _intent_id = order_ref.rsplit(":", maxsplit=1)
        intent = self._intents_by_order_ref.get(order_ref)
        if intent is None:
            append_account_event(
                self._clerk._artifacts_root,
                self._clerk._account_id,
                {
                    "event_type": "account_clerk_reconciliation_alarm",
                    "ts_ms": event.ts_ms,
                    "reason": "BROKER_EVENT_WITHOUT_DURABLE_CLERK_INTENT",
                    "order_ref": order_ref,
                    "order_id": event.order_id,
                    "perm_id": event.perm_id,
                },
            )
            return
        self._clerk.append_broker_event(intent, event)
        self._events_by_namespace.setdefault(namespace, []).append(event)


def _request_operation(request: Mapping[str, object]) -> AccountClerkRpcOperation:
    operation = request.get("operation")
    if operation not in ("submit", "recovery_flatten", "drain_events"):
        raise _AccountClerkRpcRequestRejected("UNKNOWN_OPERATION")
    return cast(AccountClerkRpcOperation, operation)


def _request_timeout_s(operation: AccountClerkRpcOperation) -> float:
    return (
        ACCOUNT_CLERK_RPC_RECOVERY_TIMEOUT_S
        if operation == "recovery_flatten"
        else ACCOUNT_CLERK_RPC_NORMAL_TIMEOUT_S
    )


def _request_identity(request: Mapping[str, object]) -> AccountClerkRpcRequestIdentity:
    intent = request.get("intent")
    if not isinstance(intent, Mapping):
        return AccountClerkRpcRequestIdentity(intent_id=None, order_ref=None)
    intent_id = intent.get("intent_id")
    order_ref = intent.get("order_ref")
    return AccountClerkRpcRequestIdentity(
        intent_id=intent_id if isinstance(intent_id, str) else None,
        order_ref=order_ref if isinstance(order_ref, str) else None,
    )


def _decode_request(line: bytes) -> dict[str, object]:
    if not line:
        raise _AccountClerkRpcRequestRejected("EMPTY_REQUEST")
    payload = json.loads(line)
    if not isinstance(payload, dict):
        raise _AccountClerkRpcRequestRejected("INVALID_REQUEST")
    return cast(dict[str, object], payload)


def _decode_response(
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
    raise AccountClerkRpcInternalError(operation=operation, request_identity=request_identity)


def _rejected_envelope(reason: str) -> AccountClerkRpcErrorEnvelope:
    return AccountClerkRpcErrorEnvelope(
        reason_code="ACCOUNT_CLERK_REJECTED",
        reason=reason,
    )


def _request_object(request: Mapping[str, object], key: str) -> dict[str, object]:
    value = request.get(key)
    if not isinstance(value, dict):
        raise _AccountClerkRpcRequestRejected("INVALID_REQUEST")
    return value


def _required_string(request: Mapping[str, object], key: str) -> str:
    value = request.get(key)
    if not isinstance(value, str) or not value:
        raise _AccountClerkRpcRequestRejected("INVALID_REQUEST")
    return value


def _optional_string(request: Mapping[str, object], key: str) -> str | None:
    value = request.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise _AccountClerkRpcRequestRejected("INVALID_REQUEST")
    return value


__all__ = [
    "ACCOUNT_CLERK_RPC_NORMAL_TIMEOUT_S",
    "ACCOUNT_CLERK_RPC_RECOVERY_TIMEOUT_S",
    "ACCOUNT_CLERK_RPC_SCHEMA_VERSION",
    "AccountClerkRpcClient",
    "AccountClerkRpcError",
    "AccountClerkRpcErrorEnvelope",
    "AccountClerkRpcInternalError",
    "AccountClerkRpcMalformedResponseError",
    "AccountClerkRpcRejectedError",
    "AccountClerkRpcRequestIdentity",
    "AccountClerkRpcServer",
    "AccountClerkRpcSuccessEnvelope",
    "AccountClerkRpcTimeoutError",
    "AccountClerkRpcUnavailableError",
]
