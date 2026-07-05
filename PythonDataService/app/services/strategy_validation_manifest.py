from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.research.parity.qc_reconciler import DivergenceCategory
from app.schemas.strategy_validation import (
    StrategyReferenceCode,
    StrategyValidationDiagnostics,
    StrategyValidationEntry,
)

logger = logging.getLogger(__name__)

_SERVICE_ROOT = Path(__file__).resolve().parents[2]
_REPO_ROOT = _SERVICE_ROOT.parent
DEFAULT_MANIFEST_PATH = _SERVICE_ROOT / "app" / "data" / "strategy_validation_manifest.json"

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
    notes: list[str] = field(default_factory=list)


def seed_strategy_validation_manifest(
    registry: list[StrategyRegistrySeed],
    evidence: list[StrategyEvidenceSeed],
) -> list[StrategyValidationEntry]:
    evidence_by_strategy = {item.strategy_key: item for item in evidence}
    entries: list[StrategyValidationEntry] = []
    for strategy in registry:
        proof = evidence_by_strategy.get(strategy.strategy_key)
        if proof is None:
            entries.append(
                StrategyValidationEntry(
                    strategy_key=strategy.strategy_key,
                    display_name=strategy.display_name,
                    description=strategy.description,
                    validation_state="needs_validation",
                    deployable=False,
                )
            )
            continue

        _validate_divergence_categories(proof.divergence_counts)
        deployable = _evidence_is_deployable(proof)
        notes = list(proof.notes)
        if not deployable:
            notes.extend(_validation_failure_notes(proof))
        entries.append(
            StrategyValidationEntry(
                strategy_key=strategy.strategy_key,
                display_name=strategy.display_name,
                description=strategy.description,
                validation_state="validated" if deployable else "needs_validation",
                deployable=deployable,
                settings_file_ref=proof.settings_file_ref,
                settings_file_sha256=proof.settings_file_sha256,
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
                    notes=notes,
                ),
            )
        )
    return entries


def load_strategy_validation_entries(
    registry: list[StrategyRegistrySeed],
    manifest_path: Path = DEFAULT_MANIFEST_PATH,
    repo_root: Path = _REPO_ROOT,
) -> list[StrategyValidationEntry]:
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("Failed to read strategy validation manifest: %s", exc)
        raise StrategyValidationManifestError("Strategy validation manifest unreadable") from exc

    evidence = [
        _evidence_seed_from_raw(item, repo_root=repo_root)
        for item in raw.get("validated_strategies", [])
    ]
    return seed_strategy_validation_manifest(registry, evidence)


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


def _evidence_seed_from_raw(raw: dict[str, Any], *, repo_root: Path) -> StrategyEvidenceSeed:
    diagnostics = raw.get("diagnostics") or {}
    settings_file_ref = str(raw["settings_file_ref"])
    settings_file_sha256 = str(raw["settings_file_sha256"])
    return StrategyEvidenceSeed(
        strategy_key=str(raw["strategy_key"]),
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
        settings_file_verified=_ref_matches_sha256(
            repo_root,
            settings_file_ref,
            settings_file_sha256,
        ),
        notes=list(diagnostics.get("notes") or []),
    )


def _evidence_is_deployable(proof: StrategyEvidenceSeed) -> bool:
    return (
        proof.reconciliation_status == "passed"
        and proof.verdict == "passed"
        and proof.settings_file_verified
    )


def _validation_failure_notes(proof: StrategyEvidenceSeed) -> list[str]:
    notes: list[str] = []
    if proof.reconciliation_status != "passed":
        notes.append(f"Reconciliation status is {proof.reconciliation_status}; deployability requires passed.")
    if proof.verdict != "passed":
        notes.append(f"Diagnostics verdict is {proof.verdict}; deployability requires passed.")
    if not proof.settings_file_verified:
        notes.append("Settings file hash no longer matches the validation manifest.")
    return notes


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
