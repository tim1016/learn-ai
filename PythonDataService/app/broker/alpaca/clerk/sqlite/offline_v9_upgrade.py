"""Verified offline v8-to-v9 authority upgrade for the SQLite Clerk.

Version 9 changes ownership topology; it is deliberately not an automatic
startup migration.  This ceremony snapshots v8, rebuilds a staged v9 database
from the finalized mirror, proves the journal and materialized projections,
then atomically publishes the stage only after every proof succeeds.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from app.broker.alpaca.clerk.sqlite import schema, writes
from app.broker.alpaca.clerk.sqlite.database_verification import (
    DatabaseVerification,
    verify_database,
)
from app.broker.alpaca.clerk.sqlite.folds import DEFAULT_FOLD_REGISTRY, FoldRegistry
from app.broker.alpaca.clerk.sqlite.mirror import MirrorFile, MirrorIdentity
from app.broker.alpaca.clerk.sqlite.operational_files import (
    atomic_write_json,
    relative_reference,
)
from app.broker.alpaca.clerk.sqlite.recovery import (
    ProcessStopProof,
    RecoveryReceipt,
    create_verified_backup,
    restore_verified_backup,
)
from app.broker.alpaca.clerk.sqlite.registry import EstablishedAccountsRegistry
from app.broker.alpaca.clerk.sqlite.repository_lifecycle import exclusive_recovery_fence
from app.broker.alpaca.paths import fsync_directory
from app.utils.timestamps import Clock, now_ms_utc

DB_FILENAME = "clerk.db"
MIRROR_FILENAME = "custody_transitions.mirror"
UPGRADE_DIRECTORY = "offline-v9-upgrades"
PROCESS_STOP_PROOF_MAX_AGE_MS = 60_000


class OfflineV9UpgradeRefused(Exception):
    """The authority cannot safely enter or complete the offline ceremony."""


@dataclass(frozen=True)
class OfflineV9UpgradeReceipt:
    """A durable external receipt for a no-new-transition schema ceremony."""

    account_id: str
    authority_generation: int
    db_identity_token: str
    source_schema_version: int
    target_schema_version: int
    backup_reference: str
    receipt_reference: str
    transition_count: int
    last_row_hash: str
    completed_at_ms: int
    applied: bool


def upgrade_v8_authority_offline(
    *,
    account_id: str,
    artifacts_root: Path,
    process_stop_proof: ProcessStopProof,
    clock: Clock = now_ms_utc,
    fold_registry: FoldRegistry = DEFAULT_FOLD_REGISTRY,
    max_process_stop_proof_age_ms: int = PROCESS_STOP_PROOF_MAX_AGE_MS,
    before_swap: Callable[[Path], None] | None = None,
    after_swap: Callable[[Path], None] | None = None,
) -> OfflineV9UpgradeReceipt:
    """Upgrade one stopped v8 authority without changing its custody journal.

    ``before_swap`` and ``after_swap`` are intentionally narrow test seams.
    A pre-swap exception is a failed proof: the staged database is retained for
    forensics and the original v8 file remains selected.  A post-swap exception
    leaves a durable prepared receipt; a retry verifies and finalizes that
    already-published v9 authority rather than attempting a second swap.
    """
    with exclusive_recovery_fence(artifacts_root=artifacts_root, account_id=account_id):
        now = clock()
        _validate_process_stop_proof(
            process_stop_proof,
            account_id=account_id,
            now_ms=now,
            max_age_ms=max_process_stop_proof_age_ms,
        )
        return _upgrade_v8_authority_offline_fenced(
            account_id=account_id,
            artifacts_root=artifacts_root,
            process_stop_proof=process_stop_proof,
            clock=clock,
            fold_registry=fold_registry,
            before_swap=before_swap,
            after_swap=after_swap,
        )


def _upgrade_v8_authority_offline_fenced(
    *,
    account_id: str,
    artifacts_root: Path,
    process_stop_proof: ProcessStopProof,
    clock: Clock,
    fold_registry: FoldRegistry,
    before_swap: Callable[[Path], None] | None,
    after_swap: Callable[[Path], None] | None,
) -> OfflineV9UpgradeReceipt:
    """Perform the ceremony while startup is excluded by the account fence."""
    now = clock()
    accounts_root, account_dir = writes.account_paths(artifacts_root, account_id)
    db_path = writes.confined_account_file(artifacts_root, account_id, DB_FILENAME)
    mirror_path = writes.confined_account_file(artifacts_root, account_id, MIRROR_FILENAME)
    upgrade_root = account_dir / UPGRADE_DIRECTORY
    upgrade_root.mkdir(parents=True, exist_ok=True)
    fsync_directory(upgrade_root)

    control = _read_control(db_path)
    if control["schema_version"] in {schema.OFFLINE_V9_SCHEMA_VERSION, schema.SCHEMA_VERSION}:
        return _completed_receipt_or_refuse(
            account_id=account_id,
            upgrade_root=upgrade_root,
            control=control,
            db_path=db_path,
            clock=clock,
        )
    if control["schema_version"] != 8:
        raise OfflineV9UpgradeRefused(
            f"offline v9 upgrade requires schema_version=8, found {control['schema_version']}"
        )
    _require_no_live_lease(control, now_ms=now)
    established = EstablishedAccountsRegistry(accounts_root).latest(account_id)
    if established is None:
        raise OfflineV9UpgradeRefused("v8 authority is not present in the established-accounts registry")
    if (
        established.authority_generation != control["authority_generation"]
        or established.db_identity_token != control["db_identity_token"]
    ):
        raise OfflineV9UpgradeRefused("v8 authority identity disagrees with established-accounts registry")

    receipt_path = upgrade_root / f"upgrade-g{control['authority_generation']}-v8-to-v9.json"
    prepared_path = receipt_path.with_suffix(".prepared.json")
    source: DatabaseVerification | None = None
    backup_reference: str | None = None
    stage_dir: Path | None = None
    published = False
    try:
        source = verify_database(
            db_path,
            expected_account_id=account_id,
            expected_generation=established.authority_generation,
            expected_db_identity=established.db_identity_token,
        )
        backup = create_verified_backup(
            account_id=account_id,
            artifacts_root=artifacts_root,
            clock=clock,
        )
        backup_reference = relative_reference(artifacts_root, backup.bundle_path)
        stage_dir = Path(tempfile.mkdtemp(prefix=".v9-stage-", dir=upgrade_root))
        stage_path = stage_dir / DB_FILENAME
        _rebuild_stage_from_finalized_mirror(
            stage_path=stage_path,
            mirror_path=mirror_path,
            account_id=account_id,
            source_control=control,
            fold_registry=fold_registry,
        )
        stage = verify_database(
            stage_path,
            expected_account_id=account_id,
            expected_generation=source.authority_generation,
            expected_db_identity=source.db_identity_token,
        )
        _assert_journal_identity(source, stage)
        _assert_projection_parity(db_path, stage_path)
        _checkpoint_source_wal(db_path)
        _assert_source_unchanged(
            db_path=db_path,
            account_id=account_id,
            source=source,
        )
        if before_swap is not None:
            before_swap(stage_path)
        prepared_payload = _prepared_receipt_payload(
            account_id=account_id,
            source=source,
            backup_reference=backup_reference,
            receipt_path=receipt_path,
            artifacts_root=artifacts_root,
            prepared_at_ms=clock(),
        )
        # This durable intent is the recovery point for the only dangerous
        # interval: publication happens after it, completion after publication.
        atomic_write_json(prepared_path, prepared_payload)
        os.replace(stage_path, db_path)
        published = True
        if after_swap is not None:
            after_swap(db_path)
        fsync_directory(account_dir)
        return _complete_prepared_upgrade(
            prepared_payload=prepared_payload,
            receipt_path=receipt_path,
            clock=clock,
            applied=True,
        )
    except Exception as exc:
        # After ``os.replace`` the v9 database is the selected authority.
        # Never label that as a failed v8 ceremony: the prepared receipt gives
        # a retry a verified, deterministic completion route instead.
        if published:
            raise
        atomic_write_json(
            receipt_path.with_suffix(".failed.json"),
            {
                "status": "failed",
                "account_id": account_id,
                "authority_generation": control["authority_generation"],
                "db_identity_token": control["db_identity_token"],
                "source_schema_version": control["schema_version"],
                "target_schema_version": schema.OFFLINE_V9_SCHEMA_VERSION,
                "backup_reference": backup_reference,
                "failed_at_ms": clock(),
                "reason": str(exc),
                "stage_reference": (
                    relative_reference(artifacts_root, stage_dir) if stage_dir is not None else None
                ),
            },
        )
        raise


def _validate_process_stop_proof(
    proof: ProcessStopProof,
    *,
    account_id: str,
    now_ms: int,
    max_age_ms: int = PROCESS_STOP_PROOF_MAX_AGE_MS,
) -> None:
    if (
        proof.account_id != account_id
        or type(proof.observed_at_ms) is not int
        or not isinstance(proof.proof_reference, str)
        or not proof.proof_reference
    ):
        raise OfflineV9UpgradeRefused("offline upgrade requires an account-bound process-stop proof")
    if type(max_age_ms) is not int or max_age_ms < 0:
        raise OfflineV9UpgradeRefused("process-stop proof maximum age must be a non-negative integer")
    age_ms = now_ms - proof.observed_at_ms
    if age_ms < 0 or age_ms > max_age_ms:
        raise OfflineV9UpgradeRefused("process-stop proof is stale; obtain fresh offline evidence")


def rollback_v9_upgrade_offline(
    *,
    account_id: str,
    artifacts_root: Path,
    process_stop_proof: ProcessStopProof,
    max_process_stop_proof_age_ms: int = PROCESS_STOP_PROOF_MAX_AGE_MS,
    clock: Clock = now_ms_utc,
) -> RecoveryReceipt:
    """Restore the verified v8 backup recorded by a completed upgrade receipt.

    The shared restore protocol owns the exclusive recovery fence, preserves the
    pre-restore v9 files, and verifies the selected bundle before publication.
    This wrapper only chooses the receipt-bound backup; it never accepts a
    hand-selected backup path.
    """
    now = clock()
    _validate_process_stop_proof(
        process_stop_proof,
        account_id=account_id,
        now_ms=now,
        max_age_ms=max_process_stop_proof_age_ms,
    )
    _, account_dir = writes.account_paths(artifacts_root, account_id)
    control = _read_control(writes.confined_account_file(artifacts_root, account_id, DB_FILENAME))
    if control["schema_version"] not in {schema.OFFLINE_V9_SCHEMA_VERSION, schema.SCHEMA_VERSION}:
        raise OfflineV9UpgradeRefused("only a published v9 authority can be rolled back")
    receipt = _completed_receipt_or_refuse(
        account_id=account_id,
        upgrade_root=account_dir / UPGRADE_DIRECTORY,
        control=control,
        db_path=writes.confined_account_file(artifacts_root, account_id, DB_FILENAME),
        clock=clock,
    )
    return restore_verified_backup(
        account_id=account_id,
        artifacts_root=artifacts_root,
        bundle_path=artifacts_root / receipt.backup_reference,
        process_stop_proof=process_stop_proof,
        max_process_stop_proof_age_ms=max_process_stop_proof_age_ms,
        clock=clock,
    )


def _read_control(db_path: Path) -> sqlite3.Row:
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT schema_version, account_id, db_identity_token, authority_generation, "
            "control_revision, created_at_ms, last_open_at_ms, reset_provenance_json, "
            "execution_lease_owner, execution_lease_expires_at_ms FROM control_meta WHERE id = 1"
        ).fetchone()
        if row is None:
            raise OfflineV9UpgradeRefused("authority has no control_meta row")
        return row
    finally:
        conn.close()


def _require_no_live_lease(control: sqlite3.Row, *, now_ms: int) -> None:
    owner = control["execution_lease_owner"]
    expires = control["execution_lease_expires_at_ms"]
    if owner is not None and expires is not None and int(expires) >= now_ms:
        raise OfflineV9UpgradeRefused(
            "authority execution lease is active; stop the data-plane process before upgrading"
        )


def _rebuild_stage_from_finalized_mirror(
    *,
    stage_path: Path,
    mirror_path: Path,
    account_id: str,
    source_control: sqlite3.Row,
    fold_registry: FoldRegistry,
) -> None:
    """Build the stage exclusively from finalized mirror facts and v9 folds."""
    rows = MirrorFile(
        mirror_path,
        expected_identity=MirrorIdentity(
            account_id=account_id,
            authority_generation=source_control["authority_generation"],
            db_identity_token=source_control["db_identity_token"],
        ),
    ).rebuild()
    conn = sqlite3.connect(stage_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        schema.configure_connection(conn)
        schema.apply_v9_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO control_meta "
                "(id, schema_version, broker, account_id, db_identity_token, authority_generation, "
                "control_revision, created_at_ms, last_open_at_ms, reset_provenance_json, "
                "execution_lease_owner, execution_lease_expires_at_ms) "
                "VALUES (1, ?, 'alpaca', ?, ?, ?, 0, ?, ?, ?, NULL, NULL)",
                (
                    schema.OFFLINE_V9_SCHEMA_VERSION,
                    account_id,
                    source_control["db_identity_token"],
                    source_control["authority_generation"],
                    source_control["created_at_ms"],
                    source_control["last_open_at_ms"],
                    source_control["reset_provenance_json"],
                ),
            )
            for row in rows:
                if row.authority_generation != source_control["authority_generation"]:
                    raise OfflineV9UpgradeRefused("finalized mirror contains another authority generation")
                writes.insert_custody_transition_row(
                    conn,
                    sequence=row.sequence,
                    prev_hash=row.prev_hash,
                    row_hash=row.row_hash,
                    payload=dict(row.payload),
                )
                fold_registry.apply(conn, dict(row.payload))
                writes.advance_control_revision(conn)
                writes.insert_mirror_fence_prepare_row(
                    conn,
                    sequence=row.sequence,
                    row_hash=row.row_hash,
                    authority_generation=row.authority_generation,
                    recorded_at_ms=row.recorded_at_ms,
                )
            revision = conn.execute(
                "SELECT control_revision FROM control_meta WHERE id = 1"
            ).fetchone()["control_revision"]
            if revision != source_control["control_revision"]:
                raise OfflineV9UpgradeRefused(
                    "v9 mirror replay control revision disagrees with v8 source"
                )
            conn.commit()
            _checkpoint_wal(conn, label="staged v9 authority")
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()


def _assert_journal_identity(source: DatabaseVerification, stage: DatabaseVerification) -> None:
    if (
        stage.schema_version != schema.OFFLINE_V9_SCHEMA_VERSION
        or stage.account_id != source.account_id
        or stage.authority_generation != source.authority_generation
        or stage.db_identity_token != source.db_identity_token
        or stage.control_revision != source.control_revision
        or stage.transition_count != source.transition_count
        or stage.last_sequence != source.last_sequence
        or stage.last_row_hash != source.last_row_hash
    ):
        raise OfflineV9UpgradeRefused("staged v9 journal identity does not match the v8 source")


def _assert_projection_parity(source_path: Path, stage_path: Path) -> None:
    if _projection_signature(source_path) != _projection_signature(stage_path):
        raise OfflineV9UpgradeRefused("staged v9 projections do not match the v8 source")


def _projection_signature(path: Path) -> dict[str, tuple[tuple[Any, ...], ...]]:
    """Stable v8/v9-compatible current-state proof without new v9 columns."""
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        return {
            "strategy_instances": _rows(conn, "SELECT strategy_instance_id, symbol, config_hash, created_at_ms, retired_at_ms FROM strategy_instances ORDER BY strategy_instance_id"),
            "runs": _rows(conn, "SELECT run_id, strategy_instance_id, lifecycle_run_id, state, started_at_ms, stopped_at_ms FROM runs ORDER BY run_id"),
            "commands": _rows(conn, "SELECT command_id, authority_generation, idempotency_key, payload_hash, kind, strategy_instance_id, run_id, action, intended_end_state, state, effect_operation_id, receipt_id, created_at_ms, updated_at_ms FROM commands ORDER BY command_id"),
            "effects": _rows(conn, "SELECT effect_operation_id, authority_generation, idempotency_key, command_id, strategy_instance_id, run_id, kind, state, custody_owner, created_at_ms, updated_at_ms, terminal_receipt_id, claim_owner, claim_token, claimed_at_ms, claim_expires_at_ms FROM effect_operations ORDER BY effect_operation_id"),
            "orders": _rows(conn, "SELECT order_ref, effect_operation_id, client_order_id, broker_order_id, role, broker_state, submitted_at_ms, updated_at_ms FROM orders ORDER BY order_ref"),
            "operation_order_links": _rows(conn, "SELECT effect_operation_id, order_ref, role, linked_at_ms FROM operation_order_links ORDER BY effect_operation_id, order_ref"),
            "fills": _rows(conn, "SELECT fill_id, order_ref, qty, price, side, is_correction, execution_id, evidence_source, event_kind, superseded_execution_ref, fee, fee_fidelity, source_event_at_ms, clerk_observed_at_ms, recorded_at_ms, recorded_transition_sequence FROM fills ORDER BY fill_id"),
            "external_orders": _rows(conn, "SELECT external_order_id, broker_order_id, client_order_id, symbol, side, qty, order_type, limit_price, stop_price, filled_avg_price, observed_at_ms, acknowledged_at_ms, ack_operator, evidence_refs_json FROM external_orders ORDER BY external_order_id"),
            "positions": _rows(conn, "SELECT strategy_instance_id, symbol, attributed_qty, updated_at_ms FROM positions ORDER BY strategy_instance_id, symbol"),
            "holds": _rows(conn, "SELECT CASE scope WHEN 'BOT' THEN 'CUSTODY_SUBJECT' ELSE scope END, strategy_instance_id, reason_code, state, opened_at_ms, resolved_at_ms, evidence_refs_json FROM holds ORDER BY hold_id"),
            "uncertainties": _rows(conn, "SELECT uncertainty_id, CASE scope WHEN 'BOT' THEN 'CUSTODY_SUBJECT' ELSE scope END, severity, blocks_new_exposure, allows_reduction, custody_owner, strategy_instance_id, reason_code, headline, explanation, operator_impact, next_step, observed_at_ms, resolved_at_ms, evidence_refs_json, facts_schema_version, facts_json FROM uncertainties ORDER BY uncertainty_id"),
            "reconciliations": _rows(conn, "SELECT reconciliation_id, effect_operation_id, order_ref, trigger, attempted_at_ms, outcome, evidence_refs_json FROM reconciliations ORDER BY reconciliation_id"),
            "receipts": _rows(conn, "SELECT receipt_id, command_id, effect_operation_id, terminal_state, summary_code, proof_reference, recorded_at_ms, facts_json FROM receipts ORDER BY receipt_id"),
            "bot_config": _rows(conn, "SELECT strategy_instance_id, strategy_key, display_name, config_json, config_hash, created_at_ms FROM bot_config ORDER BY strategy_instance_id"),
            "decision_receipts": _rows(conn, "SELECT strategy_instance_id, seq, outcome, symbol, intent_id, order_ref, observed_at_ms, facts_json FROM decision_receipts ORDER BY strategy_instance_id, seq"),
        }
    finally:
        conn.close()


def _rows(conn: sqlite3.Connection, query: str) -> tuple[tuple[Any, ...], ...]:
    return tuple(tuple(row) for row in conn.execute(query).fetchall())


def _assert_source_unchanged(
    *, db_path: Path,
    account_id: str,
    source: DatabaseVerification,
) -> None:
    latest = verify_database(
        db_path,
        expected_account_id=account_id,
        expected_generation=source.authority_generation,
        expected_db_identity=source.db_identity_token,
    )
    if latest != source:
        raise OfflineV9UpgradeRefused("v8 source changed during the offline upgrade ceremony")


def _checkpoint_source_wal(db_path: Path) -> None:
    """Make the verified v8 source one self-contained file before its swap."""
    conn = sqlite3.connect(db_path)
    try:
        _checkpoint_wal(conn, label="v8 source authority")
    finally:
        conn.close()
    for suffix in ("-wal", "-shm"):
        sidecar = db_path.with_name(f"{db_path.name}{suffix}")
        if sidecar.exists() or sidecar.is_symlink():
            sidecar.unlink()
    fsync_directory(db_path.parent)


def _checkpoint_wal(conn: sqlite3.Connection, *, label: str) -> None:
    row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    if row is None or row[0] != 0:
        raise OfflineV9UpgradeRefused(f"{label} WAL checkpoint could not obtain an exclusive cut")


def _prepared_receipt_payload(
    *,
    account_id: str,
    source: DatabaseVerification,
    backup_reference: str,
    receipt_path: Path,
    artifacts_root: Path,
    prepared_at_ms: int,
) -> dict[str, Any]:
    """Return the fsynced intent that makes post-swap recovery deterministic."""
    return {
        "status": "prepared",
        "account_id": account_id,
        "authority_generation": source.authority_generation,
        "db_identity_token": source.db_identity_token,
        "source_schema_version": 8,
        "target_schema_version": schema.OFFLINE_V9_SCHEMA_VERSION,
        "backup_reference": backup_reference,
        "receipt_reference": relative_reference(artifacts_root, receipt_path),
        "transition_count": source.transition_count,
        "last_row_hash": source.last_row_hash,
        "prepared_at_ms": prepared_at_ms,
    }


def _complete_prepared_upgrade(
    *,
    prepared_payload: dict[str, Any],
    receipt_path: Path,
    clock: Clock,
    applied: bool,
) -> OfflineV9UpgradeReceipt:
    """Publish the completed receipt from an already-verified prepare record."""
    receipt = OfflineV9UpgradeReceipt(
        account_id=prepared_payload["account_id"],
        authority_generation=prepared_payload["authority_generation"],
        db_identity_token=prepared_payload["db_identity_token"],
        source_schema_version=prepared_payload["source_schema_version"],
        target_schema_version=prepared_payload["target_schema_version"],
        backup_reference=prepared_payload["backup_reference"],
        receipt_reference=prepared_payload["receipt_reference"],
        transition_count=prepared_payload["transition_count"],
        last_row_hash=prepared_payload["last_row_hash"],
        completed_at_ms=clock(),
        applied=applied,
    )
    atomic_write_json(receipt_path, {"status": "completed", **asdict(receipt)})
    return receipt


def _completed_receipt_or_refuse(
    *,
    account_id: str,
    upgrade_root: Path,
    control: sqlite3.Row,
    db_path: Path,
    clock: Clock,
) -> OfflineV9UpgradeReceipt:
    receipt_path = upgrade_root / f"upgrade-g{control['authority_generation']}-v8-to-v9.json"
    if receipt_path.is_file():
        payload = _load_receipt_payload(receipt_path)
        if payload.get("status") != "completed":
            raise OfflineV9UpgradeRefused("completed v9 upgrade receipt is malformed")
        return _receipt_from_completed_payload(
            payload=payload,
            account_id=account_id,
            control=control,
            receipt_path=receipt_path,
        )

    prepared_path = receipt_path.with_suffix(".prepared.json")
    if not prepared_path.is_file():
        raise OfflineV9UpgradeRefused("v9 authority lacks the required completed offline-upgrade receipt")
    prepared = _load_receipt_payload(prepared_path)
    _assert_prepared_receipt_matches_published_authority(
        payload=prepared,
        account_id=account_id,
        control=control,
        receipt_path=receipt_path,
        db_path=db_path,
    )
    return _complete_prepared_upgrade(
        prepared_payload=prepared,
        receipt_path=receipt_path,
        clock=clock,
        applied=False,
    )


def _load_receipt_payload(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OfflineV9UpgradeRefused("offline v9 upgrade receipt cannot be read") from exc
    if not isinstance(payload, dict):
        raise OfflineV9UpgradeRefused("offline v9 upgrade receipt must be an object")
    return payload


def _receipt_from_completed_payload(
    *,
    payload: dict[str, Any],
    account_id: str,
    control: sqlite3.Row,
    receipt_path: Path,
) -> OfflineV9UpgradeReceipt:
    expected_reference = relative_reference(receipt_path.parents[4], receipt_path)
    try:
        receipt = OfflineV9UpgradeReceipt(
            account_id=payload["account_id"],
            authority_generation=payload["authority_generation"],
            db_identity_token=payload["db_identity_token"],
            source_schema_version=payload["source_schema_version"],
            target_schema_version=payload["target_schema_version"],
            backup_reference=payload["backup_reference"],
            receipt_reference=payload["receipt_reference"],
            transition_count=payload["transition_count"],
            last_row_hash=payload["last_row_hash"],
            completed_at_ms=payload["completed_at_ms"],
            applied=False,
        )
    except (KeyError, TypeError) as exc:
        raise OfflineV9UpgradeRefused("completed v9 upgrade receipt is malformed") from exc
    if (
        receipt.account_id != account_id
        or receipt.authority_generation != control["authority_generation"]
        or receipt.db_identity_token != control["db_identity_token"]
        or receipt.source_schema_version != 8
        or receipt.target_schema_version != schema.OFFLINE_V9_SCHEMA_VERSION
        or receipt.receipt_reference != expected_reference
    ):
        raise OfflineV9UpgradeRefused("completed v9 upgrade receipt does not bind this authority")
    return receipt


def _assert_prepared_receipt_matches_published_authority(
    *,
    payload: dict[str, Any],
    account_id: str,
    control: sqlite3.Row,
    receipt_path: Path,
    db_path: Path,
) -> None:
    expected_reference = relative_reference(receipt_path.parents[4], receipt_path)
    try:
        expected_generation = payload["authority_generation"]
        expected_identity = payload["db_identity_token"]
        expected_transition_count = payload["transition_count"]
        expected_last_hash = payload["last_row_hash"]
    except KeyError as exc:
        raise OfflineV9UpgradeRefused("prepared v9 upgrade receipt is malformed") from exc
    if (
        payload.get("status") != "prepared"
        or payload.get("account_id") != account_id
        or payload.get("source_schema_version") != 8
        or payload.get("target_schema_version") != schema.OFFLINE_V9_SCHEMA_VERSION
        or payload.get("receipt_reference") != expected_reference
        or expected_generation != control["authority_generation"]
        or expected_identity != control["db_identity_token"]
        or not isinstance(payload.get("backup_reference"), str)
    ):
        raise OfflineV9UpgradeRefused("prepared v9 upgrade receipt does not bind this authority")
    verification = verify_database(
        db_path,
        expected_account_id=account_id,
        expected_generation=expected_generation,
        expected_db_identity=expected_identity,
    )
    if (
        verification.schema_version not in {schema.OFFLINE_V9_SCHEMA_VERSION, schema.SCHEMA_VERSION}
        or verification.transition_count != expected_transition_count
        or verification.last_row_hash != expected_last_hash
    ):
        raise OfflineV9UpgradeRefused("published v9 authority does not match its prepared upgrade receipt")
