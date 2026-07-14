"""Unix-socket transport for the Account Clerk authority boundary."""

from __future__ import annotations

import asyncio
import json

from app.broker.ibkr.models import IbkrOrderEvent
from app.engine.live.account_artifacts import append_account_event
from app.engine.live.account_clerk import (
    AccountClerk,
    AccountClerkBrokerAckReceipt,
    AccountClerkRecoveryFlattenReceipt,
    account_clerk_socket_path,
)
from app.engine.live.account_owner import AccountOwnerSubmitIntent


class AccountClerkRpcClient:
    """Bot-side client: enqueue intents; it never holds a broker adapter."""

    def __init__(self, *, artifacts_root, account_id: str) -> None:
        self._socket_path = account_clerk_socket_path(artifacts_root, account_id)

    async def submit(self, intent: AccountOwnerSubmitIntent) -> AccountClerkBrokerAckReceipt:
        payload = await self._request({"operation": "submit", "intent": intent.model_dump(mode="json")})
        return AccountClerkBrokerAckReceipt.model_validate(payload["broker_acked"])

    async def submit_recovery_flatten(self, intent: AccountOwnerSubmitIntent) -> AccountClerkRecoveryFlattenReceipt:
        """Ask the Clerk to flatten the calling bot's own namespace."""

        payload = await self._request(
            {
                "operation": "recovery_flatten",
                "actor": "bot",
                "actor_strategy_instance_id": intent.strategy_instance_id,
                "actor_run_id": intent.run_id,
                "actor_bot_order_namespace": intent.bot_order_namespace,
                "intent": intent.model_dump(mode="json"),
            }
        )
        return AccountClerkRecoveryFlattenReceipt.model_validate(payload["recovery_flatten"])

    async def submit_operator_recovery_flatten(
        self,
        intent: AccountOwnerSubmitIntent,
    ) -> AccountClerkRecoveryFlattenReceipt:
        """Run the explicit operator cure for a retired namespace."""

        payload = await self._request(
            {
                "operation": "recovery_flatten",
                "actor": "operator",
                "intent": intent.model_dump(mode="json"),
            }
        )
        return AccountClerkRecoveryFlattenReceipt.model_validate(payload["recovery_flatten"])

    async def drain_events(self, *, bot_order_namespace: str) -> list[IbkrOrderEvent]:
        payload = await self._request(
            {"operation": "drain_events", "bot_order_namespace": bot_order_namespace}
        )
        return [IbkrOrderEvent.model_validate(event) for event in payload["events"]]

    async def _request(self, request: dict[str, object]) -> dict[str, object]:
        if not self._socket_path.exists():
            raise RuntimeError("ACCOUNT_CLERK_UNAVAILABLE:SOCKET_MISSING")
        try:
            reader, writer = await asyncio.open_unix_connection(str(self._socket_path))
        except OSError as exc:
            raise RuntimeError("ACCOUNT_CLERK_UNAVAILABLE:SOCKET_CONNECT_FAILED") from exc
        try:
            writer.write((json.dumps(request) + "\n").encode())
            await writer.drain()
            line = await reader.readline()
        finally:
            writer.close()
            await writer.wait_closed()
        if not line:
            raise RuntimeError("ACCOUNT_CLERK_UNAVAILABLE:EMPTY_RESPONSE")
        payload = json.loads(line)
        if "error" in payload:
            raise RuntimeError(f"ACCOUNT_CLERK_REJECTED:{payload['error']}")
        return payload


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
        try:
            line = await reader.readline()
            request = json.loads(line)
            if request["operation"] == "submit":
                intent = AccountOwnerSubmitIntent.model_validate(request["intent"])
                recorded, broker_acked = await self._clerk.submit_intent(intent)
                self._intents_by_order_ref[intent.order_ref] = intent
                response = {
                    "recorded": recorded.model_dump(mode="json"),
                    "broker_acked": broker_acked.model_dump(mode="json"),
                }
            elif request["operation"] == "recovery_flatten":
                intent = AccountOwnerSubmitIntent.model_validate(request["intent"])
                actor = request.get("actor")
                if actor not in ("bot", "operator"):
                    raise ValueError("ACCOUNT_CLERK_INVALID_RECOVERY_ACTOR")
                recovery = await self._clerk.submit_recovery_flatten(
                    intent,
                    actor=actor,
                    actor_strategy_instance_id=request.get("actor_strategy_instance_id"),
                    actor_run_id=request.get("actor_run_id"),
                    actor_bot_order_namespace=request.get("actor_bot_order_namespace"),
                )
                self._intents_by_order_ref[intent.order_ref] = intent
                response = {"recovery_flatten": recovery.model_dump(mode="json")}
            elif request["operation"] == "drain_events":
                namespace = str(request["bot_order_namespace"])
                events = self._events_by_namespace.pop(namespace, [])
                response = {"events": [event.model_dump(mode="json") for event in events]}
            else:
                response = {"error": "ACCOUNT_CLERK_UNKNOWN_OPERATION"}
        except Exception as exc:
            response = {"error": str(exc)}
        writer.write((json.dumps(response) + "\n").encode())
        await writer.drain()
        writer.close()
        await writer.wait_closed()

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


__all__ = ["AccountClerkRpcClient", "AccountClerkRpcServer"]
