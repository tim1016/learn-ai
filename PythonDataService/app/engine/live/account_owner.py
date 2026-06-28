"""AccountOwner single-writer submit lane for paper orders."""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.broker.ibkr.models import IbkrOrderSpec
from app.engine.live.account_artifacts import (
    AccountOwnerGeneration,
    append_account_event,
    evaluate_account_instance_binding,
    read_account_events,
    read_account_freeze,
    write_account_owner_generation,
)
from app.engine.live.account_classifier import AccountClassifierDecision
from app.schemas.live_runs import GateResult


class AccountOwnerSubmitIntent(BaseModel):
    """Durable runner intent accepted by AccountOwner intake."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    trace_id: str = Field(min_length=1)
    account_id: str = Field(min_length=1)
    strategy_instance_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    bot_order_namespace: str = Field(min_length=1)
    intent_id: str = Field(min_length=1)
    order_ref: str = Field(min_length=1)
    intent_kind: str = Field(min_length=1)
    order_spec: dict
    owner_generation: int = Field(ge=0)
    created_at_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def _check_order_ref(self) -> AccountOwnerSubmitIntent:
        expected = f"{self.bot_order_namespace}:{self.intent_id}"
        if self.order_ref != expected:
            raise ValueError(f"order_ref {self.order_ref!r} != {expected!r}")
        return self


@dataclass(frozen=True)
class AccountOwnerSubmitResult:
    status: Literal["accepted", "rejected", "failed", "uncertain"]
    trace_id: str
    account_id: str
    strategy_instance_id: str
    run_id: str
    intent_id: str
    order_ref: str
    owner_generation: int
    order_id: int | None = None
    perm_id: int | None = None
    exec_id: str | None = None
    reason: str | None = None
    diagnostics: dict | None = None


class AccountOwnerSubmitRejected(RuntimeError):
    def __init__(self, *, reason: str, diagnostics: dict) -> None:
        super().__init__(f"AccountOwnerSubmitRejected(reason={reason!r})")
        self.reason = reason
        self.diagnostics = diagnostics


class ClientIdInUseError(RuntimeError):
    """IBKR client id is already in use (error code 326)."""

    code = 326

    def __init__(self, client_id: int) -> None:
        super().__init__(f"IBKR client id {client_id} is already in use")
        self.client_id = client_id


class AccountOwner:
    """Single serialized account submit lane."""

    def __init__(
        self,
        *,
        artifacts_root: Path,
        account_id: str,
        broker,
        owner_generation_provider: Callable[[], int],
        classifier: Callable[[AccountOwnerSubmitIntent], AccountClassifierDecision],
    ) -> None:
        self._artifacts_root = artifacts_root
        self._account_id = account_id
        self._broker = broker
        self._owner_generation_provider = owner_generation_provider
        self._classifier = classifier
        self._lock = asyncio.Lock()
        self._accepting = True
        self._phase = "accepting"

    @property
    def accepting(self) -> bool:
        return self._accepting

    def reconnect_gate_result(self) -> GateResult:
        if self._phase == "accepting":
            return GateResult(
                gate_id="account_owner.reconnect",
                status="pass",
                source="account_owner",
                operator_reason="accepting",
                operator_next_step="GATE_PASSING",
                evidence_at_ms=time.time_ns() // 1_000_000,
            )
        if self._phase == "frozen":
            return GateResult(
                gate_id="account_owner.reconnect",
                status="freeze",
                source="account_owner",
                operator_reason="frozen",
                operator_next_step="CHECK_IBKR",
                evidence_at_ms=time.time_ns() // 1_000_000,
            )
        return GateResult(
            gate_id="account_owner.reconnect",
            status="unknown",
            source="account_owner",
            operator_reason=self._phase,
            operator_next_step="WAIT_FOR_RECONNECT_DRAIN",
            evidence_at_ms=time.time_ns() // 1_000_000,
        )

    async def submit(self, intent: AccountOwnerSubmitIntent) -> AccountOwnerSubmitResult:
        if not self._accepting:
            self._reject(intent, "ACCOUNT_OWNER_RECONNECTING", self._diagnostics(intent))
        async with self._lock:
            if not self._accepting:
                self._reject(intent, "ACCOUNT_OWNER_RECONNECTING", self._diagnostics(intent))
            return await self._submit_locked(intent)

    async def handle_reconnect(
        self,
        *,
        reconnect: Callable[[int], object],
        classify_inflight: Callable[[dict], object],
        reconcile: Callable[[], object],
        client_id_range: tuple[int, ...],
        backoff: Callable[[int], object] | None = None,
    ) -> None:
        self._set_phase("reconnecting")
        async with self._lock:
            await self._handle_reconnect_locked(
                reconnect=reconnect,
                classify_inflight=classify_inflight,
                reconcile=reconcile,
                client_id_range=client_id_range,
                backoff=backoff,
            )

    async def _handle_reconnect_locked(
        self,
        *,
        reconnect: Callable[[int], object],
        classify_inflight: Callable[[dict], object],
        reconcile: Callable[[], object],
        client_id_range: tuple[int, ...],
        backoff: Callable[[int], object] | None,
    ) -> None:
        connected = False
        for attempt, client_id in enumerate(client_id_range, start=1):
            try:
                await _maybe_await(reconnect(client_id))
                connected = True
                break
            except ClientIdInUseError:
                append_account_event(
                    self._artifacts_root,
                    self._account_id,
                    {
                        "event_type": "account_owner_client_id_in_use",
                        "client_id": client_id,
                        "attempt": attempt,
                        "code": ClientIdInUseError.code,
                    },
                )
                if backoff is not None:
                    await _maybe_await(backoff(attempt))
        if not connected:
            self._set_phase("frozen")
            append_account_event(
                self._artifacts_root,
                self._account_id,
                {"event_type": "account_owner_reconnect_frozen", "reason": "CLIENT_ID_RANGE_EXHAUSTED"},
            )
            return

        self._set_phase("draining")
        for prepared in self._prepared_without_terminal():
            outcome = await _maybe_await(classify_inflight(prepared))
            append_account_event(
                self._artifacts_root,
                self._account_id,
                {
                    "event_type": f"account_owner_reconnect_drain_{outcome}",
                    "diagnostics": prepared.get("diagnostics", {}),
                },
            )

        decision = await _maybe_await(reconcile())
        gate = decision.to_gate_result()
        if gate.status == "pass":
            self._set_phase("accepting")
            append_account_event(
                self._artifacts_root,
                self._account_id,
                {
                    "event_type": "account_owner_reconnect_resumed",
                    "reason": decision.reason,
                    "gate_result": gate.model_dump(mode="json"),
                },
            )
            return

        self._set_phase("frozen")
        append_account_event(
            self._artifacts_root,
            self._account_id,
            {
                "event_type": "account_owner_reconnect_blocked",
                "reason": decision.reason,
                "gate_result": gate.model_dump(mode="json"),
            },
        )

    async def record_prepared_for_test(self, intent: AccountOwnerSubmitIntent) -> None:
        spec = IbkrOrderSpec.model_validate(intent.order_spec)
        append_account_event(
            self._artifacts_root,
            intent.account_id,
            {
                "event_type": "account_owner_submit_prepared",
                "created_at_ms": intent.created_at_ms,
                "diagnostics": self._diagnostics(intent),
                "order_spec": spec.model_dump(),
            },
        )

    async def _submit_locked(self, intent: AccountOwnerSubmitIntent) -> AccountOwnerSubmitResult:
        diagnostics = self._diagnostics(intent)
        if intent.account_id != self._account_id:
            self._reject(intent, "ACCOUNT_MISMATCH", diagnostics)

        freeze = read_account_freeze(self._artifacts_root, intent.account_id)
        if freeze is not None:
            self._reject(intent, "ACCOUNT_FROZEN", diagnostics | {"freeze_reason": freeze.reason})

        registry_gate = evaluate_account_instance_binding(
            self._artifacts_root,
            account_id=intent.account_id,
            strategy_instance_id=intent.strategy_instance_id,
            run_id=intent.run_id,
            bot_order_namespace=intent.bot_order_namespace,
        )
        if registry_gate.status != "pass":
            self._reject(intent, registry_gate.operator_reason, diagnostics)

        current_generation = self._owner_generation_provider()
        if intent.owner_generation != current_generation:
            self._reject(
                intent,
                "OWNER_GENERATION_MISMATCH",
                diagnostics | {"current_owner_generation": current_generation},
            )

        classifier_decision = self._classifier(intent)
        classifier_gate = classifier_decision.to_gate_result()
        if classifier_gate.status != "pass":
            self._reject(intent, classifier_decision.reason, diagnostics)

        spec = IbkrOrderSpec.model_validate(intent.order_spec)
        if spec.order_ref != intent.order_ref:
            self._reject(intent, "ORDER_SPEC_REF_MISMATCH", diagnostics)

        append_account_event(
            self._artifacts_root,
            intent.account_id,
            {
                "event_type": "account_owner_submit_prepared",
                "created_at_ms": intent.created_at_ms,
                "diagnostics": diagnostics,
                "order_spec": spec.model_dump(),
            },
        )

        try:
            ack = await self._broker.place_order(spec)
        except Exception as exc:
            reason = f"BROKER_SUBMIT_UNCERTAIN:{type(exc).__name__}"
            payload = {
                "event_type": "account_owner_submit_uncertain",
                "created_at_ms": intent.created_at_ms,
                "reason": reason,
                "diagnostics": diagnostics,
            }
            append_account_event(self._artifacts_root, intent.account_id, payload)
            return self._result(intent, "uncertain", reason=reason, diagnostics=diagnostics)

        terminal_diagnostics = diagnostics | {
            "order_id": _try_int(getattr(ack, "order_id", None)),
            "perm_id": _try_int(getattr(ack, "perm_id", None)),
            "exec_id": getattr(ack, "exec_id", None),
        }
        append_account_event(
            self._artifacts_root,
            intent.account_id,
            {
                "event_type": "account_owner_submit_accepted",
                "created_at_ms": intent.created_at_ms,
                "diagnostics": terminal_diagnostics,
            },
        )
        return self._result(
            intent,
            "accepted",
            order_id=terminal_diagnostics["order_id"],
            perm_id=terminal_diagnostics["perm_id"],
            exec_id=terminal_diagnostics["exec_id"],
            diagnostics=terminal_diagnostics,
        )

    def _reject(
        self,
        intent: AccountOwnerSubmitIntent,
        reason: str,
        diagnostics: dict,
    ) -> None:
        append_account_event(
            self._artifacts_root,
            intent.account_id,
            {
                "event_type": "account_owner_submit_rejected",
                "created_at_ms": intent.created_at_ms,
                "reason": reason,
                "diagnostics": diagnostics,
            },
        )
        raise AccountOwnerSubmitRejected(reason=reason, diagnostics=diagnostics)

    def _diagnostics(self, intent: AccountOwnerSubmitIntent) -> dict:
        return {
            "trace_id": intent.trace_id,
            "bot_id": intent.strategy_instance_id,
            "strategy_instance_id": intent.strategy_instance_id,
            "account_id": intent.account_id,
            "run_id": intent.run_id,
            "intent_id": intent.intent_id,
            "order_ref": intent.order_ref,
            "owner_generation": intent.owner_generation,
            "broker_client_id": getattr(self._broker, "client_id", None),
            "order_id": None,
            "perm_id": None,
            "exec_id": None,
        }

    def _set_phase(self, phase: str) -> None:
        self._phase = phase
        self._accepting = phase == "accepting"
        write_account_owner_generation(
            self._artifacts_root,
            AccountOwnerGeneration(
                account_id=self._account_id,
                generation=self._owner_generation_provider(),
                phase=phase,  # type: ignore[arg-type]
                recorded_at_ms=time.time_ns() // 1_000_000,
                source="account_owner",
            ),
        )

    def _prepared_without_terminal(self) -> list[dict]:
        events = read_account_events(self._artifacts_root, self._account_id)
        terminal_refs = {
            (event.get("diagnostics") or {}).get("order_ref")
            for event in events
            if str(event.get("event_type") or "").startswith("account_owner_submit_")
            and event.get("event_type") != "account_owner_submit_prepared"
        }
        return [
            event
            for event in events
            if event.get("event_type") == "account_owner_submit_prepared"
            and (event.get("diagnostics") or {}).get("order_ref") not in terminal_refs
        ]

    def _result(
        self,
        intent: AccountOwnerSubmitIntent,
        status: Literal["accepted", "rejected", "failed", "uncertain"],
        *,
        order_id: int | None = None,
        perm_id: int | None = None,
        exec_id: str | None = None,
        reason: str | None = None,
        diagnostics: dict | None = None,
    ) -> AccountOwnerSubmitResult:
        return AccountOwnerSubmitResult(
            status=status,
            trace_id=intent.trace_id,
            account_id=intent.account_id,
            strategy_instance_id=intent.strategy_instance_id,
            run_id=intent.run_id,
            intent_id=intent.intent_id,
            order_ref=intent.order_ref,
            owner_generation=intent.owner_generation,
            order_id=order_id,
            perm_id=perm_id,
            exec_id=exec_id,
            reason=reason,
            diagnostics=diagnostics,
        )


def _try_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def _maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value


__all__ = [
    "AccountOwner",
    "AccountOwnerSubmitIntent",
    "AccountOwnerSubmitRejected",
    "AccountOwnerSubmitResult",
    "ClientIdInUseError",
]
