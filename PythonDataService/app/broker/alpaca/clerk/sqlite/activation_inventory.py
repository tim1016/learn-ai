"""Read-only activation qualification for an explicit in-use account inventory.

This is ADR 0037's migration gate.  The inventory source remains an operator
input: this module cannot discover which broker accounts are in use.  Given
that closed set, however, it emits a receipt only when every named account has
one valid activation fence bound to its verified SQLite authority and cutover
artifacts.  Any absent, stale, malformed, or conflicting input refuses the
whole qualification.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from app.broker.alpaca.clerk.sqlite import writes
from app.broker.alpaca.clerk.sqlite.activation import (
    ActivationRecordInvalid,
    ActivationStore,
)
from app.broker.alpaca.clerk.sqlite.database_verification import (
    DatabaseVerificationFailed,
    verify_database,
)
from app.broker.alpaca.clerk.sqlite.repository import DB_FILENAME
from app.broker.alpaca.paths import safe_path_component

_INVENTORY_FIELDS = frozenset(
    {"schema_version", "source", "captured_at_ms", "account_ids"}
)


class ActivationInventoryQualificationFailed(Exception):
    """The inventory scope or at least one named activation cannot be proven."""


@dataclass(frozen=True)
class ActivationInventory:
    """A dated, explicit export of all Alpaca accounts declared in use."""

    schema_version: Literal[1]
    source: str
    captured_at_ms: int
    account_ids: tuple[str, ...]


@dataclass(frozen=True)
class QualifiedActivation:
    """Activation and verified database head for one inventory account."""

    account_id: str
    activation_id: str
    authority_generation: int
    db_identity_token: str
    schema_version: int
    control_revision: int
    transition_count: int
    last_sequence: int
    last_row_hash: str


@dataclass(frozen=True)
class ActivationInventoryQualificationReceipt:
    """Content-addressed evidence that the complete supplied scope qualified."""

    schema_version: Literal[1]
    status: Literal["qualified"]
    qualification_id: str
    inventory_sha256: str
    source: str
    captured_at_ms: int
    qualified_at_ms: int
    accounts: tuple[QualifiedActivation, ...]


def read_activation_inventory(path: Path) -> ActivationInventory:
    """Read one strict, regular-file inventory without following symlinks."""
    if path.is_symlink():
        raise ActivationInventoryQualificationFailed(
            "activation inventory must not be a symbolic link"
        )
    if not path.is_file():
        raise ActivationInventoryQualificationFailed(
            "activation inventory must be a regular file"
        )
    try:
        payload = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ActivationInventoryQualificationFailed(
            f"activation inventory cannot be verified: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise ActivationInventoryQualificationFailed(
            "activation inventory must be a JSON object"
        )
    if frozenset(payload) != _INVENTORY_FIELDS:
        raise ActivationInventoryQualificationFailed(
            "activation inventory fields do not match schema version 1"
        )
    return _inventory_from_payload(payload)


def qualify_activation_inventory(
    inventory: ActivationInventory,
    *,
    artifacts_root: Path,
    qualified_at_ms: int,
    max_inventory_age_ms: int,
) -> ActivationInventoryQualificationReceipt:
    """Qualify every account or refuse without producing a partial receipt."""
    _validate_int64_ms(qualified_at_ms, label="qualification timestamp")
    if type(max_inventory_age_ms) is not int or max_inventory_age_ms < 0:
        raise ActivationInventoryQualificationFailed(
            "max inventory age must be a non-negative integer"
        )
    age_ms = qualified_at_ms - inventory.captured_at_ms
    if age_ms < 0:
        raise ActivationInventoryQualificationFailed(
            "activation inventory timestamp is in the future"
        )
    if age_ms > max_inventory_age_ms:
        raise ActivationInventoryQualificationFailed(
            "activation inventory is stale and must be exported again"
        )

    try:
        accounts_root, _ = writes.account_paths(
            artifacts_root, inventory.account_ids[0]
        )
    except (OSError, ValueError) as exc:
        raise ActivationInventoryQualificationFailed(
            f"activation artifacts root cannot be resolved: {exc}"
        ) from exc
    store = ActivationStore(accounts_root)
    qualified_accounts = tuple(
        _qualify_account(
            account_id,
            artifacts_root=artifacts_root,
            store=store,
        )
        for account_id in sorted(inventory.account_ids)
    )
    inventory_payload = {
        "schema_version": inventory.schema_version,
        "source": inventory.source,
        "captured_at_ms": inventory.captured_at_ms,
        "account_ids": list(inventory.account_ids),
    }
    inventory_sha256 = _sha256_json(inventory_payload)
    receipt_payload = {
        "schema_version": 1,
        "status": "qualified",
        "inventory_sha256": inventory_sha256,
        "source": inventory.source,
        "captured_at_ms": inventory.captured_at_ms,
        "qualified_at_ms": qualified_at_ms,
        "accounts": [asdict(account) for account in qualified_accounts],
    }
    return ActivationInventoryQualificationReceipt(
        schema_version=1,
        status="qualified",
        qualification_id=_sha256_json(receipt_payload),
        inventory_sha256=inventory_sha256,
        source=inventory.source,
        captured_at_ms=inventory.captured_at_ms,
        qualified_at_ms=qualified_at_ms,
        accounts=qualified_accounts,
    )


def _inventory_from_payload(payload: dict[str, Any]) -> ActivationInventory:
    if payload["schema_version"] != 1 or type(payload["schema_version"]) is not int:
        raise ActivationInventoryQualificationFailed(
            "activation inventory schema_version must be 1"
        )
    source = payload["source"]
    if not isinstance(source, str) or not source.strip():
        raise ActivationInventoryQualificationFailed(
            "activation inventory source must be a non-blank string"
        )
    captured_at_ms = payload["captured_at_ms"]
    _validate_int64_ms(captured_at_ms, label="inventory capture timestamp")
    account_ids = payload["account_ids"]
    if not isinstance(account_ids, list) or not account_ids:
        raise ActivationInventoryQualificationFailed(
            "activation inventory must name at least one in-use account"
        )
    if any(not isinstance(account_id, str) for account_id in account_ids):
        raise ActivationInventoryQualificationFailed(
            "activation inventory account IDs must be strings"
        )
    if len(set(account_ids)) != len(account_ids):
        raise ActivationInventoryQualificationFailed(
            "activation inventory contains a duplicate account ID"
        )
    for account_id in account_ids:
        try:
            safe_path_component(account_id, "account_id")
        except ValueError as exc:
            raise ActivationInventoryQualificationFailed(
                f"activation inventory has an unsafe account ID: {account_id!r}"
            ) from exc
    return ActivationInventory(
        schema_version=1,
        source=source.strip(),
        captured_at_ms=captured_at_ms,
        account_ids=tuple(account_ids),
    )


def _qualify_account(
    account_id: str,
    *,
    artifacts_root: Path,
    store: ActivationStore,
) -> QualifiedActivation:
    try:
        activation = store.latest(account_id)
        if activation is None:
            raise ActivationInventoryQualificationFailed(
                f"account {account_id!r} has no activation fence"
            )
        _, account_dir = writes.account_paths(artifacts_root, account_id)
        lexical_db_path = account_dir / DB_FILENAME
        if lexical_db_path.is_symlink():
            raise ActivationInventoryQualificationFailed(
                f"account {account_id!r} SQLite authority must not be a symbolic link"
            )
        db_path = writes.confined_account_file(
            artifacts_root, account_id, DB_FILENAME
        )
        database = verify_database(
            db_path,
            expected_account_id=account_id,
            expected_generation=activation.authority_generation,
            expected_db_identity=activation.db_identity_token,
        )
        resolved = store.resolve(
            account_id,
            database.authority_generation,
            database.db_identity_token,
            artifacts_root=artifacts_root,
        )
        if resolved is None:  # pragma: no cover - latest() already proved it exists
            raise ActivationInventoryQualificationFailed(
                f"account {account_id!r} activation fence disappeared during qualification"
            )
    except ActivationInventoryQualificationFailed:
        raise
    except (ActivationRecordInvalid, DatabaseVerificationFailed, OSError, ValueError) as exc:
        raise ActivationInventoryQualificationFailed(
            f"account {account_id!r} activation cannot be qualified: {exc}"
        ) from exc
    return QualifiedActivation(
        account_id=account_id,
        activation_id=resolved.activation_id,
        authority_generation=database.authority_generation,
        db_identity_token=database.db_identity_token,
        schema_version=database.schema_version,
        control_revision=database.control_revision,
        transition_count=database.transition_count,
        last_sequence=database.last_sequence,
        last_row_hash=database.last_row_hash,
    )


def _validate_int64_ms(value: object, *, label: str) -> None:
    if type(value) is not int or value < 0 or value > 2**63 - 1:
        raise ActivationInventoryQualificationFailed(
            f"{label} must be int64 ms UTC"
        )


def _sha256_json(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()
