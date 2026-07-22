"""Bounded, versioned Unix-socket RPC for the Account Clerk authority boundary."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from app.broker.ibkr.models import IbkrOrderEvent
from app.engine.live.account_artifacts import read_account_clerk_generation
from app.engine.live.account_clerk import (
    AccountClerk,
    AccountClerkBrokerAckReceipt,
    AccountClerkCancelNamespaceReceipt,
    AccountClerkCancelNamespaceUncertainError,
    AccountClerkEmergencyFlattenIncompleteError,
    AccountClerkEmergencyFlattenReceipt,
    AccountClerkGenerationFencedError,
    AccountClerkIntentRejected,
    AccountClerkRecoveryFlattenReceipt,
    account_clerk_socket_path,
    read_account_clerk_journal,
)
from app.engine.live.account_clerk_cursor import (
    AccountClerkEventConsumerIdentity,
    AccountClerkEventCursorRepo,
)
from app.engine.live.account_clerk_journal import normalize_broker_event
from app.engine.live.account_clerk_journal_models import AccountClerkEmergencyAuthorization
from app.engine.live.account_clerk_rpc_protocol import (
    ACCOUNT_CLERK_RPC_NORMAL_TIMEOUT_S,
    ACCOUNT_CLERK_RPC_RECOVERY_TIMEOUT_S,
    ACCOUNT_CLERK_RPC_SCHEMA_VERSION,
    AccountClerkRpcCancelNamespaceUncertainError,  # noqa: F401 - public compatibility re-export
    AccountClerkRpcError,
    AccountClerkRpcErrorEnvelope,
    AccountClerkRpcGenerationHandshake,
    AccountClerkRpcGenerationMismatchError,
    AccountClerkRpcInternalError,
    AccountClerkRpcMalformedResponseError,
    AccountClerkRpcOperation,  # noqa: F401 - public compatibility re-export
    AccountClerkRpcRejectedError,
    AccountClerkRpcRequestIdentity,
    AccountClerkRpcServerErrorCode,  # noqa: F401 - public compatibility re-export
    AccountClerkRpcSuccessEnvelope,
    AccountClerkRpcTimeoutError,
    AccountClerkRpcUnavailableError,
    _AccountClerkRpcRequestRejected,
    decode_generation_handshake,
    decode_request,
    decode_response,
    optional_string,
    rejected_envelope,
    request_identity,
    request_object,
    request_operation,
    request_timeout_s,
    required_nonnegative_int,
    required_string,
)
from app.engine.live.account_clerk_rpc_protocol import (
    WRITE_OPERATIONS as _WRITE_OPERATIONS,
)
from app.engine.live.account_owner import AccountOwnerSubmitIntent
from app.engine.live.account_registry import (
    ACTIVE_INSTANCE_BINDING_STATES,
    AccountInstanceBinding,
    BindingRetirementFoldResult,
    index_account_instance_bindings,
    read_account_instance_registry,
)
from app.schemas.journal_cures import JournalCureReceipt, JournalCureRequest
from app.services.bot_deletion import BotDeletionCorruptError, bot_retirement_is_pending
from app.services.journal_cures import JournalCureError, JournalCureHandler

logger = logging.getLogger(__name__)

# Deliberately a module seam: timeout tests replace it with a barrier-controlled
# context manager, while production always uses asyncio.timeout.
_request_timeout = asyncio.timeout


@dataclass(frozen=True)
class AccountClerkDeliveredEvent:
    """One at-least-once Clerk journal delivery to a bot run.

    ``acknowledge_after_durable_event_write`` is intentionally separate from
    drain: callers invoke it only after their run-scoped durable callback WAL
    accepted (or proved it already contains) this journal sequence.
    """

    journal_seq: int
    event: IbkrOrderEvent
    _consumer: AccountClerkEventConsumerIdentity
    _cursor: AccountClerkEventCursorRepo

    def acknowledge_after_durable_event_write(self) -> bool:
        """Durably advance this bot's cursor after its own event fsync."""

        return self._cursor.advance_after_durable_event_write(
            self._consumer,
            journal_seq=self.journal_seq,
        )


class AccountClerkCallbackPersistenceError(RuntimeError):
    """The Clerk can no longer durably accept broker callbacks."""

    def __init__(self, failure: BaseException) -> None:
        super().__init__("ACCOUNT_CLERK_CALLBACK_PERSISTENCE_FAILED")
        self.failure = failure


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
                request_identity=request_identity({"intent": intent.model_dump(mode="json")}),
            ) from exc

    async def authorize_emergency_flatten(
        self,
        *,
        operation_id: str,
        reconciliation_evidence_version: str,
    ) -> AccountClerkEmergencyAuthorization:
        """Ask the Clerk to issue a short-lived, exact-confirmation receipt."""

        request = {
            "operation": "authorize_emergency_flatten",
            "operation_id": operation_id,
            "confirmation_token": "FLATTEN",
            "reconciliation_evidence_version": reconciliation_evidence_version,
            "no_exact_recovery_candidate": True,
        }
        payload = await self._request(request)
        try:
            return AccountClerkEmergencyAuthorization.model_validate(payload["authorization"])
        except (KeyError, ValidationError, TypeError) as exc:
            raise AccountClerkRpcMalformedResponseError(
                operation="authorize_emergency_flatten",
                request_identity=request_identity(request),
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
                request_identity=request_identity(request),
            ) from exc

    async def submit_recovery_flatten_batch(
        self,
        intents: tuple[AccountOwnerSubmitIntent, ...],
    ) -> tuple[AccountClerkRecoveryFlattenReceipt, ...]:
        """Ask the Clerk to cancel once and flatten every exposed symbol."""

        if not intents:
            raise ValueError("recovery batch must include at least one intent")
        first = intents[0]
        request = {
            "operation": "recovery_flatten_batch",
            "actor": "bot",
            "actor_strategy_instance_id": first.strategy_instance_id,
            "actor_run_id": first.run_id,
            "actor_bot_order_namespace": first.bot_order_namespace,
            "intents": [intent.model_dump(mode="json") for intent in intents],
        }
        payload = await self._request(request)
        try:
            values = payload["recovery_flattened"]
            if not isinstance(values, list):
                raise TypeError("recovery batch payload must be a list")
            return tuple(AccountClerkRecoveryFlattenReceipt.model_validate(value) for value in values)
        except (KeyError, ValidationError, TypeError) as exc:
            raise AccountClerkRpcMalformedResponseError(
                operation="recovery_flatten_batch",
                request_identity=request_identity(request),
            ) from exc

    async def cancel_namespace(
        self,
        intent: AccountOwnerSubmitIntent,
    ) -> AccountClerkCancelNamespaceReceipt:
        """Request one durable, terminal namespace cancellation."""

        request = {"operation": "cancel_namespace", "intent": intent.model_dump(mode="json")}
        payload = await self._request(request)
        try:
            return AccountClerkCancelNamespaceReceipt.model_validate(payload["cancel_confirmed"])
        except (KeyError, ValidationError, TypeError) as exc:
            raise AccountClerkRpcMalformedResponseError(
                operation="cancel_namespace",
                request_identity=request_identity(request),
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
                request_identity=request_identity(request),
            ) from exc

    async def apply_operator_adjustment(self, request: JournalCureRequest) -> JournalCureReceipt:
        """Ask the live Clerk to serialize one stale-claim cure."""

        rpc_request = {"operation": "operator_adjustment", "request": request.model_dump(mode="json")}
        payload = await self._request(rpc_request)
        try:
            return JournalCureReceipt.model_validate(payload["operator_adjustment"])
        except (KeyError, ValidationError, TypeError) as exc:
            raise AccountClerkRpcMalformedResponseError(
                operation="operator_adjustment",
                request_identity=request_identity(rpc_request),
            ) from exc

    async def fold_binding_retirements(self) -> BindingRetirementFoldResult:
        """Ask an already-running Clerk to fold queued daemon liveness facts."""

        request = {"operation": "fold_binding_retirements"}
        payload = await self._request(request)
        try:
            result = payload["binding_retirement_fold"]
            if not isinstance(result, dict):
                raise TypeError("binding retirement fold payload must be an object")
            values = {
                field: result[field]
                for field in (
                    "proposals_seen",
                    "retirements_applied",
                    "superseded_proposals",
                )
            }
            if any(type(value) is not int or value < 0 for value in values.values()):
                raise TypeError("binding retirement fold counters must be non-negative integers")
            return BindingRetirementFoldResult(**values)
        except (KeyError, TypeError) as exc:
            raise AccountClerkRpcMalformedResponseError(
                operation="fold_binding_retirements",
                request_identity=request_identity(request),
            ) from exc

    async def drain_events(
        self,
        *,
        after_seq: int,
        consumer: AccountClerkEventConsumerIdentity,
        cursor: AccountClerkEventCursorRepo,
    ) -> list[AccountClerkDeliveredEvent]:
        """Read non-destructive Clerk journal rows after a durable cursor.

        A bot cannot claim a sequence different from its persisted cursor.
        That prevents a stale in-memory loop from consuming events on behalf of
        a restarted run while preserving intentional at-least-once recovery
        when a crash happened before a cursor acknowledgement.
        """

        if after_seq < 0:
            raise ValueError("after_seq must be >= 0")
        if consumer.account_id != self._account_id:
            raise ValueError("consumer account_id does not match this Clerk client")
        if cursor.last_journal_seq(consumer) != after_seq:
            raise ValueError("after_seq does not match the durable Clerk event cursor")
        request = {
            "operation": "drain_events",
            "after_seq": after_seq,
            **consumer.model_dump(mode="json"),
        }
        payload = await self._request(request)
        try:
            events = payload["events"]
            if not isinstance(events, list):
                raise TypeError("drain_events payload must be a list")
            deliveries: list[AccountClerkDeliveredEvent] = []
            last_journal_seq = after_seq
            for item in events:
                if not isinstance(item, dict):
                    raise TypeError("drain_events delivery must be an object")
                journal_seq = item.get("journal_seq")
                if not isinstance(journal_seq, int) or isinstance(journal_seq, bool):
                    raise TypeError("drain_events journal_seq must be an integer")
                if journal_seq <= last_journal_seq:
                    raise ValueError("drain_events journal sequences must be strictly ordered")
                event = normalize_broker_event(item.get("event"))
                if event is None:
                    raise ValueError("drain_events payload contains an invalid broker event")
                deliveries.append(
                    AccountClerkDeliveredEvent(
                        journal_seq=journal_seq,
                        event=event,
                        _consumer=consumer,
                        _cursor=cursor,
                    )
                )
                last_journal_seq = journal_seq
            return deliveries
        except (KeyError, ValidationError, TypeError, ValueError) as exc:
            raise AccountClerkRpcMalformedResponseError(
                operation="drain_events",
                request_identity=request_identity(request),
            ) from exc

    async def _request(self, request: dict[str, object]) -> dict[str, object]:
        operation = request_operation(request)
        request_identity_value = request_identity(request)
        if not self._socket_path.exists():
            raise AccountClerkRpcUnavailableError(
                reason="SOCKET_MISSING",
                operation=operation,
                request_identity=request_identity_value,
            )

        writer: asyncio.StreamWriter | None = None
        try:
            async with _request_timeout(request_timeout_s(operation)):
                reader, writer, _served_generation = await self._open_generation_checked_connection(
                    operation=operation,
                    request_identity=request_identity_value,
                )
                writer.write((json.dumps(request) + "\n").encode())
                await writer.drain()
                line = await reader.readline()
        except TimeoutError as exc:
            raise AccountClerkRpcTimeoutError(
                operation=operation,
                request_identity=request_identity_value,
            ) from exc
        except (ConnectionError, OSError) as exc:
            raise AccountClerkRpcUnavailableError(
                reason="SOCKET_CONNECTION_LOST",
                operation=operation,
                request_identity=request_identity_value,
            ) from exc
        finally:
            if writer is not None:
                writer.close()
                await writer.wait_closed()

        if not line:
            raise AccountClerkRpcUnavailableError(
                reason="EMPTY_RESPONSE",
                operation=operation,
                request_identity=request_identity_value,
            )
        return decode_response(
            line,
            operation=operation,
            request_identity=request_identity_value,
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
            handshake = decode_generation_handshake(
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


class AccountClerkHostRpcClient(AccountClerkRpcClient):
    """Host-only Clerk client carrying the daemon's private binding capability."""

    def __init__(self, *, artifacts_root: Path, account_id: str, host_capability: str) -> None:
        super().__init__(artifacts_root=artifacts_root, account_id=account_id)
        if not host_capability:
            raise ValueError("host_capability must not be empty")
        self._host_capability = host_capability

    async def record_binding_decision(
        self,
        binding: AccountInstanceBinding,
    ) -> AccountInstanceBinding:
        """Request a Clerk binding transition with the host-only capability."""

        request = {
            "operation": "record_binding_decision",
            "binding": binding.model_dump(mode="json"),
            "host_capability": self._host_capability,
        }
        payload = await self._request(request)
        try:
            return AccountInstanceBinding.model_validate(payload["binding_decision"])
        except (KeyError, ValidationError, TypeError) as exc:
            raise AccountClerkRpcMalformedResponseError(
                operation="record_binding_decision",
                request_identity=request_identity(request),
            ) from exc

    async def emergency_flatten_account(
        self,
        *,
        operation_id: str,
        authorization_id: str,
    ) -> AccountClerkEmergencyFlattenReceipt:
        """Ask the current Clerk—not a daemon subprocess—to flatten its account."""

        request = {
            "operation": "emergency_flatten_account",
            "operation_id": operation_id,
            "authorization_id": authorization_id,
            "host_capability": self._host_capability,
        }
        payload = await self._request(request)
        try:
            return AccountClerkEmergencyFlattenReceipt.model_validate(payload["emergency_flatten"])
        except (KeyError, ValidationError, TypeError) as exc:
            raise AccountClerkRpcMalformedResponseError(
                operation="emergency_flatten_account",
                request_identity=request_identity(request),
            ) from exc

    async def prepare_emergency_flatten(self, *, operation_id: str, authorization_id: str) -> None:
        """Close Clerk intake before this daemon pauses bot processes."""

        request = {
            "operation": "prepare_emergency_flatten",
            "operation_id": operation_id,
            "authorization_id": authorization_id,
            "host_capability": self._host_capability,
        }
        await self._request(request)

    async def mark_emergency_bots_paused(self, *, operation_id: str, authorization_id: str) -> None:
        """Persist host-proven termination of every on-duty account bot."""

        await self._request(
            {
                "operation": "mark_emergency_bots_paused",
                "operation_id": operation_id,
                "authorization_id": authorization_id,
                "host_capability": self._host_capability,
            }
        )

    async def mark_emergency_requires_reconciliation(self, *, operation_id: str, reason: str) -> None:
        """Persist a host pause failure without attempting any broker action."""

        await self._request(
            {
                "operation": "mark_emergency_requires_reconciliation",
                "operation_id": operation_id,
                "reason": reason,
                "host_capability": self._host_capability,
            }
        )


class AccountClerkRpcServer:
    """Clerk-process RPC server; the broker stays exclusively behind this seam."""

    def __init__(
        self,
        clerk: AccountClerk,
        *,
        on_callback_persistence_failure: Callable[[BaseException], None] | None = None,
        operator_adjustment_handler: JournalCureHandler | None = None,
    ) -> None:
        self._clerk = clerk
        self._server: asyncio.AbstractServer | None = None
        self._socket_path = account_clerk_socket_path(clerk._artifacts_root, clerk._account_id)
        if clerk._clerk_generation is None:
            raise RuntimeError("ACCOUNT_CLERK_GENERATION_REQUIRED_FOR_RPC")
        self._served_generation = clerk._clerk_generation
        self._callback_queue: asyncio.Queue[IbkrOrderEvent] = asyncio.Queue()
        self._callback_worker: asyncio.Task[None] | None = None
        self._callback_failure: BaseException | None = None
        self._on_callback_persistence_failure = on_callback_persistence_failure
        self._operator_adjustment_handler = operator_adjustment_handler
        clerk.set_callback_drain(lambda: self._flush_broker_callbacks(raise_on_failure=True))
        self._closing = False
        set_callback = getattr(clerk._broker, "set_broker_callback_sink", None)
        if callable(set_callback):
            set_callback(self._record_broker_event)

    async def start(self) -> None:
        self._socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self._socket_path.exists():
            self._socket_path.unlink()
        # Restore durable attribution before the Clerk starts broker streaming.
        await self._clerk.rebuild_attribution()
        # Daemon liveness is evidence, not a binding-state authority. Fold its
        # ordered proposals before publishing this Clerk socket to any bot.
        await self._clerk.fold_binding_retirement_proposals()
        self._callback_worker = asyncio.create_task(
            self._persist_broker_callbacks(),
            name="account-clerk-broker-callback-writer",
        )
        # Reconciliation happens while the callback durability worker is live
        # but before the socket is published. No bot can race a recovery
        # classification into a broker write.
        await self._clerk.recover_account_state()
        self._server = await asyncio.start_unix_server(self._handle, path=str(self._socket_path))
        self._socket_path.chmod(0o600)

    async def close(self) -> None:
        # Fence before awaiting the callback drain: otherwise a request already
        # accepted by the Unix server can place an order after its callback
        # writer has been stopped.
        self._closing = True
        self._clerk.close_normal_submit_intake()
        await self._clerk.wait_for_broker_writes_quiesced()
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
            request = decode_request(await reader.readline())
            operation = request_operation(request)
            response: AccountClerkRpcSuccessEnvelope | AccountClerkRpcErrorEnvelope = await self._dispatch(request)
        except _AccountClerkRpcRequestRejected as exc:
            response = rejected_envelope(exc.reason)
        except AccountClerkIntentRejected as exc:
            response = rejected_envelope(exc.reason)
        except JournalCureError as exc:
            response = rejected_envelope(exc.reason_code)
        except AccountClerkGenerationFencedError:
            response = rejected_envelope("CLERK_GENERATION_STALE")
        except AccountClerkCancelNamespaceUncertainError:
            response = AccountClerkRpcErrorEnvelope(
                reason_code="ACCOUNT_CLERK_CANCEL_NAMESPACE_UNCERTAIN"
            )
        except AccountClerkEmergencyFlattenIncompleteError as exc:
            response = rejected_envelope(str(exc))
        except RuntimeError as exc:
            # Clerk policy rejections are intentionally stable, public reason
            # codes. Unexpected runtime failures continue to the internal
            # error path below rather than exposing implementation details.
            reason = str(exc)
            if reason.startswith("CLERK_"):
                response = rejected_envelope(reason)
            else:
                response = AccountClerkRpcErrorEnvelope(reason_code="ACCOUNT_CLERK_INTERNAL_ERROR")
        except (json.JSONDecodeError, ValidationError):
            response = rejected_envelope("INVALID_REQUEST")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(
                "Account Clerk RPC server failure",
                exc_info=True,
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
        operation = request_operation(request)
        if self._closing and operation in _WRITE_OPERATIONS:
            raise _AccountClerkRpcRequestRejected("CLERK_RPC_CLOSED")
        if operation == "submit":
            intent = AccountOwnerSubmitIntent.model_validate(request_object(request, "intent"))
            recorded, broker_acked = await self._clerk.submit_intent(intent)
            return AccountClerkRpcSuccessEnvelope(
                payload={
                    "recorded": recorded.model_dump(mode="json"),
                    "broker_acked": broker_acked.model_dump(mode="json"),
                }
            )
        if operation == "emergency_flatten_account":
            receipt = await self._clerk.emergency_flatten_account(
                operation_id=required_string(request, "operation_id"),
                authorization_id=required_string(request, "authorization_id"),
                host_capability=required_string(request, "host_capability"),
            )
            return AccountClerkRpcSuccessEnvelope(
                payload={"emergency_flatten": receipt.model_dump(mode="json")}
            )
        if operation == "prepare_emergency_flatten":
            await self._clerk.prepare_emergency_flatten(
                operation_id=required_string(request, "operation_id"),
                authorization_id=required_string(request, "authorization_id"),
                host_capability=required_string(request, "host_capability"),
            )
            return AccountClerkRpcSuccessEnvelope(payload={})
        if operation == "mark_emergency_bots_paused":
            await self._clerk.mark_emergency_bots_paused(
                operation_id=required_string(request, "operation_id"),
                authorization_id=required_string(request, "authorization_id"),
                host_capability=required_string(request, "host_capability"),
            )
            return AccountClerkRpcSuccessEnvelope(payload={})
        if operation == "mark_emergency_requires_reconciliation":
            await self._clerk.mark_emergency_requires_reconciliation(
                operation_id=required_string(request, "operation_id"),
                reason=required_string(request, "reason"),
                host_capability=required_string(request, "host_capability"),
            )
            return AccountClerkRpcSuccessEnvelope(payload={})
        if operation == "authorize_emergency_flatten":
            if request.get("confirmation_token") != "FLATTEN" or request.get("no_exact_recovery_candidate") is not True:
                raise _AccountClerkRpcRequestRejected("INVALID_EMERGENCY_AUTHORIZATION")
            authorization = await self._clerk.authorize_emergency_flatten(
                operation_id=required_string(request, "operation_id"),
                confirmation_token="FLATTEN",
                reconciliation_evidence_version=required_string(request, "reconciliation_evidence_version"),
                no_exact_recovery_candidate=True,
            )
            return AccountClerkRpcSuccessEnvelope(
                payload={"authorization": authorization.model_dump(mode="json")}
            )
        if operation == "recovery_flatten":
            intent = AccountOwnerSubmitIntent.model_validate(request_object(request, "intent"))
            actor = request.get("actor")
            if actor not in ("bot", "operator"):
                raise _AccountClerkRpcRequestRejected("INVALID_RECOVERY_ACTOR")
            recovery = await self._clerk.submit_recovery_flatten(
                intent,
                actor=actor,
                actor_strategy_instance_id=optional_string(request, "actor_strategy_instance_id"),
                actor_run_id=optional_string(request, "actor_run_id"),
                actor_bot_order_namespace=optional_string(request, "actor_bot_order_namespace"),
            )
            return AccountClerkRpcSuccessEnvelope(
                payload={"recovery_flatten": recovery.model_dump(mode="json")}
            )
        if operation == "recovery_flatten_batch":
            intents_data = request.get("intents")
            if not isinstance(intents_data, list):
                raise _AccountClerkRpcRequestRejected("INVALID_REQUEST")
            intents = tuple(AccountOwnerSubmitIntent.model_validate(value) for value in intents_data)
            actor = request.get("actor")
            if actor not in ("bot", "operator"):
                raise _AccountClerkRpcRequestRejected("INVALID_RECOVERY_ACTOR")
            recovery = await self._clerk.submit_recovery_flatten_batch(
                intents,
                actor=actor,
                actor_strategy_instance_id=optional_string(request, "actor_strategy_instance_id"),
                actor_run_id=optional_string(request, "actor_run_id"),
                actor_bot_order_namespace=optional_string(request, "actor_bot_order_namespace"),
            )
            return AccountClerkRpcSuccessEnvelope(
                payload={"recovery_flattened": [item.model_dump(mode="json") for item in recovery]}
            )
        if operation == "cancel_namespace":
            intent = AccountOwnerSubmitIntent.model_validate(request_object(request, "intent"))
            receipt = await self._clerk.cancel_namespace(intent)
            return AccountClerkRpcSuccessEnvelope(
                payload={"cancel_confirmed": receipt.model_dump(mode="json")}
            )
        if operation == "operator_adjustment":
            cure_request = JournalCureRequest.model_validate(request_object(request, "request"))
            if self._operator_adjustment_handler is None:
                raise _AccountClerkRpcRequestRejected("OPERATOR_ADJUSTMENT_UNAVAILABLE")
            receipt = await self._operator_adjustment_handler(cure_request)
            return AccountClerkRpcSuccessEnvelope(
                payload={"operator_adjustment": receipt.model_dump(mode="json")}
            )
        if operation == "record_binding_decision":
            binding = AccountInstanceBinding.model_validate(request_object(request, "binding"))
            recorded = await self._clerk.record_binding_decision(
                binding,
                host_capability=required_string(request, "host_capability"),
            )
            return AccountClerkRpcSuccessEnvelope(
                payload={"binding_decision": recorded.model_dump(mode="json")}
            )
        if operation == "fold_binding_retirements":
            folded = await self._clerk.fold_binding_retirement_proposals()
            return AccountClerkRpcSuccessEnvelope(
                payload={
                    "binding_retirement_fold": {
                        "proposals_seen": folded.proposals_seen,
                        "retirements_applied": folded.retirements_applied,
                        "superseded_proposals": folded.superseded_proposals,
                    }
                }
            )
        consumer = self._validated_event_consumer(request)
        after_seq = required_nonnegative_int(request, "after_seq")
        events = await asyncio.to_thread(self._journal_events_after, consumer, after_seq)
        return AccountClerkRpcSuccessEnvelope(
            payload={"events": events}
        )

    def _validated_event_consumer(
        self,
        request: Mapping[str, object],
    ) -> AccountClerkEventConsumerIdentity:
        """Reject any consumer that is not the active full registry identity."""

        consumer = AccountClerkEventConsumerIdentity.model_validate(
            {
                "account_id": required_string(request, "account_id"),
                "strategy_instance_id": required_string(request, "strategy_instance_id"),
                "run_id": required_string(request, "run_id"),
                "bot_order_namespace": required_string(request, "bot_order_namespace"),
            }
        )
        if consumer.account_id != self._clerk._account_id:
            raise _AccountClerkRpcRequestRejected("EVENT_CONSUMER_ACCOUNT_MISMATCH")
        try:
            if bot_retirement_is_pending(
                self._clerk._artifacts_root,
                consumer.strategy_instance_id,
            ):
                raise _AccountClerkRpcRequestRejected("RETIREMENT_PENDING")
        except (BotDeletionCorruptError, ValueError) as exc:
            raise _AccountClerkRpcRequestRejected("RETIREMENT_TRANSITION_UNREADABLE") from exc
        binding_index = index_account_instance_bindings(
            read_account_instance_registry(self._clerk._artifacts_root, self._clerk._account_id),
            account_id=self._clerk._account_id,
        )
        binding = binding_index.latest_by_instance.get(consumer.strategy_instance_id)
        if (
            binding is None
            or binding.lifecycle_state not in ACTIVE_INSTANCE_BINDING_STATES
            or binding.account_id != consumer.account_id
            or binding.run_id != consumer.run_id
            or binding.bot_order_namespace != consumer.bot_order_namespace
        ):
            raise _AccountClerkRpcRequestRejected("STALE_EVENT_CONSUMER")
        return consumer

    def _journal_events_after(
        self,
        consumer: AccountClerkEventConsumerIdentity,
        after_seq: int,
    ) -> list[dict[str, object]]:
        """Project only this active consumer's ordered callback journal rows."""

        deliveries: list[dict[str, object]] = []
        for entry in read_account_clerk_journal(self._clerk._artifacts_root, self._clerk._account_id):
            if entry.seq <= after_seq or entry.entry_kind != "broker_event" or entry.intent is None:
                continue
            if (
                entry.intent.account_id != consumer.account_id
                or entry.intent.strategy_instance_id != consumer.strategy_instance_id
                or entry.intent.run_id != consumer.run_id
                or entry.intent.bot_order_namespace != consumer.bot_order_namespace
            ):
                continue
            event = normalize_broker_event(entry.broker_event)
            if event is None:
                raise RuntimeError("ACCOUNT_CLERK_JOURNAL_BROKER_EVENT_INVALID")
            deliveries.append(
                {
                    "journal_seq": entry.seq,
                    "event": event.model_dump(mode="json"),
                }
            )
        return deliveries

    def _record_broker_event(self, event: IbkrOrderEvent) -> None:
        """Queue broker callbacks; disk work is serialized off the event loop."""

        if self._callback_failure is not None:
            raise AccountClerkCallbackPersistenceError(self._callback_failure)
        self._callback_queue.put_nowait(event)

    async def _persist_broker_callbacks(self) -> None:
        """Fsync callbacks before they enter the non-authoritative relay cache."""

        while True:
            event = await self._callback_queue.get()
            try:
                await self._clerk.record_broker_event(event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # This is the same safety boundary as a dead broker stream:
                # once a callback cannot be durably recorded, no future normal
                # broker write may start. Set the failure before awaiting the
                # alarm fsync so receipt-time callbacks reject immediately.
                self._callback_failure = exc
                try:
                    await self._clerk.mark_event_stream_down(exc)
                except Exception:
                    logger.exception(
                        "Account Clerk callback persistence failure could not write its alarm",
                        extra={"account_id": self._clerk._account_id},
                    )
                if self._on_callback_persistence_failure is not None:
                    try:
                        self._on_callback_persistence_failure(exc)
                    except Exception:
                        logger.exception(
                            "Account Clerk callback persistence failure hook raised",
                            extra={"account_id": self._clerk._account_id},
                        )
                self._discard_queued_callbacks_after_failure()
                logger.exception(
                    "Account Clerk callback persistence failed; Clerk intake is closed",
                    extra={"account_id": self._clerk._account_id},
                )
                return
            finally:
                self._callback_queue.task_done()

    def _discard_queued_callbacks_after_failure(self) -> None:
        """Balance every queued task so shutdown never waits on a dead worker."""

        while True:
            try:
                self._callback_queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            self._callback_queue.task_done()

    async def _flush_broker_callbacks(self, *, raise_on_failure: bool = False) -> None:
        """Complete callbacks received before normal Clerk shutdown."""

        if self._callback_worker is not None:
            await self._callback_queue.join()
        if raise_on_failure and self._callback_failure is not None:
            raise AccountClerkCallbackPersistenceError(self._callback_failure)


__all__ = [
    "ACCOUNT_CLERK_RPC_NORMAL_TIMEOUT_S",
    "ACCOUNT_CLERK_RPC_RECOVERY_TIMEOUT_S",
    "ACCOUNT_CLERK_RPC_SCHEMA_VERSION",
    "AccountClerkCallbackPersistenceError",
    "AccountClerkDeliveredEvent",
    "AccountClerkEventConsumerIdentity",
    "AccountClerkEventCursorRepo",
    "AccountClerkHostRpcClient",
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
