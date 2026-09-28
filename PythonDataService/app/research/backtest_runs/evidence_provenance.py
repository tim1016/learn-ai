"""Producer-authored conventions and their current applicability (#2544).

Dates, program versions, and metric catalog IDs cannot establish these facts:
B3/B4 changed conventions without changing those identifiers. Missing metadata
is unknown. This assessment never rewrites historical data or metric values.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class RunEvidenceProvenance(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    data_contract: str
    statistics_basis: str
    daily_return_convention: str
    data_availability_hash: str | None = None


class EvidenceApplicability(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["current", "affected", "unknown"]
    affected_issues: tuple[str, ...] = ()
    explanation: str
    requires_manual_override: bool


def assess_evidence_provenance(provenance: object) -> EvidenceApplicability:
    """Classify only positively recorded conventions; absence proves no defect."""
    values = provenance.model_dump() if isinstance(provenance, RunEvidenceProvenance) else provenance
    if not isinstance(values, dict) or values.get("schema_version") != 1:
        values = {}
    affected = tuple(
        issue for field, old_value, issue in (
            ("data_contract", "spec_local_zip_partial/v1", "#2445/#2446"),
            ("statistics_basis", "paired_realized_trade_ledger/v1", "#2447"),
            ("daily_return_convention", "platform_skip_first_session/v1", "#2448"),
        ) if values.get(field) == old_value
    )
    if affected:
        return EvidenceApplicability(
            status="affected",
            affected_issues=affected,
            explanation=(
                "Recorded conventions identify evidence affected by " + ", ".join(affected)
                + ". Rerun the saved configuration, compare the new results, then explicitly review a new Golden case. "
                "A deliberate Manual override may accept the recorded limitation; it is not profitability certification."
            ),
            requires_manual_override=True,
        )
    current = (
        values.get("data_contract") in {"lake_complete_sessions/v1", "fixture_identity/v1"}
        and values.get("statistics_basis") == "marked_equity_curve/v1"
        and values.get("daily_return_convention") == "initial_capital_first_session/v1"
    )
    return EvidenceApplicability(
        status="current" if current else "unknown",
        explanation=(
            "The recorded data and headline metric conventions use the corrected producer paths. "
            "Human acceptance and all independent deployment checks still apply."
            if current else
            "This record does not establish all data and metric conventions. It is unknown, not known affected. "
            "Existing acceptance is preserved; a new acceptance requires a deliberate Manual override, or rerun "
            "the saved configuration and review its new evidence."
        ),
        requires_manual_override=not current,
    )
