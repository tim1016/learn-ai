"""Producer-authored conventions and their current applicability (#2544).

Dates, program versions, and metric catalog IDs cannot establish these facts:
B3/B4 changed conventions without changing those identifiers. Missing metadata
is unknown. This assessment never rewrites historical data or metric values.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.lean_sidecar.closing_bar import ClosingBarConvention
from app.utils.session_anchors import MAX_TIMESTAMP_MS


class ClosingBarSkipRecord(BaseModel):
    """One decision a run's closing-bar convention set aside (#2607).

    An ENTER produced no trade. An EXIT stayed due and filled on the next
    session's first decision instead of at this bar's close.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    bar_close_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    intent: Literal["ENTER", "EXIT"]
    close_price: float


class RunEvidenceProvenance(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    data_contract: str
    statistics_basis: str
    daily_return_convention: str
    data_availability_hash: str | None = None
    # How the run settled a Signal Program decision on the session's closing
    # bar (#2607), and every decision that convention set aside. Absent on
    # evidence recorded before #2607, whose non-LEAN runs filled such a
    # decision at that bar's close.
    closing_bar_convention: str | None = None
    closing_bar_skips: tuple[ClosingBarSkipRecord, ...] = ()


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
        and values.get("closing_bar_convention") in {convention.value for convention in ClosingBarConvention}
    )
    return EvidenceApplicability(
        status="current" if current else "unknown",
        explanation=(
            "The recorded data, headline metric and closing-bar conventions use the corrected producer paths. "
            "Human acceptance and all independent deployment checks still apply."
            if current else
            "This record does not establish all data, metric and closing-bar conventions. It is unknown, not known "
            "affected. Existing acceptance is preserved; a new acceptance requires a deliberate Manual override, or "
            "rerun the saved configuration and review its new evidence."
        ),
        requires_manual_override=not current,
    )
