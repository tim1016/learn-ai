"""Durable, explicit activation fences for the isolated no-submit authorities.

One base, two namespaces: the ``sim:`` Dry Run world (``SyntheticActivation*``)
and the ``shadow:`` world (``shadow_activation.py``). Each namespace binds which
account ids it admits, its own error types, and its own append-only file under
``accounts/<namespace>/``; the sealing, verification and monotonic-generation
rules are the same code.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, ClassVar, Self

from app.broker.alpaca.clerk.account_authority import require_synthetic_account_id
from app.broker.alpaca.paths import resolve_contained_path
from app.utils.advisory_lock import advisory_file_lock

SYNTHETIC_ACTIVATION_FILENAME = "synthetic_activation.jsonl"


class IsolatedActivationInvalid(ValueError):
    """A durable isolated-authority activation fence cannot be trusted."""


class IsolatedActivationConflict(IsolatedActivationInvalid):
    """Another writer activated the same authority generation first."""


class SyntheticActivationInvalid(IsolatedActivationInvalid):
    """The durable synthetic activation fence cannot be trusted."""


class SyntheticActivationConflict(SyntheticActivationInvalid, IsolatedActivationConflict):
    """Another writer activated the same synthetic authority generation first."""


# ``require_*_account_id`` deliberately raises a sibling domain error; this
# alias keeps the narrow constructor-validation catch readable.
AccountError = ValueError


@dataclass(frozen=True)
class IsolatedActivationRecord:
    """One append-only activation proof for an isolated Clerk account.

    Subclasses bind the namespace: ``require_account_id`` admits its ids,
    ``invalid_error`` is the error type every rejection raises, ``label`` is
    the human-readable prefix of those rejections.
    """

    schema_version: int
    account_id: str
    authority_generation: int
    db_identity_token: str
    activated_at_ms: int
    activation_sha256: str

    require_account_id: ClassVar[Callable[[str], str]]
    invalid_error: ClassVar[type[IsolatedActivationInvalid]] = IsolatedActivationInvalid
    label: ClassVar[str] = "isolated activation"

    @classmethod
    def create(
        cls,
        *,
        account_id: str,
        authority_generation: int,
        db_identity_token: str,
        activated_at_ms: int,
    ) -> Self:
        payload = {
            "schema_version": 1,
            "account_id": cls.require_account_id(account_id),
            "authority_generation": authority_generation,
            "db_identity_token": db_identity_token,
            "activated_at_ms": activated_at_ms,
        }
        return cls(**payload, activation_sha256=canonical_sha256(payload))

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> Self:
        required = {
            "schema_version",
            "account_id",
            "authority_generation",
            "db_identity_token",
            "activated_at_ms",
            "activation_sha256",
        }
        if not isinstance(payload, Mapping) or set(payload) != required:
            raise cls.invalid_error(f"{cls.label} record has an invalid shape")
        int_fields = ("schema_version", "authority_generation", "activated_at_ms")
        if any(type(payload[field]) is not int for field in int_fields):
            raise cls.invalid_error(f"{cls.label} record has invalid integer facts")
        if not -(2**63) <= payload["activated_at_ms"] <= 2**63 - 1:
            raise cls.invalid_error(f"{cls.label} timestamp is outside signed int64 range")
        string_fields = ("account_id", "db_identity_token", "activation_sha256")
        if any(type(payload[field]) is not str for field in string_fields):
            raise cls.invalid_error(f"{cls.label} record has invalid string facts")
        try:
            record = cls(**payload)
            cls.require_account_id(record.account_id)
        except (TypeError, AccountError, ValueError) as exc:
            raise cls.invalid_error(f"{cls.label} record has invalid values") from exc
        if record.schema_version != 1 or record.authority_generation < 1:
            raise cls.invalid_error(f"{cls.label} record has an unsupported generation")
        if not record.db_identity_token:
            raise cls.invalid_error(f"{cls.label} record has invalid identity facts")
        if record.activation_sha256 != canonical_sha256(_unsigned_payload(record)):
            raise cls.invalid_error(f"{cls.label} record digest does not verify")
        return record


@dataclass(frozen=True)
class SyntheticActivationRecord(IsolatedActivationRecord):
    """One append-only activation proof for a synthetic Clerk account."""

    require_account_id = staticmethod(require_synthetic_account_id)
    invalid_error = SyntheticActivationInvalid
    label = "synthetic activation"


class IsolatedActivationStore:
    """Append-only account-scoped activation store with no Alpaca cutover proof."""

    record_type: ClassVar[type[IsolatedActivationRecord]] = IsolatedActivationRecord
    conflict_error: ClassVar[type[IsolatedActivationConflict]] = IsolatedActivationConflict

    def __init__(self, artifacts_root: Path, *, namespace_dir: str, filename: str) -> None:
        self._path = resolve_contained_path(artifacts_root, "accounts", namespace_dir, filename)

    @property
    def path(self) -> Path:
        return self._path

    @property
    def _label(self) -> str:
        return self.record_type.label

    def latest(self, account_id: str) -> IsolatedActivationRecord | None:
        self.record_type.require_account_id(account_id)
        latest: IsolatedActivationRecord | None = None
        for record in self._read_all():
            if record.account_id != account_id:
                continue
            if latest is not None and record.authority_generation <= latest.authority_generation:
                raise self.record_type.invalid_error(f"{self._label} generations are not increasing")
            latest = record
        return latest

    def append(self, record: IsolatedActivationRecord) -> None:
        canonical = self.record_type.from_payload(asdict(record))
        # The prior-generation check and durable append form one transaction.
        # A sibling advisory lock serializes independent activation processes;
        # fsync remains the exact durable boundary inside that transaction.
        with advisory_file_lock(self._path):
            prior = self.latest(canonical.account_id)
            if prior is not None and canonical.authority_generation <= prior.authority_generation:
                raise self.conflict_error(f"{self._label} generation must increase")
            if self._path.is_symlink() or (self._path.exists() and not self._path.is_file()):
                raise self.record_type.invalid_error(f"{self._label} ledger must be a regular file")
            self._path.parent.mkdir(parents=True, exist_ok=True)
            existed = self._path.exists()
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(asdict(canonical), sort_keys=True, separators=(",", ":")) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            if not existed:
                directory_fd = os.open(self._path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)

    def _read_all(self) -> list[IsolatedActivationRecord]:
        if self._path.is_symlink() or (self._path.exists() and not self._path.is_file()):
            raise self.record_type.invalid_error(f"{self._label} ledger must be a regular file")
        if not self._path.exists():
            return []
        records: list[IsolatedActivationRecord] = []
        try:
            for raw in self._path.read_text(encoding="utf-8").splitlines():
                if raw:
                    payload = json.loads(raw)
                    if not isinstance(payload, Mapping):
                        raise self.record_type.invalid_error(
                            f"{self._label} record has an invalid JSON payload shape"
                        )
                    records.append(self.record_type.from_payload(payload))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, IsolatedActivationInvalid) as exc:
            raise self.record_type.invalid_error(f"{self._label} ledger cannot be read") from exc
        return records


class SyntheticActivationStore(IsolatedActivationStore):
    """The ``sim:`` fence at ``accounts/synthetic/synthetic_activation.jsonl``."""

    record_type = SyntheticActivationRecord
    conflict_error = SyntheticActivationConflict

    def __init__(self, artifacts_root: Path) -> None:
        super().__init__(artifacts_root, namespace_dir="synthetic", filename=SYNTHETIC_ACTIVATION_FILENAME)

    def latest(self, account_id: str) -> SyntheticActivationRecord | None:  # narrow the return type
        record = super().latest(account_id)
        assert record is None or isinstance(record, SyntheticActivationRecord)
        return record


def _unsigned_payload(record: IsolatedActivationRecord) -> dict[str, Any]:
    return {
        "schema_version": record.schema_version,
        "account_id": record.account_id,
        "authority_generation": record.authority_generation,
        "db_identity_token": record.db_identity_token,
        "activated_at_ms": record.activated_at_ms,
    }


def canonical_sha256(payload: dict[str, Any]) -> str:
    """sha256 over the canonical JSON of ``payload`` -- the one sealing function
    every isolated-authority record uses."""
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).hexdigest()


__all__ = [
    "SYNTHETIC_ACTIVATION_FILENAME",
    "IsolatedActivationConflict",
    "IsolatedActivationInvalid",
    "IsolatedActivationRecord",
    "IsolatedActivationStore",
    "SyntheticActivationConflict",
    "SyntheticActivationInvalid",
    "SyntheticActivationRecord",
    "SyntheticActivationStore",
    "canonical_sha256",
]
