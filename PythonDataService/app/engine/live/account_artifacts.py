"""Account-scoped lifecycle artifacts for live-paper bots."""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.engine.live.live_state_sidecar import _file_lock, _fsync_parent_dir
from app.schemas.live_runs import GateResult

ACCOUNT_FREEZE_FILENAME = "unresolved_exposure.flag"
ACCOUNT_EVENTS_FILENAME = "account_events.jsonl"
ACCOUNT_EVENTS_SEQUENCE_FILENAME = "account_events.seq"
ACCOUNT_OWNER_GENERATION_FILENAME = "owner_generation.json"
ACCOUNT_CLERK_GENERATION_FILENAME = "clerk_generation.json"
ACCOUNT_CLERK_LEASE_FILENAME = "clerk_lease.json"
ACCOUNT_RECOVERY_CLEARANCE_FILENAME = "account_recovery_clearance.json"
ACCOUNT_RESTART_INTENSITY_CLEARANCE_FILENAME = "account_restart_intensity_clearance.json"
ACCOUNT_RECOVERY_EVIDENCE_EVENT_TYPES = frozenset(
    {
        "account_recovery_proof_recorded",
        "account_audited_override_recorded",
        "account_freeze_cleared",
    }
)
RESTART_INTENSITY_REASON = "restart_intensity.threshold_breached"
RESTART_INTENSITY_SOURCE = "account_restart_intensity"
ACCOUNT_EVENT_TS_FIELD_PRECEDENCE: tuple[str, ...] = (
    "recorded_at_ms",
    "created_at_ms",
    "approved_at_ms",
    "cleared_at_ms",
    "updated_at_ms",
    "decided_at_ms",
    "completed_at_ms",
    "started_at_ms",
)
ACCOUNT_EVENT_TIMESTAMP_FIELDS: frozenset[str] = frozenset(
    (
        "ts_ms",
        "placed_at_ms",
        "valid_until_ms",
        "window_start_ms",
        "window_end_ms",
        "evidence_at_ms",
        "event_at_ms",
        "arrived_at_ms",
        *ACCOUNT_EVENT_TS_FIELD_PRECEDENCE,
    )
)

_ACCOUNT_ID_RE = re.compile(r"^[A-Z][A-Z0-9]+$")
logger = logging.getLogger(__name__)


class AccountArtifactError(ValueError):
    """Raised when an account artifact path or payload is invalid."""


@dataclass(frozen=True)
class AccountEventSequenceRepair:
    """Forensic receipt for a guarded account-event sequence repair."""

    account_id: str
    rewritten_rows: int
    backup_path: Path | None


class AccountClerkLeaseUnavailableError(RuntimeError):
    """Raised when no historical clerk lease satisfies a read-side evidence gate."""

    def __init__(self, *, account_id: str, reason: str) -> None:
        super().__init__(f"account clerk lease unavailable for {account_id}: {reason}")
        self.account_id = account_id
        self.reason = reason


def safe_account_artifact_id(account_id: str) -> str:
    """Return an account id that is safe to use as one artifact path segment."""

    if account_id != account_id.strip():
        raise AccountArtifactError(f"invalid account_id: {account_id!r}")
    match = _ACCOUNT_ID_RE.fullmatch(account_id)
    if match is None:
        raise AccountArtifactError(f"invalid account_id: {account_id!r}")
    matched_account_id = match.group(0)
    safe_account_id = os.path.basename(matched_account_id)
    if safe_account_id != matched_account_id:
        raise AccountArtifactError(f"invalid account_id: {account_id!r}")
    return safe_account_id


# Private compatibility alias for the older binding-ledger import. New callers
# should use the public name so the path-boundary contract is explicit.
_safe_account_path_segment = safe_account_artifact_id


class AccountEventRecord(BaseModel):
    """Typed forward-write envelope for account-scoped audit events."""

    model_config = ConfigDict(frozen=True, extra="allow")

    account_id: str = Field(min_length=1, max_length=64)
    event_type: str = Field(min_length=1, max_length=128)
    seq: int = Field(ge=1)
    ts_ms: int = Field(ge=0, le=9_223_372_036_854_775_807)


class AccountFreezeEvidence(BaseModel):
    """Persisted account-level freeze evidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    account_id: str = Field(min_length=1, max_length=64)
    freeze_kind: Literal["account", "exposure"] = "account"
    reason: str = Field(min_length=1)
    source: str = Field(min_length=1)
    recorded_at_ms: int = Field(ge=0)
    operator_next_step: str = Field(min_length=1)
    cleared_at_ms: int | None = Field(default=None, ge=0)
    cleared_reason: str | None = None
    cleared_source: str | None = None

    @property
    def pauses_healthy_runs(self) -> bool:
        """Whether this active freeze pauses submits without halting a healthy run.

        Restart-intensity evidence stays active until the authoritative provider
        records a clear. Its distinct lifecycle treatment relies on its complete
        typed provenance, rather than a reason string that another freeze could
        reuse.
        """
        return (
            self.freeze_kind == "account"
            and self.source == RESTART_INTENSITY_SOURCE
            and self.reason.startswith(RESTART_INTENSITY_REASON)
        )

    def to_gate_result(self) -> GateResult:
        return GateResult(
            gate_id="account.unresolved_exposure",
            status="freeze",
            source=self.source,
            operator_reason=self.reason,
            operator_next_step=self.operator_next_step,
            evidence_at_ms=self.recorded_at_ms,
        )


class AccountOwnerGeneration(BaseModel):
    """Persisted generation evidence from the retired AccountOwner writer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    account_id: str = Field(min_length=1, max_length=64)
    generation: int = Field(ge=0)
    phase: Literal["accepting", "reconnecting", "draining", "frozen"] = "accepting"
    recorded_at_ms: int = Field(ge=0)
    source: str = Field(min_length=1)


class AccountClerkGeneration(BaseModel):
    """Persisted generation evidence from the retired account-Clerk writer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    account_id: str = Field(min_length=1, max_length=64)
    generation: int = Field(ge=1)
    phase: Literal["accepting", "reconnecting", "draining", "frozen"] = "accepting"
    recorded_at_ms: int = Field(ge=0)
    source: str = Field(min_length=1)


class AccountClerkLease(BaseModel):
    """A historical daemon-supervised account-Clerk lease.

    ``ibkr_client_id`` identifies the broker session that authored the
    evidence. ``None`` remains accepted only for pre-field artifacts.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    account_id: str = Field(min_length=1, max_length=64)
    generation: int = Field(ge=1)
    pid: int = Field(ge=1)
    ibkr_client_id: int | None = Field(default=None, ge=1)
    status: Literal["RUNNING", "DRAINING"] = "RUNNING"
    started_at_ms: int = Field(ge=0)
    renewed_at_ms: int = Field(ge=0)
    valid_until_ms: int = Field(ge=0)


class AccountRecoveryProof(BaseModel):
    """Broker-backed proof that an account freeze can be cleared."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    account_id: str = Field(min_length=1, max_length=64)
    recovery_id: str = Field(min_length=1, max_length=128)
    requested_action: Literal["emergency_flatten", "reconcile"] = "emergency_flatten"
    requested_by: str = Field(min_length=1, max_length=128)
    broker_evidence: dict[str, object] = Field(default_factory=dict)
    reconciliation_result: Literal["clean", "uncertain", "contradicted"]
    final_gate_result: GateResult
    recorded_at_ms: int = Field(ge=0)


class AccountAuditedOverride(BaseModel):
    """Explicit operator override evidence for account recovery."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    account_id: str = Field(min_length=1, max_length=64)
    override_id: str = Field(min_length=1, max_length=128)
    approved_decision: Literal["continue", "adopt", "ignore_baseline", "poison_run", "freeze"]
    reason: str = Field(min_length=1)
    approved_by: str = Field(min_length=1, max_length=128)
    approved_at_ms: int = Field(ge=0)
    valid_until_ms: int = Field(ge=0)
    prior_evidence: dict[str, object] = Field(min_length=1)
    next_reconciliation_step: str = Field(min_length=1)
    strategy_instance_id: str | None = None
    run_id: str | None = None
    bot_order_namespace: str | None = None
    affected_order_refs: tuple[str, ...] = ()


class AccountRecoveryClearance(BaseModel):
    """The latest typed proof that permits a crash-retired bot to restart.

    This artifact is deliberately separate from the operator history and the
    active-freeze artifact. A crash-retired binding can require a recovery
    decision even when no unresolved-exposure freeze was ever created.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    account_id: str = Field(min_length=1, max_length=64)
    source: Literal["account_recovery_proof", "account_audited_override"]
    evidence_id: str = Field(min_length=1, max_length=128)
    cleared_at_ms: int = Field(ge=0)


class RestartIntensityPolicy(BaseModel):
    """Durable account restart-intensity threshold."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    threshold: int = Field(default=3, ge=1)
    window_ms: int = Field(default=300_000, ge=1)
    scope: Literal["account"] = "account"
    source: str = RESTART_INTENSITY_SOURCE


def account_artifacts_root(artifacts_root: Path, account_id: str) -> Path:
    """Return the confined account artifact directory for one account id.

    ``account_id`` can arrive from URL path segments on operator recovery
    endpoints. Require the already-canonical account-id spelling, reconstruct
    the path component from the regex match, then resolve and assert it remains below
    ``<artifacts_root>/accounts``. The match-group reconstruction and resolved-prefix
    check mirror CodeQL's path-injection guidance.
    """
    # Keep the regex capture in this path builder rather than accepting the
    # result of a custom validator. CodeQL recognizes Match.group() as a
    # path-segment sanitizer, while it cannot always follow that fact across
    # a helper return value.
    match = _ACCOUNT_ID_RE.fullmatch(account_id)
    if match is None or account_id != account_id.strip():
        raise AccountArtifactError(f"invalid account_id: {account_id!r}")
    safe_account_id = match.group(0)
    resolved_root = os.path.realpath(os.path.join(os.fspath(artifacts_root), "accounts"))
    resolved = os.path.realpath(os.path.join(resolved_root, safe_account_id))
    root_prefix = resolved_root.rstrip(os.sep) + os.sep
    if not resolved.startswith(root_prefix):
        raise AccountArtifactError(f"path traversal detected for account_id: {account_id!r}")
    return Path(resolved)


def account_artifact_file_path(
    artifacts_root: Path,
    account_id: str,
    filename: str,
) -> Path:
    """Return a static artifact filename confined below one account root.

    This is the public filesystem boundary for account-scoped artifacts. The
    account id is regex-captured in :func:`account_artifacts_root`; the static
    filename and resolved prefix check also reject a leaf symlink that escapes
    the account directory.
    """

    return _account_artifact_file_path(artifacts_root, account_id, filename)


def list_account_artifact_ids(artifacts_root: Path) -> tuple[str, ...]:
    """Return the strict durable-account directory inventory without repairing it."""

    accounts_root = Path(os.path.realpath(os.path.join(os.fspath(artifacts_root), "accounts")))
    try:
        children = tuple(sorted(accounts_root.iterdir(), key=lambda path: path.name))
    except FileNotFoundError:
        return ()
    except OSError as exc:
        raise AccountArtifactError(f"cannot read account artifact directory: {exc}") from exc

    account_ids: list[str] = []
    for child in children:
        if child.is_symlink():
            raise AccountArtifactError(f"account artifact directory must not be a symlink: {child.name!r}")
        if not child.is_dir():
            continue
        account_ids.append(_safe_account_path_segment(child.name))
    return tuple(account_ids)


def _account_artifact_file_path(
    artifacts_root: Path,
    account_id: str,
    filename: str,
) -> Path:
    """Return a static artifact filename confined below an account root."""
    if filename != os.path.basename(filename):
        raise AccountArtifactError(f"invalid account artifact filename: {filename!r}")
    root = account_artifacts_root(artifacts_root, account_id)
    root_real = os.path.realpath(os.fspath(root))
    candidate = os.path.realpath(os.path.join(root_real, filename))
    root_prefix = root_real.rstrip(os.sep) + os.sep
    if not candidate.startswith(root_prefix):
        raise AccountArtifactError(f"artifact path traversal detected for account_id: {account_id!r}")
    return Path(candidate)


def _existing_account_artifact_file_path(
    artifacts_root: Path,
    account_id: str,
    filename: str,
) -> Path | None:
    """Return an existing, non-symlinked static artifact selected from disk.

    The account ID is validated before comparing it with names supplied by the
    server-owned account directory listing. It is never used to construct a
    path for a read operation; this both prevents symlink traversal and keeps
    the read boundary visible to CodeQL.
    """
    safe_account_id = _safe_account_path_segment(account_id)
    if filename != os.path.basename(filename):
        raise AccountArtifactError(f"invalid account artifact filename: {filename!r}")
    accounts_root = Path(os.path.realpath(os.path.join(os.fspath(artifacts_root), "accounts")))
    try:
        account_root = next(
            child
            for child in accounts_root.iterdir()
            if child.name == safe_account_id and child.is_dir() and not child.is_symlink()
        )
    except FileNotFoundError:
        return None
    except StopIteration:
        return None
    for artifact_path in account_root.iterdir():
        if artifact_path.name == filename:
            if artifact_path.is_symlink():
                raise AccountArtifactError(f"artifact path traversal detected for account_id: {account_id!r}")
            return artifact_path if artifact_path.is_file() else None
    return None


def write_account_freeze(artifacts_root: Path, evidence: AccountFreezeEvidence) -> Path:
    path = _account_artifact_file_path(
        artifacts_root,
        evidence.account_id,
        ACCOUNT_FREEZE_FILENAME,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(path, evidence.model_dump())
    _append_account_event(
        artifacts_root,
        evidence.account_id,
        {
            "event_type": "account_freeze_recorded",
            "account_id": evidence.account_id,
            "reason": evidence.reason,
            "freeze_kind": evidence.freeze_kind,
            "source": evidence.source,
            "recorded_at_ms": evidence.recorded_at_ms,
            "operator_next_step": evidence.operator_next_step,
        },
    )
    return path


def read_account_freeze(artifacts_root: Path, account_id: str) -> AccountFreezeEvidence | None:
    evidence = read_account_freeze_evidence(artifacts_root, account_id)
    if evidence is None or evidence.cleared_at_ms is not None:
        return None
    return evidence


def read_account_freeze_evidence(
    artifacts_root: Path,
    account_id: str,
) -> AccountFreezeEvidence | None:
    """Read the typed freeze artifact, including a recorded clearance.

    Safety gates use this artifact for recovery provenance rather than
    reconstructing it from the non-authoritative operator history.
    """

    path = _existing_account_artifact_file_path(
        artifacts_root,
        account_id,
        ACCOUNT_FREEZE_FILENAME,
    )
    if path is None:
        return None
    evidence = AccountFreezeEvidence.model_validate_json(path.read_text(encoding="utf-8"))
    if evidence.account_id != account_id:
        raise AccountArtifactError("account freeze evidence belongs to another account")
    return evidence


def clear_account_freeze(
    artifacts_root: Path,
    *,
    recovery_proof: AccountRecoveryProof | None = None,
    audited_override: AccountAuditedOverride | None = None,
    now_ms: int | None = None,
) -> None:
    if (recovery_proof is None) == (audited_override is None):
        raise AccountArtifactError("provide exactly one recovery_proof or audited_override")
    account_id = recovery_proof.account_id if recovery_proof is not None else audited_override.account_id
    path = _account_artifact_file_path(
        artifacts_root,
        account_id,
        ACCOUNT_FREEZE_FILENAME,
    )
    if recovery_proof is not None:
        cleared_at_ms = recovery_proof.recorded_at_ms
        cleared_reason = f"recovery:{recovery_proof.recovery_id}"
        cleared_source = "account_recovery_proof"
        if recovery_proof.reconciliation_result != "clean" or recovery_proof.final_gate_result.status != "pass":
            raise AccountArtifactError("recovery proof must have clean reconciliation and passing final gate")
        recovery_event = {
            "event_type": "account_recovery_proof_recorded",
            "account_id": account_id,
            "recovery_id": recovery_proof.recovery_id,
            "requested_action": recovery_proof.requested_action,
            "requested_by": recovery_proof.requested_by,
            "broker_evidence": recovery_proof.broker_evidence,
            "reconciliation_result": recovery_proof.reconciliation_result,
            "final_gate_result": recovery_proof.final_gate_result.model_dump(mode="json"),
            "recorded_at_ms": recovery_proof.recorded_at_ms,
        }
    else:
        assert audited_override is not None
        effective_now_ms = time.time_ns() // 1_000_000 if now_ms is None else now_ms
        if audited_override.valid_until_ms < effective_now_ms:
            raise AccountArtifactError("audited override is stale")
        if audited_override.approved_decision == "freeze":
            raise AccountArtifactError("freeze override cannot clear an account freeze")
        cleared_at_ms = effective_now_ms
        cleared_reason = f"override:{audited_override.override_id}:{audited_override.approved_decision}"
        cleared_source = "account_audited_override"
        recovery_event = {
            "event_type": "account_audited_override_recorded",
            "account_id": account_id,
            "override_id": audited_override.override_id,
            "approved_decision": audited_override.approved_decision,
            "reason": audited_override.reason,
            "approved_by": audited_override.approved_by,
            "approved_at_ms": audited_override.approved_at_ms,
            "valid_until_ms": audited_override.valid_until_ms,
            "prior_evidence": audited_override.prior_evidence,
            "next_reconciliation_step": audited_override.next_reconciliation_step,
            "strategy_instance_id": audited_override.strategy_instance_id,
            "run_id": audited_override.run_id,
            "bot_order_namespace": audited_override.bot_order_namespace,
            "affected_order_refs": list(audited_override.affected_order_refs),
        }
    with _file_lock(path):
        if not path.is_file():
            raise AccountArtifactError(f"account freeze does not exist for {account_id!r}")
        evidence = AccountFreezeEvidence.model_validate_json(path.read_text(encoding="utf-8"))
        cleared = evidence.model_copy(
            update={
                "cleared_at_ms": cleared_at_ms,
                "cleared_reason": cleared_reason,
                "cleared_source": cleared_source,
            }
        )
        _atomic_write_json_locked(path, cleared.model_dump())
        try:
            record_account_recovery_clearance(
                artifacts_root,
                recovery_proof=recovery_proof,
                audited_override=audited_override,
                cleared_at_ms=cleared_at_ms,
            )
        except Exception:
            # The cleared freeze is the authority transition. A clearance
            # write failure keeps restart permission conservatively blocked;
            # it must not turn a committed unfreeze into a false failure.
            logger.exception(
                "account freeze cleared but recovery clearance could not be recorded",
                extra={"account_id": account_id, "cleared_at_ms": cleared_at_ms},
            )
    if evidence.pauses_healthy_runs:
        try:
            _record_restart_intensity_clearance(artifacts_root, account_id, cleared_at_ms)
        except Exception:
            # A retained cutoff only prevents a false re-freeze; a failed write
            # keeps the account conservatively subject to restart-intensity, never
            # a false unblock.
            logger.exception(
                "account freeze cleared but restart-intensity cutoff could not be retained",
                extra={"account_id": account_id, "cleared_at_ms": cleared_at_ms},
            )
    cleared_event = {
        "event_type": "account_freeze_cleared",
        "account_id": account_id,
        "reason": evidence.reason,
        "source": evidence.source,
        "recorded_at_ms": evidence.recorded_at_ms,
        "cleared_at_ms": cleared_at_ms,
        "cleared_reason": cleared_reason,
        "cleared_source": cleared_source,
    }
    for event in (recovery_event, cleared_event):
        try:
            _append_account_event(artifacts_root, account_id, event)
        except Exception:
            # Operator history mirrors the committed authority change. Keep a
            # failed mirror visible in structured logs without lying that the
            # freeze remains active.
            logger.exception(
                "account freeze clearance history mirror failed",
                extra={"account_id": account_id, "event_type": event["event_type"]},
            )


def read_account_recovery_clearance(
    artifacts_root: Path,
    account_id: str,
) -> AccountRecoveryClearance | None:
    """Read the dedicated restart-clearance artifact without display history."""

    path = _existing_account_artifact_file_path(
        artifacts_root,
        account_id,
        ACCOUNT_RECOVERY_CLEARANCE_FILENAME,
    )
    if path is None:
        return None
    clearance = AccountRecoveryClearance.model_validate_json(path.read_text(encoding="utf-8"))
    if clearance.account_id != account_id:
        raise AccountArtifactError("account recovery clearance belongs to another account")
    return clearance


def record_account_recovery_clearance(
    artifacts_root: Path,
    *,
    recovery_proof: AccountRecoveryProof | None = None,
    audited_override: AccountAuditedOverride | None = None,
    cleared_at_ms: int | None = None,
) -> AccountRecoveryClearance:
    """Persist qualified recovery evidence before mirroring it to the desk."""

    if (recovery_proof is None) == (audited_override is None):
        raise AccountArtifactError("provide exactly one recovery_proof or audited_override")
    if recovery_proof is not None:
        if recovery_proof.reconciliation_result != "clean" or recovery_proof.final_gate_result.status != "pass":
            raise AccountArtifactError("recovery proof must have clean reconciliation and passing final gate")
        candidate = AccountRecoveryClearance(
            account_id=recovery_proof.account_id,
            source="account_recovery_proof",
            evidence_id=recovery_proof.recovery_id,
            cleared_at_ms=recovery_proof.recorded_at_ms if cleared_at_ms is None else cleared_at_ms,
        )
    else:
        assert audited_override is not None
        candidate_cleared_at_ms = audited_override.approved_at_ms if cleared_at_ms is None else cleared_at_ms
        if audited_override.valid_until_ms < candidate_cleared_at_ms:
            raise AccountArtifactError("audited override is stale")
        if audited_override.approved_decision == "freeze":
            raise AccountArtifactError("freeze override cannot clear an account freeze")
        candidate = AccountRecoveryClearance(
            account_id=audited_override.account_id,
            source="account_audited_override",
            evidence_id=audited_override.override_id,
            cleared_at_ms=candidate_cleared_at_ms,
        )

    path = _account_artifact_file_path(
        artifacts_root,
        candidate.account_id,
        ACCOUNT_RECOVERY_CLEARANCE_FILENAME,
    )
    with _file_lock(path):
        existing = read_account_recovery_clearance(artifacts_root, candidate.account_id)
        if existing is not None and existing.cleared_at_ms >= candidate.cleared_at_ms:
            return existing
        _atomic_write_json_locked(path, candidate.model_dump())
    return candidate


def read_or_migrate_account_recovery_clearance(
    artifacts_root: Path,
    account_id: str,
) -> AccountRecoveryClearance | None:
    """Read the typed clearance, seeding it once from legacy recovery evidence.

    Accounts cleared before the typed ``account_recovery_clearance.json`` artifact
    existed retain only immutable ``account_recovery_proof_recorded`` or
    ``account_audited_override_recorded`` history. Without a one-time migration a
    legitimately recovered crash-retired binding would stay blocked forever. The
    migration is fail-closed: it never materializes a clearance that legacy
    evidence does not already prove, and it surfaces (rather than silences) a
    malformed legacy row.
    """

    existing = read_account_recovery_clearance(artifacts_root, account_id)
    if existing is not None:
        return existing
    legacy = _legacy_recovery_clearance(artifacts_root, account_id)
    if legacy is None:
        return None
    path = _account_artifact_file_path(
        artifacts_root,
        account_id,
        ACCOUNT_RECOVERY_CLEARANCE_FILENAME,
    )
    with _file_lock(path):
        current = read_account_recovery_clearance(artifacts_root, account_id)
        if current is not None:
            return current
        _atomic_write_json_locked(path, legacy.model_dump())
    return legacy


def _legacy_recovery_clearance(
    artifacts_root: Path,
    account_id: str,
) -> AccountRecoveryClearance | None:
    """Reconstruct the latest recovery clearance from pre-artifact events."""

    best: AccountRecoveryClearance | None = None
    for event in read_legacy_account_events(artifacts_root, account_id):
        event_type = event.get("event_type")
        if event_type == "account_recovery_proof_recorded":
            source: Literal["account_recovery_proof", "account_audited_override"] = "account_recovery_proof"
            evidence_id = event.get("recovery_id")
            cleared_at_ms = event.get("recorded_at_ms")
        elif event_type == "account_audited_override_recorded":
            if event.get("approved_decision") == "freeze":
                # A freeze override never cleared a restart block.
                continue
            source = "account_audited_override"
            evidence_id = event.get("override_id")
            cleared_at_ms = event.get("approved_at_ms")
        else:
            continue
        if not isinstance(evidence_id, str) or not 0 < len(evidence_id) <= 128:
            raise AccountArtifactError("legacy account recovery clearance has an invalid evidence id")
        if not isinstance(cleared_at_ms, int) or isinstance(cleared_at_ms, bool) or cleared_at_ms < 0:
            raise AccountArtifactError("legacy account recovery clearance has an invalid timestamp")
        candidate = AccountRecoveryClearance(
            account_id=account_id,
            source=source,
            evidence_id=evidence_id,
            cleared_at_ms=cleared_at_ms,
        )
        if best is None or candidate.cleared_at_ms > best.cleared_at_ms:
            best = candidate
    return best


def read_account_events(artifacts_root: Path, account_id: str) -> list[dict]:
    """Read deterministic operational history without creating global authority.

    Historical ``account_events.jsonl`` rows remain immutable legacy evidence.
    Forward writes live in producer-owned logs and are merged only for reads;
    the returned ``seq`` is a display cursor, never a broker or causal clock.
    Clerk order and exposure authority stays in ``clerk_journal.jsonl``.
    """

    from app.engine.live.producer_operational_log import (
        merge_operator_history,
        read_producer_operational_events,
    )

    legacy_events = _read_legacy_account_events(artifacts_root, account_id, tolerant=False)
    history = merge_operator_history(
        legacy_events=legacy_events,
        producer_records=read_producer_operational_events(artifacts_root, account_id=account_id),
    )
    return [
        {
            **event.payload,
            "account_id": event.account_id,
            "event_type": event.event_type,
            "seq": display_seq,
            "ts_ms": event.display_clock_ms,
            "event_at_ms": event.event_at_ms,
            "arrived_at_ms": event.arrived_at_ms,
            "recorded_at_ms": event.recorded_at_ms,
            "history_provenance": event.source,
            "producer": event.producer,
            "producer_boot_id": event.producer_boot_id,
            "producer_seq": event.producer_seq,
        }
        for display_seq, event in enumerate(history, start=1)
    ]


def read_legacy_account_events(artifacts_root: Path, account_id: str) -> list[dict]:
    """Read only immutable pre-split account-event evidence strictly."""

    return _read_legacy_account_events(artifacts_root, account_id, tolerant=False)


def read_account_events_with_snapshot(
    artifacts_root: Path,
    account_id: str,
) -> tuple[list[dict], bytes]:
    """Read deterministic display history and its virtual serialized snapshot.

    The snapshot is a stable serialization of the merged read projection, not
    a byte image of one physical log and not Clerk authority.  Callers that
    need order or exposure proof must read the canonical Clerk journal.
    """

    events = read_account_events(artifacts_root, account_id)
    raw = b"".join(json.dumps(event, separators=(",", ":"), sort_keys=True).encode() + b"\n" for event in events)
    return events, raw


def read_account_events_tolerant(artifacts_root: Path, account_id: str) -> list[dict]:
    """Read legacy rows tolerantly; producer logs retain their own strict scope."""

    rows, _source_hash = read_account_events_tolerant_with_hash(artifacts_root, account_id)
    return rows


def repair_account_event_sequence(
    artifacts_root: Path,
    account_id: str,
) -> AccountEventSequenceRepair:
    """Restore contiguous event sequence numbers without discarding evidence.

    This is an explicit operator repair for a ledger whose JSON rows remain
    valid but whose durable sequence envelope was duplicated by an interrupted
    or legacy writer. It refuses malformed or cross-account rows, snapshots
    the original bytes beside the ledger, then atomically rewrites only the
    ``seq`` field and repairs the companion counter.
    """

    path = _account_event_ledger_path(artifacts_root, account_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _file_lock(path):
        if not path.is_file():
            return AccountEventSequenceRepair(
                account_id=account_id,
                rewritten_rows=0,
                backup_path=None,
            )
        raw = path.read_bytes()
        events = _parse_account_event_bytes(path, raw, tolerant=False)
        for index, event in enumerate(events, start=1):
            try:
                record = AccountEventRecord.model_validate(event)
            except ValidationError as exc:
                raise AccountArtifactError(f"invalid account event row {index}: {exc}") from exc
            if record.account_id != account_id:
                raise AccountArtifactError(f"account event row {index} belongs to another account")

        if all(event.get("seq") == index for index, event in enumerate(events, start=1)):
            _write_account_event_sequence_locked(path, len(events))
            return AccountEventSequenceRepair(
                account_id=account_id,
                rewritten_rows=0,
                backup_path=None,
            )

        backup_path = path.with_name(f"{path.name}.pre-resequence-{_sha256_bytes(raw)}.bak")
        if backup_path.exists():
            if backup_path.read_bytes() != raw:
                raise AccountArtifactError("account event repair backup conflicts with current ledger bytes")
        else:
            with open(backup_path, "xb") as fh:
                fh.write(raw)
                fh.flush()
                os.fsync(fh.fileno())
            _fsync_parent_dir(backup_path)

        rewritten_rows: list[bytes] = []
        for index, event in enumerate(events, start=1):
            repaired = {**event, "seq": index}
            try:
                record = AccountEventRecord.model_validate(repaired)
            except ValidationError as exc:
                raise AccountArtifactError(f"invalid repaired account event row {index}: {exc}") from exc
            rewritten_rows.append(
                json.dumps(record.model_dump(mode="json"), separators=(",", ":"), sort_keys=True).encode()
            )
        rewritten_bytes = b"\n".join(rewritten_rows) + (b"\n" if rewritten_rows else b"")
        _atomic_replace_account_event_bytes_locked(path, rewritten_bytes)
        _write_account_event_sequence_locked(path, len(events))
    return AccountEventSequenceRepair(
        account_id=account_id,
        rewritten_rows=len(events),
        backup_path=backup_path,
    )


def read_account_events_tolerant_with_hash(artifacts_root: Path, account_id: str) -> tuple[list[dict], str | None]:
    """Read tolerant account events and hash the same byte snapshot."""

    events = _read_legacy_account_events(artifacts_root, account_id, tolerant=True)
    raw = b"".join(json.dumps(event, separators=(",", ":"), sort_keys=True).encode() + b"\n" for event in events)
    return events, _sha256_bytes(raw) if raw else None


def _parse_account_event_bytes(path: Path, raw: bytes, *, tolerant: bool) -> list[dict]:
    if not tolerant:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AccountArtifactError(f"invalid account event UTF-8 in {path}: {exc}") from exc
        lines: list[str | bytes] = text.splitlines()
    else:
        lines = raw.splitlines()

    events: list[dict] = []
    for line_no, line_value in enumerate(lines, start=1):
        if isinstance(line_value, bytes):
            if not line_value.strip():
                continue
            try:
                line = line_value.decode("utf-8")
            except UnicodeDecodeError as exc:
                logger.warning(
                    "Skipping unreadable account event row",
                    extra={"path": str(path), "line_no": line_no, "error": str(exc)},
                )
                continue
        else:
            line = line_value
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            if not tolerant:
                raise AccountArtifactError(f"malformed account event row {line_no} in {path}: {exc}") from exc
            logger.warning(
                "Skipping malformed account event row", extra={"path": str(path), "line_no": line_no, "error": str(exc)}
            )
            continue
        if not isinstance(row, dict):
            if not tolerant:
                raise AccountArtifactError(f"non-object account event row {line_no} in {path}: {type(row).__name__}")
            logger.warning(
                "Skipping non-object account event row",
                extra={"path": str(path), "line_no": line_no, "row_type": type(row).__name__},
            )
            continue
        events.append(row)
    return events


def _read_legacy_account_events(
    artifacts_root: Path,
    account_id: str,
    *,
    tolerant: bool,
) -> list[dict]:
    """Read immutable pre-split evidence without acquiring a shared writer lock."""

    path = _existing_account_artifact_file_path(
        artifacts_root,
        account_id,
        ACCOUNT_EVENTS_FILENAME,
    )
    if path is None:
        return []
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise AccountArtifactError(f"cannot read legacy account event journal: {exc}") from exc
    return _parse_account_event_bytes(path, raw, tolerant=tolerant)


def append_account_event(
    artifacts_root: Path,
    account_id: str,
    payload: dict,
    *,
    only_if_receipt_absent: bool = False,
) -> bool:
    """Append an operational event; durable receipt identities are exactly-once.

    Callers cannot opt a receipt-bearing transition into restart-duplicate
    semantics.  ``only_if_receipt_absent`` remains for explicit call sites,
    while every non-empty ``receipt_id`` receives the same producer-local
    cross-boot claim automatically.
    """
    receipt_id = payload.get("receipt_id")
    if only_if_receipt_absent and (not isinstance(receipt_id, str) or not receipt_id):
        raise AccountArtifactError("idempotent account event receipt_id must not be empty")
    receipt_is_present = isinstance(receipt_id, str) and bool(receipt_id)
    return _append_account_event(
        artifacts_root,
        account_id,
        {**payload, "account_id": account_id},
        only_if_receipt_absent=only_if_receipt_absent or receipt_is_present,
    )


def read_account_owner_generation(
    artifacts_root: Path,
    account_id: str,
) -> AccountOwnerGeneration | None:
    path = _existing_account_artifact_file_path(
        artifacts_root,
        account_id,
        ACCOUNT_OWNER_GENERATION_FILENAME,
    )
    if path is None:
        return None
    generation = AccountOwnerGeneration.model_validate_json(path.read_text(encoding="utf-8"))
    if generation.account_id != account_id:
        raise AccountArtifactError("account owner generation belongs to another account")
    return generation


def read_account_clerk_generation(artifacts_root: Path, account_id: str) -> AccountClerkGeneration | None:
    path = _existing_account_artifact_file_path(
        artifacts_root,
        account_id,
        ACCOUNT_CLERK_GENERATION_FILENAME,
    )
    if path is None:
        return None
    generation = AccountClerkGeneration.model_validate_json(path.read_text(encoding="utf-8"))
    if generation.account_id != account_id:
        raise AccountArtifactError("account clerk generation belongs to another account")
    return generation


def read_account_clerk_lease(artifacts_root: Path, account_id: str) -> AccountClerkLease | None:
    path = _existing_account_artifact_file_path(
        artifacts_root,
        account_id,
        ACCOUNT_CLERK_LEASE_FILENAME,
    )
    if path is None:
        return None
    lease = AccountClerkLease.model_validate_json(path.read_text(encoding="utf-8"))
    if lease.account_id != account_id:
        raise AccountArtifactError("account clerk lease belongs to another account")
    return lease


def require_active_account_clerk_generation(
    artifacts_root: Path,
    account_id: str,
    *,
    now_ms: int,
) -> int:
    """Return a clerk generation backed by matching, unexpired lease evidence.

    A generation file alone is deliberately insufficient: a crashed or reaped
    clerk leaves it behind. Surviving evidence readers therefore require a
    matching, unexpired RUNNING lease as well as the generation record.
    """

    generation = read_account_clerk_generation(artifacts_root, account_id)
    if generation is None:
        raise AccountClerkLeaseUnavailableError(
            account_id=account_id,
            reason="CLERK_GENERATION_MISSING",
        )
    lease = read_account_clerk_lease(artifacts_root, account_id)
    if lease is None:
        raise AccountClerkLeaseUnavailableError(
            account_id=account_id,
            reason="CLERK_LEASE_MISSING",
        )
    if lease.generation != generation.generation:
        raise AccountClerkLeaseUnavailableError(
            account_id=account_id,
            reason="CLERK_LEASE_GENERATION_MISMATCH",
        )
    if lease.status != "RUNNING":
        raise AccountClerkLeaseUnavailableError(
            account_id=account_id,
            reason="CLERK_LEASE_NOT_RUNNING",
        )
    if lease.valid_until_ms <= now_ms:
        raise AccountClerkLeaseUnavailableError(
            account_id=account_id,
            reason="CLERK_LEASE_EXPIRED",
        )
    return generation.generation


def read_active_accepting_account_clerk_generation(
    artifacts_root: Path,
    account_id: str,
    *,
    now_ms: int,
) -> AccountClerkGeneration | None:
    """Return generation evidence only for a Clerk recorded as accepting.

    Lease/artifact failures intentionally propagate so each boundary can
    preserve its existing fail-closed logging or unreadable-evidence mapping.
    """

    clerk = read_account_clerk_generation(artifacts_root, account_id)
    active_generation = require_active_account_clerk_generation(
        artifacts_root,
        account_id,
        now_ms=now_ms,
    )
    if clerk is None or clerk.phase != "accepting" or clerk.generation != active_generation:
        return None
    return clerk


def evaluate_restart_intensity(
    artifacts_root: Path,
    *,
    account_id: str,
    now_ms: int,
    policy: RestartIntensityPolicy | None = None,
    record_freeze: bool = True,
) -> GateResult:
    policy = policy or RestartIntensityPolicy()
    window_start_ms = max(
        now_ms - policy.window_ms,
        _latest_restart_intensity_clear_ms(artifacts_root, account_id) or 0,
    )
    restart_events = _restart_intensity_binding_events(
        artifacts_root,
        account_id=account_id,
        window_start_ms=window_start_ms,
        now_ms=now_ms,
    )
    restart_groups = _restart_intensity_groups(restart_events)
    observed_count = _restart_intensity_count(restart_groups)
    reason = _restart_intensity_reason(
        observed_count=observed_count,
        threshold=policy.threshold,
        window_ms=policy.window_ms,
        window_start_ms=window_start_ms,
        window_end_ms=now_ms,
    )
    if observed_count < policy.threshold:
        return GateResult(
            gate_id="account.restart_intensity",
            status="pass",
            source=policy.source,
            operator_reason=reason,
            operator_next_step="GATE_PASSING",
            evidence_at_ms=now_ms,
        )

    gate = GateResult(
        gate_id="account.restart_intensity",
        status="freeze",
        source=policy.source,
        operator_reason=reason,
        operator_next_step="STOP_RESTARTING_AND_RECOVER_ACCOUNT",
        evidence_at_ms=now_ms,
    )
    if record_freeze and read_account_freeze(artifacts_root, account_id) is None:
        affected_instances = tuple(
            sorted(
                {
                    str(event.get("strategy_instance_id"))
                    for events_in_group in restart_groups.values()
                    for event in events_in_group
                    if event.get("strategy_instance_id")
                }
            )
        )
        _append_account_event(
            artifacts_root,
            account_id,
            {
                "event_type": "account_restart_intensity_breached",
                "account_id": account_id,
                "observed_count": observed_count,
                "threshold": policy.threshold,
                "window_ms": policy.window_ms,
                "window_start_ms": window_start_ms,
                "window_end_ms": now_ms,
                "affected_instance_ids": list(affected_instances),
                "operator_next_step": gate.operator_next_step,
            },
        )
        write_account_freeze(
            artifacts_root,
            AccountFreezeEvidence(
                account_id=account_id,
                freeze_kind="account",
                reason=reason,
                source=policy.source,
                recorded_at_ms=now_ms,
                operator_next_step=gate.operator_next_step or "STOP_RESTARTING_AND_RECOVER_ACCOUNT",
            ),
        )
    return gate


def project_restart_intensity_gate(
    artifacts_root: Path,
    *,
    account_id: str,
    now_ms: int,
    additional_start_groups: int = 1,
    policy: RestartIntensityPolicy | None = None,
) -> GateResult:
    """Project whether a proposed authorized start would breach the restart gate.

    Admission paths use this before persisting an authorization or attempting a
    start.  The projection is deliberately read-only: only an actual accepted
    activation may create the durable account freeze.
    """
    if additional_start_groups < 1:
        raise ValueError("additional_start_groups must be at least one")
    policy = policy or RestartIntensityPolicy()
    window_start_ms = max(
        now_ms - policy.window_ms,
        _latest_restart_intensity_clear_ms(artifacts_root, account_id) or 0,
    )
    restart_events = _restart_intensity_binding_events(
        artifacts_root,
        account_id=account_id,
        window_start_ms=window_start_ms,
        now_ms=now_ms,
    )
    # Dead pre-start projection retained for parity with ``evaluate``: it treats
    # the proposed start(s) as repeat activations of the busiest bot, a
    # conservative upper bound that never under-reports intensity.
    projected_count = _restart_intensity_count(_restart_intensity_groups(restart_events)) + additional_start_groups
    reason = _restart_intensity_reason(
        observed_count=projected_count,
        threshold=policy.threshold,
        window_ms=policy.window_ms,
        window_start_ms=window_start_ms,
        window_end_ms=now_ms,
    )
    if projected_count < policy.threshold:
        return GateResult(
            gate_id="account.restart_intensity",
            status="pass",
            source=policy.source,
            operator_reason=reason,
            operator_next_step="GATE_PASSING",
            evidence_at_ms=now_ms,
        )
    return GateResult(
        gate_id="account.restart_intensity",
        status="freeze",
        source=policy.source,
        operator_reason=reason,
        operator_next_step="WAIT_OR_RECOVER_ACCOUNT_BEFORE_STARTING_ANOTHER_BOT",
        evidence_at_ms=now_ms,
    )


def _restart_intensity_reason(
    *,
    observed_count: int,
    threshold: int,
    window_ms: int,
    window_start_ms: int,
    window_end_ms: int,
) -> str:
    return (
        f"{RESTART_INTENSITY_REASON}:observed={observed_count}:threshold={threshold}:"
        f"window_ms={window_ms}:window_start_ms={window_start_ms}:window_end_ms={window_end_ms}"
    )


def _restart_intensity_groups(restart_events: list[dict]) -> dict[str, list[dict]]:
    """Group ACTIVE activations by bot identity.

    Restart intensity is one bot restarted repeatedly (crash-churn), not a batch
    of distinct freshly-deployed bots each starting once.  Grouping by
    ``strategy_instance_id`` lets the caller measure the busiest bot's activation
    count (:func:`_restart_intensity_count`), so deploying N distinct bots does
    not read as N restarts of one account.
    """

    groups: dict[str, list[dict]] = {}
    for event in restart_events:
        instance = str(event.get("strategy_instance_id") or f"seq:{event.get('seq')}")
        groups.setdefault(instance, []).append(event)
    return groups


def _restart_intensity_count(groups: dict[str, list[dict]]) -> int:
    """Intensity = the most activations any single bot accrued in the window."""

    return max((len(events) for events in groups.values()), default=0)


def _restart_intensity_binding_events(
    artifacts_root: Path,
    *,
    account_id: str,
    window_start_ms: int,
    now_ms: int,
) -> list[dict]:
    """Project restart evidence from the typed binding registry, not history."""

    # Runtime import avoids the legacy compatibility import cycle: the registry
    # owns activation state, while this module owns the restart policy.
    from app.engine.live.account_registry import read_account_instance_registry

    return [
        {
            "strategy_instance_id": binding.strategy_instance_id,
            "run_id": binding.run_id,
            "bot_order_namespace": binding.bot_order_namespace,
            "recorded_at_ms": binding.recorded_at_ms,
        }
        for binding in read_account_instance_registry(artifacts_root, account_id)
        if binding.lifecycle_state == "ACTIVE" and window_start_ms <= binding.recorded_at_ms <= now_ms
    ]


def _record_restart_intensity_clearance(
    artifacts_root: Path,
    account_id: str,
    cleared_at_ms: int,
) -> None:
    """Retain the restart-intensity clear cutoff independently of the freeze slot.

    The active-freeze artifact holds one freeze at a time, so a later unrelated
    freeze would otherwise erase the cutoff that starts a fresh restart-intensity
    window. This dedicated record keeps the latest cutoff monotonically.
    """

    path = _account_artifact_file_path(
        artifacts_root,
        account_id,
        ACCOUNT_RESTART_INTENSITY_CLEARANCE_FILENAME,
    )
    with _file_lock(path):
        existing = _read_restart_intensity_clearance_ms(artifacts_root, account_id)
        if existing is not None and existing >= cleared_at_ms:
            return
        _atomic_write_json_locked(path, {"account_id": account_id, "cleared_at_ms": cleared_at_ms})


def _read_restart_intensity_clearance_ms(artifacts_root: Path, account_id: str) -> int | None:
    """Read the retained restart-intensity clear cutoff, if any."""

    path = _existing_account_artifact_file_path(
        artifacts_root,
        account_id,
        ACCOUNT_RESTART_INTENSITY_CLEARANCE_FILENAME,
    )
    if path is None:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AccountArtifactError("account restart-intensity clearance is unreadable") from exc
    if not isinstance(payload, dict) or payload.get("account_id") != account_id:
        raise AccountArtifactError("account restart-intensity clearance belongs to another account")
    cleared_at_ms = payload.get("cleared_at_ms")
    if not isinstance(cleared_at_ms, int) or isinstance(cleared_at_ms, bool) or cleared_at_ms < 0:
        raise AccountArtifactError("account restart-intensity clearance has an invalid timestamp")
    return cleared_at_ms


def _latest_restart_intensity_clear_ms(artifacts_root: Path, account_id: str) -> int | None:
    candidates: list[int] = []
    dedicated = _read_restart_intensity_clearance_ms(artifacts_root, account_id)
    if dedicated is not None:
        candidates.append(dedicated)
    evidence = read_account_freeze_evidence(artifacts_root, account_id)
    if (
        evidence is not None
        and evidence.cleared_at_ms is not None
        and evidence.source == RESTART_INTENSITY_SOURCE
        and evidence.reason.startswith(RESTART_INTENSITY_REASON)
    ):
        candidates.append(evidence.cleared_at_ms)
    return max(candidates) if candidates else None


def _atomic_write_json(path: Path, payload: dict) -> None:
    with _file_lock(path):
        _atomic_write_json_locked(path, payload)


def _atomic_write_json_locked(path: Path, payload: dict) -> None:
    data = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    _fsync_parent_dir(path)


def _atomic_replace_account_event_bytes_locked(path: Path, data: bytes) -> None:
    """Atomically replace a validated event ledger while its writer lock is held."""

    tmp = path.with_suffix(path.suffix + ".resequence.tmp")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    _fsync_parent_dir(path)


def _append_account_event(
    artifacts_root: Path,
    account_id: str,
    payload: dict,
    *,
    only_if_receipt_absent: bool = False,
) -> bool:
    from app.engine.live.producer_operational_log import (
        ProducerOperationalLogError,
        append_producer_operational_event,
        append_producer_operational_event_if_absent,
    )

    enriched = {**payload, "account_id": account_id}
    _validate_account_event_timestamp_fields(enriched)
    event_type = enriched.get("event_type")
    if not isinstance(event_type, str) or not event_type:
        raise AccountArtifactError("account event event_type must be a non-empty string")
    producer = _producer_for_account_event(event_type)
    idempotency_key = _account_event_idempotency_key(enriched)
    # The compatibility payload can contain historical/generic timestamps
    # such as ``created_at_ms``.  They are evidence *about* the operation,
    # never proof of when this producer durably wrote this row.  Keep that
    # evidence in payload and let the producer log establish the record clock.
    recorded_at_ms = time.time_ns() // 1_000_000
    event_at_ms = _compatibility_event_at_ms(enriched)
    if only_if_receipt_absent:
        try:
            return append_producer_operational_event_if_absent(
                artifacts_root,
                account_id=account_id,
                producer=producer,
                idempotency_key=idempotency_key,
                event_type=event_type,
                payload=enriched,
                event_at_ms=event_at_ms,
                arrived_at_ms=_int_ms_or_none(enriched.get("arrived_at_ms")),
                recorded_at_ms=recorded_at_ms,
            )
        except ProducerOperationalLogError as exc:
            raise AccountArtifactError(str(exc)) from exc
    try:
        append_producer_operational_event(
            artifacts_root,
            account_id=account_id,
            producer=producer,
            idempotency_key=idempotency_key,
            event_type=event_type,
            payload=enriched,
            event_at_ms=event_at_ms,
            arrived_at_ms=_int_ms_or_none(enriched.get("arrived_at_ms")),
            recorded_at_ms=recorded_at_ms,
        )
    except ProducerOperationalLogError as exc:
        raise AccountArtifactError(str(exc)) from exc
    return True


def _producer_for_account_event(event_type: str) -> Literal["bot", "clerk_supervisor", "daemon", "data_plane"]:
    """Route former shared events to the process that owns their operation."""

    if event_type.startswith(("account_instance_binding", "account_binding_retirement")):
        return "daemon"
    if event_type.startswith(("account_owner_", "account_clerk_", "account_epoch_")):
        return "clerk_supervisor"
    if event_type.startswith("account_observation_lease_shadow"):
        return "bot"
    return "data_plane"


def _account_event_idempotency_key(payload: Mapping[str, object]) -> str:
    """Return a receipt identity without collapsing separate observations.

    A caller-supplied receipt is the durable replay identity.  Older account
    event callers that do not own such a receipt represent independent
    operational observations even when their payload and test clock coincide,
    so each gets an immutable per-emission identity rather than being silently
    dropped as a duplicate.
    """

    receipt_id = payload.get("receipt_id")
    if isinstance(receipt_id, str) and receipt_id:
        raw_key = f"receipt:{receipt_id}"
        # Recovery receipts may legally contain a 256-character user supplied
        # idempotency component.  The producer record keeps its key bounded;
        # hash only its representation, never the durable receipt in payload.
        if len(raw_key) <= 256:
            return raw_key
        import hashlib

        return f"receipt-sha256:{hashlib.sha256(raw_key.encode()).hexdigest()}"
    import hashlib

    serialized = json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str)
    return f"observation:{hashlib.sha256(serialized.encode()).hexdigest()}:{uuid.uuid4().hex}"


def _compatibility_event_at_ms(payload: Mapping[str, object]) -> int | None:
    """Map only unambiguous legacy operation clocks to the source-event clock.

    ``created_at_ms`` and generic legacy ``ts_ms`` deliberately remain payload
    evidence: neither proves when the callback occurred.  Named state-change
    clocks, by contrast, are already authored as the time the corresponding
    transition happened and retain the old display ordering without ever being
    presented as this producer's durable-record clock.
    """

    explicit = _int_ms_or_none(payload.get("event_at_ms"))
    if explicit is not None:
        return explicit
    fields = (
        ("cleared_at_ms", "recorded_at_ms")
        if payload.get("event_type") == "account_freeze_cleared"
        else ("recorded_at_ms", "cleared_at_ms")
    ) + (
        "approved_at_ms",
        "updated_at_ms",
        "decided_at_ms",
        "completed_at_ms",
        "started_at_ms",
    )
    for field in fields:
        value = _int_ms_or_none(payload.get(field))
        if value is not None:
            return value
    return None


def _account_event_ledger_path(artifacts_root: Path, account_id: str) -> Path:
    """Return the one contained account-event ledger path for both sinks.

    The validator and realpath checks stay together because this helper is the
    sole CodeQL-auditable boundary between a caller supplied account id and an
    on-disk ledger. Callers must use its returned path rather than composing a
    second account-event filename.
    """

    match = _ACCOUNT_ID_RE.fullmatch(account_id)
    if match is None or account_id != account_id.strip():
        raise AccountArtifactError(f"invalid account_id: {account_id!r}")
    safe_account_id = match.group(0)
    accounts_root = os.path.realpath(os.path.join(os.fspath(artifacts_root), "accounts"))
    account_root = os.path.realpath(os.path.join(accounts_root, safe_account_id))
    accounts_prefix = accounts_root if accounts_root.endswith(os.sep) else f"{accounts_root}{os.sep}"
    if not account_root.startswith(accounts_prefix):
        raise AccountArtifactError(f"path traversal detected for account_id: {account_id!r}")
    event_filename = os.path.basename(ACCOUNT_EVENTS_FILENAME)
    event_path = os.path.realpath(os.path.join(account_root, event_filename))
    account_prefix = account_root if account_root.endswith(os.sep) else f"{account_root}{os.sep}"
    if event_filename != ACCOUNT_EVENTS_FILENAME or not event_path.startswith(account_prefix):
        raise AccountArtifactError(f"artifact path traversal detected for account_id: {account_id!r}")
    return Path(event_path)


def _next_account_event_seq_locked(path: Path) -> int:
    """Allocate from a durable counter, repairing it from the ledger tail.

    The event line is fsynced before its counter is advanced.  A crash in that
    narrow interval leaves a counter behind the tail, while a stale/ahead
    counter can only have been written before its corresponding event became
    durable.  In both cases the tail wins, so recovery neither duplicates nor
    skips a durable sequence identity.  Reading one tail row is constant work
    and replaces the former full-ledger rescan on every append.
    """

    counter_path = _account_event_sequence_path(path)
    counter = _read_account_event_sequence_counter(counter_path)
    tail_seq = _read_account_event_tail_seq(path)
    if tail_seq is None:
        return 1
    if counter is None or counter != tail_seq:
        return tail_seq + 1
    return counter + 1


def _account_event_sequence_path(event_path: Path) -> Path:
    """Return the static counter companion to a validated event-ledger path."""

    return event_path.with_name(ACCOUNT_EVENTS_SEQUENCE_FILENAME)


def _read_account_event_sequence_counter(path: Path) -> int | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    value = payload.get("last_seq") if isinstance(payload, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _read_account_event_tail_seq(path: Path) -> int | None:
    """Read only the final complete ledger row for crash-counter recovery."""

    if not path.is_file() or path.stat().st_size == 0:
        return None
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        end = fh.tell()
        chunk_size = 4096
        data = b""
        while end > 0:
            size = min(chunk_size, end)
            end -= size
            fh.seek(end)
            data = fh.read(size) + data
            lines = data.splitlines()
            if len(lines) < 2 and end > 0:
                continue
            for line in reversed(lines):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    # Old ledgers may contain tolerated projection rows.  The
                    # last valid sequence is enough to migrate them without a
                    # full rescan; strict readers still fail closed later.
                    continue
                if not isinstance(row, dict):
                    continue
                seq = row.get("seq")
                if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
                    continue
                return seq
    return None


def _write_account_event_sequence_locked(event_path: Path, last_seq: int) -> None:
    counter_path = _account_event_sequence_path(event_path)
    _atomic_write_json_locked(counter_path, {"last_seq": last_seq})


def resolve_account_event_ts_ms(row: Mapping[str, object]) -> tuple[int | None, str | None]:
    explicit = _int_ms_or_none(row.get("ts_ms"))
    if explicit is not None:
        return explicit, "ts_ms"
    if row.get("event_type") == "account_freeze_cleared":
        cleared = _int_ms_or_none(row.get("cleared_at_ms"))
        if cleared is not None:
            return cleared, "cleared_at_ms"
    for field in ACCOUNT_EVENT_TS_FIELD_PRECEDENCE:
        candidate = _int_ms_or_none(row.get(field))
        if candidate is not None:
            return candidate, field
    return None, None


def _int_ms_or_none(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) and value >= 0 else None


def _validate_account_event_timestamp_fields(payload: Mapping[str, object]) -> None:
    for field in ACCOUNT_EVENT_TIMESTAMP_FIELDS:
        if field in payload and _int_ms_or_none(payload.get(field)) is None:
            raise AccountArtifactError(f"account event {field} must be a non-negative int64 ms UTC value")


def _sha256_bytes(raw: bytes) -> str:
    import hashlib

    return hashlib.sha256(raw).hexdigest()


_REGISTRY_COMPAT_EXPORTS = frozenset(
    {
        "ACCOUNT_INSTANCE_REGISTRY_FILENAME",
        "ACTIVE_INSTANCE_BINDING_STATES",
        "AccountInstanceBinding",
        "AccountInstanceBindingIndex",
        "bot_order_namespace_for_instance",
        "compute_reconcile_namespaces",
        "crash_retired_restart_blocking_binding",
        "evaluate_account_instance_binding",
        "has_account_recovery_evidence_after",
        "index_account_instance_bindings",
        "latest_account_instance_binding",
        "read_account_instance_registry",
    }
)


def __getattr__(name: str) -> object:
    if name not in _REGISTRY_COMPAT_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from app.engine.live import account_registry

    value = getattr(account_registry, name)
    globals()[name] = value
    return value


_LOCAL_EXPORTS = [
    "ACCOUNT_EVENTS_FILENAME",
    "ACCOUNT_CLERK_GENERATION_FILENAME",
    "ACCOUNT_CLERK_LEASE_FILENAME",
    "ACCOUNT_FREEZE_FILENAME",
    "ACCOUNT_OWNER_GENERATION_FILENAME",
    "ACCOUNT_RECOVERY_CLEARANCE_FILENAME",
    "ACCOUNT_RESTART_INTENSITY_CLEARANCE_FILENAME",
    "RESTART_INTENSITY_REASON",
    "RESTART_INTENSITY_SOURCE",
    "AccountArtifactError",
    "AccountEventSequenceRepair",
    "AccountClerkLeaseUnavailableError",
    "AccountAuditedOverride",
    "AccountEventRecord",
    "AccountClerkGeneration",
    "AccountClerkLease",
    "AccountFreezeEvidence",
    "AccountOwnerGeneration",
    "AccountRecoveryProof",
    "AccountRecoveryClearance",
    "RestartIntensityPolicy",
    "account_artifacts_root",
    "list_account_artifact_ids",
    "append_account_event",
    "clear_account_freeze",
    "evaluate_restart_intensity",
    "project_restart_intensity_gate",
    "read_account_events",
    "read_legacy_account_events",
    "repair_account_event_sequence",
    "read_account_events_with_snapshot",
    "read_account_clerk_generation",
    "read_account_clerk_lease",
    "read_active_accepting_account_clerk_generation",
    "require_active_account_clerk_generation",
    "read_account_events_tolerant",
    "read_account_events_tolerant_with_hash",
    "read_account_freeze",
    "read_account_freeze_evidence",
    "read_account_recovery_clearance",
    "read_or_migrate_account_recovery_clearance",
    "read_account_owner_generation",
    "resolve_account_event_ts_ms",
    "record_account_recovery_clearance",
    "write_account_freeze",
]

__all__ = [*_LOCAL_EXPORTS, *_REGISTRY_COMPAT_EXPORTS]
