"""Durable account suspension for attributable-but-unmanaged custody.

The account Clerk journal remains the source of order, execution, and exposure
facts.  This module owns only the account-level *permission* projection that
is needed when a retired bot still has nonterminal custody.  A suspension is
therefore neither foreign exposure nor a terminal state for healthy siblings:
it blocks new account entries until the Clerk records a fresh clean/adopted
epoch reconciliation after the retired custody is gone.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.engine.live.account_artifacts import account_artifact_file_path, read_account_clerk_generation
from app.engine.live.account_clerk_journal import is_economic_terminal_broker_event
from app.engine.live.account_clerk_journal_models import AccountClerkJournalEntry
from app.engine.live.account_epoch import AccountEpoch, AccountEpochState
from app.engine.live.live_state_sidecar import _file_lock, _fsync_parent_dir
from app.schemas.account_truth import AccountTruthResponse

ACCOUNT_SAFETY_FILENAME = "account_safety.json"
ACCOUNT_SAFETY_ADMISSION_FILENAME = "account_safety_admission"
ACCOUNT_SAFETY_ADMISSION_MAINTENANCE_FILENAME = "account_safety_admission_maintenance.json"
ACCOUNT_SAFETY_ADMISSION_REPAIR_FILENAME = "account_safety_admission_repairs.jsonl"
_ACCOUNT_SAFETY_ADMISSION_TIMEOUT_S = 10.0
_ACCOUNT_SAFETY_ADMISSION_POLL_S = 0.01
logger = logging.getLogger(__name__)


class AccountSafetyVerdict(StrEnum):
    """Closed account-level permission vocabulary from ADR 0033."""

    CLEAN = "CLEAN"
    RECONCILING = "RECONCILING"
    SUSPENDED = "SUSPENDED"
    CONTAMINATED = "CONTAMINATED"


class RetiredOwnerCustody(BaseModel):
    """Immutable Clerk identity retained when an originator is retired."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    account_id: str = Field(min_length=1, max_length=64)
    strategy_instance_id: str = Field(min_length=1, max_length=128)
    intent_id: str = Field(min_length=1, max_length=128)
    order_ref: str = Field(min_length=1, max_length=512)
    # Clerk evidence has a durable order/intention lifecycle; broker evidence
    # is an independently observed fact that only a later fresh Account Truth
    # observation may clear.
    evidence_source: Literal["clerk", "broker"] = "clerk"


class AccountSafetySuspension(BaseModel):
    """The bounded evidence that keeps account entry permission suspended."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    suspension_id: str = Field(min_length=1, max_length=256)
    reason_code: str = "RETIRED_OWNER_LIVE_EXPOSURE"
    custody: tuple[RetiredOwnerCustody, ...] = ()
    detected_at_ms: int = Field(ge=0)
    reconciliation_epoch: AccountEpoch | None = None
    reconciliation_id: str | None = Field(default=None, min_length=1, max_length=128)
    requires_broker_clearance: bool = False


class AccountSafetyState(BaseModel):
    """Durable permission state, never a second order/exposure ledger."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    account_id: str = Field(min_length=1, max_length=64)
    verdict: AccountSafetyVerdict = AccountSafetyVerdict.CLEAN
    suspension: AccountSafetySuspension | None = None
    last_reconciliation_id: str | None = Field(default=None, min_length=1, max_length=128)
    last_broker_observed_at_ms: int | None = Field(default=None, ge=0)
    updated_at_ms: int = Field(ge=0)


class AccountSafetyCorruptError(RuntimeError):
    """The durable safety projection cannot be read and must fail closed."""


class AccountSafetyEntryBlockedError(RuntimeError):
    """A suspended account rejected a new entry before A0 or A1."""

    def __init__(self, state: AccountSafetyState) -> None:
        super().__init__(
            state.suspension.reason_code
            if state.suspension is not None
            else "ACCOUNT_SAFETY_ENTRY_BLOCKED"
        )
        self.state = state


class AccountSafetyAdmissionError(RuntimeError):
    """The host/VM-shared account safety permit cannot be acquired."""


class AccountSafetyAdmissionRepairReceipt(BaseModel):
    """Audited, host-authorized removal of stranded admission markers."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    account_id: str = Field(min_length=1, max_length=64)
    operation_id: str = Field(min_length=16, max_length=128)
    authorized_by: str = Field(min_length=1, max_length=128)
    repaired_at_ms: int = Field(ge=0)
    maintenance_generation: int = Field(ge=1)
    clerk_fence_generation: int = Field(ge=1)
    removed_markers: tuple[str, ...]


class AccountSafetyAdmissionMaintenanceState(BaseModel):
    """Durable cross-runtime fence around admission-marker repair."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    account_id: str = Field(min_length=1, max_length=64)
    generation: int = Field(ge=0)
    status: Literal["OPEN", "REPAIRING"] = "OPEN"
    repair_operation_id: str | None = Field(default=None, min_length=16, max_length=128)


class AccountSafetyAdmissionParticipant(BaseModel):
    """One cross-runtime admission operation enrolled in a Clerk generation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    account_id: str = Field(min_length=1, max_length=64)
    participant_id: str = Field(min_length=16, max_length=64)
    clerk_generation: int = Field(ge=0)
    enrolled_at_ms: int = Field(ge=0)


class AccountSafetyAdmissionTransition(BaseModel):
    """The O_EXCL mutex owner for a maintenance-state transition."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    account_id: str = Field(min_length=1, max_length=64)
    clerk_generation: int = Field(ge=0)
    acquired_at_ms: int = Field(ge=0)


def account_safety_path(artifacts_root: Path, account_id: str) -> Path:
    """Return the one static durable permission projection for an account."""

    return account_artifact_file_path(artifacts_root, account_id, ACCOUNT_SAFETY_FILENAME)


def account_safety_admission_path(artifacts_root: Path, account_id: str) -> Path:
    """Return the shared admission-coordinator anchor for an account."""

    return account_artifact_file_path(
        artifacts_root,
        account_id,
        ACCOUNT_SAFETY_ADMISSION_FILENAME,
    )


def account_safety_admission_repair_path(artifacts_root: Path, account_id: str) -> Path:
    """Return the append-only receipt ledger for explicit admission repairs."""

    return account_artifact_file_path(
        artifacts_root,
        account_id,
        ACCOUNT_SAFETY_ADMISSION_REPAIR_FILENAME,
    )


def account_safety_admission_maintenance_path(artifacts_root: Path, account_id: str) -> Path:
    """Return the durable generation fence for admission-marker maintenance."""

    return account_artifact_file_path(
        artifacts_root,
        account_id,
        ACCOUNT_SAFETY_ADMISSION_MAINTENANCE_FILENAME,
    )


def _account_safety_admission_paths(
    artifacts_root: Path,
    account_id: str,
) -> tuple[Path, Path, Path, Path]:
    """Return the gate, writer marker, and reader directory for one account."""

    target = account_safety_admission_path(artifacts_root, account_id)
    coordinator = target.with_name(f".{target.name}")
    return (
        target,
        coordinator / "gate",
        coordinator / "writer",
        coordinator / "readers",
    )


def _account_safety_admission_participants_path(artifacts_root: Path, account_id: str) -> Path:
    """Return the shared liveness roster for active admission participants."""

    target = account_safety_admission_path(artifacts_root, account_id)
    return target.with_name(f".{target.name}") / "participants"


def _account_safety_admission_transition_path(artifacts_root: Path, account_id: str) -> Path:
    """Return the O_EXCL cross-runtime mutex for maintenance transitions."""

    target = account_safety_admission_path(artifacts_root, account_id)
    return target.with_name(f".{target.name}") / "maintenance-transition"


def _account_safety_state_transition_path(artifacts_root: Path, account_id: str) -> Path:
    """Return the cross-runtime mutex for durable safety-state transitions."""

    target = account_safety_path(artifacts_root, account_id)
    return target.with_name(f".{target.name}.transition")


def _create_exclusive_marker(path: Path, *, deadline: float, payload: str | None = None) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    while True:
        try:
            descriptor = os.open(path, flags, 0o600)
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise AccountSafetyAdmissionError(
                    f"account safety admission marker is already held: {path}"
                ) from None
            time.sleep(_ACCOUNT_SAFETY_ADMISSION_POLL_S)
            continue
        except OSError as exc:
            raise AccountSafetyAdmissionError(
                f"account safety admission marker could not be acquired: {path}: {exc}"
            ) from exc
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload or f"{{\"created_at_ms\":{time.time_ns() // 1_000_000}}}")
            handle.flush()
            os.fsync(handle.fileno())
        _fsync_parent_dir(path)
        return


def _remove_marker(path: Path) -> None:
    try:
        path.unlink()
        _fsync_parent_dir(path)
    except FileNotFoundError:
        logger.error("account safety admission marker disappeared while held", extra={"path": str(path)})


def _read_admission_maintenance_locked(
    path: Path,
    *,
    account_id: str,
) -> AccountSafetyAdmissionMaintenanceState:
    if not path.exists():
        return AccountSafetyAdmissionMaintenanceState(account_id=account_id, generation=0)
    try:
        state = AccountSafetyAdmissionMaintenanceState.model_validate_json(
            path.read_text(encoding="utf-8")
        )
    except (OSError, ValidationError, ValueError) as exc:
        raise AccountSafetyCorruptError(
            f"account safety admission maintenance state at {path} is unreadable: {exc}"
        ) from exc
    if state.account_id != account_id:
        raise AccountSafetyCorruptError("account safety admission maintenance account id mismatch")
    return state


def _write_admission_maintenance_locked(
    path: Path,
    state: AccountSafetyAdmissionMaintenanceState,
) -> None:
    temp_path = path.with_suffix(path.suffix + ".tmp")
    payload = state.model_dump_json().encode("utf-8")
    with open(temp_path, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(temp_path, path)
    except Exception:
        with contextlib.suppress(OSError):
            temp_path.unlink()
        raise
    _fsync_parent_dir(path)


@contextmanager
def _account_safety_admission_transition_lock(
    artifacts_root: Path,
    account_id: str,
    *,
    repair_generation: int | None = None,
) -> Iterator[None]:
    """Serialize admission enrollment and repair with a shared O_EXCL marker.

    ``flock`` is not reliable across the host/VM filesystem boundary, so this
    lock deliberately uses the same durable O_EXCL primitive as the admission
    gate. A generation identifies the owner for audit, but is not proof that
    the process died: repair therefore never breaks an extant transition
    marker and fails closed until external host termination is confirmed.
    """

    transition_path = _account_safety_admission_transition_path(artifacts_root, account_id)
    with _account_safety_generation_transition_lock(
        transition_path,
        artifacts_root=artifacts_root,
        account_id=account_id,
        repair_generation=repair_generation,
    ):
        yield


@contextmanager
def _account_safety_state_transition_lock(
    artifacts_root: Path,
    account_id: str,
    *,
    repair_generation: int | None = None,
) -> Iterator[None]:
    """Serialize durable safety projection mutations with a host/VM O_EXCL lock."""

    with _account_safety_generation_transition_lock(
        _account_safety_state_transition_path(artifacts_root, account_id),
        artifacts_root=artifacts_root,
        account_id=account_id,
        repair_generation=repair_generation,
    ):
        yield


@contextmanager
def _account_safety_generation_transition_lock(
    transition_path: Path,
    *,
    artifacts_root: Path,
    account_id: str,
    repair_generation: int | None,
) -> Iterator[None]:
    """Acquire one generation-stamped O_EXCL transition marker."""

    transition_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + _ACCOUNT_SAFETY_ADMISSION_TIMEOUT_S
    while True:
        try:
            transition = AccountSafetyAdmissionTransition(
                account_id=account_id,
                clerk_generation=(repair_generation or _current_clerk_generation(artifacts_root, account_id)),
                acquired_at_ms=time.time_ns() // 1_000_000,
            )
            _create_exclusive_marker(
                transition_path,
                deadline=(deadline if repair_generation is None else time.monotonic()),
                payload=transition.model_dump_json(),
            )
            break
        except AccountSafetyAdmissionError:
            raise
    try:
        yield
    finally:
        _remove_marker(transition_path)


def _current_clerk_generation(artifacts_root: Path, account_id: str) -> int:
    generation = read_account_clerk_generation(artifacts_root, account_id)
    return generation.generation if generation is not None else 0


def _marker_clerk_generation(path: Path, model: type[BaseModel]) -> int | None:
    try:
        marker = model.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError, ValueError):
        return None
    generation = getattr(marker, "clerk_generation", None)
    return generation if isinstance(generation, int) else None


@contextmanager
def _account_safety_admission_participant(
    artifacts_root: Path,
    account_id: str,
) -> Iterator[None]:
    """Enroll one reader/writer atomically with the maintenance generation."""

    maintenance_path = account_safety_admission_maintenance_path(artifacts_root, account_id)
    participants = _account_safety_admission_participants_path(artifacts_root, account_id)
    maintenance_path.parent.mkdir(parents=True, exist_ok=True)
    participants.mkdir(parents=True, exist_ok=True)
    participant_id = uuid4().hex
    participant = participants / participant_id
    deadline = time.monotonic() + _ACCOUNT_SAFETY_ADMISSION_TIMEOUT_S
    with _account_safety_admission_transition_lock(artifacts_root, account_id):
        state = _read_admission_maintenance_locked(maintenance_path, account_id=account_id)
        if state.status != "OPEN":
            raise AccountSafetyAdmissionError(
                "account safety admission is fenced for maintenance: "
                f"operation={state.repair_operation_id!r} generation={state.generation}"
            )
        _create_exclusive_marker(
            participant,
            deadline=deadline,
            payload=AccountSafetyAdmissionParticipant(
                account_id=account_id,
                participant_id=participant_id,
                clerk_generation=_current_clerk_generation(artifacts_root, account_id),
                enrolled_at_ms=time.time_ns() // 1_000_000,
            ).model_dump_json(),
        )
    try:
        yield
    finally:
        _remove_marker(participant)


class _AccountSafetySuspensionReservation:
    """A writer turnstile held while existing entry readers drain."""

    def __init__(self, *, gate: Path, writer: Path, readers: Path) -> None:
        self._gate = gate
        self._writer = writer
        self._readers = readers

    def wait_for_readers(self) -> None:
        """Wait for pre-existing entry admissions, never removing stale proof."""

        deadline = time.monotonic() + _ACCOUNT_SAFETY_ADMISSION_TIMEOUT_S
        while any(self._readers.iterdir()):
            if time.monotonic() >= deadline:
                raise AccountSafetyAdmissionError(
                    f"account safety entry readers did not drain: {self._readers}"
                )
            time.sleep(_ACCOUNT_SAFETY_ADMISSION_POLL_S)

    def close(self) -> None:
        _remove_marker(self._writer)
        _remove_marker(self._gate)


@contextmanager
def account_safety_suspension_reservation(
    artifacts_root: Path,
    account_id: str,
) -> Iterator[_AccountSafetySuspensionReservation]:
    """Reserve the writer turnstile before waiting for entry admissions.

    The reservation is intentionally split from ``wait_for_readers`` so the
    caller can close the turnstile before blocking on pre-existing entry work.
    Clerk callbacks intentionally do not take this writer permit: a broker
    implementation may wait synchronously for its callback receipt before its
    own entry permit can drain.
    """

    with _account_safety_admission_participant(artifacts_root, account_id):
        target, gate, writer, readers = _account_safety_admission_paths(artifacts_root, account_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        readers.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + _ACCOUNT_SAFETY_ADMISSION_TIMEOUT_S
        _create_exclusive_marker(gate, deadline=deadline)
        try:
            _create_exclusive_marker(writer, deadline=deadline)
        except Exception:
            _remove_marker(gate)
            raise
        reservation = _AccountSafetySuspensionReservation(gate=gate, writer=writer, readers=readers)
        try:
            yield reservation
        finally:
            reservation.close()


@contextmanager
def account_safety_entry_admission_lock(artifacts_root: Path, account_id: str) -> Iterator[None]:
    """Hold a shared entry permit through A0, A1, and broker I/O.

    Entries may proceed together. A suspension/retirement reservation closes
    the turnstile, waits for all existing readers, and then has exclusive
    authority. Every marker is created with ``O_EXCL`` on the shared mount,
    which works across the host/VM boundary where ``flock`` is insufficient.
    """

    with _account_safety_admission_participant(artifacts_root, account_id):
        target, gate, writer, readers = _account_safety_admission_paths(artifacts_root, account_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        readers.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + _ACCOUNT_SAFETY_ADMISSION_TIMEOUT_S
        _create_exclusive_marker(gate, deadline=deadline)
        reader = readers / uuid4().hex
        try:
            if writer.exists():
                raise AccountSafetyAdmissionError(
                    f"account safety suspension is reserving admission: {writer}"
                )
            _create_exclusive_marker(reader, deadline=deadline)
        finally:
            _remove_marker(gate)
        try:
            yield
        finally:
            _remove_marker(reader)


@contextmanager
def account_safety_admission_lock(artifacts_root: Path, account_id: str) -> Iterator[None]:
    """Hold exclusive suspension/retirement authority for an account."""

    with account_safety_suspension_reservation(artifacts_root, account_id) as reservation:
        reservation.wait_for_readers()
        yield


def repair_account_safety_admission(
    artifacts_root: Path,
    account_id: str,
    *,
    operation_id: str,
    authorized_by: str,
    repaired_at_ms: int,
    clerk_fence_generation: int = 1,
) -> AccountSafetyAdmissionRepairReceipt:
    """Repair stranded markers behind an atomic cross-runtime maintenance fence.

    The O_EXCL transition marker serializes repair with every reader/writer's
    participant enrollment across the host/VM boundary. Neither a generation
    nor a timeout proves that a remote process stopped, so any extant
    participant or state-transition marker remains fail-closed. The repair
    only removes ordinary markers once the shared protocol proves no owner is
    still in a critical section.
    """

    target, gate, writer, readers = _account_safety_admission_paths(artifacts_root, account_id)
    receipt_path = account_safety_admission_repair_path(artifacts_root, account_id)
    maintenance_path = account_safety_admission_maintenance_path(artifacts_root, account_id)
    participants = _account_safety_admission_participants_path(artifacts_root, account_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    participants.mkdir(parents=True, exist_ok=True)
    with _file_lock(receipt_path):
        existing = _repair_receipt_for_operation(receipt_path, operation_id)
        if existing is not None:
            _finalize_admission_maintenance_after_receipt(
                artifacts_root,
                account_id,
                operation_id=operation_id,
                clerk_fence_generation=clerk_fence_generation,
                receipt=existing,
            )
            return existing
        with _account_safety_admission_transition_lock(
            artifacts_root,
            account_id,
            repair_generation=clerk_fence_generation,
        ):
            maintenance = _read_admission_maintenance_locked(
                maintenance_path,
                account_id=account_id,
            )
            if maintenance.status == "OPEN":
                maintenance = maintenance.model_copy(
                    update={
                        "generation": maintenance.generation + 1,
                        "status": "REPAIRING",
                        "repair_operation_id": operation_id,
                    }
                )
                _write_admission_maintenance_locked(maintenance_path, maintenance)
            elif maintenance.repair_operation_id != operation_id:
                raise AccountSafetyAdmissionError(
                    "account safety admission is already fenced for maintenance: "
                    f"operation={maintenance.repair_operation_id!r} generation={maintenance.generation}"
                )

        deadline = time.monotonic() + _ACCOUNT_SAFETY_ADMISSION_TIMEOUT_S
        while _admission_participants_remain(participants):
            if time.monotonic() >= deadline:
                raise AccountSafetyAdmissionError(
                    "account safety admission participants did not quiesce during maintenance: "
                    f"operation={operation_id!r}"
                )
            time.sleep(_ACCOUNT_SAFETY_ADMISSION_POLL_S)

        _require_no_state_transition(
            _account_safety_state_transition_path(artifacts_root, account_id),
        )

        # The O_EXCL transition excludes new enrollment. Existing contexts
        # remove their normal marker before their participant marker, so an
        # empty/current-fenced roster proves the remaining markers are
        # orphaned rather than active broker work.
        with _account_safety_admission_transition_lock(
            artifacts_root,
            account_id,
            repair_generation=clerk_fence_generation,
        ):
            maintenance = _read_admission_maintenance_locked(
                maintenance_path,
                account_id=account_id,
            )
            # ``flock`` above is a same-host optimization only. Re-read the
            # durable receipt under the cross-runtime O_EXCL transition before
            # appending so two VM/host callers cannot duplicate an operation.
            committed = _repair_receipt_for_operation(receipt_path, operation_id)
            if committed is not None:
                _validate_repair_receipt_identity(
                    committed,
                    account_id=account_id,
                    clerk_fence_generation=clerk_fence_generation,
                )
                if maintenance.status == "REPAIRING" and maintenance.repair_operation_id == operation_id:
                    _write_admission_maintenance_locked(
                        maintenance_path,
                        maintenance.model_copy(update={"status": "OPEN", "repair_operation_id": None}),
                    )
                elif maintenance.status != "OPEN":
                    raise AccountSafetyAdmissionError(
                        "account safety maintenance generation changed after repair receipt"
                    )
                return committed
            if maintenance.status != "REPAIRING" or maintenance.repair_operation_id != operation_id:
                raise AccountSafetyAdmissionError("account safety maintenance generation changed during repair")
            if _admission_participants_remain(participants):
                raise AccountSafetyAdmissionError("account safety participants reappeared during repair")
            removed = _remove_orphaned_admission_markers(gate=gate, writer=writer, readers=readers)
            receipt = AccountSafetyAdmissionRepairReceipt(
                account_id=account_id,
                operation_id=operation_id,
                authorized_by=authorized_by,
                repaired_at_ms=repaired_at_ms,
                maintenance_generation=maintenance.generation,
                clerk_fence_generation=clerk_fence_generation,
                removed_markers=removed,
            )
            with open(receipt_path, "a", encoding="utf-8") as handle:
                handle.write(receipt.model_dump_json() + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            _fsync_parent_dir(receipt_path)
            _write_admission_maintenance_locked(
                maintenance_path,
                maintenance.model_copy(
                    update={"status": "OPEN", "repair_operation_id": None}
                ),
            )
            return receipt


def _admission_participants_remain(participants: Path) -> bool:
    """Validate the participant roster and report whether it remains nonempty."""

    remaining = False
    for marker in sorted(participants.iterdir(), key=lambda path: path.name):
        if marker.is_symlink() or not marker.is_file():
            raise AccountSafetyAdmissionError(
                f"account safety admission participant is not a regular file: {marker}"
            )
        if _marker_clerk_generation(marker, AccountSafetyAdmissionParticipant) is None:
            raise AccountSafetyAdmissionError(
                f"account safety admission participant is unreadable: {marker}"
            )
        remaining = True
    return remaining


def _require_no_state_transition(marker: Path) -> None:
    """Refuse to erase a possibly-live safety projection critical section."""

    if not marker.exists():
        return
    raise AccountSafetyAdmissionError(
        f"account safety state transition remains owned by a participant: {marker}"
    )


def _finalize_admission_maintenance_after_receipt(
    artifacts_root: Path,
    account_id: str,
    *,
    operation_id: str,
    clerk_fence_generation: int,
    receipt: AccountSafetyAdmissionRepairReceipt,
) -> None:
    """Complete a repair interrupted after its receipt fsync but before OPEN."""

    _validate_repair_receipt_identity(
        receipt,
        account_id=account_id,
        clerk_fence_generation=clerk_fence_generation,
    )
    maintenance_path = account_safety_admission_maintenance_path(artifacts_root, account_id)
    with _account_safety_admission_transition_lock(
        artifacts_root,
        account_id,
        repair_generation=clerk_fence_generation,
    ):
        maintenance = _read_admission_maintenance_locked(maintenance_path, account_id=account_id)
        if maintenance.status == "OPEN":
            return
        if maintenance.status != "REPAIRING" or maintenance.repair_operation_id != operation_id:
            raise AccountSafetyAdmissionError("account safety maintenance generation changed after receipt")
        _write_admission_maintenance_locked(
            maintenance_path,
            maintenance.model_copy(update={"status": "OPEN", "repair_operation_id": None}),
        )


def _validate_repair_receipt_identity(
    receipt: AccountSafetyAdmissionRepairReceipt,
    *,
    account_id: str,
    clerk_fence_generation: int,
) -> None:
    if receipt.account_id != account_id:
        raise AccountSafetyCorruptError("account safety repair receipt account id does not match its path")
    if receipt.clerk_fence_generation != clerk_fence_generation:
        raise AccountSafetyCorruptError(
            "account safety repair replay used a different Clerk fence generation"
        )


def _remove_orphaned_admission_markers(
    *,
    gate: Path,
    writer: Path,
    readers: Path,
) -> tuple[str, ...]:
    removed: list[str] = []
    for marker in (gate, writer):
        if marker.exists():
            if marker.is_symlink() or not marker.is_file():
                raise AccountSafetyAdmissionError(
                    f"account safety admission marker is not a regular file: {marker}"
                )
            marker.unlink()
            _fsync_parent_dir(marker)
            removed.append(marker.name)
    if readers.exists():
        if readers.is_symlink() or not readers.is_dir():
            raise AccountSafetyAdmissionError(
                f"account safety admission readers path is not a directory: {readers}"
            )
        for marker in sorted(readers.iterdir(), key=lambda path: path.name):
            if marker.is_symlink() or not marker.is_file():
                raise AccountSafetyAdmissionError(
                    f"account safety reader marker is not a regular file: {marker}"
                )
            marker.unlink()
            _fsync_parent_dir(marker)
            removed.append(f"readers/{marker.name}")
    return tuple(removed)


def _repair_receipt_for_operation(
    path: Path,
    operation_id: str,
) -> AccountSafetyAdmissionRepairReceipt | None:
    if not path.exists():
        return None
    try:
        rows = [
            AccountSafetyAdmissionRepairReceipt.model_validate_json(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line
        ]
    except (OSError, ValidationError, ValueError) as exc:
        raise AccountSafetyCorruptError(
            f"account safety repair ledger at {path} is unreadable: {exc}"
        ) from exc
    matches = [row for row in rows if row.operation_id == operation_id]
    if len(matches) > 1:
        raise AccountSafetyCorruptError(
            f"account safety repair ledger repeats operation_id {operation_id!r}"
        )
    return matches[0] if matches else None


def retired_owner_nonterminal_custody(
    entries: Iterable[AccountClerkJournalEntry],
    *,
    strategy_instance_id: str,
) -> tuple[RetiredOwnerCustody, ...]:
    """Return the exact retired-owner intents that still need a manager.

    A broker acknowledgement is deliberately nonterminal: it can still carry
    an open order or live position.  Only an economic terminal callback,
    explicit A0 expiry, or an adopted/halt reconciliation resolves custody.
    """

    grouped: dict[str, list[AccountClerkJournalEntry]] = {}
    for entry in entries:
        if entry.intent is None or entry.intent.strategy_instance_id != strategy_instance_id:
            continue
        grouped.setdefault(entry.intent.intent_id, []).append(entry)

    retained: list[RetiredOwnerCustody] = []
    for intent_id, intent_entries in grouped.items():
        recorded = next((entry for entry in intent_entries if entry.entry_kind == "recorded"), None)
        if recorded is None or recorded.intent is None or _custody_is_terminal(intent_entries):
            continue
        retained.append(
            RetiredOwnerCustody(
                account_id=recorded.intent.account_id,
                strategy_instance_id=strategy_instance_id,
                intent_id=intent_id,
                order_ref=recorded.intent.order_ref,
            )
        )
    return tuple(
        sorted(
            retained,
            key=lambda item: (
                item.account_id,
                item.order_ref,
                item.intent_id,
                item.evidence_source,
            ),
        )
    )


class AccountSafetyAuthority:
    """Single-writer durable suspension authority for one account.

    The Clerk calls :meth:`bind_reconciliation` only after it invalidates the
    account epoch.  The authority itself cannot create a clean state: only
    :meth:`lift_if_proven` can do that, and it requires the exact successor
    reconciliation receipt plus zero retained retired-owner custody.
    """

    def __init__(
        self,
        *,
        artifacts_root: Path,
        account_id: str,
        now_ms: Callable[[], int],
    ) -> None:
        self._artifacts_root = artifacts_root
        self._account_id = account_id
        self._now_ms = now_ms

    def read(self) -> AccountSafetyState:
        """Read the durable state, synthesizing clean only when absent."""

        path = account_safety_path(self._artifacts_root, self._account_id)
        with _account_safety_state_transition_lock(self._artifacts_root, self._account_id):
            state = self._read_locked(path)
        return state or AccountSafetyState(
            account_id=self._account_id,
            updated_at_ms=self._now_ms(),
        )

    def suspend_retired_owner_custody(
        self,
        custody: Iterable[RetiredOwnerCustody],
    ) -> AccountSafetyState:
        """Durably close entry permission while retaining exact owner evidence."""

        retained = tuple(
            sorted(
                set(custody),
                key=lambda item: (
                    item.account_id,
                    item.order_ref,
                    item.intent_id,
                    item.evidence_source,
                ),
            )
        )
        if any(item.account_id != self._account_id for item in retained):
            raise ValueError("ACCOUNT_SAFETY_CUSTODY_ACCOUNT_MISMATCH")
        if not retained:
            return self.read()
        path = account_safety_path(self._artifacts_root, self._account_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with _account_safety_state_transition_lock(self._artifacts_root, self._account_id):
            existing = self._read_locked(path)
            existing_custody = existing.suspension.custody if existing and existing.suspension else ()
            combined = tuple(
                sorted(
                    {*existing_custody, *retained},
                    key=lambda item: (
                        item.account_id,
                        item.order_ref,
                        item.intent_id,
                        item.evidence_source,
                    ),
                )
            )
            if (
                existing is not None
                and existing.verdict is AccountSafetyVerdict.SUSPENDED
                and existing.suspension is not None
                and existing.suspension.custody == combined
            ):
                return existing
            suspension_id = _suspension_id(self._account_id, combined)
            state = AccountSafetyState(
                account_id=self._account_id,
                verdict=AccountSafetyVerdict.SUSPENDED,
                suspension=AccountSafetySuspension(
                    suspension_id=suspension_id,
                    custody=combined,
                    detected_at_ms=self._now_ms(),
                ),
                last_reconciliation_id=(existing.last_reconciliation_id if existing is not None else None),
                last_broker_observed_at_ms=(
                    existing.last_broker_observed_at_ms if existing is not None else None
                ),
                updated_at_ms=self._now_ms(),
            )
            self._write_locked(path, state)
            return state

    def mark_broker_clearance_required(self, *, detected_at_ms: int) -> AccountSafetyState:
        """Require a later fresh Account Truth observation before release.

        A callback can terminally fill an order while leaving a live position.
        The Clerk journal alone cannot attribute a same-symbol position shared
        with a healthy sibling, so it must not release this suspension until
        Account Truth has observed the retired broker exposure gone.
        """

        path = account_safety_path(self._artifacts_root, self._account_id)
        with _account_safety_state_transition_lock(self._artifacts_root, self._account_id):
            state = self._read_locked(path)
            if state is None or state.verdict is not AccountSafetyVerdict.SUSPENDED:
                return state or AccountSafetyState(
                    account_id=self._account_id,
                    updated_at_ms=self._now_ms(),
                )
            suspension = state.suspension
            assert suspension is not None
            updated = state.model_copy(
                update={
                    "suspension": suspension.model_copy(
                        update={
                            "requires_broker_clearance": True,
                            "reconciliation_epoch": None,
                            "reconciliation_id": None,
                        }
                    ),
                    # Do not move the observation watermark backwards.  A
                    # late callback still requires a *newer* full Truth
                    # sweep; an already-recorded snapshot cannot prove that
                    # the callback's resulting position was cleared.
                    "last_broker_observed_at_ms": max(
                        detected_at_ms,
                        state.last_broker_observed_at_ms or 0,
                    ),
                    "updated_at_ms": self._now_ms(),
                }
            )
            self._write_locked(path, updated)
            return updated

    def observe_broker_retired_owner_custody(
        self,
        custody: Iterable[RetiredOwnerCustody],
        *,
        observed_at_ms: int,
    ) -> AccountSafetyState:
        """Replace broker-derived retired custody from one ordered Truth sweep.

        A fresh clean observation only removes the *broker* evidence. It never
        clears the suspension itself; the Clerk still needs its exact successor
        epoch reconciliation before :meth:`lift_if_proven` can reopen entries.
        """

        observed = tuple(
            sorted(
                set(custody),
                key=lambda item: (
                    item.account_id,
                    item.order_ref,
                    item.intent_id,
                    item.evidence_source,
                ),
            )
        )
        if any(item.account_id != self._account_id for item in observed):
            raise ValueError("ACCOUNT_SAFETY_CUSTODY_ACCOUNT_MISMATCH")
        if any(item.evidence_source != "broker" for item in observed):
            raise ValueError("ACCOUNT_SAFETY_BROKER_EVIDENCE_SOURCE_REQUIRED")
        path = account_safety_path(self._artifacts_root, self._account_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with _account_safety_state_transition_lock(self._artifacts_root, self._account_id):
            existing = self._read_locked(path)
            if (
                existing is not None
                and existing.last_broker_observed_at_ms is not None
                and observed_at_ms <= existing.last_broker_observed_at_ms
            ):
                return existing
            prior = existing.suspension.custody if existing and existing.suspension else ()
            clerk_custody = tuple(item for item in prior if item.evidence_source == "clerk")
            combined = tuple(
                sorted(
                    {*clerk_custody, *observed},
                    key=lambda item: (
                        item.account_id,
                        item.order_ref,
                        item.intent_id,
                        item.evidence_source,
                    ),
                )
            )
            prior_suspension = existing.suspension if existing is not None else None
            prior_broker_custody = tuple(
                item
                for item in (prior_suspension.custody if prior_suspension is not None else ())
                if item.evidence_source == "broker"
            )
            broker_evidence_changed = prior_broker_custody != observed
            if not combined and existing is None:
                state = AccountSafetyState(
                    account_id=self._account_id,
                    last_broker_observed_at_ms=observed_at_ms,
                    updated_at_ms=self._now_ms(),
                )
            elif not combined:
                # A clean Truth sweep must not manufacture a suspension for
                # an account that was already clean.  If it cleared the final
                # broker-owned evidence from a prior suspension, retain that
                # suspension until the Clerk supplies a new epoch proof.
                assert existing is not None
                if existing.verdict is not AccountSafetyVerdict.SUSPENDED:
                    state = existing.model_copy(
                        update={
                            "last_broker_observed_at_ms": observed_at_ms,
                            "updated_at_ms": self._now_ms(),
                        }
                    )
                else:
                    assert prior_suspension is not None
                    state = existing.model_copy(
                        update={
                            "suspension": prior_suspension.model_copy(
                                update={
                                    "custody": (),
                                    "suspension_id": _suspension_id(self._account_id, ()),
                                    "reconciliation_epoch": None,
                                    "reconciliation_id": None,
                                    "requires_broker_clearance": False,
                                }
                            ),
                            "last_broker_observed_at_ms": observed_at_ms,
                            "updated_at_ms": self._now_ms(),
                        }
                    )
            else:
                # Do not invalidate an already bound Clerk proof merely
                # because a routine clean account sweep repeated while the
                # suspension is supported only by Clerk custody.  Any changed
                # broker fact, including its explicit clean successor, does
                # invalidate that proof.
                reuse_reconciliation = (
                    prior_suspension is not None
                    and not broker_evidence_changed
                    and not prior_suspension.requires_broker_clearance
                )
                state = AccountSafetyState(
                    account_id=self._account_id,
                    verdict=AccountSafetyVerdict.SUSPENDED,
                    suspension=AccountSafetySuspension(
                        suspension_id=_suspension_id(self._account_id, combined),
                        custody=combined,
                        detected_at_ms=(
                            prior_suspension.detected_at_ms
                            if prior_suspension is not None
                            else observed_at_ms
                        ),
                        reconciliation_epoch=(
                            prior_suspension.reconciliation_epoch
                            if reuse_reconciliation
                            else None
                        ),
                        reconciliation_id=(
                            prior_suspension.reconciliation_id if reuse_reconciliation else None
                        ),
                        # A broker fact must be followed by its clean Truth
                        # successor and then a Clerk epoch proof.
                        requires_broker_clearance=bool(observed),
                    ),
                    last_reconciliation_id=(
                        existing.last_reconciliation_id if existing is not None else None
                    ),
                    last_broker_observed_at_ms=observed_at_ms,
                    updated_at_ms=self._now_ms(),
                )
            self._write_locked(path, state)
            return state

    def bind_reconciliation(self, epoch: AccountEpochState) -> AccountSafetyState:
        """Bind a suspension to the invalid epoch that must prove its cure."""

        if epoch.status != "INVALID" or epoch.reconciliation_id is None:
            raise ValueError("ACCOUNT_SAFETY_RECONCILIATION_EPOCH_REQUIRED")
        path = account_safety_path(self._artifacts_root, self._account_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with _account_safety_state_transition_lock(self._artifacts_root, self._account_id):
            state = self._read_locked(path)
            if state is None or state.verdict is not AccountSafetyVerdict.SUSPENDED:
                return state or AccountSafetyState(
                    account_id=self._account_id,
                    updated_at_ms=self._now_ms(),
                )
            assert state.suspension is not None
            if (
                state.suspension.reconciliation_epoch == epoch.current_epoch
                and state.suspension.reconciliation_id == epoch.reconciliation_id
            ):
                return state
            updated = state.model_copy(
                update={
                    "suspension": state.suspension.model_copy(
                        update={
                            "reconciliation_epoch": epoch.current_epoch,
                            "reconciliation_id": epoch.reconciliation_id,
                        }
                    ),
                    "updated_at_ms": self._now_ms(),
                }
            )
            self._write_locked(path, updated)
            return updated

    def require_entry_admission(self) -> AccountSafetyState:
        """Fail before a new account entry can create custody or broker risk."""

        state = self.read()
        if state.verdict is not AccountSafetyVerdict.CLEAN:
            raise AccountSafetyEntryBlockedError(state)
        return state

    def lift_if_proven(
        self,
        *,
        epoch: AccountEpochState,
        retained_custody: Iterable[RetiredOwnerCustody],
    ) -> AccountSafetyState:
        """Lift only from the exact successor of a suspension's epoch proof."""

        remaining = tuple(retained_custody)
        path = account_safety_path(self._artifacts_root, self._account_id)
        with _account_safety_state_transition_lock(self._artifacts_root, self._account_id):
            state = self._read_locked(path)
            if state is None or state.verdict is not AccountSafetyVerdict.SUSPENDED:
                return state or AccountSafetyState(
                    account_id=self._account_id,
                    updated_at_ms=self._now_ms(),
                )
            suspension = state.suspension
            assert suspension is not None
            if not _may_lift(suspension, epoch=epoch, retained_custody=remaining):
                return state
            updated = AccountSafetyState(
                account_id=self._account_id,
                verdict=AccountSafetyVerdict.CLEAN,
                last_reconciliation_id=epoch.last_reconciliation_id,
                updated_at_ms=self._now_ms(),
            )
            self._write_locked(path, updated)
            return updated

    def _read_locked(self, path: Path) -> AccountSafetyState | None:
        if not path.exists():
            return None
        try:
            state = AccountSafetyState.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError, ValueError) as exc:
            raise AccountSafetyCorruptError(f"account safety state at {path} is unreadable: {exc}") from exc
        if state.account_id != self._account_id:
            raise AccountSafetyCorruptError("account safety state account id does not match its path")
        return state

    @staticmethod
    def _write_locked(path: Path, state: AccountSafetyState) -> None:
        temp_path = path.with_suffix(path.suffix + ".tmp")
        payload = state.model_dump_json().encode("utf-8")
        with open(temp_path, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.replace(temp_path, path)
        except Exception:
            with contextlib.suppress(OSError):
                temp_path.unlink()
            raise
        _fsync_parent_dir(path)


def _custody_is_terminal(entries: Iterable[AccountClerkJournalEntry]) -> bool:
    return any(
        entry.entry_kind == "custody_expired_before_submit"
        or (
            entry.entry_kind == "reconciliation"
            and entry.reconciliation_verdict in {"RECOVER_ADOPT", "HALT"}
        )
        or (
            entry.entry_kind == "broker_event"
            and is_economic_terminal_broker_event(entry.broker_event)
        )
        for entry in entries
    )


def _may_lift(
    suspension: AccountSafetySuspension,
    *,
    epoch: AccountEpochState,
    retained_custody: tuple[RetiredOwnerCustody, ...],
) -> bool:
    return (
        not retained_custody
        and not suspension.requires_broker_clearance
        and not any(item.evidence_source == "broker" for item in suspension.custody)
        and suspension.reconciliation_epoch is not None
        and suspension.reconciliation_id is not None
        and epoch.status == "CLEAN"
        and epoch.reconciliation_verdict in {"CLEAN", "ADOPTED"}
        and epoch.last_reconciliation_id == suspension.reconciliation_id
    )


def _suspension_id(account_id: str, custody: tuple[RetiredOwnerCustody, ...]) -> str:
    # Order refs can be hundreds of characters long.  Keep this durable ID
    # bounded while retaining a deterministic receipt identity for the exact
    # custody set; the full evidence stays in ``AccountSafetySuspension``.
    identity = "\0".join(
        "\0".join(
            (
                item.account_id,
                item.strategy_instance_id,
                item.intent_id,
                item.order_ref,
                item.evidence_source,
            )
        )
        for item in custody
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return f"account-suspension:{account_id}:{digest}"


def retired_owner_broker_custody_from_account_truth(
    account_truth: AccountTruthResponse,
) -> tuple[RetiredOwnerCustody, ...]:
    """Project only exact retired-owner broker facts from an Account Truth sweep."""

    account_id = account_truth.account_id
    if account_id is None:
        return ()
    custody: set[RetiredOwnerCustody] = set()
    for order in account_truth.orders:
        if (
            order.fact_kind == "open_order"
            and _is_retired_truth_owner(order.owner.owner_class, order.owner.owner_binding_state)
        ):
            custody.add(
                RetiredOwnerCustody(
                    account_id=account_id,
                    strategy_instance_id=order.owner.owner_key,
                    intent_id=_broker_evidence_id("order", order.lifecycle_id),
                    order_ref=order.order_ref or _broker_evidence_ref("order", order.lifecycle_id),
                    evidence_source="broker",
                )
            )
    for position in account_truth.positions:
        if _is_retired_truth_owner(position.owner.owner_class, position.owner.owner_binding_state):
            identity = f"{position.con_id}:{position.symbol.upper()}"
            custody.add(
                RetiredOwnerCustody(
                    account_id=account_id,
                    strategy_instance_id=position.owner.owner_key,
                    intent_id=_broker_evidence_id("position", identity),
                    order_ref=_broker_evidence_ref("position", identity),
                    evidence_source="broker",
                )
            )
    return tuple(
        sorted(
            custody,
            key=lambda item: (
                item.account_id,
                item.order_ref,
                item.intent_id,
                item.evidence_source,
            ),
        )
    )


def _is_retired_truth_owner(owner_class: str, binding_state: str) -> bool:
    return owner_class in {"bot", "mixed_known"} and binding_state == "RETIRED"


def _broker_evidence_id(kind: str, identity: str) -> str:
    return f"broker-{kind}:{hashlib.sha256(identity.encode('utf-8')).hexdigest()}"


def _broker_evidence_ref(kind: str, identity: str) -> str:
    return f"broker-{kind}:{hashlib.sha256(identity.encode('utf-8')).hexdigest()}"


def account_safety_blocks_current_bot(
    safety: AccountSafetyState,
    strategy_instance_id: str,
) -> bool:
    """Return True iff account safety should block this specific bot.

    A suspension whose custody list names only other bots' strategy_instance_ids
    must not cascade to bots that have no retired-owner exposure of their own.
    When custody is empty or strategy_instance_id is unknown the check is
    conservative (True) so no unexamined gap silently permits entry.
    """
    if safety.verdict is not AccountSafetyVerdict.SUSPENDED:
        return False
    suspension = safety.suspension
    if suspension is None or not suspension.custody:
        return True
    if not strategy_instance_id:
        return True
    return any(c.strategy_instance_id == strategy_instance_id for c in suspension.custody)


__all__ = [
    "ACCOUNT_SAFETY_ADMISSION_FILENAME",
    "ACCOUNT_SAFETY_ADMISSION_REPAIR_FILENAME",
    "ACCOUNT_SAFETY_FILENAME",
    "AccountSafetyAdmissionError",
    "AccountSafetyAdmissionRepairReceipt",
    "AccountSafetyAuthority",
    "AccountSafetyCorruptError",
    "AccountSafetyEntryBlockedError",
    "AccountSafetyState",
    "AccountSafetySuspension",
    "AccountSafetyVerdict",
    "RetiredOwnerCustody",
    "account_safety_admission_lock",
    "account_safety_admission_path",
    "account_safety_admission_repair_path",
    "account_safety_blocks_current_bot",
    "account_safety_entry_admission_lock",
    "account_safety_path",
    "account_safety_suspension_reservation",
    "repair_account_safety_admission",
    "retired_owner_broker_custody_from_account_truth",
    "retired_owner_nonterminal_custody",
]
