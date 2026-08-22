"""Durable, explicit activation fence for isolated synthetic authorities."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from app.broker.alpaca.clerk.account_authority import require_synthetic_account_id
from app.broker.alpaca.paths import resolve_contained_path
from app.utils.advisory_lock import advisory_file_lock

SYNTHETIC_ACTIVATION_FILENAME = "synthetic_activation.jsonl"


class SyntheticActivationInvalid(ValueError):
    """The durable synthetic activation fence cannot be trusted."""


class SyntheticActivationConflict(SyntheticActivationInvalid):
    """Another writer activated the same synthetic authority generation first."""


@dataclass(frozen=True)
class SyntheticActivationRecord:
    """One append-only activation proof for a synthetic Clerk account."""

    schema_version: int
    account_id: str
    authority_generation: int
    db_identity_token: str
    activated_at_ms: int
    activation_sha256: str

    @classmethod
    def create(
        cls,
        *,
        account_id: str,
        authority_generation: int,
        db_identity_token: str,
        activated_at_ms: int,
    ) -> SyntheticActivationRecord:
        payload = {
            "schema_version": 1,
            "account_id": require_synthetic_account_id(account_id),
            "authority_generation": authority_generation,
            "db_identity_token": db_identity_token,
            "activated_at_ms": activated_at_ms,
        }
        return cls(
            **payload,
            activation_sha256=_digest(payload),
        )

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> SyntheticActivationRecord:
        required = {
            "schema_version",
            "account_id",
            "authority_generation",
            "db_identity_token",
            "activated_at_ms",
            "activation_sha256",
        }
        if not isinstance(payload, Mapping) or set(payload) != required:
            raise SyntheticActivationInvalid("synthetic activation record has an invalid shape")
        int_fields = ("schema_version", "authority_generation", "activated_at_ms")
        if any(type(payload[field]) is not int for field in int_fields):
            raise SyntheticActivationInvalid("synthetic activation record has invalid integer facts")
        if not -(2**63) <= payload["activated_at_ms"] <= 2**63 - 1:
            raise SyntheticActivationInvalid("synthetic activation timestamp is outside signed int64 range")
        string_fields = ("account_id", "db_identity_token", "activation_sha256")
        if any(type(payload[field]) is not str for field in string_fields):
            raise SyntheticActivationInvalid("synthetic activation record has invalid string facts")
        try:
            record = cls(**payload)
            require_synthetic_account_id(record.account_id)
        except (TypeError, AccountError, ValueError) as exc:
            raise SyntheticActivationInvalid("synthetic activation record has invalid values") from exc
        if record.schema_version != 1 or record.authority_generation < 1:
            raise SyntheticActivationInvalid("synthetic activation record has an unsupported generation")
        if not record.db_identity_token:
            raise SyntheticActivationInvalid("synthetic activation record has invalid identity facts")
        if record.activation_sha256 != _digest(_unsigned_payload(record)):
            raise SyntheticActivationInvalid("synthetic activation record digest does not verify")
        return record


# ``require_synthetic_account_id`` deliberately raises a sibling domain error;
# this alias keeps the narrow constructor-validation catch readable.
AccountError = ValueError


class SyntheticActivationStore:
    """Append-only account-scoped activation store with no Alpaca cutover proof."""

    def __init__(self, artifacts_root: Path) -> None:
        self._path = resolve_contained_path(
            artifacts_root, "accounts", "synthetic", SYNTHETIC_ACTIVATION_FILENAME
        )

    @property
    def path(self) -> Path:
        return self._path

    def latest(self, account_id: str) -> SyntheticActivationRecord | None:
        require_synthetic_account_id(account_id)
        latest: SyntheticActivationRecord | None = None
        for record in self._read_all():
            if record.account_id != account_id:
                continue
            if latest is not None and record.authority_generation <= latest.authority_generation:
                raise SyntheticActivationInvalid("synthetic activation generations are not increasing")
            latest = record
        return latest

    def append(self, record: SyntheticActivationRecord) -> None:
        canonical = SyntheticActivationRecord.from_payload(asdict(record))
        # The prior-generation check and durable append form one transaction.
        # A sibling advisory lock serializes independent activation processes;
        # fsync remains the exact durable boundary inside that transaction.
        with advisory_file_lock(self._path):
            prior = self.latest(canonical.account_id)
            if prior is not None and canonical.authority_generation <= prior.authority_generation:
                raise SyntheticActivationConflict("synthetic activation generation must increase")
            if self._path.is_symlink() or (self._path.exists() and not self._path.is_file()):
                raise SyntheticActivationInvalid("synthetic activation ledger must be a regular file")
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

    def _read_all(self) -> list[SyntheticActivationRecord]:
        if self._path.is_symlink() or (self._path.exists() and not self._path.is_file()):
            raise SyntheticActivationInvalid("synthetic activation ledger must be a regular file")
        if not self._path.exists():
            return []
        records: list[SyntheticActivationRecord] = []
        try:
            for raw in self._path.read_text(encoding="utf-8").splitlines():
                if raw:
                    payload = json.loads(raw)
                    if not isinstance(payload, Mapping):
                        raise SyntheticActivationInvalid(
                            "synthetic activation record has an invalid JSON payload shape"
                        )
                    records.append(SyntheticActivationRecord.from_payload(payload))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, SyntheticActivationInvalid) as exc:
            raise SyntheticActivationInvalid("synthetic activation ledger cannot be read") from exc
        return records


def _unsigned_payload(record: SyntheticActivationRecord) -> dict[str, Any]:
    return {
        "schema_version": record.schema_version,
        "account_id": record.account_id,
        "authority_generation": record.authority_generation,
        "db_identity_token": record.db_identity_token,
        "activated_at_ms": record.activated_at_ms,
    }


def _digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).hexdigest()


__all__ = [
    "SYNTHETIC_ACTIVATION_FILENAME",
    "SyntheticActivationConflict",
    "SyntheticActivationInvalid",
    "SyntheticActivationRecord",
    "SyntheticActivationStore",
]
