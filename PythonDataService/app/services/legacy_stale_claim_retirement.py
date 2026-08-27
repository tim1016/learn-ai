"""Read-only fold of legacy per-run sidecar exposure retirement receipts.

The operator-driven proof-and-retire ceremony this module used to serve
(LegacyStaleClaimRetirementService — discover, prove, and receipt one-time
legacy sidecar retirements against fresh IBKR Account Truth) was retired
along with the rest of IBKR account authority (PR-A of #1813, fix round 2,
2026-08-27): its sole HTTP surface (routers/account_reconciliation.py) and
both frontend callers were deleted by the same PR, leaving it with zero
production callers. What survives is exactly what
app.services.fleet_contamination.py's retired_legacy_claim_keys() call needs:
folding already-recorded retirement receipts (and their legacy pre-split
event-log equivalents) into the set of claim keys hidden from the legacy
fleet projection. No new receipt can be recorded through this module anymore.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from app.engine.live.account_artifacts import (
    AccountArtifactError,
    account_artifact_file_path,
    read_legacy_account_events,
)
from app.engine.live.live_state_sidecar import _file_lock
from app.schemas.account_reconciliation import LegacyStaleClaimRetirementReceipt
from app.schemas.artifact_io import atomic_write_pydantic_artifact

LEGACY_STALE_CLAIM_RETIRED_EVENT = "legacy_stale_claim_retired"
LEGACY_STALE_CLAIM_RETIREMENTS_FILENAME = "legacy_stale_claim_retirements.json"


class LegacyStaleClaimRetirementError(AccountArtifactError):
    """A proof required to retire a legacy claim is absent or contradictory."""

    def __init__(self, reason_code: str, detail: str) -> None:
        super().__init__(f"{reason_code}: {detail}")
        self.reason_code = reason_code
        self.detail = detail


class _LegacyStaleClaimRetirementsArtifact(BaseModel):
    """Typed retirement receipts that control legacy sidecar exclusion."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    account_id: str = Field(min_length=1, max_length=64)
    receipts: tuple[LegacyStaleClaimRetirementReceipt, ...] = ()
    legacy_claim_keys: tuple[tuple[str, str, str, str], ...] = ()


def retired_legacy_claim_keys(
    artifacts_root: Path,
    account_id: str,
) -> frozenset[tuple[str, str, str, str]]:
    """Fold receipt events into the exact claims hidden from legacy sidecar sums."""

    path = _retirement_artifact_path(artifacts_root, account_id)
    with _file_lock(path):
        artifact = _read_or_migrate_retirement_artifact_locked(artifacts_root, account_id, path)
    return frozenset(
        (*(_retired_claim_key(receipt) for receipt in artifact.receipts), *artifact.legacy_claim_keys)
    )


def _legacy_retired_claim_keys(
    artifacts_root: Path,
    account_id: str,
) -> tuple[tuple[str, str, str, str], ...]:
    """Extract only valid immutable pre-split retirement identities once."""

    keys: set[tuple[str, str, str, str]] = set()
    for event in read_legacy_account_events(artifacts_root, account_id):
        if event.get("event_type") != LEGACY_STALE_CLAIM_RETIRED_EVENT:
            continue
        values = (
            event.get("strategy_instance_id"),
            event.get("run_id"),
            event.get("symbol"),
            event.get("bot_order_namespace"),
        )
        if all(isinstance(value, str) and value for value in values):
            strategy_instance_id, run_id, symbol, namespace = values
            keys.add((strategy_instance_id, run_id, symbol.upper(), namespace))
    return tuple(sorted(keys))


def _retirement_artifact_path(artifacts_root: Path, account_id: str) -> Path:
    return account_artifact_file_path(
        artifacts_root,
        account_id,
        LEGACY_STALE_CLAIM_RETIREMENTS_FILENAME,
    )


def _read_retirement_artifact(
    path: Path,
    account_id: str,
) -> _LegacyStaleClaimRetirementsArtifact:
    try:
        artifact = _LegacyStaleClaimRetirementsArtifact.model_validate_json(
            path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        raise LegacyStaleClaimRetirementError(
            "LEGACY_CLAIM_RETIREMENT_ARTIFACT_UNREADABLE",
            "The typed legacy-claim retirement artifact cannot be read.",
        ) from exc
    if artifact.account_id != account_id:
        raise LegacyStaleClaimRetirementError(
            "LEGACY_CLAIM_RETIREMENT_ARTIFACT_ACCOUNT_MISMATCH",
            "The typed legacy-claim retirement artifact belongs to another account.",
        )
    return artifact


def _read_or_migrate_retirement_artifact_locked(
    artifacts_root: Path,
    account_id: str,
    path: Path,
) -> _LegacyStaleClaimRetirementsArtifact:
    """Materialize the compatibility snapshot, including an empty marker, once."""

    if path.exists():
        return _read_retirement_artifact(path, account_id)
    artifact = _LegacyStaleClaimRetirementsArtifact(
        account_id=account_id,
        legacy_claim_keys=_legacy_retired_claim_keys(artifacts_root, account_id),
    )
    atomic_write_pydantic_artifact(path, artifact)
    return artifact


def _retired_claim_key(receipt: LegacyStaleClaimRetirementReceipt) -> tuple[str, str, str, str]:
    return (
        receipt.strategy_instance_id,
        receipt.run_id,
        receipt.symbol.upper(),
        receipt.bot_order_namespace,
    )


__all__ = [
    "LEGACY_STALE_CLAIM_RETIRED_EVENT",
    "LegacyStaleClaimRetirementError",
    "retired_legacy_claim_keys",
]
