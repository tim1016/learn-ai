"""Durable initialization evidence for one inactive SQLite Clerk generation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from app.broker.alpaca.clerk.sqlite.database_verification import (
    DatabaseVerification,
    verify_database,
)
from app.broker.alpaca.clerk.sqlite.mirror import (
    MirrorChainBroken,
    MirrorFile,
    MirrorIdentity,
)
from app.broker.alpaca.clerk.sqlite.operational_files import (
    atomic_write_json,
    canonical_json_bytes,
)
from app.broker.alpaca.clerk.sqlite.recovery import RecoveryRefused
from app.broker.alpaca.clerk.sqlite.repository import DB_FILENAME, MIRROR_FILENAME
from app.broker.alpaca.paths import fsync_directory, resolve_contained_path


class CutoverRefused(RecoveryRefused):
    """The cutover plan is invalid, stale, or no longer matches reality."""


@dataclass(frozen=True)
class CutoverInitializationEvidence:
    """Content-addressed proof that this database came from reviewed prep."""

    initialization_intent_id: str
    receipt_sha256: str
    mirror_sha256: str


def initialization_intent_payload(
    *,
    account_id: str,
    authority_generation: int,
    broker_evidence: object,
    runner_roster: tuple[object, ...],
    legacy_artifacts: tuple[object, ...],
) -> tuple[dict[str, object], str]:
    """Build the immutable pre-initialization evidence for one generation."""
    evidence = {
        "schema_version": 1,
        "operation": "CUTOVER_INITIALIZATION_INTENT",
        "account_id": account_id,
        "authority_generation": authority_generation,
        "broker_evidence": asdict(broker_evidence),
        "runner_roster": [asdict(item) for item in runner_roster],
        "legacy_artifacts": [asdict(item) for item in legacy_artifacts],
    }
    intent_id = hashlib.sha256(canonical_json_bytes(evidence)).hexdigest()
    return {**evidence, "initialization_intent_id": intent_id}, intent_id


def require_or_publish_initialization_intent(
    *,
    account_dir: Path,
    payload: dict[str, object],
    authority_generation: int,
    allow_create: bool,
) -> None:
    """Create the pre-init marker once, or require an exact retry match."""
    evidence_dir = cutover_evidence_directory(account_dir, create=allow_create)
    intent_path = initialization_intent_path(
        evidence_dir,
        authority_generation=authority_generation,
    )
    encoded = canonical_json_bytes(payload)
    if intent_path.is_symlink() or (
        intent_path.exists() and not intent_path.is_file()
    ):
        raise CutoverRefused("cutover initialization intent has an unsafe type")
    if intent_path.is_file():
        if intent_path.read_bytes() != encoded:
            raise CutoverRefused(
                "cutover initialization retry evidence differs from its durable intent"
            )
        return
    if not allow_create:
        raise CutoverRefused(
            "established SQLite authority has no durable cutover initialization intent"
        )
    atomic_write_json(intent_path, payload)


def cutover_evidence_directory(account_dir: Path, *, create: bool) -> Path:
    """Return the confined evidence directory without following a link."""
    evidence_dir = account_dir / "cutover-evidence"
    if evidence_dir.is_symlink():
        raise CutoverRefused(
            "cutover-evidence directory must not be a symbolic link"
        )
    if evidence_dir.exists():
        if not evidence_dir.is_dir():
            raise CutoverRefused(
                "cutover-evidence path must be a regular directory"
            )
    elif create:
        evidence_dir.mkdir()
        fsync_directory(account_dir)
    else:
        raise CutoverRefused(
            "established SQLite authority has no durable cutover initialization "
            "intent; cutover-evidence directory is missing"
        )
    resolved = resolve_contained_path(account_dir, "cutover-evidence")
    if resolved != evidence_dir:
        raise CutoverRefused("cutover-evidence directory is not confined")
    return evidence_dir


def initialization_intent_path(
    evidence_dir: Path,
    *,
    authority_generation: int,
) -> Path:
    """Keep the generation-one name while isolating successor evidence."""
    if authority_generation == 1:
        return evidence_dir / "initialization-intent.json"
    return evidence_dir / f"initialization-intent-g{authority_generation}.json"


def verify_inactive_generation(
    *,
    account_id: str,
    account_dir: Path,
    authority_generation: int,
    db_identity_token: str,
) -> DatabaseVerification:
    """Verify an unused initialized generation before it may be activated."""
    require_checkpointed_database(account_dir)
    database = verify_database(
        account_dir / DB_FILENAME,
        expected_account_id=account_id,
        expected_generation=authority_generation,
        expected_db_identity=db_identity_token,
        immutable=True,
    )
    if database.control_revision != 0 or database.transition_count != 0:
        raise CutoverRefused(
            "cutover initialization can resume only an unused SQLite authority"
        )
    verify_empty_generation_mirror(
        account_id=account_id,
        account_dir=account_dir,
        authority_generation=authority_generation,
        db_identity_token=db_identity_token,
    )
    return database


def verify_empty_generation_mirror(
    *,
    account_id: str,
    account_dir: Path,
    authority_generation: int,
    db_identity_token: str,
) -> str:
    """Verify and hash the exact unused mirror bound to one generation."""
    generation_label = (
        "generation-one" if authority_generation == 1 else f"generation-{authority_generation}"
    )
    mirror_path = account_dir / MIRROR_FILENAME
    if mirror_path.is_symlink() or not mirror_path.is_file():
        raise CutoverRefused(
            f"cutover requires a regular non-symbolic-link {generation_label} mirror"
        )
    try:
        mirror_rows = MirrorFile(
            mirror_path,
            expected_identity=MirrorIdentity(
                account_id=account_id,
                authority_generation=authority_generation,
                db_identity_token=db_identity_token,
            ),
        ).rebuild()
        encoded = mirror_path.read_bytes()
    except (MirrorChainBroken, OSError, ValueError) as exc:
        raise CutoverRefused(f"{generation_label} mirror does not verify: {exc}") from exc
    if mirror_rows:
        raise CutoverRefused(f"cutover requires an empty {generation_label} mirror")
    return hashlib.sha256(encoded).hexdigest()


def require_completed_initialization(
    *,
    account_id: str,
    account_dir: Path,
    database: DatabaseVerification,
) -> CutoverInitializationEvidence:
    """Verify the pre-init intent and its database-bound completion receipt."""
    evidence_dir = cutover_evidence_directory(account_dir, create=False)
    intent = read_initialization_record(
        initialization_intent_path(
            evidence_dir,
            authority_generation=database.authority_generation,
        ),
        label="cutover initialization intent",
    )
    intent_id = intent.get("initialization_intent_id")
    intent_evidence = {
        key: value
        for key, value in intent.items()
        if key != "initialization_intent_id"
    }
    expected_intent_id = hashlib.sha256(
        canonical_json_bytes(intent_evidence)
    ).hexdigest()
    if (
        intent.get("schema_version") != 1
        or intent.get("operation") != "CUTOVER_INITIALIZATION_INTENT"
        or intent.get("account_id") != account_id
        or intent.get("authority_generation") != database.authority_generation
        or intent_id != expected_intent_id
    ):
        raise CutoverRefused("cutover initialization intent does not verify")

    receipt_path = (
        evidence_dir
        / f"initialization-g{database.authority_generation}-{database.db_identity_token}.json"
    )
    receipt = read_initialization_record(
        receipt_path,
        label="cutover initialization receipt",
    )
    if (
        receipt.get("schema_version") != 1
        or receipt.get("operation") != "CUTOVER_INITIALIZE"
        or receipt.get("account_id") != account_id
        or receipt.get("initialization_intent_id") != intent_id
        or receipt.get("database") != asdict(database)
        or receipt.get("broker_evidence") != intent.get("broker_evidence")
        or receipt.get("runner_roster") != intent.get("runner_roster")
        or receipt.get("legacy_artifacts") != intent.get("legacy_artifacts")
    ):
        raise CutoverRefused(
            "cutover initialization receipt does not match its intent and database"
        )
    mirror_sha256 = verify_empty_generation_mirror(
        account_id=account_id,
        account_dir=account_dir,
        authority_generation=database.authority_generation,
        db_identity_token=database.db_identity_token,
    )
    return CutoverInitializationEvidence(
        initialization_intent_id=expected_intent_id,
        receipt_sha256=hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
        mirror_sha256=mirror_sha256,
    )


def read_initialization_record(path: Path, *, label: str) -> dict[str, Any]:
    """Read one regular JSON evidence artifact into an object."""
    if path.is_symlink() or not path.is_file():
        raise CutoverRefused(f"{label} must be a regular non-symbolic-link file")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CutoverRefused(f"{label} is unreadable: {exc}") from exc
    if not isinstance(payload, dict):
        raise CutoverRefused(f"{label} must contain a JSON object")
    return payload


def require_checkpointed_database(account_dir: Path) -> None:
    """Reject SQLite state that could still contain uncheckpointed WAL writes."""
    db_path = account_dir / DB_FILENAME
    if db_path.is_symlink():
        raise CutoverRefused("SQLite database must not be a symbolic link")
    for suffix in ("-wal", "-shm"):
        sidecar = account_dir / f"{DB_FILENAME}{suffix}"
        if sidecar.is_symlink():
            raise CutoverRefused(f"SQLite sidecar {sidecar.name!r} is a symbolic link")
        if sidecar.exists():
            raise CutoverRefused(
                "cutover requires a cleanly stopped, checkpointed SQLite authority; "
                f"remove no files manually (found {sidecar.name})"
            )
