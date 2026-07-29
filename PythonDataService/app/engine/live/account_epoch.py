"""Durable account-epoch evidence and broker-write proof owned by the Clerk.

An epoch is deliberately a *proof horizon*, not a connection boolean.  When
the Clerk observes a condition that can make broker knowledge stale, it moves
the account into an invalid epoch and records an idempotent receipt.  A broker
write resumes only after the Clerk durably records a reconciled successor.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from app.engine.live.account_artifacts import (
    ACCOUNT_CLERK_GENERATION_FILENAME,
    account_artifact_file_path,
    append_account_event,
)
from app.engine.live.live_state_sidecar import _file_lock, _fsync_parent_dir

logger = logging.getLogger(__name__)

ACCOUNT_EPOCH_FILENAME = "account_epoch.json"
EpochInvalidationTrigger = Literal[
    "SOCKET_LOSS",
    "IBKR_1100",
    "IBKR_1101",
    "IBKR_1102",
    "CRITICAL_STREAM_SILENCE",
    "ACTIVE_POLL_FAILED",
    "CLERK_DEATH",
    "GENERATION_FENCED",
    "RETIRED_OWNER_EXPOSURE",
]
EpochReconciliationDepth = Literal["full", "incremental"]
EpochReconciliationVerdict = Literal["NOT_REQUIRED", "RECONCILING", "CLEAN", "ADOPTED", "CONTAMINATED"]


class AccountEpoch(BaseModel):
    """The Clerk boot and monotonic proof sequence behind one fact."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    clerk_boot_id: str = Field(min_length=1, max_length=128)
    epoch_seq: int = Field(ge=1)


class AccountEpochInvalidationReceipt(BaseModel):
    """One immutable, idempotent proof that an epoch gate would close."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    receipt_id: str = Field(min_length=1, max_length=256)
    account_id: str = Field(min_length=1, max_length=64)
    trigger: EpochInvalidationTrigger
    origin_epoch: AccountEpoch
    observed_epoch: AccountEpoch
    reconciliation_id: str
    required_reconciliation_depth: EpochReconciliationDepth
    already_invalid: bool
    recorded_at_ms: int = Field(ge=0)


class AccountEpochOutageChanges(BaseModel):
    """Typed discovered and changed facts from one broker outage comparison."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    discovered: tuple[dict[str, JsonValue], ...] = ()
    changed: tuple[dict[str, JsonValue], ...] = ()

    @property
    def has_changes(self) -> bool:
        return bool(self.discovered or self.changed)


class AccountEpochOutageDiff(BaseModel):
    """Complete, JSON-safe proof payload retained with one reconciliation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    required_reconciliation_depth: EpochReconciliationDepth
    performed_reconciliation_depth: Literal["full"] = "full"
    intents: AccountEpochOutageChanges
    orders: AccountEpochOutageChanges
    executions: AccountEpochOutageChanges
    positions: AccountEpochOutageChanges

    @property
    def has_changes(self) -> bool:
        return any(
            changes.has_changes
            for changes in (self.intents, self.orders, self.executions, self.positions)
        )


class AccountEpochReconciliationProof(BaseModel):
    """Immutable Clerk evidence that may, but need not, reopen the write gate."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    account_id: str = Field(min_length=1, max_length=64)
    reconciliation_id: str = Field(min_length=1, max_length=128)
    candidate_epoch: AccountEpoch
    required_reconciliation_depth: EpochReconciliationDepth
    # The Clerk currently executes a full broker-fact sweep even when 1102
    # requires only an incremental minimum. The recorded proof makes that
    # conservative superset explicit rather than silently relabelling it.
    performed_reconciliation_depth: Literal["full"] = "full"
    journal_tail_seq: int = Field(ge=0)
    evidence_status: Literal["COMPLETE", "UNAVAILABLE"]
    outage_diff: AccountEpochOutageDiff

    @property
    def permits_clean_successor(self) -> bool:
        """Whether this complete, flat proof can mint exactly one successor."""

        return (
            self.evidence_status == "COMPLETE"
            and not self.outage_diff.has_changes
        )


class AccountEpochState(BaseModel):
    """The current persisted shadow-epoch authority for one account."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    account_id: str = Field(min_length=1, max_length=64)
    current_epoch: AccountEpoch
    clerk_generation: int = Field(ge=1)
    status: Literal["CLEAN", "INVALID"]
    would_block_reason: str | None = None
    required_reconciliation_depth: EpochReconciliationDepth | None = None
    invalidation_triggers: tuple[EpochInvalidationTrigger, ...] = ()
    # These are written in the same atomic replacement as the new epoch.
    # Account-event projection is replayable from them after a crash.
    invalidation_receipts: tuple[AccountEpochInvalidationReceipt, ...] = ()
    receipt_projection_status: Literal["CURRENT", "PENDING"] = "CURRENT"
    reconciliation_verdict: EpochReconciliationVerdict = "NOT_REQUIRED"
    outage_diff: dict[str, object] | None = None
    reconciliation_id: str | None = Field(default=None, min_length=1, max_length=128)
    last_reconciliation_id: str | None = Field(default=None, min_length=1, max_length=128)
    updated_at_ms: int = Field(ge=0)


class AccountEpochGenerationFencedError(RuntimeError):
    """A stale Clerk attempted to mutate a newer account epoch."""


class AccountEpochWriteBlockedError(RuntimeError):
    """The current account epoch has not durably regained broker-write proof."""

    def __init__(self, state: AccountEpochState) -> None:
        super().__init__(state.would_block_reason or "ACCOUNT_EPOCH_RECONCILING")
        self.state = state


class AccountEpochAuthority:
    """Serialize and persist one account's shadow proof horizon.

    The authority is intentionally synchronous: all mutations are tiny,
    fsynced account-root writes that run inside the Clerk's existing fenced
    boundaries.  It is never instantiated by a bot-side RPC client.
    """

    def __init__(
        self,
        *,
        artifacts_root: Path,
        account_id: str,
        clerk_generation: int,
        clerk_boot_id: str,
        now_ms: Callable[[], int],
        durable_generation_provider: Callable[[], int | None] | None = None,
    ) -> None:
        self._artifacts_root = artifacts_root
        self._account_id = account_id
        self._clerk_generation = clerk_generation
        self._clerk_boot_id = clerk_boot_id
        self._now_ms = now_ms
        self._durable_generation_provider = durable_generation_provider

    def initialize(self) -> AccountEpochState:
        """Attach this Clerk boot without letting an older generation rewrite proof."""

        path = self._path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with _file_lock(self._generation_path()), _file_lock(path):
            self._assert_durable_generation()
            existing = self._read_locked(path)
            if existing is not None and existing.clerk_generation > self._clerk_generation:
                raise AccountEpochGenerationFencedError("ACCOUNT_EPOCH_GENERATION_STALE")
            if existing is None:
                state = AccountEpochState(
                    account_id=self._account_id,
                    current_epoch=AccountEpoch(clerk_boot_id=self._clerk_boot_id, epoch_seq=1),
                    clerk_generation=self._clerk_generation,
                    status="CLEAN",
                    updated_at_ms=self._now_ms(),
                )
                self._write_locked(path, state)
            elif existing.clerk_generation == self._clerk_generation:
                # Reopening the same Clerk generation must not relabel its
                # evidence with a fresh process identifier.
                state = existing
            else:
                origin = existing.current_epoch
                observed = AccountEpoch(
                    clerk_boot_id=self._clerk_boot_id,
                    epoch_seq=origin.epoch_seq + 1,
                )
                reconciliation_id = _reconciliation_id(self._account_id, observed)
                death_receipt = _receipt(
                    account_id=self._account_id,
                    trigger="CLERK_DEATH",
                    origin_epoch=origin,
                    observed_epoch=observed,
                    reconciliation_id=reconciliation_id,
                    depth="full",
                    already_invalid=False,
                    recorded_at_ms=self._now_ms(),
                )
                fence_receipt = _receipt(
                    account_id=self._account_id,
                    trigger="GENERATION_FENCED",
                    origin_epoch=observed,
                    observed_epoch=observed,
                    reconciliation_id=reconciliation_id,
                    depth="full",
                    already_invalid=True,
                    recorded_at_ms=death_receipt.recorded_at_ms,
                )
                state = AccountEpochState(
                    account_id=self._account_id,
                    current_epoch=observed,
                    clerk_generation=self._clerk_generation,
                    status="INVALID",
                    would_block_reason="CLERK_DEATH",
                    required_reconciliation_depth="full",
                    invalidation_triggers=("CLERK_DEATH", "GENERATION_FENCED"),
                    invalidation_receipts=(*existing.invalidation_receipts, death_receipt, fence_receipt),
                    receipt_projection_status="PENDING",
                    reconciliation_verdict="RECONCILING",
                    reconciliation_id=reconciliation_id,
                    updated_at_ms=death_receipt.recorded_at_ms,
                )
                self._write_locked(path, state)
        return self._replay_invalidation_receipts(state)

    def read(self) -> AccountEpochState:
        path = self._path()
        with _file_lock(path):
            state = self._read_locked(path)
        if state is None:
            return self.initialize()
        return self._replay_invalidation_receipts(state)

    def current_epoch(self) -> AccountEpoch:
        return self.read().current_epoch

    def invalidate(
        self,
        trigger: EpochInvalidationTrigger,
        *,
        expected_generation: int | None = None,
    ) -> AccountEpochInvalidationReceipt:
        """Record a trigger exactly once and expose a shadow would-block verdict."""

        expected = self._clerk_generation if expected_generation is None else expected_generation
        if expected != self._clerk_generation:
            raise AccountEpochGenerationFencedError("ACCOUNT_EPOCH_GENERATION_STALE")
        path = self._path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with _file_lock(self._generation_path()), _file_lock(path):
            self._assert_durable_generation()
            state = self._read_locked(path)
            if state is None:
                state = AccountEpochState(
                    account_id=self._account_id,
                    current_epoch=AccountEpoch(clerk_boot_id=self._clerk_boot_id, epoch_seq=1),
                    clerk_generation=self._clerk_generation,
                    status="CLEAN",
                    updated_at_ms=self._now_ms(),
                )
            if state.clerk_generation != self._clerk_generation:
                raise AccountEpochGenerationFencedError("ACCOUNT_EPOCH_GENERATION_STALE")
            already_invalid = state.status == "INVALID"
            origin = state.current_epoch
            existing_receipt = next(
                (
                    item
                    for item in state.invalidation_receipts
                    if item.trigger == trigger and item.observed_epoch == origin
                ),
                None,
            )
            if existing_receipt is not None:
                return existing_receipt
            # A new outage observation always starts a new candidate epoch.
            # Keeping different signals on one candidate would let a full
            # 1101 reconciliation clear evidence that arrived later under a
            # distinct 1102 (or transport-loss) proof horizon.
            observed = AccountEpoch(
                clerk_boot_id=self._clerk_boot_id,
                epoch_seq=origin.epoch_seq + 1,
            )
            depth = _reconciliation_depth(trigger)
            reconciliation_id = _reconciliation_id(self._account_id, observed)
            receipt = _receipt(
                account_id=self._account_id,
                trigger=trigger,
                origin_epoch=origin,
                observed_epoch=observed,
                reconciliation_id=reconciliation_id,
                depth=depth,
                already_invalid=already_invalid,
                recorded_at_ms=self._now_ms(),
            )
            state = AccountEpochState(
                account_id=self._account_id,
                current_epoch=observed,
                clerk_generation=state.clerk_generation,
                status="INVALID",
                would_block_reason=trigger,
                required_reconciliation_depth=depth,
                invalidation_triggers=(trigger,),
                invalidation_receipts=(*state.invalidation_receipts, receipt),
                receipt_projection_status="PENDING",
                reconciliation_verdict="RECONCILING",
                reconciliation_id=reconciliation_id,
                updated_at_ms=receipt.recorded_at_ms,
            )
            self._write_locked(path, state)
        self._replay_invalidation_receipts(state)
        return receipt

    def require_broker_write(self) -> AccountEpochState:
        """Recheck the durable epoch immediately before a Clerk broker write."""

        path = self._path()
        with _file_lock(self._generation_path()), _file_lock(path):
            self._assert_durable_generation()
            state = self._read_locked(path)
            if state is None or state.status != "CLEAN":
                raise AccountEpochWriteBlockedError(
                    state
                    if state is not None
                    else AccountEpochState(
                        account_id=self._account_id,
                        current_epoch=AccountEpoch(clerk_boot_id=self._clerk_boot_id, epoch_seq=1),
                        clerk_generation=self._clerk_generation,
                        status="INVALID",
                        would_block_reason="ACCOUNT_EPOCH_MISSING",
                        reconciliation_verdict="RECONCILING",
                        updated_at_ms=self._now_ms(),
                    )
                )
            if state.reconciliation_verdict not in {"NOT_REQUIRED", "CLEAN", "ADOPTED"}:
                raise AccountEpochWriteBlockedError(state)
            return state

    def complete_reconciliation(self, proof: AccountEpochReconciliationProof) -> AccountEpochState:
        """Durably resolve an invalid epoch and mint a successor only on proof."""

        path = self._path()
        with _file_lock(self._generation_path()), _file_lock(path):
            self._assert_durable_generation()
            state = self._read_locked(path)
            if (
                state is None
                or state.reconciliation_id != proof.reconciliation_id
                or state.account_id != proof.account_id
                or state.current_epoch != proof.candidate_epoch
                or state.required_reconciliation_depth != proof.required_reconciliation_depth
            ):
                raise ValueError("ACCOUNT_EPOCH_RECONCILIATION_ID_MISMATCH")
            if state.status != "INVALID":
                raise ValueError("ACCOUNT_EPOCH_RECONCILIATION_NOT_REQUIRED")
            if not proof.permits_clean_successor:
                updated = state.model_copy(
                    update={
                        "status": "INVALID",
                        "would_block_reason": "ACCOUNT_EPOCH_CONTAMINATED",
                        "reconciliation_verdict": "CONTAMINATED",
                        "outage_diff": proof.outage_diff.model_dump(mode="json"),
                        "updated_at_ms": self._now_ms(),
                    }
                )
            else:
                successor = AccountEpoch(
                    clerk_boot_id=self._clerk_boot_id,
                    epoch_seq=state.current_epoch.epoch_seq + 1,
                )
                updated = state.model_copy(
                    update={
                        "current_epoch": successor,
                        "status": "CLEAN",
                        "would_block_reason": None,
                        "reconciliation_verdict": "CLEAN",
                        "outage_diff": proof.outage_diff.model_dump(mode="json"),
                        "reconciliation_id": None,
                        "last_reconciliation_id": proof.reconciliation_id,
                        "updated_at_ms": self._now_ms(),
                    }
                )
            self._write_locked(path, updated)
        verdict: Literal["CLEAN", "CONTAMINATED"] = "CLEAN" if updated.status == "CLEAN" else "CONTAMINATED"
        try:
            append_account_event(
                self._artifacts_root,
                self._account_id,
                {
                    "event_type": "account_epoch_reconciliation_resolved",
                    "receipt_id": f"account-epoch-reconciliation:{proof.reconciliation_id}:{verdict}",
                    "reconciliation_id": proof.reconciliation_id,
                    "verdict": verdict,
                    "outage_diff": proof.outage_diff.model_dump(mode="json"),
                    "origin_epoch": state.current_epoch.model_dump(mode="json"),
                    "observed_epoch": updated.current_epoch.model_dump(mode="json"),
                    "recorded_at_ms": updated.updated_at_ms,
                },
                only_if_receipt_absent=True,
            )
        except Exception:
            # The state replacement above is the durable admission proof.  A
            # projection outage must not make a completed reconciliation look
            # retriable and mint another successor; the persisted epoch state
            # remains the authoritative operator-health evidence.
            logger.exception(
                "account epoch reconciliation event projection failed",
                extra={"account_id": self._account_id, "reconciliation_id": proof.reconciliation_id},
            )
        return updated

    def _replay_invalidation_receipts(self, state: AccountEpochState) -> AccountEpochState:
        """Project fsynced epoch receipts into the account-event evidence log."""

        try:
            for receipt in state.invalidation_receipts:
                append_account_event(
                    self._artifacts_root,
                    self._account_id,
                    {
                        "event_type": "account_epoch_invalidated",
                        **receipt.model_dump(mode="json"),
                    },
                    only_if_receipt_absent=True,
                )
        except Exception:
            logger.exception(
                "account epoch receipt projection is pending",
                extra={"account_id": self._account_id},
            )
            return self._set_receipt_projection_status(state, "PENDING")
        return self._set_receipt_projection_status(state, "CURRENT")

    def _set_receipt_projection_status(
        self,
        state: AccountEpochState,
        status: Literal["CURRENT", "PENDING"],
    ) -> AccountEpochState:
        if state.receipt_projection_status == status:
            return state
        path = self._path()
        try:
            with _file_lock(self._generation_path()), _file_lock(path):
                self._assert_durable_generation()
                current = self._read_locked(path)
                if current is None:
                    return state
                if current.invalidation_receipts != state.invalidation_receipts:
                    return current
                updated = current.model_copy(update={"receipt_projection_status": status})
                self._write_locked(path, updated)
                return updated
        except Exception:
            logger.exception(
                "could not update account epoch receipt projection health",
                extra={"account_id": self._account_id},
            )
            return state

    def _assert_durable_generation(self) -> None:
        if self._durable_generation_provider is None:
            return
        if self._durable_generation_provider() != self._clerk_generation:
            raise AccountEpochGenerationFencedError("ACCOUNT_EPOCH_GENERATION_STALE")

    def _path(self) -> Path:
        return account_artifact_file_path(
            self._artifacts_root,
            self._account_id,
            ACCOUNT_EPOCH_FILENAME,
        )

    def _generation_path(self) -> Path:
        return account_artifact_file_path(
            self._artifacts_root,
            self._account_id,
            ACCOUNT_CLERK_GENERATION_FILENAME,
        )

    @staticmethod
    def _read_locked(path: Path) -> AccountEpochState | None:
        if not path.is_file():
            return None
        return AccountEpochState.model_validate_json(path.read_text(encoding="utf-8"))

    @staticmethod
    def _write_locked(path: Path, state: AccountEpochState) -> None:
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(state.model_dump(mode="json"), handle, separators=(",", ":"), sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
            _fsync_parent_dir(path)
        finally:
            if temp_path.exists():
                temp_path.unlink()


def _reconciliation_depth(trigger: EpochInvalidationTrigger) -> EpochReconciliationDepth:
    # 1102 restores maintained data while 1101 explicitly loses it.  Both
    # open a new candidate epoch; only their later proof depth differs.
    return "incremental" if trigger == "IBKR_1102" else "full"


def _reconciliation_id(account_id: str, epoch: AccountEpoch) -> str:
    return f"epoch:{account_id}:{epoch.clerk_boot_id}:{epoch.epoch_seq}"


def _receipt(
    *,
    account_id: str,
    trigger: EpochInvalidationTrigger,
    origin_epoch: AccountEpoch,
    observed_epoch: AccountEpoch,
    reconciliation_id: str,
    depth: EpochReconciliationDepth,
    already_invalid: bool,
    recorded_at_ms: int,
) -> AccountEpochInvalidationReceipt:
    return AccountEpochInvalidationReceipt(
        receipt_id=(
            f"account-epoch:{account_id}:{observed_epoch.clerk_boot_id}:"
            f"{observed_epoch.epoch_seq}:{trigger}"
        ),
        account_id=account_id,
        trigger=trigger,
        origin_epoch=origin_epoch,
        observed_epoch=observed_epoch,
        reconciliation_id=reconciliation_id,
        required_reconciliation_depth=depth,
        already_invalid=already_invalid,
        recorded_at_ms=recorded_at_ms,
    )


__all__ = [
    "ACCOUNT_EPOCH_FILENAME",
    "AccountEpoch",
    "AccountEpochAuthority",
    "AccountEpochGenerationFencedError",
    "AccountEpochInvalidationReceipt",
    "AccountEpochOutageChanges",
    "AccountEpochOutageDiff",
    "AccountEpochReconciliationProof",
    "AccountEpochState",
    "AccountEpochWriteBlockedError",
    "EpochInvalidationTrigger",
]
