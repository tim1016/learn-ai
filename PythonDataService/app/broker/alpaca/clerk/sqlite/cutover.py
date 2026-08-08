"""Human-gated activation planning and application for the SQLite Clerk.

``plan_cutover`` is strictly read-only.  ``apply_cutover`` accepts no force
mode and re-observes every safety input before quarantining legacy artifacts
and appending the activation fence.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from app.broker.alpaca.clerk.journal import INBOX_FILENAME, JOURNAL_FILENAME
from app.broker.alpaca.clerk.sqlite import writes
from app.broker.alpaca.clerk.sqlite.activation import ActivationRecord, ActivationStore
from app.broker.alpaca.clerk.sqlite.cutover_initialization import (
    CutoverInitializationEvidence,
    CutoverRefused,
)
from app.broker.alpaca.clerk.sqlite.cutover_initialization import (
    cutover_evidence_directory as _cutover_evidence_directory,
)
from app.broker.alpaca.clerk.sqlite.cutover_initialization import (
    initialization_intent_path as _initialization_intent_path,
)
from app.broker.alpaca.clerk.sqlite.cutover_initialization import (
    initialization_intent_payload as _initialization_intent_payload,
)
from app.broker.alpaca.clerk.sqlite.cutover_initialization import (
    require_or_publish_initialization_intent as _require_or_publish_initialization_intent,
)
from app.broker.alpaca.clerk.sqlite.cutover_initialization import (
    verify_empty_generation_mirror as _verify_empty_generation_mirror,
)
from app.broker.alpaca.clerk.sqlite.cutover_initialization import (
    verify_inactive_generation as _verify_inactive_generation,
)
from app.broker.alpaca.clerk.sqlite.database_verification import (
    DatabaseVerification,
    verify_database,
)
from app.broker.alpaca.clerk.sqlite.developer_reset_registry import (
    DeveloperCleanSlateResetRegistry,
)
from app.broker.alpaca.clerk.sqlite.operational_files import (
    atomic_write_json,
    canonical_json_bytes,
    confined_relative_path,
    relative_reference,
    tree_sha256,
)
from app.broker.alpaca.clerk.sqlite.registry import EstablishedAccountsRegistry
from app.broker.alpaca.clerk.sqlite.repository import (
    DB_FILENAME,
    ClerkSqliteRepository,
)
from app.broker.alpaca.paths import (
    fsync_directory,
    fsync_directory_chain,
    resolve_contained_path,
    safe_path_component,
)
from app.engine.live.cutover_roster import (
    CutoverRosterEvidenceError,
    read_quiescent_alpaca_roster,
)
from app.engine.live.identity import validate_strategy_instance_id
from app.utils.advisory_lock import advisory_file_lock
from app.utils.timestamps import Clock, now_ms_utc

DEFAULT_CONFIRMATION_TTL_MS = 120_000
MAX_CONFIRMATION_TTL_MS = 300_000
LEGACY_ARTIFACT_NAMES: tuple[str, ...] = (
    INBOX_FILENAME,
    JOURNAL_FILENAME,
    "custody_resolution_receipts.json",
    "bots",
)
LEGACY_UNSCOPED_BOT_DIRECTORY = "bots"


def _require_no_unreset_activation(
    *,
    accounts_root: Path,
    account_id: str,
    artifacts_root: Path,
) -> None:
    """Allow only a verified developer-reset fence to replace an activation."""
    if _developer_reset_replaces_activation(
        accounts_root=accounts_root,
        account_id=account_id,
        artifacts_root=artifacts_root,
    ):
        return
    if ActivationStore(accounts_root).latest(account_id) is None:
        return
    raise CutoverRefused("account already has an activation fence")


def _developer_reset_replaces_activation(
    *,
    accounts_root: Path,
    account_id: str,
    artifacts_root: Path,
) -> bool:
    """Return whether the newest activation was explicitly moved aside."""
    activation = ActivationStore(accounts_root).latest(account_id)
    return activation is not None and DeveloperCleanSlateResetRegistry(
        accounts_root
    ).authorizes_reinitialize(
        account_id=account_id,
        prior_authority_generation=activation.authority_generation,
        artifacts_root=artifacts_root,
    )


@dataclass(frozen=True)
class BrokerCutoverEvidence:
    account_id: str
    account_mode: Literal["paper"]
    observed_at_ms: int
    proof_reference: str
    positions: Mapping[str, float]
    open_order_ids: tuple[str, ...]


@dataclass(frozen=True)
class LegacyArtifactEvidence:
    """One legacy artifact, referenced relative to the Clerk artifacts root."""

    relative_path: str
    kind: str
    sha256: str


@dataclass(frozen=True)
class RunnerBotEvidence:
    """Durable proof that one configured Alpaca bot is not runnable."""

    strategy_instance_id: str
    artifact_reference: str
    artifact_sha256: str
    mode: str
    desired_state: str
    lifecycle_phase: str
    terminal_kind: str
    terminal_reason_code: str
    terminal_recorded_at_ms: int


@dataclass(frozen=True)
class CutoverPlan:
    schema_version: int
    plan_id: str
    confirmation_token: str
    account_id: str
    created_at_ms: int
    expires_at_ms: int
    initialization: CutoverInitializationEvidence
    database: DatabaseVerification
    broker_evidence: BrokerCutoverEvidence
    runner_roster: tuple[RunnerBotEvidence, ...]
    legacy_artifacts: tuple[LegacyArtifactEvidence, ...]


@dataclass(frozen=True)
class CutoverReceipt:
    activation: ActivationRecord
    plan_id: str
    quarantined_artifacts: tuple[LegacyArtifactEvidence, ...]
    receipt_reference: str


@dataclass(frozen=True)
class CutoverInitializationReceipt:
    """Evidence for a latent, inactive generation-one SQLite authority."""

    account_id: str
    initialization_intent_id: str
    database: DatabaseVerification
    broker_evidence: BrokerCutoverEvidence
    runner_roster: tuple[RunnerBotEvidence, ...]
    legacy_artifacts: tuple[LegacyArtifactEvidence, ...]
    receipt_reference: str


def initialize_cutover_authority(
    *,
    account_id: str,
    artifacts_root: Path,
    runner_artifacts_root: Path,
    broker_evidence: BrokerCutoverEvidence,
    max_broker_evidence_age_ms: int,
    clock: Clock = now_ms_utc,
) -> CutoverInitializationReceipt:
    """Create an inactive generation-one authority behind the legacy fence.

    This is the only supported production path to ``initialize()``. A fresh
    initialization proves current broker flatness and the independently read
    durable runner roster first, then closes the repository before publishing
    its evidence receipt. An exact retry may finish receipt publication for
    the verified unused generation after the proof ages out. No activation
    record is written, so legacy remains selected until apply.
    """
    accounts_root, account_dir = writes.account_paths(artifacts_root, account_id)
    with advisory_file_lock(account_dir / "cutover-initialize"):
        return _initialize_cutover_authority_locked(
            account_id=account_id,
            artifacts_root=artifacts_root,
            runner_artifacts_root=runner_artifacts_root,
            normalized_broker=_normalize_broker_evidence(broker_evidence),
            max_broker_evidence_age_ms=max_broker_evidence_age_ms,
            accounts_root=accounts_root,
            account_dir=account_dir,
            clock=clock,
        )


def _initialize_cutover_authority_locked(
    *,
    account_id: str,
    artifacts_root: Path,
    runner_artifacts_root: Path,
    normalized_broker: BrokerCutoverEvidence,
    max_broker_evidence_age_ms: int,
    accounts_root: Path,
    account_dir: Path,
    clock: Clock,
) -> CutoverInitializationReceipt:
    """Initialize or resume while holding the account's cutover lock."""
    now = clock()
    registry = EstablishedAccountsRegistry(accounts_root)
    established = registry.latest(account_id)
    reset_registry = DeveloperCleanSlateResetRegistry(accounts_root)
    reset_authorized = established is not None and reset_registry.authorizes_reinitialize(
        account_id=account_id,
        prior_authority_generation=established.authority_generation,
        artifacts_root=artifacts_root,
    )
    _validate_cutover_broker_state(
        account_id=account_id,
        broker_evidence=normalized_broker,
    )
    if established is None:
        _validate_cutover_evidence_freshness(
            broker_evidence=normalized_broker,
            now_ms=now,
            max_broker_evidence_age_ms=max_broker_evidence_age_ms,
        )
    _require_no_unreset_activation(
        accounts_root=accounts_root,
        account_id=account_id,
        artifacts_root=artifacts_root,
    )
    legacy = _legacy_artifact_evidence(
        artifacts_root,
        account_id,
        allow_empty=reset_authorized,
    )
    runner_roster = _runner_roster_evidence(
        runner_artifacts_root,
        account_id,
        legacy_strategy_instance_ids=_legacy_strategy_instance_ids(
            artifacts_root,
            account_id,
        ),
        allow_empty=reset_authorized,
    )
    intended_generation = (
        1
        if established is None
        else established.authority_generation + 1
        if reset_authorized
        else established.authority_generation
    )
    intent_payload, initialization_intent_id = _initialization_intent_payload(
        account_id=account_id,
        authority_generation=intended_generation,
        broker_evidence=normalized_broker,
        runner_roster=runner_roster,
        legacy_artifacts=legacy,
    )
    _require_or_publish_initialization_intent(
        account_dir=account_dir,
        payload=intent_payload,
        authority_generation=intended_generation,
        allow_create=established is None or reset_authorized,
    )
    if established is None or reset_authorized:
        repository = ClerkSqliteRepository.initialize(
            account_id=account_id,
            artifacts_root=artifacts_root,
            clock=clock,
        )
        repository.close()
        established = registry.latest(account_id)
    if established is None:
        raise CutoverRefused("SQLite initialization did not publish its generation fence")
    database = _verify_inactive_generation(
        account_id=account_id,
        account_dir=account_dir,
        authority_generation=established.authority_generation,
        db_identity_token=established.db_identity_token,
    )
    receipt_payload = {
        "schema_version": 1,
        "operation": "CUTOVER_INITIALIZE",
        "account_id": account_id,
        "initialization_intent_id": initialization_intent_id,
        "recorded_at_ms": established.established_at_ms,
        "database": asdict(database),
        "broker_evidence": asdict(normalized_broker),
        "runner_roster": [asdict(item) for item in runner_roster],
        "legacy_artifacts": [asdict(item) for item in legacy],
    }
    receipt_path = (
        _cutover_evidence_directory(account_dir, create=False)
        / f"initialization-g{established.authority_generation}-{established.db_identity_token}.json"
    )
    encoded_receipt = canonical_json_bytes(receipt_payload)
    if receipt_path.is_symlink() or (
        receipt_path.exists() and not receipt_path.is_file()
    ):
        raise CutoverRefused("cutover initialization receipt has an unsafe type")
    if receipt_path.is_file():
        if receipt_path.read_bytes() != encoded_receipt:
            raise CutoverRefused(
                "cutover initialization already completed with different evidence"
            )
    else:
        atomic_write_json(receipt_path, receipt_payload)
    return CutoverInitializationReceipt(
        account_id=account_id,
        initialization_intent_id=initialization_intent_id,
        database=database,
        broker_evidence=normalized_broker,
        runner_roster=runner_roster,
        legacy_artifacts=legacy,
        receipt_reference=relative_reference(artifacts_root, receipt_path),
    )


def _require_completed_cutover_initialization(
    *,
    account_id: str,
    account_dir: Path,
    database: DatabaseVerification,
) -> CutoverInitializationEvidence:
    """Require a valid pre-init intent and its database-bound completion receipt."""
    evidence_dir = _cutover_evidence_directory(account_dir, create=False)
    intent = _read_initialization_record(
        _initialization_intent_path(
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
    receipt = _read_initialization_record(
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
    mirror_sha256 = _verify_empty_generation_mirror(
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


def _read_initialization_record(path: Path, *, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise CutoverRefused(f"{label} must be a regular non-symbolic-link file")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CutoverRefused(f"{label} is unreadable: {exc}") from exc
    if not isinstance(payload, dict):
        raise CutoverRefused(f"{label} must contain a JSON object")
    return payload


def plan_cutover(
    *,
    account_id: str,
    artifacts_root: Path,
    runner_artifacts_root: Path,
    broker_evidence: BrokerCutoverEvidence,
    max_broker_evidence_age_ms: int,
    confirmation_ttl_ms: int = DEFAULT_CONFIRMATION_TTL_MS,
    clock: Clock = now_ms_utc,
) -> CutoverPlan:
    """Read and content-address every prerequisite without writing anything."""
    now = clock()
    if type(confirmation_ttl_ms) is not int or not 1 <= confirmation_ttl_ms <= MAX_CONFIRMATION_TTL_MS:
        raise CutoverRefused("confirmation TTL must be within 1..300000 ms")
    accounts_root, account_dir = writes.account_paths(artifacts_root, account_id)
    _require_no_unreset_activation(
        accounts_root=accounts_root,
        account_id=account_id,
        artifacts_root=artifacts_root,
    )
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
    initialization = _require_completed_cutover_initialization(
        account_id=account_id,
        account_dir=account_dir,
        database=database,
    )
    normalized_broker = _normalize_broker_evidence(broker_evidence)
    _validate_cutover_safety(
        account_id=account_id,
        broker_evidence=normalized_broker,
        now_ms=now,
        max_broker_evidence_age_ms=max_broker_evidence_age_ms,
    )
    legacy = _legacy_artifact_evidence(
        artifacts_root,
        account_id,
        allow_empty=_developer_reset_replaces_activation(
            accounts_root=accounts_root,
            account_id=account_id,
            artifacts_root=artifacts_root,
        ),
    )
    runner_roster = _runner_roster_evidence(
        runner_artifacts_root,
        account_id,
        legacy_strategy_instance_ids=_legacy_strategy_instance_ids(
            artifacts_root,
            account_id,
        ),
        allow_empty=_developer_reset_replaces_activation(
            accounts_root=accounts_root,
            account_id=account_id,
            artifacts_root=artifacts_root,
        ),
    )
    payload = {
        "schema_version": 3,
        "account_id": account_id,
        "created_at_ms": now,
        "expires_at_ms": now + confirmation_ttl_ms,
        "initialization": asdict(initialization),
        "database": asdict(database),
        "broker_evidence": asdict(normalized_broker),
        "runner_roster": [asdict(item) for item in runner_roster],
        "legacy_artifacts": [asdict(item) for item in legacy],
    }
    token = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    return CutoverPlan(
        schema_version=3,
        plan_id=token,
        confirmation_token=token,
        account_id=account_id,
        created_at_ms=now,
        expires_at_ms=now + confirmation_ttl_ms,
        initialization=initialization,
        database=database,
        broker_evidence=normalized_broker,
        runner_roster=runner_roster,
        legacy_artifacts=legacy,
    )


def apply_cutover(
    *,
    plan: CutoverPlan,
    confirmation_token: str,
    artifacts_root: Path,
    runner_artifacts_root: Path,
    broker_evidence: BrokerCutoverEvidence,
    max_broker_evidence_age_ms: int,
    clock: Clock = now_ms_utc,
) -> CutoverReceipt:
    """Recheck the plan, quarantine legacy state, then fsync activation."""
    now = clock()
    _validate_plan_token(plan, confirmation_token)
    if now > plan.expires_at_ms:
        raise CutoverRefused("cutover confirmation token has expired")
    accounts_root, account_dir = writes.account_paths(artifacts_root, plan.account_id)
    _require_no_unreset_activation(
        accounts_root=accounts_root,
        account_id=plan.account_id,
        artifacts_root=artifacts_root,
    )
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
    current_initialization = _require_completed_cutover_initialization(
        account_id=plan.account_id,
        account_dir=account_dir,
        database=current_database,
    )
    normalized_broker = _normalize_broker_evidence(broker_evidence)
    _validate_cutover_safety(
        account_id=plan.account_id,
        broker_evidence=normalized_broker,
        now_ms=now,
        max_broker_evidence_age_ms=max_broker_evidence_age_ms,
    )
    current_legacy = _legacy_artifact_evidence(
        artifacts_root,
        plan.account_id,
        allow_empty=_developer_reset_replaces_activation(
            accounts_root=accounts_root,
            account_id=plan.account_id,
            artifacts_root=artifacts_root,
        ),
    )
    current_roster = _runner_roster_evidence(
        runner_artifacts_root,
        plan.account_id,
        legacy_strategy_instance_ids=_legacy_strategy_instance_ids(
            artifacts_root,
            plan.account_id,
        ),
        allow_empty=_developer_reset_replaces_activation(
            accounts_root=accounts_root,
            account_id=plan.account_id,
            artifacts_root=artifacts_root,
        ),
    )
    if current_database != plan.database:
        raise CutoverRefused("SQLite database changed after cutover planning")
    if current_initialization != plan.initialization:
        raise CutoverRefused(
            "cutover initialization evidence changed after cutover planning"
        )
    if normalized_broker != plan.broker_evidence:
        raise CutoverRefused("broker evidence changed after cutover planning")
    if current_roster != plan.runner_roster:
        raise CutoverRefused("durable stopped-bot roster changed after cutover planning")
    if current_legacy != plan.legacy_artifacts:
        raise CutoverRefused("legacy authority artifacts changed after cutover planning")

    cutover_id = f"g{plan.database.authority_generation}-{now}-{plan.plan_id[:12]}"
    quarantine_dir = account_dir / "legacy-quarantine" / cutover_id
    evidence_dir = (
        _cutover_evidence_directory(account_dir, create=False) / cutover_id
    )
    moved: list[tuple[Path, Path]] = []
    activation_attempted = False
    try:
        quarantine_dir.mkdir(parents=True, exist_ok=False)
        evidence_dir.mkdir(parents=True, exist_ok=False)
        fsync_directory_chain(quarantine_dir, account_dir)
        fsync_directory_chain(evidence_dir, account_dir)
        for artifact in current_legacy:
            source = confined_relative_path(artifacts_root, artifact.relative_path)
            destination = quarantine_dir / artifact.relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            fsync_directory_chain(destination.parent, account_dir)
            os.replace(source, destination)
            moved.append((source, destination))
            fsync_directory(source.parent)
            fsync_directory(destination.parent)
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
    if evidence.account_mode != "paper":
        raise CutoverRefused("cutover requires broker evidence from a paper account")
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
        account_mode=evidence.account_mode,
        observed_at_ms=evidence.observed_at_ms,
        proof_reference=evidence.proof_reference,
        positions=dict(sorted((symbol, float(qty)) for symbol, qty in evidence.positions.items())),
        open_order_ids=tuple(sorted(evidence.open_order_ids)),
    )


def _validate_cutover_safety(
    *,
    account_id: str,
    broker_evidence: BrokerCutoverEvidence,
    now_ms: int,
    max_broker_evidence_age_ms: int,
) -> None:
    _validate_cutover_broker_state(
        account_id=account_id,
        broker_evidence=broker_evidence,
    )
    _validate_cutover_evidence_freshness(
        broker_evidence=broker_evidence,
        now_ms=now_ms,
        max_broker_evidence_age_ms=max_broker_evidence_age_ms,
    )


def _validate_cutover_broker_state(
    *,
    account_id: str,
    broker_evidence: BrokerCutoverEvidence,
) -> None:
    """Require matching, finite, flat, and order-free broker state."""
    if broker_evidence.account_id != account_id:
        raise CutoverRefused("broker account identity does not match cutover account")
    if not broker_evidence.proof_reference:
        raise CutoverRefused("broker proof reference is required")
    from app.broker.alpaca.clerk.sqlite.folds import position_quantity_is_nonzero

    quantities = tuple(float(quantity) for quantity in broker_evidence.positions.values())
    if any(not math.isfinite(quantity) for quantity in quantities):
        raise CutoverRefused("broker position quantity must be finite")
    if any(position_quantity_is_nonzero(quantity) for quantity in quantities):
        raise CutoverRefused("cutover requires a broker-flat account")
    if broker_evidence.open_order_ids:
        raise CutoverRefused("cutover requires no open broker orders")


def _validate_cutover_evidence_freshness(
    *,
    broker_evidence: BrokerCutoverEvidence,
    now_ms: int,
    max_broker_evidence_age_ms: int,
) -> None:
    """Require broker evidence inside the operator's freshness window."""
    if (
        type(max_broker_evidence_age_ms) is not int
        or max_broker_evidence_age_ms < 1
        or broker_evidence.observed_at_ms > now_ms
    ):
        raise CutoverRefused("broker evidence timestamp or freshness policy is invalid")
    if now_ms - broker_evidence.observed_at_ms > max_broker_evidence_age_ms:
        raise CutoverRefused("broker evidence is stale")


def _runner_roster_evidence(
    runner_artifacts_root: Path,
    account_id: str,
    *,
    legacy_strategy_instance_ids: frozenset[str],
    allow_empty: bool = False,
) -> tuple[RunnerBotEvidence, ...]:
    """Bind the runner-owned semantic snapshot to complete artifact hashes."""
    try:
        roster = read_quiescent_alpaca_roster(
            runner_artifacts_root,
            account_id,
            legacy_strategy_instance_ids=legacy_strategy_instance_ids,
        )
    except CutoverRosterEvidenceError as exc:
        if allow_empty and str(exc).startswith("runner evidence contains no Alpaca bots"):
            return ()
        raise CutoverRefused(str(exc)) from exc
    evidence: list[RunnerBotEvidence] = []
    for bot in roster:
        try:
            artifact_sha256 = tree_sha256(bot.artifact_dir)
        except ValueError as exc:
            raise CutoverRefused(
                f"runner artifacts for {bot.strategy_instance_id!r} are unsafe: {exc}"
            ) from exc
        evidence.append(
            RunnerBotEvidence(
                strategy_instance_id=bot.strategy_instance_id,
                artifact_reference=relative_reference(
                    runner_artifacts_root, bot.artifact_dir
                ),
                artifact_sha256=artifact_sha256,
                mode=bot.mode,
                desired_state=bot.desired_state.value,
                lifecycle_phase=bot.lifecycle_phase.value,
                terminal_kind=bot.terminal_outcome.kind,
                terminal_reason_code=bot.terminal_outcome.reason_code,
                terminal_recorded_at_ms=bot.terminal_outcome.recorded_at_ms,
            )
        )
    return tuple(evidence)


def _legacy_artifact_evidence(
    artifacts_root: Path,
    account_id: str,
    *,
    allow_empty: bool = False,
) -> tuple[LegacyArtifactEvidence, ...]:
    _accounts_root, account_dir = writes.account_paths(artifacts_root, account_id)
    evidence: list[LegacyArtifactEvidence] = []
    candidates = [account_dir / name for name in LEGACY_ARTIFACT_NAMES]
    safe_account_id = safe_path_component(account_id, "account_id")
    candidates.append(
        _resolve_legacy_candidate_without_links(
            artifacts_root,
            "accounts",
            safe_account_id,
            LEGACY_UNSCOPED_BOT_DIRECTORY,
        )
    )
    for path in candidates:
        if path.is_symlink():
            raise CutoverRefused(
                f"legacy artifact {path.name!r} is a symbolic link"
            )
        if not path.exists():
            continue
        kind = "file" if path.is_file() else "directory" if path.is_dir() else "unsupported"
        if kind == "unsupported":
            raise CutoverRefused(
                f"legacy artifact {path.name!r} has an unsupported type"
            )
        evidence.append(
            LegacyArtifactEvidence(
                relative_path=relative_reference(artifacts_root, path),
                kind=kind,
                sha256=tree_sha256(path),
            )
        )
    if not evidence and not allow_empty:
        raise CutoverRefused(
            "no legacy authority artifacts were found; verify the Clerk artifacts root"
        )
    return tuple(sorted(evidence, key=lambda item: item.relative_path))


def _resolve_legacy_candidate_without_links(root: Path, *parts: str) -> Path:
    """Confine a legacy path only after rejecting each unresolved component."""
    candidate = root
    for part in parts:
        candidate /= part
        if candidate.is_symlink():
            raise CutoverRefused(
                f"legacy artifact path component {part!r} is a symbolic link"
            )
    return resolve_contained_path(root, *parts)


def _legacy_strategy_instance_ids(
    artifacts_root: Path,
    account_id: str,
) -> frozenset[str]:
    """Read account-scoped bot identities from both historical layouts."""
    _accounts_root, account_dir = writes.account_paths(artifacts_root, account_id)
    roots = (
        account_dir / LEGACY_UNSCOPED_BOT_DIRECTORY,
        _resolve_legacy_candidate_without_links(
            artifacts_root,
            "accounts",
            safe_path_component(account_id, "account_id"),
            LEGACY_UNSCOPED_BOT_DIRECTORY,
        ),
    )
    strategy_instance_ids: set[str] = set()
    for bots_root in roots:
        if bots_root.is_symlink():
            raise CutoverRefused("legacy bot roster must not be a symbolic link")
        if not bots_root.exists():
            continue
        if not bots_root.is_dir():
            raise CutoverRefused("legacy bot roster must be a regular directory")
        for instance_dir in sorted(bots_root.iterdir(), key=lambda path: path.name):
            if instance_dir.is_symlink() or not instance_dir.is_dir():
                raise CutoverRefused(
                    f"legacy bot roster entry {instance_dir.name!r} is unsafe"
                )
            try:
                strategy_instance_ids.add(
                    validate_strategy_instance_id(instance_dir.name)
                )
            except ValueError as exc:
                raise CutoverRefused(
                    f"legacy bot roster entry {instance_dir.name!r} is invalid: {exc}"
                ) from exc
    return frozenset(strategy_instance_ids)


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
        "initialization": asdict(plan.initialization),
        "database": asdict(plan.database),
        "broker_evidence": asdict(plan.broker_evidence),
        "runner_roster": [asdict(item) for item in plan.runner_roster],
        "legacy_artifacts": [asdict(item) for item in plan.legacy_artifacts],
    }
    expected = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    if plan.schema_version != 3 or plan.plan_id != expected or plan.confirmation_token != expected:
        raise CutoverRefused("cutover plan content hash does not verify")
    if not secrets.compare_digest(supplied_token, expected):
        raise CutoverRefused("cutover confirmation token does not match the plan")


def _rollback_quarantine(moved: list[tuple[Path, Path]]) -> None:
    for source, destination in reversed(moved):
        if destination.exists() and not source.exists():
            source.parent.mkdir(parents=True, exist_ok=True)
            os.replace(destination, source)
            fsync_directory(destination.parent)
            fsync_directory(source.parent)
