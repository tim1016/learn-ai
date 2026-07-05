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
        entries.append(
            StrategyValidationEntry(
                strategy_key=strategy.strategy_key,
                display_name=strategy.display_name,
                description=strategy.description,
                validation_state="validated",
                deployable=True,
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
                    notes=list(proof.notes),
                ),
            )
        )
    return entries


def load_strategy_validation_entries(
    registry: list[StrategyRegistrySeed],
    manifest_path: Path = DEFAULT_MANIFEST_PATH,
) -> list[StrategyValidationEntry]:
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("Failed to read strategy validation manifest: %s", exc)
        raise StrategyValidationManifestError("Strategy validation manifest unreadable") from exc

    evidence = [_evidence_seed_from_raw(item) for item in raw.get("validated_strategies", [])]
    return seed_strategy_validation_manifest(registry, evidence)


def reference_code_for_entry(entry: StrategyValidationEntry, *, repo_root: Path = _REPO_ROOT) -> StrategyReferenceCode | None:
    if entry.audit_copy_ref is None:
        return None
    path = _resolve_repo_path(repo_root, entry.audit_copy_ref)
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.error("Failed to read strategy audit copy %s: %s", entry.audit_copy_ref, exc)
        raise StrategyValidationManifestError("Strategy audit copy unreadable") from exc

    sha256 = _sha256(path)
    if entry.audit_copy_sha256 is not None and sha256 != entry.audit_copy_sha256:
        raise StrategyValidationManifestError("Strategy audit copy SHA mismatch")
    return StrategyReferenceCode(path=entry.audit_copy_ref, sha256=sha256, source=source)


class StrategyValidationManifestError(RuntimeError):
    pass


def _evidence_seed_from_raw(raw: dict[str, Any]) -> StrategyEvidenceSeed:
    diagnostics = raw.get("diagnostics") or {}
    return StrategyEvidenceSeed(
        strategy_key=str(raw["strategy_key"]),
        settings_file_ref=str(raw["settings_file_ref"]),
        settings_file_sha256=str(raw["settings_file_sha256"]),
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
        notes=list(diagnostics.get("notes") or []),
    )


def _validate_divergence_categories(counts: dict[str, int]) -> None:
    unknown = sorted(set(counts) - _DIVERGENCE_CATEGORY_VALUES)
    if unknown:
        joined = ", ".join(unknown)
        raise StrategyValidationManifestError(f"Unknown divergence categories in strategy manifest: {joined}")


def _resolve_repo_path(repo_root: Path, ref: str) -> Path:
    path = (repo_root / ref).resolve()
    path.relative_to(repo_root.resolve())
    return path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
