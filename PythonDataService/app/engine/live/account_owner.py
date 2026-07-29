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

from app.broker.ibkr.client import IbkrClientIdInUseError
from app.broker.ibkr.models import IbkrOrderSpec
from app.engine.live.account_artifacts import (
    AccountArtifactError,
    AccountOwnerGeneration,
    account_artifact_file_path,
    append_account_event,
    read_account_freeze,
    read_legacy_account_events,
    write_account_owner_generation,
)
from app.engine.live.account_classifier import AccountClassifierDecision
from app.engine.live.account_effect_models import AccountEffectRequest
from app.engine.live.account_owner_fence import (
    AccountOwnerWriteFenceError,
    account_owner_write_grant,
)
from app.engine.live.account_registry import evaluate_account_instance_binding
from app.engine.live.live_state_sidecar import _file_lock
from app.schemas.artifact_io import atomic_write_pydantic_artifact
from app.schemas.live_runs import GateResult

AccountOwnerRuntimePhase = Literal["accepting", "reconnecting", "draining", "frozen"]
CustodyStage = Literal[
    "A0_CUSTODY_ACCEPTED",
    "A1_BROKER_WRITE_STARTED",
    "A2_BROKER_KNOWN",
    "A3_ECONOMIC_TERMINAL",
]
CustodyLifecycleState = Literal[
    "queued",
    "submitting",
    "broker_known",
    "economic_terminal",
    "expired_before_submit",
    "recovery_action_required",
    "submission_hold",
    "uncertain_requires_reconciliation",
]

# A manual ticket belongs to the account Clerk rather than to a deployed bot.
# These values remain inside its durable intent shape so manual broker events
# can use the same acknowledgement and callback journal without fabricating a
# bot binding.
MANUAL_ORDER_INTENT_KIND = "MANUAL_ORDER"
MANUAL_OPERATOR_STRATEGY_INSTANCE_ID = "manual-operator"
MANUAL_OPERATOR_RUN_ID = "manual-ticket"
ACCOUNT_OWNER_INFLIGHT_FILENAME = "account_owner_inflight.json"


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
    # This is only an untrusted request shape. AccountEffectClassifier derives
    # the actual effect from durable account evidence at both write boundaries.
    effect_request: AccountEffectRequest | None = None
    owner_generation: int = Field(ge=0)
    created_at_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def _check_order_ref(self) -> AccountOwnerSubmitIntent:
        expected = f"{self.bot_order_namespace}:{self.intent_id}"
        if self.order_ref != expected:
            raise ValueError(f"order_ref {self.order_ref!r} != {expected!r}")
        return self


class _AccountOwnerInflightSubmission(BaseModel):
    """Typed pre-broker record used only by AccountOwner reconnect recovery."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    order_ref: str = Field(min_length=1, max_length=256)
    diagnostics: dict
    order_spec: dict
    prepared_at_ms: int = Field(ge=0)


class _AccountOwnerInflightState(BaseModel):
    """One producer-local safety artifact, never an Account Desk authority."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    account_id: str = Field(min_length=1, max_length=64)
    submissions: tuple[_AccountOwnerInflightSubmission, ...] = ()


@dataclass(frozen=True)
class AccountOwnerSubmitResult:
    """Outcome from the legacy synchronous AccountOwner submit boundary.

    ``accepted`` means the paper broker has acknowledged the order.  It must
    never be used for an asynchronous Clerk A0 receipt: callers that opt into
    that boundary receive :class:`AccountClerkCustodyAccepted` instead.
    """

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


@dataclass(frozen=True)
class AccountClerkCustodyAccepted:
    """A durable Clerk A0 admission, explicitly not a broker acknowledgement.

    ``custody_stage`` is the latest observed Clerk fold, so an idempotent
    replay may legitimately report A1, A2, or A3.  The type itself proves A0
    was recorded for this immutable identity.
    """

    status: Literal["custody_accepted"]
    trace_id: str
    account_id: str
    strategy_instance_id: str
    run_id: str
    intent_id: str
    order_ref: str
    owner_generation: int
    journal_seq: int
    recorded_at_ms: int
    custody_stage: CustodyStage
    custody_lifecycle_state: CustodyLifecycleState

    @property
    def is_economically_or_admission_terminal(self) -> bool:
        """Whether this returned fold has no active Clerk-custody wait state."""

        return self.custody_lifecycle_state in {"economic_terminal", "expired_before_submit"}


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
        current_owner_generation_provider: Callable[[], int] | None = None,
        owner_generation_advancer: Callable[[AccountOwnerRuntimePhase, int], AccountOwnerGeneration] | None = None,
        classifier: Callable[[AccountOwnerSubmitIntent], AccountClassifierDecision],
        initial_phase: AccountOwnerRuntimePhase = "accepting",
    ) -> None:
        self._artifacts_root = artifacts_root
        self._account_id = account_id
        self._broker = broker
        self._owner_generation_provider = owner_generation_provider
        self._current_owner_generation_provider = (
            current_owner_generation_provider
            if current_owner_generation_provider is not None
            else owner_generation_provider
        )
        self._owner_generation_advancer = owner_generation_advancer
        self._classifier = classifier
        self._lock = asyncio.Lock()
        self._accepting = initial_phase == "accepting"
        self._phase: AccountOwnerRuntimePhase = initial_phase

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

    async def run_broker_write(self, *, boundary: str, write: Callable[[], object]):
        """Run a non-submit broker write under the current AccountOwner grant."""
        if not self._accepting:
            raise AccountOwnerWriteFenceError(
                reason="ACCOUNT_OWNER_RECONNECTING",
                boundary=boundary,
                account_id=self._account_id,
                current_owner_generation=self._owner_generation_provider(),
            )
        async with self._lock:
            if not self._accepting:
                raise AccountOwnerWriteFenceError(
                    reason="ACCOUNT_OWNER_RECONNECTING",
                    boundary=boundary,
                    account_id=self._account_id,
                    current_owner_generation=self._owner_generation_provider(),
                )
            owner_generation = self._owner_generation_provider()
            with account_owner_write_grant(
                account_id=self._account_id,
                owner_generation=owner_generation,
                boundary=boundary,
                owner_generation_provider=self._current_owner_generation_provider,
            ):
                return await _maybe_await(write())

    async def handle_reconnect(
        self,
        *,
        reconnect: Callable[[int], object],
        classify_inflight: Callable[[dict], object],
        reconcile: Callable[[], object],
        client_id_range: tuple[int, ...],
        backoff: Callable[[int], object] | None = None,
    ) -> None:
        self._set_phase("reconnecting", advance=True)
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
            except (ClientIdInUseError, IbkrClientIdInUseError) as exc:
                client_id_value = getattr(exc, "client_id", client_id)
                append_account_event(
                    self._artifacts_root,
                    self._account_id,
                    {
                        "event_type": "account_owner_client_id_in_use",
                        "client_id": client_id_value,
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
            if outcome == "accepted":
                order_ref = (prepared.get("diagnostics") or {}).get("order_ref")
                if isinstance(order_ref, str) and order_ref:
                    self._clear_prepared_submission(order_ref=order_ref)
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
            generation = self._set_phase("accepting")
            append_account_event(
                self._artifacts_root,
                self._account_id,
                {
                    "event_type": "account_owner_reconnect_resumed",
                    "reason": decision.reason,
                    "generation": generation.generation,
                    "phase": generation.phase,
                    "recorded_at_ms": generation.recorded_at_ms,
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
        diagnostics = self._diagnostics(intent)
        self._record_prepared_submission(
            diagnostics=diagnostics,
            order_spec=spec.model_dump(),
        )
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

        self._ensure_current_generation(
            intent,
            diagnostics,
            reason="OWNER_GENERATION_MISMATCH",
        )

        classifier_decision = await _maybe_await(self._classifier(intent))
        classifier_gate = classifier_decision.to_gate_result()
        if classifier_gate.status != "pass":
            self._reject(intent, classifier_decision.reason, diagnostics)

        spec = IbkrOrderSpec.model_validate(intent.order_spec)
        if spec.order_ref != intent.order_ref:
            self._reject(intent, "ORDER_SPEC_REF_MISMATCH", diagnostics)

        self._record_prepared_submission(
            diagnostics=diagnostics,
            order_spec=spec.model_dump(),
        )
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

        self._ensure_current_generation(
            intent,
            diagnostics,
            reason="OWNER_GENERATION_STALE_AT_BROKER_WRITE",
            clear_prepared_submission=True,
        )

        try:
            with account_owner_write_grant(
                account_id=intent.account_id,
                owner_generation=intent.owner_generation,
                boundary="broker.place_order",
                owner_generation_provider=self._current_owner_generation_provider,
            ):
                ack = await self._broker.place_order(spec)
        except AccountOwnerWriteFenceError as exc:
            self._reject(
                intent,
                exc.reason,
                diagnostics
                | {
                    "broker_write_boundary": exc.boundary,
                    "current_owner_generation": exc.current_owner_generation,
                    "grant_owner_generation": exc.grant_owner_generation,
                },
                clear_prepared_submission=True,
            )
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
        self._clear_prepared_submission(order_ref=intent.order_ref)
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
        *,
        clear_prepared_submission: bool = False,
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
        if clear_prepared_submission:
            self._clear_prepared_submission(order_ref=intent.order_ref)
        raise AccountOwnerSubmitRejected(reason=reason, diagnostics=diagnostics)

    def _ensure_current_generation(
        self,
        intent: AccountOwnerSubmitIntent,
        diagnostics: dict,
        *,
        reason: str,
        clear_prepared_submission: bool = False,
    ) -> None:
        current_generation = self._current_owner_generation_provider()
        if intent.owner_generation != current_generation:
            self._reject(
                intent,
                reason,
                diagnostics | {"current_owner_generation": current_generation},
                clear_prepared_submission=clear_prepared_submission,
            )

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

    def _set_phase(
        self,
        phase: AccountOwnerRuntimePhase,
        *,
        advance: bool = False,
    ) -> AccountOwnerGeneration:
        if phase == "accepting":
            generation = self._write_generation(phase, advance=advance)
            self._phase = phase
            self._accepting = True
            return generation

        self._phase = phase
        self._accepting = False
        return self._write_generation(phase, advance=advance)

    def _write_generation(
        self,
        phase: AccountOwnerRuntimePhase,
        *,
        recorded_at_ms: int | None = None,
        advance: bool = False,
    ) -> AccountOwnerGeneration:
        effective_recorded_at_ms = recorded_at_ms if recorded_at_ms is not None else time.time_ns() // 1_000_000
        if advance and self._owner_generation_advancer is not None:
            generation = self._owner_generation_advancer(phase, effective_recorded_at_ms)
            if generation.account_id != self._account_id or generation.phase != phase:
                raise RuntimeError("AccountOwner generation advancer returned mismatched generation")
            return generation
        else:
            generation_value = self._owner_generation_provider()
        generation = AccountOwnerGeneration(
            account_id=self._account_id,
            generation=generation_value,
            phase=phase,
            recorded_at_ms=effective_recorded_at_ms,
            source="account_owner",
        )
        write_account_owner_generation(
            self._artifacts_root,
            generation,
        )
        return generation

    def _inflight_path(self) -> Path:
        return account_artifact_file_path(
            self._artifacts_root,
            self._account_id,
            ACCOUNT_OWNER_INFLIGHT_FILENAME,
        )

    def _read_inflight_state(self) -> _AccountOwnerInflightState:
        path = self._inflight_path()
        if not path.exists():
            return _AccountOwnerInflightState(account_id=self._account_id)
        try:
            state = _AccountOwnerInflightState.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise AccountArtifactError("AccountOwner inflight recovery artifact is unreadable") from exc
        if state.account_id != self._account_id:
            raise AccountArtifactError("AccountOwner inflight recovery artifact belongs to another account")
        return state

    def _record_prepared_submission(self, *, diagnostics: dict, order_spec: dict) -> None:
        order_ref = diagnostics.get("order_ref")
        if not isinstance(order_ref, str) or not order_ref:
            raise AccountArtifactError("AccountOwner prepared submission is missing order_ref")
        path = self._inflight_path()
        with _file_lock(path):
            current = self._read_or_migrate_inflight_state_locked(path)
            replacement = _AccountOwnerInflightSubmission(
                order_ref=order_ref,
                diagnostics=diagnostics,
                order_spec=order_spec,
                prepared_at_ms=time.time_ns() // 1_000_000,
            )
            submissions = {
                item.order_ref: item
                for item in current.submissions
            }
            submissions[order_ref] = replacement
            atomic_write_pydantic_artifact(
                path,
                _AccountOwnerInflightState(
                    account_id=self._account_id,
                    submissions=tuple(submissions[key] for key in sorted(submissions)),
                ),
            )

    def _clear_prepared_submission(self, *, order_ref: str) -> None:
        path = self._inflight_path()
        with _file_lock(path):
            current = self._read_or_migrate_inflight_state_locked(path)
            remaining = tuple(
                item for item in current.submissions if item.order_ref != order_ref
            )
            if remaining == current.submissions:
                return
            atomic_write_pydantic_artifact(
                path,
                _AccountOwnerInflightState(account_id=self._account_id, submissions=remaining),
            )

    def _prepared_without_terminal(self) -> list[dict]:
        path = self._inflight_path()
        with _file_lock(path):
            state = self._read_or_migrate_inflight_state_locked(path)
        return [
            {
                "event_type": "account_owner_submit_prepared",
                "diagnostics": item.diagnostics,
                "order_spec": item.order_spec,
                "recorded_at_ms": item.prepared_at_ms,
            }
            for item in state.submissions
        ]

    def _read_or_migrate_inflight_state_locked(self, path: Path) -> _AccountOwnerInflightState:
        """Seed immutable pre-split recovery rows once under the artifact lock."""

        if path.exists():
            return self._read_inflight_state()
        events = read_legacy_account_events(self._artifacts_root, self._account_id)
        terminal_refs = {
            (event.get("diagnostics") or {}).get("order_ref")
            for event in events
            if str(event.get("event_type") or "").startswith("account_owner_submit_")
            and event.get("event_type") != "account_owner_submit_prepared"
        }
        submissions: dict[str, _AccountOwnerInflightSubmission] = {}
        for event in events:
            if event.get("event_type") != "account_owner_submit_prepared":
                continue
            diagnostics = event.get("diagnostics")
            order_spec = event.get("order_spec")
            prepared_at_ms = event.get("recorded_at_ms", event.get("created_at_ms"))
            order_ref = diagnostics.get("order_ref") if isinstance(diagnostics, dict) else None
            if (
                not isinstance(order_ref, str)
                or not order_ref
                or order_ref in terminal_refs
                or not isinstance(order_spec, dict)
                or not isinstance(prepared_at_ms, int)
                or isinstance(prepared_at_ms, bool)
                or prepared_at_ms < 0
            ):
                continue
            submissions[order_ref] = _AccountOwnerInflightSubmission(
                order_ref=order_ref,
                diagnostics=diagnostics,
                order_spec=order_spec,
                prepared_at_ms=prepared_at_ms,
            )
        state = _AccountOwnerInflightState(
            account_id=self._account_id,
            submissions=tuple(submissions[key] for key in sorted(submissions)),
        )
        atomic_write_pydantic_artifact(path, state)
        return state

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
    "AccountClerkCustodyAccepted",
    "AccountOwner",
    "AccountOwnerSubmitIntent",
    "AccountOwnerSubmitRejected",
    "AccountOwnerSubmitResult",
    "ClientIdInUseError",
    "CustodyLifecycleState",
    "CustodyStage",
]
