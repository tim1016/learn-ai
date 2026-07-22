"""Durable host-daemon command idempotency (Clerk S3 / #1156).

The host daemon is the last process boundary before a bot process is started,
stopped, or an emergency broker command is launched.  A response lost after
that boundary must therefore never cause the daemon to repeat the command.

One opaque idempotency key owns one canonical command fingerprint and one
durable outcome.  A matching duplicate can replay that outcome; a mismatched
reuse is an operator-visible conflict.  The rollout is deliberately
account-scoped: accounts not named by
``LIVE_RUNNER_DAEMON_COMMAND_IDEMPOTENCY_ENFORCED_ACCOUNTS`` record and log
duplicates in shadow mode, while named accounts replay them.  Removing an
account from that comma-separated setting reverses enforcement without losing
the audit records.

Records live under ``<artifacts>/daemon_command_idempotency``.  A pending
record from a daemon crash is never re-executed in enforced mode: its result is
``IDEMPOTENCY_OUTCOME_UNKNOWN`` and requires operator reconciliation.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.engine.live import durable_append_log
from app.schemas.artifact_io import atomic_write_pydantic_artifact

logger = logging.getLogger(__name__)

_ENFORCED_ACCOUNTS_ENV = "LIVE_RUNNER_DAEMON_COMMAND_IDEMPOTENCY_ENFORCED_ACCOUNTS"
_ROOT_NAME = "daemon_command_idempotency"

CommandRecordState = Literal["PENDING", "COMPLETED"]


class DaemonCommandRecord(BaseModel):
    """One immutable key/fingerprint binding plus its eventual outcome."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    idempotency_key: str = Field(min_length=1, max_length=256)
    command: str = Field(min_length=1, max_length=128)
    request_sha256: str = Field(min_length=64, max_length=64)
    account_id: str | None = Field(default=None, max_length=32)
    enforcement_enabled: bool
    created_at_ms: int = Field(ge=0)
    completed_at_ms: int | None = Field(default=None, ge=0)
    state: CommandRecordState
    response_status_code: int | None = Field(default=None, ge=100, le=599)
    response_body: dict[str, object] | None = None


@dataclass(frozen=True)
class DaemonCommandOutcome:
    """The transport-neutral outcome returned to the FastAPI boundary."""

    status_code: int
    body: dict[str, object]
    replayed: bool


@dataclass(frozen=True)
class _PreparedCommand:
    record: DaemonCommandRecord
    created: bool


class DaemonCommandRecordCorruptError(RuntimeError):
    """A known key has unreadable durable evidence; execution must stop."""


class DaemonCommandIdempotencyRepo:
    """Filesystem-backed single-key command ledger.

    ``prepare`` uses exclusive file creation rather than a read-then-write
    sequence, so a second daemon process cannot claim the same key.  Later
    writes use the repository's standard atomic-replace helper.
    """

    def __init__(self, artifacts_root: Path) -> None:
        self._root = artifacts_root / _ROOT_NAME

    def _path_for_key(self, idempotency_key: str) -> Path:
        digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        return self._root / f"{digest}.json"

    def prepare(
        self,
        *,
        idempotency_key: str,
        command: str,
        request_sha256: str,
        account_id: str | None,
        enforcement_enabled: bool,
        now_ms: int,
    ) -> _PreparedCommand:
        """Durably claim ``idempotency_key`` or return its prior record."""

        record = DaemonCommandRecord(
            idempotency_key=idempotency_key,
            command=command,
            request_sha256=request_sha256,
            account_id=account_id,
            enforcement_enabled=enforcement_enabled,
            created_at_ms=now_ms,
            state="PENDING",
        )
        path = self._path_for_key(idempotency_key)
        self._root.mkdir(parents=True, exist_ok=True)
        try:
            durable_append_log.create_exclusive_durable_file(path, record.model_dump_json())
        except FileExistsError:
            return _PreparedCommand(record=self._read_existing(path), created=False)
        return _PreparedCommand(record=record, created=True)

    def complete(
        self,
        record: DaemonCommandRecord,
        *,
        status_code: int,
        body: dict[str, object],
        completed_at_ms: int,
    ) -> DaemonCommandRecord:
        """Persist the sole response for a claimed command key."""

        completed = record.model_copy(
            update={
                "state": "COMPLETED",
                "completed_at_ms": completed_at_ms,
                "response_status_code": status_code,
                "response_body": body,
            }
        )
        atomic_write_pydantic_artifact(self._path_for_key(record.idempotency_key), completed)
        return completed

    @staticmethod
    def _read_existing(path: Path) -> DaemonCommandRecord:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return DaemonCommandRecord.model_validate(payload)
        except (OSError, ValueError, ValidationError) as exc:
            raise DaemonCommandRecordCorruptError(
                "A durable daemon command record is unreadable; reconcile before retrying."
            ) from exc


def daemon_command_enforcement_enabled(account_id: str | None) -> bool:
    """Return the reversible, account-scoped rollout decision.

    The empty default is intentional: it records and logs shadow evidence
    first.  Account IDs remain exact opaque broker identities; no wildcard or
    case-folding is accepted because a broad toggle would defeat the staged
    rollout boundary.
    """

    if account_id is None:
        return False
    enabled_accounts = {
        value.strip()
        for value in os.environ.get(_ENFORCED_ACCOUNTS_ENV, "").split(",")
        if value.strip()
    }
    return account_id in enabled_accounts


def validate_idempotency_key(raw: str | None) -> str:
    """Validate an opaque daemon-boundary key without normalizing it."""

    if raw is None or not raw or len(raw) > 256 or raw != raw.strip() or not raw.isprintable():
        raise ValueError("idempotency_key must be a non-empty printable value of at most 256 characters")
    return raw


def canonical_request_sha256(
    command: str,
    account_id: str | None,
    semantic_payload: dict[str, object],
) -> str:
    """Hash the command, account scope, and semantic request fields."""

    encoded = json.dumps(
        {"account_id": account_id, "command": command, "payload": semantic_payload},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class DaemonCommandIdempotencyService:
    """Serialize one daemon command key and replay its persisted outcome."""

    def __init__(
        self,
        artifacts_root: Path,
        *,
        now_ms: Callable[[], int] | None = None,
    ) -> None:
        self._repo = DaemonCommandIdempotencyRepo(artifacts_root)
        self._now_ms = now_ms or _now_ms
        self._locks_guard = threading.Lock()
        self._locks: dict[str, threading.RLock] = {}

    def execute(
        self,
        *,
        idempotency_key: str,
        command: str,
        account_id: str | None,
        semantic_payload: dict[str, object],
        invoke: Callable[[], DaemonCommandOutcome],
        enforcement_enabled: bool | None = None,
    ) -> DaemonCommandOutcome:
        """Run once, replay once, or fail closed without repeating effects.

        Callers normally inherit the account-scoped rollout flag.  A local
        control-plane command may explicitly require enforcement when it is
        itself the durable recovery boundary; that must not be coupled to the
        host daemon's staged rollout configuration.
        """

        key = validate_idempotency_key(idempotency_key)
        request_sha256 = canonical_request_sha256(command, account_id, semantic_payload)
        effective_enforcement = (
            daemon_command_enforcement_enabled(account_id)
            if enforcement_enabled is None
            else enforcement_enabled
        )
        with self._lock_for(key):
            try:
                prepared = self._repo.prepare(
                    idempotency_key=key,
                    command=command,
                    request_sha256=request_sha256,
                    account_id=account_id,
                    enforcement_enabled=effective_enforcement,
                    now_ms=self._now_ms(),
                )
            except (OSError, DaemonCommandRecordCorruptError) as exc:
                logger.exception("daemon command idempotency record is unavailable", extra={"command": command})
                return _outcome_unknown(key, str(exc))

            if not prepared.created:
                conflict = _key_reuse_conflict(prepared.record, command, account_id, request_sha256)
                if conflict is not None:
                    return conflict
                if effective_enforcement:
                    return _enforced_duplicate_outcome(prepared.record, key)
                logger.warning(
                    "daemon command idempotency duplicate observed in shadow mode",
                    extra={
                        "idempotency_key": key,
                        "command": command,
                        "account_id": account_id,
                        "first_command_state": prepared.record.state,
                    },
                )
                # Shadow mode deliberately preserves the first durable outcome
                # but still runs the action so a named account can be enabled
                # only after its duplicate evidence has been observed.
                return invoke()

            try:
                outcome = invoke()
            except BaseException:
                logger.exception(
                    "daemon command exited before its durable outcome was recorded",
                    extra={"idempotency_key": key, "command": command, "account_id": account_id},
                )
                raise
            try:
                self._repo.complete(
                    prepared.record,
                    status_code=outcome.status_code,
                    body=outcome.body,
                    completed_at_ms=self._now_ms(),
                )
            except OSError as exc:
                logger.exception(
                    "daemon command outcome could not be persisted",
                    extra={"idempotency_key": key, "command": command, "account_id": account_id},
                )
                return _outcome_unknown(key, str(exc))
            return outcome

    def _lock_for(self, idempotency_key: str) -> threading.RLock:
        with self._locks_guard:
            lock = self._locks.get(idempotency_key)
            if lock is None:
                lock = threading.RLock()
                self._locks[idempotency_key] = lock
            return lock


def _key_reuse_conflict(
    record: DaemonCommandRecord,
    command: str,
    account_id: str | None,
    request_sha256: str,
) -> DaemonCommandOutcome | None:
    if (
        record.command == command
        and record.account_id == account_id
        and record.request_sha256 == request_sha256
    ):
        return None
    return DaemonCommandOutcome(
        status_code=409,
        body={
            "reason_code": "IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_COMMAND",
            "message": "This idempotency key is already bound to a different daemon command payload.",
            "idempotency_key": record.idempotency_key,
            "recorded_command": record.command,
        },
        replayed=False,
    )


def _enforced_duplicate_outcome(record: DaemonCommandRecord, key: str) -> DaemonCommandOutcome:
    if record.state == "PENDING" or record.response_status_code is None or record.response_body is None:
        return _outcome_unknown(
            key,
            "The daemon has a prior command record without a durable outcome; reconcile before retrying.",
        )
    return DaemonCommandOutcome(
        status_code=record.response_status_code,
        body=dict(record.response_body),
        replayed=True,
    )


def _outcome_unknown(idempotency_key: str, message: str) -> DaemonCommandOutcome:
    return DaemonCommandOutcome(
        status_code=409,
        body={
            "reason_code": "IDEMPOTENCY_OUTCOME_UNKNOWN",
            "message": message,
            "idempotency_key": idempotency_key,
        },
        replayed=False,
    )


def _now_ms() -> int:
    return int(time.time() * 1000)
