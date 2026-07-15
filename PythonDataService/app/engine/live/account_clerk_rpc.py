"""Bounded, versioned Unix-socket RPC for the Account Clerk authority boundary."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.broker.ibkr.models import IbkrOrderEvent
from app.engine.live.account_artifacts import read_account_clerk_generation
from app.engine.live.account_clerk import (
    AccountClerk,
    AccountClerkBrokerAckReceipt,
    AccountClerkGenerationFencedError,
    AccountClerkIntentRejected,
    AccountClerkRecoveryFlattenReceipt,
    account_clerk_socket_path,
)
from app.engine.live.account_owner import AccountOwnerSubmitIntent
from app.engine.live.journal_exposure import normalize_broker_event

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

    def __init__(self, *, artifacts_root: Path, account_id: str) -> None:
        self._artifacts_root = artifacts_root
        self._account_id = account_id
        self._socket_path = account_clerk_socket_path(artifacts_root, account_id)

    async def verify_generation(self) -> int:
        """Verify that the reachable socket serves the durable account generation."""

        operation = "generation_handshake"
        request_identity = AccountClerkRpcRequestIdentity(intent_id=None, order_ref=None)
        if not self._socket_path.exists():
            raise AccountClerkRpcUnavailableError(
                reason="SOCKET_MISSING",
                operation=operation,
                request_identity=request_identity,
            )

        writer: asyncio.StreamWriter | None = None
        try:
            async with _request_timeout(ACCOUNT_CLERK_RPC_NORMAL_TIMEOUT_S):
                reader, writer, served_generation = await self._open_generation_checked_connection(
                    operation=operation,
                    request_identity=request_identity,
                )
                del reader
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

        return served_generation

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
            events = payload["events"]
            if not isinstance(events, list):
                raise TypeError("drain_events payload must be a list")
            normalized = [normalize_broker_event(event) for event in events]
            if any(event is None for event in normalized):
                raise ValueError("drain_events payload contains an invalid broker event")
            return [event for event in normalized if event is not None]
        except (KeyError, ValidationError, TypeError, ValueError) as exc:
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
                reader, writer, _served_generation = await self._open_generation_checked_connection(
                    operation=operation,
                    request_identity=request_identity,
                )
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

    async def _open_generation_checked_connection(
        self,
        *,
        operation: str,
        request_identity: AccountClerkRpcRequestIdentity,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, int]:
        try:
            reader, writer = await asyncio.open_unix_connection(str(self._socket_path))
        except OSError as exc:
            raise AccountClerkRpcUnavailableError(
                reason="SOCKET_CONNECT_FAILED",
                operation=operation,
                request_identity=request_identity,
            ) from exc
        try:
            handshake = _decode_generation_handshake(
                await reader.readline(),
                operation=operation,
                request_identity=request_identity,
            )
            if handshake.account_id != self._account_id:
                raise AccountClerkRpcMalformedResponseError(
                    operation=operation,
                    request_identity=request_identity,
                )
            durable_generation = self._durable_generation()
            if handshake.served_generation != durable_generation:
                raise AccountClerkRpcGenerationMismatchError(
                    expected_generation=durable_generation,
                    served_generation=handshake.served_generation,
                    operation=operation,
                    request_identity=request_identity,
                )
            return reader, writer, handshake.served_generation
        except Exception:
            writer.close()
            await writer.wait_closed()
            raise

    def _durable_generation(self) -> int:
        try:
            generation = read_account_clerk_generation(self._artifacts_root, self._account_id)
        except (OSError, ValueError) as exc:
            raise AccountClerkRpcUnavailableError(
                reason="DURABLE_GENERATION_UNAVAILABLE",
                operation="generation_handshake",
                request_identity=AccountClerkRpcRequestIdentity(intent_id=None, order_ref=None),
            ) from exc
        if generation is None:
            raise AccountClerkRpcUnavailableError(
                reason="DURABLE_GENERATION_MISSING",
                operation="generation_handshake",
                request_identity=AccountClerkRpcRequestIdentity(intent_id=None, order_ref=None),
            )
        return generation.generation


class AccountClerkRpcServer:
    """Clerk-process RPC server; the broker stays exclusively behind this seam."""

    def __init__(self, clerk: AccountClerk) -> None:
        self._clerk = clerk
        self._server: asyncio.AbstractServer | None = None
        self._socket_path = account_clerk_socket_path(clerk._artifacts_root, clerk._account_id)
        if clerk._clerk_generation is None:
            raise RuntimeError("ACCOUNT_CLERK_GENERATION_REQUIRED_FOR_RPC")
        self._served_generation = clerk._clerk_generation
        self._events_by_namespace: dict[str, list[IbkrOrderEvent]] = {}
        self._callback_queue: asyncio.Queue[IbkrOrderEvent] = asyncio.Queue()
        self._callback_worker: asyncio.Task[None] | None = None
        set_callback = getattr(clerk._broker, "set_broker_callback_sink", None)
        if callable(set_callback):
            set_callback(self._record_broker_event)

    async def start(self) -> None:
        self._socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self._socket_path.exists():
            self._socket_path.unlink()
        # Restore durable attribution before the Clerk starts broker streaming.
        await self._clerk.rebuild_attribution()
        self._callback_worker = asyncio.create_task(
            self._persist_broker_callbacks(),
            name="account-clerk-broker-callback-writer",
        )
        self._server = await asyncio.start_unix_server(self._handle, path=str(self._socket_path))
        self._socket_path.chmod(0o600)

    async def close(self) -> None:
        await self._flush_broker_callbacks()
        if self._callback_worker is not None:
            self._callback_worker.cancel()
            with suppress(asyncio.CancelledError):
                await self._callback_worker
            self._callback_worker = None
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        if self._socket_path.exists():
            self._socket_path.unlink()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        operation = "unknown"
        try:
            writer.write(
                (
                    AccountClerkRpcGenerationHandshake(
                        account_id=self._clerk._account_id,
                        served_generation=self._served_generation,
                    ).model_dump_json()
                    + "\n"
                ).encode()
            )
            await writer.drain()
            request = _decode_request(await reader.readline())
            operation = _request_operation(request)
            response: AccountClerkRpcSuccessEnvelope | AccountClerkRpcErrorEnvelope = await self._dispatch(request)
        except _AccountClerkRpcRequestRejected as exc:
            response = _rejected_envelope(exc.reason)
        except AccountClerkIntentRejected as exc:
            response = _rejected_envelope(exc.reason)
        except AccountClerkGenerationFencedError:
            response = _rejected_envelope("CLERK_GENERATION_STALE")
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
            return AccountClerkRpcSuccessEnvelope(
                payload={"recovery_flatten": recovery.model_dump(mode="json")}
            )
        namespace = _required_string(request, "bot_order_namespace")
        events = self._events_by_namespace.pop(namespace, [])
        return AccountClerkRpcSuccessEnvelope(
            payload={"events": [event.model_dump(mode="json") for event in events]}
        )

    def _record_broker_event(self, event: IbkrOrderEvent) -> None:
        """Queue broker callbacks; disk work is serialized off the event loop."""

        self._callback_queue.put_nowait(event)

    async def _persist_broker_callbacks(self) -> None:
        """Fsync callbacks before they enter the non-authoritative relay cache."""

        while True:
            event = await self._callback_queue.get()
            try:
                receipt = await self._clerk.record_broker_event(event)
                if receipt.newly_recorded and receipt.intent is not None:
                    namespace = receipt.intent.bot_order_namespace
                    self._events_by_namespace.setdefault(namespace, []).append(event)
            finally:
                self._callback_queue.task_done()

    async def _flush_broker_callbacks(self) -> None:
        """Complete callbacks received before normal Clerk shutdown."""

        if self._callback_worker is not None:
            await self._callback_queue.join()


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


def _decode_generation_handshake(
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
    "AccountClerkRpcGenerationHandshake",
    "AccountClerkRpcGenerationMismatchError",
    "AccountClerkRpcInternalError",
    "AccountClerkRpcMalformedResponseError",
    "AccountClerkRpcRejectedError",
    "AccountClerkRpcRequestIdentity",
    "AccountClerkRpcServer",
    "AccountClerkRpcSuccessEnvelope",
    "AccountClerkRpcTimeoutError",
    "AccountClerkRpcUnavailableError",
]
