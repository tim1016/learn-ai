"""Independent durable operational logs for account-scoped producers.

These logs deliberately do not compete with ``clerk_journal.jsonl``.  A row
can describe a daemon, supervisor, data-plane, or bot observation, but it is
never an order, exposure, or reconciliation authority.  Operator history may
merge these rows with legacy account events for display; that merge is stable,
not a causal total order.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import Iterable
from hashlib import sha256
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.engine.live import durable_append_log
from app.engine.live.account_artifacts import AccountArtifactError, account_artifacts_root
from app.schemas.artifact_io import atomic_write_pydantic_artifact
from app.utils.advisory_lock import advisory_file_lock
from app.utils.timestamps import now_ms_utc

PRODUCER_OPERATIONAL_LOGS_DIRECTORY = "producer_operational_logs"
PRODUCER_OPERATIONAL_RECEIPT_CLAIMS_DIRECTORY = "producer_operational_receipt_claims"
ProducerOperationalKind = Literal["bot", "clerk_supervisor", "daemon", "data_plane"]
_PRODUCER_SEGMENT_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_BOOT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_PROCESS_BOOT_ID = uuid.uuid4().hex


class ProducerOperationalLogError(ValueError):
    """Raised when one producer's durable operational evidence is unreadable."""


class ProducerOperationalLogRecord(BaseModel):
    """One immutable producer-local operational observation.

    ``producer_seq`` orders only records from this producer boot.  The three
    clocks retain their distinct meanings and may be absent when the producer
    did not observe the corresponding phase.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    account_id: str = Field(min_length=1, max_length=64)
    producer: ProducerOperationalKind
    producer_boot_id: str = Field(min_length=1, max_length=128)
    producer_seq: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=256)
    event_type: str = Field(min_length=1, max_length=128)
    event_at_ms: int | None = Field(default=None, ge=0, le=9_223_372_036_854_775_807)
    arrived_at_ms: int | None = Field(default=None, ge=0, le=9_223_372_036_854_775_807)
    recorded_at_ms: int = Field(ge=0, le=9_223_372_036_854_775_807)
    payload: dict[str, object] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_boot_id(self) -> ProducerOperationalLogRecord:
        if _BOOT_ID_RE.fullmatch(self.producer_boot_id) is None:
            raise ValueError("producer_boot_id must be a safe opaque identifier")
        return self

    @property
    def display_clock_ms(self) -> int:
        """Choose a deterministic display clock without claiming causality."""

        if self.event_at_ms is not None:
            return self.event_at_ms
        if self.arrived_at_ms is not None:
            return self.arrived_at_ms
        return self.recorded_at_ms


class _ProducerOperationalReceiptClaim(BaseModel):
    """The durable ownership token for one cross-boot receipt append.

    This intentionally lives outside the display-log tree.  A damaged old log
    must not turn an already-committed Clerk decision into a failed retry.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    account_id: str = Field(min_length=1, max_length=64)
    producer: ProducerOperationalKind
    idempotency_key: str = Field(min_length=1, max_length=256)
    event_type: str = Field(min_length=1, max_length=128)
    payload: dict[str, object] = Field(default_factory=dict)
    event_at_ms: int | None = Field(default=None, ge=0, le=9_223_372_036_854_775_807)
    arrived_at_ms: int | None = Field(default=None, ge=0, le=9_223_372_036_854_775_807)
    producer_boot_id: str = Field(min_length=1, max_length=128)
    recorded: bool = False


class MergedOperationalHistoryEvent(BaseModel):
    """One deterministic operator-history row with explicit provenance."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str = Field(min_length=1, max_length=512)
    source: Literal["legacy_account_events", "producer_operational_log", "clerk_journal"]
    account_id: str = Field(min_length=1, max_length=64)
    event_type: str = Field(min_length=1, max_length=128)
    display_clock_ms: int = Field(ge=0, le=9_223_372_036_854_775_807)
    event_at_ms: int | None = Field(default=None, ge=0, le=9_223_372_036_854_775_807)
    arrived_at_ms: int | None = Field(default=None, ge=0, le=9_223_372_036_854_775_807)
    recorded_at_ms: int = Field(ge=0, le=9_223_372_036_854_775_807)
    producer: str = Field(min_length=1, max_length=128)
    producer_boot_id: str = Field(min_length=1, max_length=128)
    producer_seq: int = Field(ge=1)
    payload: dict[str, object] = Field(default_factory=dict)


def process_producer_boot_id() -> str:
    """Return this process's opaque boot identity for a producer-local stream."""

    return _PROCESS_BOOT_ID


def append_producer_operational_event(
    artifacts_root: Path,
    *,
    account_id: str,
    producer: ProducerOperationalKind,
    idempotency_key: str,
    event_type: str,
    payload: dict[str, object],
    event_at_ms: int | None = None,
    arrived_at_ms: int | None = None,
    recorded_at_ms: int | None = None,
    producer_boot_id: str | None = None,
) -> ProducerOperationalLogRecord:
    """Durably append one operational observation or return its same-boot replay.

    Each producer boot owns a separate JSONL path, so unrelated producers do
    not allocate one shared sequence or contend on a cross-runtime mutex.
    Replay detection is intentionally scoped to that one producer boot.  A
    restart may preserve a duplicate raw observation under a new boot ID; the
    read projection deduplicates its stable ``idempotency_key`` for display
    while retaining both durable receipts for audit.
    """

    boot_id = process_producer_boot_id() if producer_boot_id is None else producer_boot_id
    path = producer_operational_log_path(
        artifacts_root,
        account_id=account_id,
        producer=producer,
        producer_boot_id=boot_id,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with advisory_file_lock(path):
        existing = _read_records_from_path(
            path,
            expected_account_id=account_id,
            expected_producer=producer,
            expected_producer_boot_id=boot_id,
        )
        replay = next((record for record in existing if record.idempotency_key == idempotency_key), None)
        if replay is not None:
            _assert_replay_matches(
                replay,
                event_type=event_type,
                payload=payload,
                event_at_ms=event_at_ms,
                arrived_at_ms=arrived_at_ms,
            )
            return replay
        record = ProducerOperationalLogRecord(
            account_id=account_id,
            producer=producer,
            producer_boot_id=boot_id,
            producer_seq=len(existing) + 1,
            idempotency_key=idempotency_key,
            event_type=event_type,
            event_at_ms=event_at_ms,
            arrived_at_ms=arrived_at_ms,
            recorded_at_ms=now_ms_utc() if recorded_at_ms is None else recorded_at_ms,
            payload=payload,
        )
        try:
            durable_append_log.append_jsonl_record(
                path,
                record.model_dump_json(),
                trusted_root=path.parent,
            )
        except ValueError as exc:
            raise ProducerOperationalLogError(str(exc)) from exc
    return record


def append_producer_operational_event_if_absent(
    artifacts_root: Path,
    *,
    account_id: str,
    producer: ProducerOperationalKind,
    idempotency_key: str,
    event_type: str,
    payload: dict[str, object],
    event_at_ms: int | None = None,
    arrived_at_ms: int | None = None,
    recorded_at_ms: int | None = None,
    producer_boot_id: str | None = None,
) -> bool:
    """Append one receipt exactly once across boots of the same producer.

    Ordinary operational observations may retain a duplicate raw row after a
    restart; callers use :func:`append_producer_operational_event` for that
    audit trail.  Receipt-bearing state transitions instead use a typed receipt
    claim outside the display-log tree.  Thus a damaged historical boot cannot
    block a retry after canonical authority has already been committed.
    """

    boot_id = process_producer_boot_id() if producer_boot_id is None else producer_boot_id
    claim_path = _receipt_claim_path(
        artifacts_root,
        account_id=account_id,
        producer=producer,
        idempotency_key=idempotency_key,
    )
    claim_path.parent.mkdir(parents=True, exist_ok=True)
    with advisory_file_lock(claim_path):
        claim = _read_receipt_claim(claim_path)
        if claim is None:
            claim = _ProducerOperationalReceiptClaim(
                account_id=account_id,
                producer=producer,
                idempotency_key=idempotency_key,
                event_type=event_type,
                payload=payload,
                event_at_ms=event_at_ms,
                arrived_at_ms=arrived_at_ms,
                producer_boot_id=boot_id,
            )
            atomic_write_pydantic_artifact(claim_path, claim)
        _assert_receipt_claim_matches(
            claim,
            account_id=account_id,
            producer=producer,
            idempotency_key=idempotency_key,
            event_type=event_type,
            payload=payload,
            event_at_ms=event_at_ms,
            arrived_at_ms=arrived_at_ms,
        )
        if claim.recorded:
            return False
        append_producer_operational_event(
            artifacts_root,
            account_id=account_id,
            producer=producer,
            idempotency_key=idempotency_key,
            event_type=event_type,
            payload=payload,
            event_at_ms=event_at_ms,
            arrived_at_ms=arrived_at_ms,
            recorded_at_ms=recorded_at_ms,
            producer_boot_id=claim.producer_boot_id,
        )
        atomic_write_pydantic_artifact(claim_path, claim.model_copy(update={"recorded": True}))
    return True


def _receipt_claim_path(
    artifacts_root: Path,
    *,
    account_id: str,
    producer: ProducerOperationalKind,
    idempotency_key: str,
) -> Path:
    """Return a confined typed claim path without exposing the receipt token."""

    if _PRODUCER_SEGMENT_RE.fullmatch(producer) is None:
        raise ProducerOperationalLogError(f"invalid producer {producer!r}")
    digest = sha256(idempotency_key.encode("utf-8")).hexdigest()
    account_root = account_artifacts_root(artifacts_root, account_id)
    claims_root = account_root / PRODUCER_OPERATIONAL_RECEIPT_CLAIMS_DIRECTORY
    path = claims_root / producer / f"{digest}.json"
    _assert_confined(path, account_root)
    return path


def _read_receipt_claim(path: Path) -> _ProducerOperationalReceiptClaim | None:
    if not path.exists():
        return None
    try:
        return _ProducerOperationalReceiptClaim.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError, ValueError) as exc:
        raise ProducerOperationalLogError("producer operational receipt claim is unreadable") from exc


def _assert_receipt_claim_matches(
    claim: _ProducerOperationalReceiptClaim,
    *,
    account_id: str,
    producer: ProducerOperationalKind,
    idempotency_key: str,
    event_type: str,
    payload: dict[str, object],
    event_at_ms: int | None,
    arrived_at_ms: int | None,
) -> None:
    if (
        claim.account_id != account_id
        or claim.producer != producer
        or claim.idempotency_key != idempotency_key
        or claim.event_type != event_type
        or claim.payload != payload
        or claim.event_at_ms != event_at_ms
        or claim.arrived_at_ms != arrived_at_ms
    ):
        raise ProducerOperationalLogError(
            "producer operational idempotency key is already bound to different evidence"
        )


def producer_operational_log_path(
    artifacts_root: Path,
    *,
    account_id: str,
    producer: ProducerOperationalKind,
    producer_boot_id: str,
) -> Path:
    """Return one confined static producer-boot log path."""

    if _PRODUCER_SEGMENT_RE.fullmatch(producer) is None:
        raise ProducerOperationalLogError(f"invalid producer {producer!r}")
    if _BOOT_ID_RE.fullmatch(producer_boot_id) is None:
        raise ProducerOperationalLogError("producer_boot_id must be a safe opaque identifier")
    account_root = account_artifacts_root(artifacts_root, account_id)
    logs_root = account_root / PRODUCER_OPERATIONAL_LOGS_DIRECTORY
    _assert_confined(logs_root, account_root)
    path = logs_root / producer / f"{producer_boot_id}.jsonl"
    _assert_confined(path, logs_root)
    return path


def read_producer_operational_events(
    artifacts_root: Path,
    *,
    account_id: str,
    producer: ProducerOperationalKind | None = None,
) -> tuple[ProducerOperationalLogRecord, ...]:
    """Read every selected producer log strictly and deterministically."""

    account_root = account_artifacts_root(artifacts_root, account_id)
    logs_root = account_root / PRODUCER_OPERATIONAL_LOGS_DIRECTORY
    if not logs_root.exists():
        return ()
    if logs_root.is_symlink() or not logs_root.is_dir():
        raise ProducerOperationalLogError("producer operational log directory is invalid")
    records: list[ProducerOperationalLogRecord] = []
    producer_paths = sorted(logs_root.iterdir(), key=lambda path: path.name)
    for producer_path in producer_paths:
        if producer_path.is_symlink() or not producer_path.is_dir():
            raise ProducerOperationalLogError("producer operational log directory entry is invalid")
        if producer is not None and producer_path.name != producer:
            continue
        if _PRODUCER_SEGMENT_RE.fullmatch(producer_path.name) is None:
            raise ProducerOperationalLogError("producer operational log has an invalid producer segment")
        for path in sorted(producer_path.iterdir(), key=lambda item: item.name):
            if path.name.startswith(".") and path.name.endswith(".jsonl.lock") and path.is_file():
                continue
            if path.is_symlink() or not path.is_file() or path.suffix != ".jsonl":
                raise ProducerOperationalLogError("producer operational log has an invalid file entry")
            records.extend(
                _read_records_from_path(
                    path,
                    expected_account_id=account_id,
                    expected_producer=producer_path.name,
                    expected_producer_boot_id=path.stem,
                )
            )
    return tuple(sorted(records, key=_record_sort_key))


def merge_operator_history(
    *,
    legacy_events: Iterable[dict[str, object]],
    producer_records: Iterable[ProducerOperationalLogRecord],
) -> tuple[MergedOperationalHistoryEvent, ...]:
    """Merge legacy and producer-local evidence for display, never causality.

    Rows with a repeated producer idempotency key are displayed once using the
    earliest stable source row.  The duplicate raw rows stay in their own
    producer logs and therefore remain inspectable during an audit.
    """

    merged: list[MergedOperationalHistoryEvent] = []
    for index, event in enumerate(legacy_events, start=1):
        account_id = event.get("account_id")
        event_type = event.get("event_type")
        recorded_at_ms = _legacy_timestamp(event)
        if not isinstance(account_id, str) or not account_id or not isinstance(event_type, str) or not event_type:
            raise ProducerOperationalLogError("legacy account event has no displayable provenance")
        legacy_seq = event.get("seq")
        seq = legacy_seq if isinstance(legacy_seq, int) and legacy_seq >= 1 else index
        merged.append(
            MergedOperationalHistoryEvent(
                # Preserve the pre-split opaque event identifier.  Provenance
                # now says that this is historical legacy evidence rather
                # than a new globally ordered producer write.
                event_id=f"{account_id}:{seq}",
                source="legacy_account_events",
                account_id=account_id,
                event_type=event_type,
                display_clock_ms=recorded_at_ms,
                recorded_at_ms=recorded_at_ms,
                producer="legacy_account_events",
                producer_boot_id="historical",
                producer_seq=seq,
                payload=dict(event),
            )
        )
    seen_idempotency: set[tuple[str, str]] = set()
    for record in sorted(producer_records, key=_record_sort_key):
        dedupe_key = (record.producer, record.idempotency_key)
        if dedupe_key in seen_idempotency:
            continue
        seen_idempotency.add(dedupe_key)
        merged.append(
            MergedOperationalHistoryEvent(
                event_id=_producer_history_event_id(record),
                source="producer_operational_log",
                account_id=record.account_id,
                event_type=record.event_type,
                display_clock_ms=record.display_clock_ms,
                event_at_ms=record.event_at_ms,
                arrived_at_ms=record.arrived_at_ms,
                recorded_at_ms=record.recorded_at_ms,
                producer=record.producer,
                producer_boot_id=record.producer_boot_id,
                producer_seq=record.producer_seq,
                payload=record.payload,
            )
        )
    return order_operator_history(merged)


def order_operator_history(
    events: Iterable[MergedOperationalHistoryEvent],
) -> tuple[MergedOperationalHistoryEvent, ...]:
    """Order an already-projected history for display, never causality.

    Canonical Clerk rows are allowed to join this read projection, but remain
    distinguishable from producer operations.  The stable tie-breakers make a
    repeated read reproducible without inventing a cross-producer happens-before
    relationship.
    """

    return tuple(sorted(events, key=_merged_sort_key))


def _read_records_from_path(
    path: Path,
    *,
    expected_account_id: str,
    expected_producer: str,
    expected_producer_boot_id: str,
) -> list[ProducerOperationalLogRecord]:
    if not path.exists():
        return []
    try:
        raw_rows = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ProducerOperationalLogError(f"cannot read producer operational log {path.name}: {exc}") from exc
    records: list[ProducerOperationalLogRecord] = []
    expected_seq = 1
    for line_no, line in enumerate(raw_rows, start=1):
        if not line.strip():
            raise ProducerOperationalLogError(f"blank producer operational row {line_no} in {path.name}")
        try:
            raw = json.loads(line)
            record = ProducerOperationalLogRecord.model_validate(raw)
        except (json.JSONDecodeError, ValidationError) as exc:
            raise ProducerOperationalLogError(
                f"invalid producer operational row {line_no} in {path.name}: {exc}"
            ) from exc
        if record.account_id != expected_account_id:
            raise ProducerOperationalLogError(
                f"producer operational row {line_no} belongs to another account"
            )
        if record.producer != expected_producer:
            raise ProducerOperationalLogError(
                f"producer operational row {line_no} disagrees with its producer log path"
            )
        if record.producer_boot_id != expected_producer_boot_id:
            raise ProducerOperationalLogError(
                f"producer operational row {line_no} disagrees with its producer boot log path"
            )
        if record.producer_seq != expected_seq:
            raise ProducerOperationalLogError(
                f"producer operational row {line_no} has non-contiguous local sequence"
            )
        expected_seq += 1
        records.append(record)
    return records


def _assert_replay_matches(
    record: ProducerOperationalLogRecord,
    *,
    event_type: str,
    payload: dict[str, object],
    event_at_ms: int | None,
    arrived_at_ms: int | None,
) -> None:
    if (
        record.event_type != event_type
        or record.payload != payload
        or record.event_at_ms != event_at_ms
        or record.arrived_at_ms != arrived_at_ms
    ):
        raise ProducerOperationalLogError(
            "producer operational idempotency key is already bound to different evidence"
        )


def _record_sort_key(record: ProducerOperationalLogRecord) -> tuple[int, int, str, str, int]:
    return (
        record.display_clock_ms,
        record.recorded_at_ms,
        record.producer,
        record.producer_boot_id,
        record.producer_seq,
    )


def _merged_sort_key(event: MergedOperationalHistoryEvent) -> tuple[int, int, str, str, int, str]:
    return (
        event.display_clock_ms,
        event.recorded_at_ms,
        event.producer,
        event.producer_boot_id,
        event.producer_seq,
        event.event_id,
    )


def _legacy_timestamp(event: dict[str, object]) -> int:
    for field in ("ts_ms", "recorded_at_ms", "created_at_ms", "updated_at_ms"):
        value = event.get(field)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    raise ProducerOperationalLogError("legacy account event has no valid display timestamp")


def _producer_history_event_id(record: ProducerOperationalLogRecord) -> str:
    import hashlib

    digest = hashlib.sha256(record.idempotency_key.encode()).hexdigest()
    return f"producer:{record.account_id}:{record.producer}:{digest}"


def _assert_confined(path: Path, root: Path) -> None:
    root_real = os.path.realpath(os.fspath(root))
    path_real = os.path.realpath(os.fspath(path))
    prefix = root_real.rstrip(os.sep) + os.sep
    if not path_real.startswith(prefix):
        raise AccountArtifactError("producer operational log path escapes account artifacts")


__all__ = [
    "PRODUCER_OPERATIONAL_LOGS_DIRECTORY",
    "MergedOperationalHistoryEvent",
    "ProducerOperationalKind",
    "ProducerOperationalLogError",
    "ProducerOperationalLogRecord",
    "append_producer_operational_event",
    "append_producer_operational_event_if_absent",
    "merge_operator_history",
    "order_operator_history",
    "process_producer_boot_id",
    "producer_operational_log_path",
    "read_producer_operational_events",
]
