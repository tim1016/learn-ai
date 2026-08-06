"""Durable activation fence for the Alpaca SQLite Clerk authority.

Database existence is never activation.  This account-rooted, append-only
ledger lives above individual account directories so deleting or substituting
``clerk.db`` cannot erase the evidence that startup must validate before it
installs broker-mutation capability.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from app.broker.alpaca.paths import (
    fsync_directory,
    resolve_contained_path,
    safe_path_component,
)

ACTIVATION_FILENAME = "_authority_activations.jsonl"
_RECORD_FIELDS = frozenset(
    {
        "activation_id",
        "account_id",
        "authority_generation",
        "db_identity_token",
        "broker_proof_reference",
        "broker_proof_sha256",
        "legacy_quarantine_manifest",
        "legacy_quarantine_manifest_sha256",
        "activated_at_ms",
    }
)


class ActivationRecordInvalid(Exception):
    """Activation evidence is malformed, contradictory, or does not match SQLite."""


@dataclass(frozen=True)
class ActivationRecord:
    """One immutable account-authority activation generation."""

    activation_id: str
    account_id: str
    authority_generation: int
    db_identity_token: str
    broker_proof_reference: str
    broker_proof_sha256: str
    legacy_quarantine_manifest: str
    legacy_quarantine_manifest_sha256: str
    activated_at_ms: int

    @classmethod
    def create(
        cls,
        *,
        account_id: str,
        authority_generation: int,
        db_identity_token: str,
        broker_proof_reference: str,
        broker_proof_sha256: str,
        legacy_quarantine_manifest: str,
        legacy_quarantine_manifest_sha256: str,
        activated_at_ms: int,
    ) -> ActivationRecord:
        payload = {
            "account_id": account_id,
            "authority_generation": authority_generation,
            "db_identity_token": db_identity_token,
            "broker_proof_reference": broker_proof_reference,
            "broker_proof_sha256": broker_proof_sha256,
            "legacy_quarantine_manifest": legacy_quarantine_manifest,
            "legacy_quarantine_manifest_sha256": legacy_quarantine_manifest_sha256,
            "activated_at_ms": activated_at_ms,
        }
        activation_id = hashlib.sha256(_canonical_json(payload).encode()).hexdigest()
        return cls(activation_id=activation_id, **payload)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ActivationRecord:
        if frozenset(payload) != _RECORD_FIELDS:
            raise ActivationRecordInvalid("activation record fields do not match schema version 1")
        try:
            record = cls(**payload)
        except TypeError as exc:
            raise ActivationRecordInvalid("activation record has invalid field types") from exc
        try:
            record._validate_shape()
        except ValueError as exc:
            raise ActivationRecordInvalid("activation record has an unsafe account ID") from exc
        expected = cls.create(
            account_id=record.account_id,
            authority_generation=record.authority_generation,
            db_identity_token=record.db_identity_token,
            broker_proof_reference=record.broker_proof_reference,
            broker_proof_sha256=record.broker_proof_sha256,
            legacy_quarantine_manifest=record.legacy_quarantine_manifest,
            legacy_quarantine_manifest_sha256=record.legacy_quarantine_manifest_sha256,
            activated_at_ms=record.activated_at_ms,
        )
        if record.activation_id != expected.activation_id:
            raise ActivationRecordInvalid("activation record content hash does not verify")
        return record

    def _validate_shape(self) -> None:
        safe_path_component(self.account_id, "account_id")
        if type(self.authority_generation) is not int or self.authority_generation < 1:
            raise ActivationRecordInvalid("activation authority_generation must be positive")
        if (
            type(self.activated_at_ms) is not int
            or self.activated_at_ms < 0
            or self.activated_at_ms > 2**63 - 1
        ):
            raise ActivationRecordInvalid("activation timestamp must be int64 ms UTC")
        for name in (
            "db_identity_token",
            "broker_proof_reference",
            "legacy_quarantine_manifest",
        ):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ActivationRecordInvalid(f"activation {name} must be non-empty")
        for name in (
            "activation_id",
            "broker_proof_sha256",
            "legacy_quarantine_manifest_sha256",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or len(value) != 64:
                raise ActivationRecordInvalid(f"activation {name} must be a SHA-256 digest")
            try:
                bytes.fromhex(value)
            except ValueError as exc:
                raise ActivationRecordInvalid(
                    f"activation {name} must be lowercase hexadecimal"
                ) from exc
            if value != value.lower():
                raise ActivationRecordInvalid(
                    f"activation {name} must be lowercase hexadecimal"
                )


class ActivationStore:
    """Append and validate account activation records.

    ``latest`` is intentionally usable before a database is opened.  Startup
    then calls ``resolve`` with metadata observed from the verified database;
    a mismatch raises rather than silently falling back to legacy authority.
    """

    def __init__(self, alpaca_accounts_root: Path) -> None:
        self._path = alpaca_accounts_root / ACTIVATION_FILENAME

    @property
    def path(self) -> Path:
        return self._path

    def latest(self, account_id: str) -> ActivationRecord | None:
        try:
            safe_path_component(account_id, "account_id")
        except ValueError as exc:
            raise ActivationRecordInvalid("activation lookup has an unsafe account ID") from exc
        latest: ActivationRecord | None = None
        for record in self._read_all():
            if record.account_id != account_id:
                continue
            if latest is not None and record.authority_generation <= latest.authority_generation:
                raise ActivationRecordInvalid(
                    f"activation generations for {account_id!r} are not strictly increasing"
                )
            latest = record
        return latest

    def resolve(
        self,
        account_id: str,
        authority_generation: int,
        db_identity_token: str,
        artifacts_root: Path | None = None,
    ) -> ActivationRecord | None:
        """Return the active matching fence, ``None`` when never activated."""
        record = self.latest(account_id)
        if record is None:
            return None
        self.validate(
            record,
            observed_generation=authority_generation,
            observed_db_identity=db_identity_token,
        )
        if artifacts_root is not None:
            self.validate_artifacts(record, artifacts_root=artifacts_root)
        return record

    @staticmethod
    def validate(
        record: ActivationRecord,
        *,
        observed_generation: int,
        observed_db_identity: str,
    ) -> None:
        if record.authority_generation != observed_generation:
            raise ActivationRecordInvalid(
                "activation authority generation does not match the verified database"
            )
        if record.db_identity_token != observed_db_identity:
            raise ActivationRecordInvalid(
                "activation database identity does not match the verified database"
            )

    @staticmethod
    def validate_artifacts(record: ActivationRecord, *, artifacts_root: Path) -> None:
        """Verify both referenced cutover artifacts without trusting their paths."""
        broker_proof = _verified_referenced_json(
            artifacts_root,
            record.broker_proof_reference,
            record.broker_proof_sha256,
            label="broker proof",
        )
        quarantine = _verified_referenced_json(
            artifacts_root,
            record.legacy_quarantine_manifest,
            record.legacy_quarantine_manifest_sha256,
            label="legacy quarantine manifest",
        )
        for label, payload in (("broker proof", broker_proof), ("quarantine", quarantine)):
            if payload.get("account_id") != record.account_id:
                raise ActivationRecordInvalid(f"{label} account does not match activation")
            if payload.get("authority_generation") != record.authority_generation:
                raise ActivationRecordInvalid(
                    f"{label} authority generation does not match activation"
                )
            if payload.get("db_identity_token") != record.db_identity_token:
                raise ActivationRecordInvalid(
                    f"{label} database identity does not match activation"
                )

    def append(self, record: ActivationRecord) -> None:
        """Append one new generation and fsync it before returning."""
        canonical = ActivationRecord.from_payload(asdict(record))
        latest = self.latest(record.account_id)
        if latest is not None and record.authority_generation <= latest.authority_generation:
            raise ActivationRecordInvalid(
                "new activation generation must be greater than the existing activation"
            )
        if self._path.is_symlink() or (self._path.exists() and not self._path.is_file()):
            raise ActivationRecordInvalid("activation ledger must be a regular non-symbolic-link file")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        existed = self._path.is_file()
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(_canonical_json(asdict(canonical)) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        if not existed:
            fsync_directory(self._path.parent)

    def _read_all(self) -> list[ActivationRecord]:
        if self._path.is_symlink():
            raise ActivationRecordInvalid("activation ledger must not be a symbolic link")
        if self._path.exists() and not self._path.is_file():
            raise ActivationRecordInvalid("activation ledger must be a regular file")
        if not self._path.is_file():
            return []
        records: list[ActivationRecord] = []
        try:
            with self._path.open(encoding="utf-8") as handle:
                for line_number, raw in enumerate(handle, start=1):
                    stripped = raw.strip()
                    if not stripped:
                        continue
                    payload = json.loads(stripped)
                    if not isinstance(payload, dict):
                        raise ActivationRecordInvalid(
                            f"activation record line {line_number} is not an object"
                        )
                    records.append(ActivationRecord.from_payload(payload))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ActivationRecordInvalid(f"activation ledger cannot be verified: {exc}") from exc
        return records


def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _verified_referenced_json(
    artifacts_root: Path,
    reference: str,
    expected_sha256: str,
    *,
    label: str,
) -> dict[str, Any]:
    relative = Path(reference)
    if relative.is_absolute() or not relative.parts:
        raise ActivationRecordInvalid(f"activation {label} path is not relative")
    try:
        current = artifacts_root.resolve(strict=True)
    except OSError as exc:
        raise ActivationRecordInvalid(f"activation artifacts root cannot be resolved: {exc}") from exc
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ActivationRecordInvalid(f"activation {label} path contains a symbolic link")
    candidate = resolve_contained_path(artifacts_root, *relative.parts)
    try:
        encoded = candidate.read_bytes()
    except OSError as exc:
        raise ActivationRecordInvalid(f"activation {label} cannot be read: {exc}") from exc
    if hashlib.sha256(encoded).hexdigest() != expected_sha256:
        raise ActivationRecordInvalid(f"activation {label} SHA-256 does not verify")
    try:
        payload = json.loads(encoded)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ActivationRecordInvalid(f"activation {label} is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ActivationRecordInvalid(f"activation {label} must be a JSON object")
    return payload
