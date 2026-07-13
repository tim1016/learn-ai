from __future__ import annotations

import fcntl
import getpass
import hashlib
import hmac
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.research.parity.qc_reconciler import DivergenceCategory
from app.schemas.strategy_validation import (
    StrategyBehavioralEquivalence,
    StrategyEvidenceSnapshot,
    StrategyReferenceCode,
    StrategyValidationDetail,
    StrategyValidationDiagnostics,
    StrategyValidationEntry,
    StrategyValidationFlagEvent,
    StrategyValidationFlagRequest,
    StrategyValidationRefreshResult,
)

logger = logging.getLogger(__name__)

_SERVICE_ROOT = Path(__file__).resolve().parents[2]
_REPO_ROOT = _SERVICE_ROOT.parent
DEFAULT_MANIFEST_PATH = _SERVICE_ROOT / "app" / "data" / "strategy_validation_manifest.json"
DEFAULT_FLAG_EVENTS_PATH = _SERVICE_ROOT / "artifacts" / "strategy_validation" / "flag_events.json"

_DIVERGENCE_CATEGORY_VALUES = {category.value for category in DivergenceCategory}


@dataclass(frozen=True)
class StrategyRegistrySeed:
    strategy_key: str
    display_name: str
    description: str


@dataclass(frozen=True)
class StrategyEvidenceSeed:
    strategy_key: str
    settings_file_ref: str
    settings_file_sha256: str
    qc_cloud_backtest_id: str
    audit_copy_ref: str
    audit_copy_sha256: str
    reconciliation_ref: str
    validation_case_symbol: str
    trades_matched: int
    trades_validated: int
    pnl_max_abs_diff: str
    divergence_counts: dict[str, int] = field(default_factory=dict)
    verdict: str = "passed"
    reconciliation_status: str = "passed"
    settings_file_verified: bool = True
    validator_code_ref: str | None = None
    validator_code_sha256: str | None = None
    notes: list[str] = field(default_factory=list)


def strategy_registry_seeds() -> list[StrategyRegistrySeed]:
    from app.engine.strategy.registry import _STRATEGY_REGISTRY

    return [
        StrategyRegistrySeed(
            strategy_key=key,
            display_name=registration.display_name,
            description=registration.description,
        )
        for key, registration in sorted(_STRATEGY_REGISTRY.items())
    ]


def seed_strategy_validation_manifest(
    registry: list[StrategyRegistrySeed],
    evidence: list[StrategyEvidenceSeed],
    flag_events: list[StrategyValidationFlagEvent] | None = None,
) -> list[StrategyValidationEntry]:
    evidence_by_strategy = {item.strategy_key: item for item in evidence}
    events_by_strategy = _events_by_strategy(flag_events or [])
    entries: list[StrategyValidationEntry] = []
    for strategy in registry:
        proof = evidence_by_strategy.get(strategy.strategy_key)
        events = events_by_strategy.get(strategy.strategy_key, [])
        current_event = _current_flag_event(events)
        if proof is None:
            entries.append(
                StrategyValidationEntry(
                    strategy_key=strategy.strategy_key,
                    display_name=strategy.display_name,
                    description=strategy.description,
                    validation_state=_validation_state_for_event(current_event),
                    deployable=False,
                    behavioral_equivalence=current_event.behavioral_equivalence if current_event else None,
                    current_flag_event=current_event,
                    flag_events=events,
                )
            )
            continue

        _validate_divergence_categories(proof.divergence_counts)
        qc_cloud_backtest_id = (
            current_event.evidence_snapshot.qc_cloud_backtest_id
            if current_event is not None and current_event.evidence_snapshot.qc_cloud_backtest_id
            else proof.qc_cloud_backtest_id
        )
        evidence_deployable = _evidence_is_deployable(proof) and bool(qc_cloud_backtest_id)
        deployable = _event_accepts_deploy(current_event) and evidence_deployable
        notes = list(proof.notes)
        if not evidence_deployable:
            notes.extend(_validation_failure_notes(proof))
        entries.append(
            StrategyValidationEntry(
                strategy_key=strategy.strategy_key,
                display_name=strategy.display_name,
                description=strategy.description,
                validation_state=_validation_state_for_event(current_event),
                deployable=deployable,
                validator_code_ref=proof.validator_code_ref,
                validator_code_sha256=proof.validator_code_sha256,
                settings_file_ref=proof.settings_file_ref,
                settings_file_sha256=proof.settings_file_sha256,
                qc_cloud_backtest_id=qc_cloud_backtest_id,
                audit_copy_ref=proof.audit_copy_ref,
                audit_copy_sha256=proof.audit_copy_sha256,
                reconciliation_ref=proof.reconciliation_ref,
                validation_case_symbol=proof.validation_case_symbol,
                reconciliation_status=proof.reconciliation_status,
                diagnostics=StrategyValidationDiagnostics(
                    verdict=proof.verdict,
                    trades_matched=proof.trades_matched,
                    trades_validated=proof.trades_validated,
                    pnl_max_abs_diff=proof.pnl_max_abs_diff,
                    divergence_counts=dict(proof.divergence_counts),
                    notes=notes,
                ),
                behavioral_equivalence=current_event.behavioral_equivalence if current_event else None,
                current_flag_event=current_event,
                flag_events=events,
            )
        )
    return entries


def load_strategy_validation_entries(
    registry: list[StrategyRegistrySeed],
    manifest_path: Path = DEFAULT_MANIFEST_PATH,
    flag_events_path: Path = DEFAULT_FLAG_EVENTS_PATH,
    repo_root: Path = _REPO_ROOT,
) -> list[StrategyValidationEntry]:
    raw = _load_manifest_raw(manifest_path)

    evidence = [
        _evidence_seed_from_raw(item, repo_root=repo_root)
        for item in raw.get("validated_strategies", [])
    ]
    seed_events = _flag_events_from_raw(raw.get("seed_flag_events", raw.get("flag_events", [])))
    runtime_events = _load_runtime_flag_events(flag_events_path)
    flag_events = [*seed_events, *runtime_events]
    return seed_strategy_validation_manifest(registry, evidence, flag_events)


def append_strategy_validation_flag_event(
    strategy_key: str,
    request: StrategyValidationFlagRequest,
    registry: list[StrategyRegistrySeed],
    *,
    manifest_path: Path = DEFAULT_MANIFEST_PATH,
    flag_events_path: Path = DEFAULT_FLAG_EVENTS_PATH,
    repo_root: Path = _REPO_ROOT,
    flagged_by: str,
    now_ms: int | None = None,
) -> StrategyValidationEntry:
    raw = _load_manifest_raw(manifest_path)
    evidence = [
        _evidence_seed_from_raw(item, repo_root=repo_root)
        for item in raw.get("validated_strategies", [])
    ]
    proof_by_strategy = {item.strategy_key: item for item in evidence}
    if not any(seed.strategy_key == strategy_key for seed in registry):
        raise StrategyValidationNotFoundError(strategy_key)

    proof = proof_by_strategy.get(strategy_key)
    if proof is not None:
        _validate_divergence_categories(proof.divergence_counts)
    current_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    snapshot = _snapshot_for_proof(proof)
    if request.qc_cloud_backtest_id is not None:
        snapshot = snapshot.model_copy(
            update={"qc_cloud_backtest_id": request.qc_cloud_backtest_id}
        )
    event = StrategyValidationFlagEvent(
        event_id=uuid.uuid4().hex,
        strategy_key=strategy_key,
        flag=request.flag,
        flagged_by=flagged_by,
        flagged_at_ms=current_ms,
        reason=request.reason,
        behavioral_equivalence=_behavioral_equivalence_for_flag(
            request.flag,
            proof,
            qc_cloud_backtest_id=snapshot.qc_cloud_backtest_id,
        ),
        evidence_snapshot=snapshot,
        evidence_snapshot_sha256=_snapshot_sha256(snapshot),
    )

    _append_runtime_flag_event(flag_events_path, event)

    entries = load_strategy_validation_entries(
        registry,
        manifest_path=manifest_path,
        flag_events_path=flag_events_path,
        repo_root=repo_root,
    )
    return _entry_by_strategy(entries, strategy_key)


def refresh_strategy_validation_manifest_evidence(
    strategy_key: str,
    registry: list[StrategyRegistrySeed],
    *,
    manifest_path: Path = DEFAULT_MANIFEST_PATH,
    flag_events_path: Path = DEFAULT_FLAG_EVENTS_PATH,
    repo_root: Path = _REPO_ROOT,
    now_ms: int | None = None,
) -> StrategyValidationRefreshResult:
    entries = load_strategy_validation_entries(
        registry,
        manifest_path=manifest_path,
        flag_events_path=flag_events_path,
        repo_root=repo_root,
    )
    entry = _entry_by_strategy(entries, strategy_key)
    detail = StrategyValidationDetail(
        **entry.model_dump(),
        reference_code=reference_code_for_entry(entry, repo_root=repo_root),
    )
    refreshed_at_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    return StrategyValidationRefreshResult(
        refresh_id=f"manifest-evidence:{strategy_key}:{refreshed_at_ms}",
        refreshed_at_ms=refreshed_at_ms,
        detail=detail,
    )


def local_strategy_validation_actor() -> str:
    try:
        username = getpass.getuser().strip()
    except (OSError, RuntimeError):
        username = ""
    return f"local:{username}" if username else "local:unknown"


def reference_code_for_entry(entry: StrategyValidationEntry, *, repo_root: Path = _REPO_ROOT) -> StrategyReferenceCode | None:
    if entry.audit_copy_ref is None:
        return None
    path = _resolve_project_ref(repo_root, entry.audit_copy_ref)
    try:
        source_bytes = path.read_bytes()
        source = source_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        logger.error("Failed to read strategy audit copy %s: %s", entry.audit_copy_ref, exc)
        raise StrategyValidationManifestError("Strategy audit copy unreadable") from exc

    sha256 = _sha256_bytes(source_bytes)
    if entry.audit_copy_sha256 is not None and sha256 != entry.audit_copy_sha256:
        raise StrategyValidationManifestError("Strategy audit copy SHA mismatch")
    return StrategyReferenceCode(path=entry.audit_copy_ref, sha256=sha256, source=source)


class StrategyValidationManifestError(RuntimeError):
    pass


class StrategyValidationNotFoundError(RuntimeError):
    pass


def _load_manifest_raw(manifest_path: Path) -> dict[str, Any]:
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("Failed to read strategy validation manifest: %s", exc)
        raise StrategyValidationManifestError("Strategy validation manifest unreadable") from exc


def _load_runtime_flag_events(flag_events_path: Path) -> list[StrategyValidationFlagEvent]:
    if not flag_events_path.exists():
        return []
    raw = _load_flag_event_log_raw(flag_events_path)
    return _flag_events_from_raw(raw.get("flag_events", []))


def _load_flag_event_log_raw(flag_events_path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(flag_events_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("Failed to read strategy validation flag event log: %s", exc)
        raise StrategyValidationManifestError("Strategy validation flag event log unreadable") from exc
    if isinstance(raw, list):
        return {"schema_version": "1.0", "flag_events": raw}
    if not isinstance(raw, dict):
        raise StrategyValidationManifestError("Strategy validation flag event log must be an object")
    return raw


def _write_json_atomic(path: Path, raw: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp_path.write_text(
            json.dumps(raw, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        tmp_path.replace(path)
    except OSError as exc:
        logger.error("Failed to write strategy validation JSON artifact: %s", exc)
        raise StrategyValidationManifestError("Strategy validation JSON artifact unwritable") from exc
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def _append_runtime_flag_event(
    flag_events_path: Path,
    event: StrategyValidationFlagEvent,
) -> None:
    flag_events_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = flag_events_path.with_suffix(f"{flag_events_path.suffix}.lock")
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        raw = (
            _load_flag_event_log_raw(flag_events_path)
            if flag_events_path.exists()
            else {"schema_version": "1.0", "flag_events": []}
        )
        raw.setdefault("schema_version", "1.0")
        raw.setdefault("flag_events", [])
        if not isinstance(raw["flag_events"], list):
            raise StrategyValidationManifestError("Strategy validation flag_events must be a list")
        raw["flag_events"].append(event.model_dump())
        _write_json_atomic(flag_events_path, raw)
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _flag_events_from_raw(raw_events: Any) -> list[StrategyValidationFlagEvent]:
    if raw_events is None:
        return []
    if not isinstance(raw_events, list):
        raise StrategyValidationManifestError("Strategy validation flag_events must be a list")
    try:
        events = [StrategyValidationFlagEvent.model_validate(item) for item in raw_events]
    except ValidationError as exc:
        logger.error("Invalid strategy validation flag event: %s", exc)
        raise StrategyValidationManifestError("Strategy validation flag event invalid") from exc
    for event in events:
        _verify_flag_event_snapshot_hash(event)
    return events


def _verify_flag_event_snapshot_hash(event: StrategyValidationFlagEvent) -> None:
    actual = _snapshot_sha256(event.evidence_snapshot)
    if not hmac.compare_digest(actual, event.evidence_snapshot_sha256):
        raise StrategyValidationManifestError("Strategy validation flag event snapshot SHA mismatch")


def _events_by_strategy(
    events: list[StrategyValidationFlagEvent],
) -> dict[str, list[StrategyValidationFlagEvent]]:
    grouped: dict[str, list[StrategyValidationFlagEvent]] = {}
    for event in events:
        grouped.setdefault(event.strategy_key, []).append(event)
    return grouped


def _current_flag_event(events: list[StrategyValidationFlagEvent]) -> StrategyValidationFlagEvent | None:
    active_events = [
        (index, event)
        for index, event in enumerate(events)
        if event.superseded_by_event_id is None
    ]
    if not active_events:
        return None
    return max(active_events, key=lambda item: (item[1].flagged_at_ms, item[0]))[1]


def _validation_state_for_event(event: StrategyValidationFlagEvent | None) -> str:
    return "validated" if event is not None and event.flag == "validated" else "needs_validation"


def _event_accepts_deploy(event: StrategyValidationFlagEvent | None) -> bool:
    return (
        event is not None
        and event.flag == "validated"
        and event.behavioral_equivalence.verdict == "accepted_for_deploy"
    )


def _snapshot_for_proof(proof: StrategyEvidenceSeed | None) -> StrategyEvidenceSnapshot:
    if proof is None:
        return StrategyEvidenceSnapshot()
    return StrategyEvidenceSnapshot(
        settings_file_ref=proof.settings_file_ref,
        settings_file_sha256=proof.settings_file_sha256,
        validator_code_ref=proof.validator_code_ref,
        validator_code_sha256=proof.validator_code_sha256,
        qc_cloud_backtest_id=proof.qc_cloud_backtest_id,
        audit_copy_ref=proof.audit_copy_ref,
        audit_copy_sha256=proof.audit_copy_sha256,
        reconciliation_ref=proof.reconciliation_ref,
        validation_case_symbol=proof.validation_case_symbol,
        reconciliation_status=proof.reconciliation_status,
        diagnostics=StrategyValidationDiagnostics(
            verdict=proof.verdict,
            trades_matched=proof.trades_matched,
            trades_validated=proof.trades_validated,
            pnl_max_abs_diff=proof.pnl_max_abs_diff,
            divergence_counts=dict(proof.divergence_counts),
            notes=list(proof.notes),
        ),
    )


def _snapshot_sha256(snapshot: StrategyEvidenceSnapshot) -> str:
    payload = json.dumps(
        _snapshot_hash_payload(snapshot),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return _sha256_bytes(payload)


def _snapshot_hash_payload(snapshot: StrategyEvidenceSnapshot) -> dict[str, Any]:
    payload = snapshot.model_dump()
    if (
        snapshot.validator_code_ref is None
        and snapshot.validator_code_sha256 is None
    ):
        payload.pop("validator_code_ref", None)
        payload.pop("validator_code_sha256", None)
    return payload


def _behavioral_equivalence_for_flag(
    flag: str,
    proof: StrategyEvidenceSeed | None,
    *,
    qc_cloud_backtest_id: str | None = None,
) -> StrategyBehavioralEquivalence:
    if flag == "invalidated":
        return StrategyBehavioralEquivalence(
            verdict="rejected",
            detail="Human validation rejected this strategy for deployment.",
            tolerance_reason="Rejected human flag events are never deployable.",
            gating_divergence_counts=_gating_divergence_counts(proof),
        )
    if proof is None:
        return StrategyBehavioralEquivalence(
            verdict="evidence_only",
            detail="Human validation recorded without a registered engine evidence snapshot.",
            tolerance_reason="No registered evidence snapshot is available for a deployability decision.",
        )
    if _evidence_is_deployable(proof) and bool(qc_cloud_backtest_id or proof.qc_cloud_backtest_id):
        return StrategyBehavioralEquivalence(
            verdict="accepted_for_deploy",
            detail="Human validation accepted the current engine evidence for deployment.",
                tolerance="manifest_reconciliation_passed",
                tolerance_reason=(
                    "Registered reconciliation status and diagnostics verdict are passed; "
                    "the manifest LEAN validator hash and deploy binding hash also match "
                    "the current source."
                ),
            gating_divergence_counts=_gating_divergence_counts(proof),
        )
    missing_id_reason = (
        " A QC Cloud backtest ID is also required."
        if not (qc_cloud_backtest_id or proof.qc_cloud_backtest_id)
        else ""
    )
    return StrategyBehavioralEquivalence(
        verdict="evidence_only",
        detail="Human validation recorded, but engine evidence is not deployable.",
        tolerance="manifest_reconciliation_not_accepted",
        tolerance_reason=(" ".join(_validation_failure_notes(proof)) + missing_id_reason).strip(),
        gating_divergence_counts=_gating_divergence_counts(proof),
    )


def _gating_divergence_counts(proof: StrategyEvidenceSeed | None) -> dict[str, int]:
    return dict(proof.divergence_counts) if proof is not None else {}


def _entry_by_strategy(
    entries: list[StrategyValidationEntry],
    strategy_key: str,
) -> StrategyValidationEntry:
    entry = next((item for item in entries if item.strategy_key == strategy_key), None)
    if entry is None:
        raise StrategyValidationNotFoundError(strategy_key)
    return entry


def _evidence_seed_from_raw(raw: dict[str, Any], *, repo_root: Path) -> StrategyEvidenceSeed:
    diagnostics = raw.get("diagnostics") or {}
    validator_code_ref = _optional_manifest_string(raw.get("validator_code_ref"))
    validator_code_sha256 = _optional_manifest_string(raw.get("validator_code_sha256"))
    settings_file_ref = str(raw["settings_file_ref"])
    settings_file_sha256 = str(raw["settings_file_sha256"])
    settings_file_verified = _ref_matches_sha256(
        repo_root,
        settings_file_ref,
        settings_file_sha256,
    )
    validator_code_verified = (
        validator_code_ref is not None
        and validator_code_sha256 is not None
        and _ref_matches_sha256(repo_root, validator_code_ref, validator_code_sha256)
    )
    return StrategyEvidenceSeed(
        strategy_key=str(raw["strategy_key"]),
        validator_code_ref=validator_code_ref,
        validator_code_sha256=validator_code_sha256,
        settings_file_ref=settings_file_ref,
        settings_file_sha256=settings_file_sha256,
        qc_cloud_backtest_id=str(raw["qc_cloud_backtest_id"]),
        audit_copy_ref=str(raw["audit_copy_ref"]),
        audit_copy_sha256=str(raw["audit_copy_sha256"]),
        reconciliation_ref=str(raw["reconciliation_ref"]),
        validation_case_symbol=str(raw["validation_case_symbol"]),
        trades_matched=int(diagnostics.get("trades_matched", 0)),
        trades_validated=int(diagnostics.get("trades_validated", 0)),
        pnl_max_abs_diff=str(diagnostics.get("pnl_max_abs_diff", "")),
        divergence_counts=dict(diagnostics.get("divergence_counts") or {}),
        verdict=str(diagnostics.get("verdict", "passed")),
        reconciliation_status=str(raw.get("reconciliation_status", "passed")),
        settings_file_verified=settings_file_verified and validator_code_verified,
        notes=list(diagnostics.get("notes") or []),
    )


def _evidence_is_deployable(proof: StrategyEvidenceSeed) -> bool:
    return (
        proof.reconciliation_status == "passed"
        and proof.verdict == "passed"
        and proof.settings_file_verified
        and proof.validator_code_ref is not None
        and proof.validator_code_sha256 is not None
    )


def _validation_failure_notes(proof: StrategyEvidenceSeed) -> list[str]:
    notes: list[str] = []
    if proof.reconciliation_status != "passed":
        notes.append(f"Reconciliation status is {proof.reconciliation_status}; deployability requires passed.")
    if proof.verdict != "passed":
        notes.append(f"Diagnostics verdict is {proof.verdict}; deployability requires passed.")
    if proof.validator_code_ref is None or proof.validator_code_sha256 is None:
        notes.append("LEAN validator evidence is missing; deployability requires an explicit validator binding.")
    if not proof.settings_file_verified:
        notes.append("Validator/deploy binding hash no longer matches the validation manifest.")
    return notes


def _optional_manifest_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _validate_divergence_categories(counts: dict[str, int]) -> None:
    unknown = sorted(set(counts) - _DIVERGENCE_CATEGORY_VALUES)
    if unknown:
        joined = ", ".join(unknown)
        raise StrategyValidationManifestError(f"Unknown divergence categories in strategy manifest: {joined}")


def _ref_matches_sha256(repo_root: Path, ref: str, expected_sha256: str) -> bool:
    try:
        return _sha256(_resolve_project_ref(repo_root, ref)) == expected_sha256
    except (OSError, ValueError) as exc:
        logger.warning("Failed to verify strategy validation ref %s: %s", ref, exc)
        return False


def _resolve_project_ref(repo_root: Path, ref: str) -> Path:
    root = repo_root.resolve()
    primary = (root / ref).resolve()
    primary.relative_to(root)
    if primary.exists():
        return primary

    service_fallback = _service_ref_fallback(ref)
    if service_fallback is not None:
        return service_fallback
    return primary


def _service_ref_fallback(ref: str) -> Path | None:
    if ref.startswith("PythonDataService/"):
        path = (_SERVICE_ROOT / ref.removeprefix("PythonDataService/")).resolve()
        path.relative_to(_SERVICE_ROOT.resolve())
        return path
    if ref.startswith("references/qc-shadow/"):
        path = (
            _SERVICE_ROOT
            / "app"
            / "data"
            / "qc-shadow"
            / ref.removeprefix("references/qc-shadow/")
        ).resolve()
        path.relative_to((_SERVICE_ROOT / "app" / "data" / "qc-shadow").resolve())
        return path
    return None


def _sha256(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
