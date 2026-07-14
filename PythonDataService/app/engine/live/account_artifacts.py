"""Account-scoped lifecycle artifacts for live-paper bots."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.engine.live.live_state_sidecar import _file_lock, _fsync_parent_dir
from app.schemas.cohort_batch_launch import validate_cohort_batch_launch_window_and_members
from app.schemas.live_runs import GateResult

ACCOUNT_FREEZE_FILENAME = "unresolved_exposure.flag"
ACCOUNT_EVENTS_FILENAME = "account_events.jsonl"
ACCOUNT_OWNER_GENERATION_FILENAME = "owner_generation.json"
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
        *ACCOUNT_EVENT_TS_FIELD_PRECEDENCE,
    )
)

_ACCOUNT_ID_RE = re.compile(r"^[A-Z][A-Z0-9]+$")
logger = logging.getLogger(__name__)


class AccountArtifactError(ValueError):
    """Raised when an account artifact path or payload is invalid."""


def _safe_account_path_segment(account_id: str) -> str:
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


class AccountEventRecord(BaseModel):
    """Typed forward-write envelope for account-scoped audit events."""

    model_config = ConfigDict(frozen=True, extra="allow")

    account_id: str = Field(min_length=1, max_length=64)
    event_type: str = Field(min_length=1, max_length=128)
    seq: int = Field(ge=1)
    ts_ms: int = Field(ge=0, le=9_223_372_036_854_775_807)


class AccountFreezeEvidence(BaseModel):
    """Durable account-level freeze evidence."""

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
    """Current AccountOwner fencing generation for one account."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    account_id: str = Field(min_length=1, max_length=64)
    generation: int = Field(ge=0)
    phase: Literal["accepting", "reconnecting", "draining", "frozen"] = "accepting"
    recorded_at_ms: int = Field(ge=0)
    source: str = Field(min_length=1)


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


class RestartIntensityPolicy(BaseModel):
    """Durable account restart-intensity threshold."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    threshold: int = Field(default=3, ge=1)
    window_ms: int = Field(default=300_000, ge=1)
    scope: Literal["account"] = "account"
    source: str = RESTART_INTENSITY_SOURCE


class CohortBatchLaunchReceipt(BaseModel):
    """Operator authorization for one deliberate multi-bot launch window."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    schema_version: int = 1
    account_id: str = Field(min_length=1, max_length=64)
    cohort_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    member_strategy_instance_ids: tuple[str, ...] = Field(min_length=1, max_length=128)
    window_start_ms: int = Field(ge=0)
    window_end_ms: int = Field(ge=0)
    authorized_by: str = Field(min_length=1, max_length=128)
    recorded_at_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_window_and_members(self) -> CohortBatchLaunchReceipt:
        validate_cohort_batch_launch_window_and_members(
            self.window_start_ms,
            self.window_end_ms,
            self.member_strategy_instance_ids,
        )
        return self


def account_artifacts_root(artifacts_root: Path, account_id: str) -> Path:
    """Return the confined account artifact directory for one account id.

    ``account_id`` can arrive from URL path segments on operator recovery
    endpoints. Require the already-canonical account-id spelling, reconstruct
    the path component from the regex match, then resolve and assert it remains below
    ``<artifacts_root>/accounts``. The match-group reconstruction, basename
    extraction, and containment check mirror CodeQL's path-injection guidance.
    """
    # Keep the regex capture in this path builder rather than accepting the
    # result of a custom validator. CodeQL recognizes Match.group() as a
    # path-segment sanitizer, while it cannot always follow that fact across
    # a helper return value.
    match = _ACCOUNT_ID_RE.fullmatch(account_id)
    if match is None or account_id != account_id.strip():
        raise AccountArtifactError(f"invalid account_id: {account_id!r}")
    safe_account_id = match.group(0)
    accounts_root = os.path.realpath(os.path.join(os.fspath(artifacts_root), "accounts"))
    candidate = os.path.realpath(os.path.join(accounts_root, safe_account_id))
    try:
        common = os.path.commonpath([candidate, accounts_root])
    except ValueError as exc:
        raise AccountArtifactError(
            f"account artifact path {candidate} cannot share a root with {accounts_root}"
        ) from exc
    if common != accounts_root:
        raise AccountArtifactError(f"path traversal detected for account_id: {account_id!r}")
    return Path(candidate)


def _account_artifact_file_path(
    artifacts_root: Path,
    account_id: str,
    filename: str,
) -> Path:
    """Return a static artifact filename confined below an account root."""
    if filename != os.path.basename(filename):
        raise AccountArtifactError(f"invalid account artifact filename: {filename!r}")
    root = account_artifacts_root(artifacts_root, account_id)
    path = Path(os.path.realpath(os.path.join(os.fspath(root), filename)))
    root_real = os.fspath(root)
    root_prefix = root_real if root_real.endswith(os.sep) else f"{root_real}{os.sep}"
    if not os.fspath(path).startswith(root_prefix):
        raise AccountArtifactError(f"artifact path traversal detected for account_id: {account_id!r}")
    return path


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
    accounts_root = Path(
        os.path.realpath(os.path.join(os.fspath(artifacts_root), "accounts"))
    )
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
                raise AccountArtifactError(
                    f"artifact path traversal detected for account_id: {account_id!r}"
                )
            return artifact_path if artifact_path.is_file() else None
    return None


def write_account_freeze(artifacts_root: Path, evidence: AccountFreezeEvidence) -> Path:
    root = account_artifacts_root(artifacts_root, evidence.account_id)
    root.mkdir(parents=True, exist_ok=True)
    path = root / ACCOUNT_FREEZE_FILENAME
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
    path = _existing_account_artifact_file_path(
        artifacts_root,
        account_id,
        ACCOUNT_FREEZE_FILENAME,
    )
    if path is None:
        return None
    evidence = AccountFreezeEvidence.model_validate_json(path.read_text(encoding="utf-8"))
    if evidence.cleared_at_ms is not None:
        return None
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
    root = account_artifacts_root(artifacts_root, account_id)
    path = root / ACCOUNT_FREEZE_FILENAME
    if not path.is_file():
        raise AccountArtifactError(f"account freeze does not exist for {account_id!r}")
    evidence = AccountFreezeEvidence.model_validate_json(path.read_text(encoding="utf-8"))
    if recovery_proof is not None:
        cleared_at_ms = recovery_proof.recorded_at_ms
        cleared_reason = f"recovery:{recovery_proof.recovery_id}"
        cleared_source = "account_recovery_proof"
        if recovery_proof.reconciliation_result != "clean" or recovery_proof.final_gate_result.status != "pass":
            raise AccountArtifactError("recovery proof must have clean reconciliation and passing final gate")
        _append_account_event(
            artifacts_root,
            account_id,
            {
                "event_type": "account_recovery_proof_recorded",
                "account_id": account_id,
                "recovery_id": recovery_proof.recovery_id,
                "requested_action": recovery_proof.requested_action,
                "requested_by": recovery_proof.requested_by,
                "broker_evidence": recovery_proof.broker_evidence,
                "reconciliation_result": recovery_proof.reconciliation_result,
                "final_gate_result": recovery_proof.final_gate_result.model_dump(mode="json"),
                "recorded_at_ms": recovery_proof.recorded_at_ms,
            },
        )
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
        _append_account_event(
            artifacts_root,
            account_id,
            {
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
            },
        )
    cleared = evidence.model_copy(
        update={
            "cleared_at_ms": cleared_at_ms,
            "cleared_reason": cleared_reason,
            "cleared_source": cleared_source,
        }
    )
    _atomic_write_json(path, cleared.model_dump())
    _append_account_event(
        artifacts_root,
        account_id,
        {
            "event_type": "account_freeze_cleared",
            "account_id": account_id,
            "reason": evidence.reason,
            "source": evidence.source,
            "recorded_at_ms": evidence.recorded_at_ms,
            "cleared_at_ms": cleared_at_ms,
            "cleared_reason": cleared_reason,
            "cleared_source": cleared_source,
        },
    )


def read_account_events(artifacts_root: Path, account_id: str) -> list[dict]:
    """Read account events strictly for canonical safety consumers."""

    path = account_artifacts_root(artifacts_root, account_id) / ACCOUNT_EVENTS_FILENAME
    if not path.is_file():
        return []
    return _parse_account_event_bytes(path, path.read_bytes(), tolerant=False)


def read_account_events_tolerant(artifacts_root: Path, account_id: str) -> list[dict]:
    """Read account events tolerantly for legacy projection/replay adapters."""

    rows, _source_hash = read_account_events_tolerant_with_hash(artifacts_root, account_id)
    return rows


def read_account_events_tolerant_with_hash(artifacts_root: Path, account_id: str) -> tuple[list[dict], str | None]:
    """Read tolerant account events and hash the same byte snapshot."""

    path = account_artifacts_root(artifacts_root, account_id) / ACCOUNT_EVENTS_FILENAME
    events: list[dict] = []
    if not path.is_file():
        return events, None
    with _file_lock(path):
        raw = path.read_bytes()
    return _parse_account_event_bytes(path, raw, tolerant=True), _sha256_bytes(raw)


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
            logger.warning("Skipping malformed account event row", extra={"path": str(path), "line_no": line_no, "error": str(exc)})
            continue
        if not isinstance(row, dict):
            if not tolerant:
                raise AccountArtifactError(f"non-object account event row {line_no} in {path}: {type(row).__name__}")
            logger.warning("Skipping non-object account event row", extra={"path": str(path), "line_no": line_no, "row_type": type(row).__name__})
            continue
        events.append(row)
    return events


def append_account_event(
    artifacts_root: Path,
    account_id: str,
    payload: dict,
    *,
    only_if_receipt_absent: bool = False,
) -> bool:
    """Append an account event, optionally rejecting a duplicate receipt id atomically."""
    receipt_id = payload.get("receipt_id")
    if only_if_receipt_absent and (not isinstance(receipt_id, str) or not receipt_id):
        raise AccountArtifactError("idempotent account event receipt_id must not be empty")
    return _append_account_event(
        artifacts_root,
        account_id,
        {**payload, "account_id": account_id},
        only_if_receipt_absent=only_if_receipt_absent,
    )


def record_cohort_batch_launch_receipt(
    artifacts_root: Path,
    receipt: CohortBatchLaunchReceipt,
) -> None:
    """Append operator authorization for one cohort start window."""

    _append_account_event(
        artifacts_root,
        receipt.account_id,
        {
            "event_type": "cohort_batch_launch_authorized",
            **receipt.model_dump(mode="json"),
        },
    )


def write_account_owner_generation(
    artifacts_root: Path,
    generation: AccountOwnerGeneration,
) -> Path:
    root = account_artifacts_root(artifacts_root, generation.account_id)
    root.mkdir(parents=True, exist_ok=True)
    path = root / ACCOUNT_OWNER_GENERATION_FILENAME
    _atomic_write_json(path, generation.model_dump())
    _append_account_event(
        artifacts_root,
        generation.account_id,
        {
            "event_type": "account_owner_generation_recorded",
            "account_id": generation.account_id,
            "generation": generation.generation,
            "phase": generation.phase,
            "recorded_at_ms": generation.recorded_at_ms,
            "source": generation.source,
        },
    )
    return path


def advance_account_owner_generation(
    artifacts_root: Path,
    account_id: str,
    *,
    phase: Literal["accepting", "reconnecting", "draining", "frozen"],
    recorded_at_ms: int,
    source: str,
) -> AccountOwnerGeneration:
    root = account_artifacts_root(artifacts_root, account_id)
    root.mkdir(parents=True, exist_ok=True)
    path = root / ACCOUNT_OWNER_GENERATION_FILENAME
    with _file_lock(path):
        existing = (
            AccountOwnerGeneration.model_validate_json(path.read_text(encoding="utf-8"))
            if path.is_file()
            else None
        )
        generation = AccountOwnerGeneration(
            account_id=account_id,
            generation=(existing.generation + 1 if existing is not None else 1),
            phase=phase,
            recorded_at_ms=recorded_at_ms,
            source=source,
        )
        _atomic_write_json_locked(path, generation.model_dump())
        _append_account_event(
            artifacts_root,
            account_id,
            {
                "event_type": "account_owner_generation_recorded",
                "account_id": account_id,
                "generation": generation.generation,
                "phase": generation.phase,
                "recorded_at_ms": generation.recorded_at_ms,
                "source": generation.source,
            },
        )
    return generation


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
    return AccountOwnerGeneration.model_validate_json(path.read_text(encoding="utf-8"))


def evaluate_restart_intensity(
    artifacts_root: Path,
    *,
    account_id: str,
    now_ms: int,
    policy: RestartIntensityPolicy | None = None,
    record_freeze: bool = True,
) -> GateResult:
    policy = policy or RestartIntensityPolicy()
    events = read_account_events(artifacts_root, account_id)
    window_start_ms = max(now_ms - policy.window_ms, _latest_restart_intensity_clear_ms(events) or 0)
    restart_events = [
        event
        for event in events
        if event.get("event_type") == "account_instance_binding_recorded"
        and event.get("lifecycle_state") == "ACTIVE"
        and window_start_ms <= int(event.get("recorded_at_ms") or -1) <= now_ms
    ]
    restart_groups = _restart_intensity_groups(events, restart_events)
    observed_count = len(restart_groups)
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


def _restart_intensity_groups(
    account_events: list[dict],
    restart_events: list[dict],
) -> dict[str, list[dict]]:
    """Fold authorized cohort members into one restart event each.

    A cohort receipt applies only to a member binding authored after the
    receipt and inside its window. Daemon-attributed crash restarts are never
    eligible, even if their instance id appears in the operator's receipt.
    """

    receipts = _cohort_batch_launch_receipts(account_events)
    groups: dict[str, list[dict]] = {}
    for event in restart_events:
        cohort_key = _cohort_key_for_binding(event, receipts)
        event_key = cohort_key or f"binding:{event.get('seq')}"
        groups.setdefault(event_key, []).append(event)
    return groups


def _cohort_batch_launch_receipts(events: list[dict]) -> tuple[tuple[int, CohortBatchLaunchReceipt], ...]:
    receipts: list[tuple[int, CohortBatchLaunchReceipt]] = []
    for event in events:
        if event.get("event_type") != "cohort_batch_launch_authorized":
            continue
        try:
            receipt = CohortBatchLaunchReceipt.model_validate(event)
            seq = int(event["seq"])
        except (KeyError, TypeError, ValueError, ValidationError):
            logger.warning("Ignoring invalid cohort batch launch receipt in account events")
            continue
        receipts.append((seq, receipt))
    return tuple(receipts)


def _cohort_key_for_binding(
    binding_event: dict,
    receipts: tuple[tuple[int, CohortBatchLaunchReceipt], ...],
) -> str | None:
    source = str(binding_event.get("source") or "")
    if source.startswith("host_daemon.") and ("crash" in source or "restart" in source):
        return None
    strategy_instance_id = binding_event.get("strategy_instance_id")
    recorded_at_ms = binding_event.get("recorded_at_ms")
    if not isinstance(strategy_instance_id, str) or not isinstance(recorded_at_ms, int):
        return None
    eligible = [
        (seq, receipt)
        for seq, receipt in receipts
        if receipt.recorded_at_ms <= recorded_at_ms
        and receipt.window_start_ms <= recorded_at_ms <= receipt.window_end_ms
        and strategy_instance_id in receipt.member_strategy_instance_ids
    ]
    if not eligible:
        return None
    seq, _receipt = max(eligible, key=lambda item: item[0])
    return f"cohort:{seq}"


def _latest_restart_intensity_clear_ms(events: list[dict]) -> int | None:
    clears = [
        int(event["cleared_at_ms"])
        for event in events
        if event.get("event_type") == "account_freeze_cleared"
        and str(event.get("reason") or "").startswith(RESTART_INTENSITY_REASON)
        and event.get("cleared_at_ms") is not None
    ]
    return max(clears) if clears else None


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


def _append_account_event(
    artifacts_root: Path,
    account_id: str,
    payload: dict,
    *,
    only_if_receipt_absent: bool = False,
) -> bool:
    path = _account_artifact_file_path(
        artifacts_root,
        account_id,
        ACCOUNT_EVENTS_FILENAME,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with _file_lock(path):
        if only_if_receipt_absent:
            events = read_account_events(artifacts_root, account_id)
            receipt_id = payload["receipt_id"]
            if any(event.get("receipt_id") == receipt_id for event in events):
                return False
        enriched = dict(payload)
        enriched["seq"] = _next_account_event_seq_locked(path)
        enriched["ts_ms"] = _account_event_ts_ms_for_write(enriched)
        try:
            record = AccountEventRecord.model_validate(enriched)
        except ValidationError as exc:
            raise AccountArtifactError(f"invalid account event payload: {exc}") from exc
        line = json.dumps(record.model_dump(mode="json"), separators=(",", ":"), sort_keys=True) + "\n"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
        _fsync_parent_dir(path)
    return True


def _next_account_event_seq_locked(path: Path) -> int:
    if not path.exists():
        return 1
    max_seq = 0
    row_count = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row_count += 1
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        seq = row.get("seq")
        if isinstance(seq, int) and not isinstance(seq, bool) and seq > max_seq:
            max_seq = seq
    return max(max_seq, row_count) + 1


def _account_event_ts_ms_for_write(payload: dict) -> int:
    if "ts_ms" in payload and _int_ms_or_none(payload.get("ts_ms")) is None:
        raise AccountArtifactError("account event ts_ms must be a non-negative int64 ms UTC value")
    _validate_account_event_timestamp_fields(payload)
    resolved, _field = resolve_account_event_ts_ms(payload)
    if resolved is not None:
        return resolved
    return time.time_ns() // 1_000_000


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
        "write_account_instance_binding",
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
    "ACCOUNT_FREEZE_FILENAME",
    "ACCOUNT_OWNER_GENERATION_FILENAME",
    "RESTART_INTENSITY_REASON",
    "RESTART_INTENSITY_SOURCE",
    "AccountArtifactError",
    "AccountAuditedOverride",
    "CohortBatchLaunchReceipt",
    "AccountEventRecord",
    "AccountFreezeEvidence",
    "AccountOwnerGeneration",
    "AccountRecoveryProof",
    "RestartIntensityPolicy",
    "account_artifacts_root",
    "advance_account_owner_generation",
    "append_account_event",
    "clear_account_freeze",
    "evaluate_restart_intensity",
    "read_account_events",
    "read_account_events_tolerant",
    "read_account_events_tolerant_with_hash",
    "read_account_freeze",
    "read_account_owner_generation",
    "record_cohort_batch_launch_receipt",
    "resolve_account_event_ts_ms",
    "write_account_freeze",
    "write_account_owner_generation",
]

__all__ = [*_LOCAL_EXPORTS, *_REGISTRY_COMPAT_EXPORTS]
