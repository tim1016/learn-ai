"""Human-gated activation planning and application for the SQLite Clerk.

``plan_cutover`` is strictly read-only.  ``apply_cutover`` accepts no force
mode and re-observes every safety input before quarantining legacy artifacts
and appending the activation fence.
"""

from __future__ import annotations

import hashlib
import math
import os
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from app.broker.alpaca.clerk.journal import INBOX_FILENAME, JOURNAL_FILENAME
from app.broker.alpaca.clerk.sqlite import writes
from app.broker.alpaca.clerk.sqlite.activation import ActivationRecord, ActivationStore
from app.broker.alpaca.clerk.sqlite.database_verification import (
    DatabaseVerification,
    verify_database,
)
from app.broker.alpaca.clerk.sqlite.operational_files import (
    atomic_write_json,
    canonical_json_bytes,
    relative_reference,
    tree_sha256,
)
from app.broker.alpaca.clerk.sqlite.recovery import RecoveryRefused
from app.broker.alpaca.clerk.sqlite.registry import EstablishedAccountsRegistry
from app.broker.alpaca.clerk.sqlite.repository import DB_FILENAME
from app.broker.alpaca.paths import fsync_directory, fsync_directory_chain
from app.utils.timestamps import Clock, now_ms_utc

DEFAULT_CONFIRMATION_TTL_MS = 120_000
MAX_CONFIRMATION_TTL_MS = 300_000
LEGACY_ARTIFACT_NAMES: tuple[str, ...] = (
    INBOX_FILENAME,
    JOURNAL_FILENAME,
    "custody_resolution_receipts.json",
    "bots",
)


class CutoverRefused(RecoveryRefused):
    """The cutover plan is invalid, stale, or no longer matches reality."""


@dataclass(frozen=True)
class BrokerCutoverEvidence:
    account_id: str
    observed_at_ms: int
    proof_reference: str
    positions: Mapping[str, float]
    open_order_ids: tuple[str, ...]


@dataclass(frozen=True)
class LegacyArtifactEvidence:
    relative_path: str
    kind: str
    sha256: str


@dataclass(frozen=True)
class CutoverPlan:
    schema_version: int
    plan_id: str
    confirmation_token: str
    account_id: str
    created_at_ms: int
    expires_at_ms: int
    database: DatabaseVerification
    broker_evidence: BrokerCutoverEvidence
    expected_strategy_instance_ids: tuple[str, ...]
    stopped_strategy_instance_ids: tuple[str, ...]
    legacy_artifacts: tuple[LegacyArtifactEvidence, ...]


@dataclass(frozen=True)
class CutoverReceipt:
    activation: ActivationRecord
    plan_id: str
    quarantined_artifacts: tuple[LegacyArtifactEvidence, ...]
    receipt_reference: str


def plan_cutover(
    *,
    account_id: str,
    artifacts_root: Path,
    broker_evidence: BrokerCutoverEvidence,
    expected_strategy_instance_ids: Sequence[str],
    stopped_strategy_instance_ids: Sequence[str],
    max_broker_evidence_age_ms: int,
    confirmation_ttl_ms: int = DEFAULT_CONFIRMATION_TTL_MS,
    clock: Clock = now_ms_utc,
) -> CutoverPlan:
    """Read and content-address every prerequisite without writing anything."""
    now = clock()
    if type(confirmation_ttl_ms) is not int or not 1 <= confirmation_ttl_ms <= MAX_CONFIRMATION_TTL_MS:
        raise CutoverRefused("confirmation TTL must be within 1..300000 ms")
    accounts_root, account_dir = writes.account_paths(artifacts_root, account_id)
    if ActivationStore(accounts_root).latest(account_id) is not None:
        raise CutoverRefused("account already has an activation fence")
    established = EstablishedAccountsRegistry(accounts_root).latest(account_id)
    if established is None:
        raise CutoverRefused("account has no established SQLite authority")
    _require_checkpointed_database(account_dir)
    database = verify_database(
        account_dir / DB_FILENAME,
        expected_account_id=account_id,
        expected_generation=established.authority_generation,
        expected_db_identity=established.db_identity_token,
        immutable=True,
    )
    normalized_broker = _normalize_broker_evidence(broker_evidence)
    _validate_cutover_safety(
        account_id=account_id,
        broker_evidence=normalized_broker,
        expected_strategy_instance_ids=expected_strategy_instance_ids,
        stopped_strategy_instance_ids=stopped_strategy_instance_ids,
        now_ms=now,
        max_broker_evidence_age_ms=max_broker_evidence_age_ms,
    )
    legacy = _legacy_artifact_evidence(account_dir)
    payload = {
        "schema_version": 1,
        "account_id": account_id,
        "created_at_ms": now,
        "expires_at_ms": now + confirmation_ttl_ms,
        "database": asdict(database),
        "broker_evidence": asdict(normalized_broker),
        "expected_strategy_instance_ids": sorted(expected_strategy_instance_ids),
        "stopped_strategy_instance_ids": sorted(stopped_strategy_instance_ids),
        "legacy_artifacts": [asdict(item) for item in legacy],
    }
    token = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    return CutoverPlan(
        schema_version=1,
        plan_id=token,
        confirmation_token=token,
        account_id=account_id,
        created_at_ms=now,
        expires_at_ms=now + confirmation_ttl_ms,
        database=database,
        broker_evidence=normalized_broker,
        expected_strategy_instance_ids=tuple(sorted(expected_strategy_instance_ids)),
        stopped_strategy_instance_ids=tuple(sorted(stopped_strategy_instance_ids)),
        legacy_artifacts=legacy,
    )


def apply_cutover(
    *,
    plan: CutoverPlan,
    confirmation_token: str,
    artifacts_root: Path,
    broker_evidence: BrokerCutoverEvidence,
    expected_strategy_instance_ids: Sequence[str],
    stopped_strategy_instance_ids: Sequence[str],
    max_broker_evidence_age_ms: int,
    clock: Clock = now_ms_utc,
) -> CutoverReceipt:
    """Recheck the plan, quarantine legacy state, then fsync activation."""
    now = clock()
    _validate_plan_token(plan, confirmation_token)
    if now > plan.expires_at_ms:
        raise CutoverRefused("cutover confirmation token has expired")
    accounts_root, account_dir = writes.account_paths(artifacts_root, plan.account_id)
    if ActivationStore(accounts_root).latest(plan.account_id) is not None:
        raise CutoverRefused("account already has an activation fence")
    established = EstablishedAccountsRegistry(accounts_root).latest(plan.account_id)
    if established is None:
        raise CutoverRefused("account establishment evidence disappeared")
    _require_checkpointed_database(account_dir)
    current_database = verify_database(
        account_dir / DB_FILENAME,
        expected_account_id=plan.account_id,
        expected_generation=established.authority_generation,
        expected_db_identity=established.db_identity_token,
        immutable=True,
    )
    normalized_broker = _normalize_broker_evidence(broker_evidence)
    _validate_cutover_safety(
        account_id=plan.account_id,
        broker_evidence=normalized_broker,
        expected_strategy_instance_ids=expected_strategy_instance_ids,
        stopped_strategy_instance_ids=stopped_strategy_instance_ids,
        now_ms=now,
        max_broker_evidence_age_ms=max_broker_evidence_age_ms,
    )
    current_legacy = _legacy_artifact_evidence(account_dir)
    if current_database != plan.database:
        raise CutoverRefused("SQLite database changed after cutover planning")
    if normalized_broker != plan.broker_evidence:
        raise CutoverRefused("broker evidence changed after cutover planning")
    if tuple(sorted(expected_strategy_instance_ids)) != plan.expected_strategy_instance_ids:
        raise CutoverRefused("governed bot roster changed after cutover planning")
    if tuple(sorted(stopped_strategy_instance_ids)) != plan.stopped_strategy_instance_ids:
        raise CutoverRefused("stopped-bot evidence changed after cutover planning")
    if current_legacy != plan.legacy_artifacts:
        raise CutoverRefused("legacy authority artifacts changed after cutover planning")

    cutover_id = f"g{plan.database.authority_generation}-{now}-{plan.plan_id[:12]}"
    quarantine_dir = account_dir / "legacy-quarantine" / cutover_id
    evidence_dir = account_dir / "cutover-evidence" / cutover_id
    moved: list[tuple[Path, Path]] = []
    activation_attempted = False
    try:
        quarantine_dir.mkdir(parents=True, exist_ok=False)
        evidence_dir.mkdir(parents=True, exist_ok=False)
        fsync_directory_chain(quarantine_dir, account_dir)
        fsync_directory_chain(evidence_dir, account_dir)
        for artifact in current_legacy:
            source = account_dir / artifact.relative_path
            destination = quarantine_dir / artifact.relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, destination)
            moved.append((source, destination))
        fsync_directory(account_dir)
        quarantine_manifest = quarantine_dir / "manifest.json"
        quarantine_sha = atomic_write_json(
            quarantine_manifest,
            {
                "schema_version": 1,
                "account_id": plan.account_id,
                "authority_generation": plan.database.authority_generation,
                "db_identity_token": plan.database.db_identity_token,
                "cutover_plan_id": plan.plan_id,
                "recorded_at_ms": now,
                "artifacts": [asdict(item) for item in current_legacy],
            },
        )
        proof_path = evidence_dir / "broker-proof.json"
        proof_sha = atomic_write_json(
            proof_path,
            {
                "schema_version": 1,
                "account_id": plan.account_id,
                "authority_generation": plan.database.authority_generation,
                "db_identity_token": plan.database.db_identity_token,
                "cutover_plan_id": plan.plan_id,
                "broker_evidence": asdict(normalized_broker),
                "recorded_at_ms": now,
            },
        )
        activation = ActivationRecord.create(
            account_id=plan.account_id,
            authority_generation=plan.database.authority_generation,
            db_identity_token=plan.database.db_identity_token,
            broker_proof_reference=relative_reference(artifacts_root, proof_path),
            broker_proof_sha256=proof_sha,
            legacy_quarantine_manifest=relative_reference(
                artifacts_root, quarantine_manifest
            ),
            legacy_quarantine_manifest_sha256=quarantine_sha,
            activated_at_ms=now,
        )
        receipt_path = evidence_dir / "cutover-receipt.json"
        atomic_write_json(
            receipt_path,
            {
                "schema_version": 1,
                "activation": asdict(activation),
                "plan_id": plan.plan_id,
                "recorded_at_ms": now,
            },
        )
        activation_attempted = True
        ActivationStore(accounts_root).append(activation)
    except Exception:
        # Once append begins, its exact durability outcome may be unknowable
        # (for example disk-full during fsync). Leave all evidence in place so
        # startup fails closed against either a complete or malformed fence.
        if not activation_attempted:
            _rollback_quarantine(moved)
            fsync_directory(account_dir)
        raise
    return CutoverReceipt(
        activation=activation,
        plan_id=plan.plan_id,
        quarantined_artifacts=current_legacy,
        receipt_reference=relative_reference(artifacts_root, receipt_path),
    )


def _normalize_broker_evidence(evidence: BrokerCutoverEvidence) -> BrokerCutoverEvidence:
    if (
        not isinstance(evidence.account_id, str)
        or not evidence.account_id
        or type(evidence.observed_at_ms) is not int
        or not isinstance(evidence.proof_reference, str)
        or not evidence.proof_reference
    ):
        raise CutoverRefused("broker evidence fields have invalid types")
    if any(
        not isinstance(symbol, str)
        or not symbol
        or isinstance(quantity, bool)
        or not isinstance(quantity, (int, float))
        for symbol, quantity in evidence.positions.items()
    ):
        raise CutoverRefused("broker position fields have invalid types")
    if any(not isinstance(order_id, str) or not order_id for order_id in evidence.open_order_ids):
        raise CutoverRefused("broker open-order identities have invalid types")
    return BrokerCutoverEvidence(
        account_id=evidence.account_id,
        observed_at_ms=evidence.observed_at_ms,
        proof_reference=evidence.proof_reference,
        positions=dict(sorted((symbol, float(qty)) for symbol, qty in evidence.positions.items())),
        open_order_ids=tuple(sorted(evidence.open_order_ids)),
    )


def _validate_cutover_safety(
    *,
    account_id: str,
    broker_evidence: BrokerCutoverEvidence,
    expected_strategy_instance_ids: Sequence[str],
    stopped_strategy_instance_ids: Sequence[str],
    now_ms: int,
    max_broker_evidence_age_ms: int,
) -> None:
    if broker_evidence.account_id != account_id:
        raise CutoverRefused("broker account identity does not match cutover account")
    if not broker_evidence.proof_reference:
        raise CutoverRefused("broker proof reference is required")
    if (
        type(max_broker_evidence_age_ms) is not int
        or max_broker_evidence_age_ms < 1
        or broker_evidence.observed_at_ms > now_ms
    ):
        raise CutoverRefused("broker evidence timestamp or freshness policy is invalid")
    if now_ms - broker_evidence.observed_at_ms > max_broker_evidence_age_ms:
        raise CutoverRefused("broker evidence is stale")
    from app.broker.alpaca.clerk.sqlite.reconcile import POSITION_QTY_EPSILON

    quantities = tuple(float(quantity) for quantity in broker_evidence.positions.values())
    if any(not math.isfinite(quantity) for quantity in quantities):
        raise CutoverRefused("broker position quantity must be finite")
    if any(abs(quantity) > POSITION_QTY_EPSILON for quantity in quantities):
        raise CutoverRefused("cutover requires a broker-flat account")
    if broker_evidence.open_order_ids:
        raise CutoverRefused("cutover requires no open broker orders")
    if set(expected_strategy_instance_ids) != set(stopped_strategy_instance_ids):
        raise CutoverRefused("cutover requires every governed bot to be stopped")


def _legacy_artifact_evidence(account_dir: Path) -> tuple[LegacyArtifactEvidence, ...]:
    evidence: list[LegacyArtifactEvidence] = []
    for name in LEGACY_ARTIFACT_NAMES:
        path = account_dir / name
        if not path.exists():
            continue
        if path.is_symlink():
            raise CutoverRefused(f"legacy artifact {name!r} is a symbolic link")
        kind = "file" if path.is_file() else "directory" if path.is_dir() else "unsupported"
        if kind == "unsupported":
            raise CutoverRefused(f"legacy artifact {name!r} has an unsupported type")
        evidence.append(
            LegacyArtifactEvidence(relative_path=name, kind=kind, sha256=tree_sha256(path))
        )
    return tuple(evidence)


def _require_checkpointed_database(account_dir: Path) -> None:
    """Require a cleanly stopped authority before immutable DB verification."""
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


def _validate_plan_token(plan: CutoverPlan, supplied_token: str) -> None:
    payload = {
        "schema_version": plan.schema_version,
        "account_id": plan.account_id,
        "created_at_ms": plan.created_at_ms,
        "expires_at_ms": plan.expires_at_ms,
        "database": asdict(plan.database),
        "broker_evidence": asdict(plan.broker_evidence),
        "expected_strategy_instance_ids": list(plan.expected_strategy_instance_ids),
        "stopped_strategy_instance_ids": list(plan.stopped_strategy_instance_ids),
        "legacy_artifacts": [asdict(item) for item in plan.legacy_artifacts],
    }
    expected = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    if plan.schema_version != 1 or plan.plan_id != expected or plan.confirmation_token != expected:
        raise CutoverRefused("cutover plan content hash does not verify")
    if not secrets.compare_digest(supplied_token, expected):
        raise CutoverRefused("cutover confirmation token does not match the plan")


def _rollback_quarantine(moved: list[tuple[Path, Path]]) -> None:
    for source, destination in reversed(moved):
        if destination.exists() and not source.exists():
            source.parent.mkdir(parents=True, exist_ok=True)
            os.replace(destination, source)
