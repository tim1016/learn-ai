"""Named strategy-catalog module (#1703, PRD #1697 S6 — "catalog honesty").

Composes one strategy-catalog entity from exactly two facets:

- **definition** — the canonical engine registry
  (``app.engine.strategy.registry._STRATEGY_REGISTRY``), filtered to
  entries flagged ``catalog_visible=True``.
- **validation state** — the append-only flag-event log, read through
  ``StrategyValidationEntry`` (owned by ``app.services.strategy_validation_manifest``,
  untouched by this module).

This is an extraction of the evidence-disposition derivation that used to
be inlined in ``paper_deploy_service._strategy_views`` (#1698 stale-proof
demotion, #1706 evidence-only override), plus the new runtime annotation
(#1703): a validated strategy with no registered live-decision runtime is
composed as a visible, non-selectable row — reusing ``blocked_explanation``
with wording distinct from a stale proof — rather than silently dropped.

Storage, run history, and per-symbol facets are explicitly excluded: those
remain owned by ``bot_binding_repository`` / ``bot_registry_projection``.
The SQLite custody layer's display-name resolution
(``sqlite_panel_source.py``, ``app/broker/alpaca/clerk/sqlite/*``) is a
read-only consumer of a different, storage-scoped catalog
(``catalog_projection_service.py`` / ``sqlite_panel_adapter.py``) and is
not touched here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.schemas.strategy_validation import StrategyValidationEntry, StrategyValidationFlagEvent
from app.services import canary_admission
from app.services.bot_trade_strategy import (
    alpaca_paper_strategy_default_symbol,
    supported_alpaca_paper_strategy_keys,
)
from app.services.strategy_validation_manifest import (
    strategy_audit_copy_is_current,
    strategy_settings_file_is_current,
    strategy_validator_code_is_current,
)
from app.services.strategy_validation_policy import strategy_validation_policy

EvidenceStatus = Literal["accepted", "evidence_only", "blocked"]
PaperAccessState = Literal["not_required", "blocked", "available", "enabled"]

logger = logging.getLogger(__name__)

NO_RUNTIME_BLOCKED_EXPLANATION = (
    "This strategy has no registered live-decision runtime. It is validated, "
    "but the Python runner cannot dispatch it yet — this is a 'not built "
    "yet' block, distinct from an unmet validation or a stale evidence proof."
)
CANARY_NOT_ALLOWLISTED_BLOCKED_EXPLANATION = (
    "Paper trading is not enabled for this strategy on this account yet. "
    "Review and enable Paper access below. Dry Run is still available."
)
_EVIDENCE_ONLY_OVERRIDE_EXPLANATION = (
    "A human marked this strategy validated, but its behavioral evidence is "
    "evidence-only and is not accepted as behavioral-equivalence proof. Continuing "
    "can produce decisions that have not been reconciled to the reference implementation."
)
_PAPER_ACCESS_PROOF_BLOCKED_EXPLANATION = (
    "Paper access cannot be reviewed until this strategy has a current accepted validation proof."
)


@dataclass(frozen=True)
class CatalogEntry:
    """One composed strategy-catalog entity: definition + validation state.

    ``has_runtime`` is the definition facet's own dispatch fact
    (``supported_alpaca_paper_strategy_keys()``) — kept distinct from
    ``evidence_status`` / ``selectable``, which are derived entirely from
    the validation facet. A row is never dropped for lacking a runtime; it
    is always composed, annotated ``selectable=False`` with a
    ``blocked_explanation`` naming the reason, so "not built yet" reads
    differently from "not validated" / a stale proof.
    """

    strategy_key: str
    label: str
    explanation: str
    validation_case_symbol: str
    has_runtime: bool
    evidence_status: EvidenceStatus
    paper_access_state: PaperAccessState
    selectable: bool
    override_explanation: str | None
    blocked_explanation: str | None


def _is_catalog_visible(strategy_key: str) -> bool:
    """The definition facet's own gate: a registered, catalog-visible key."""
    registration = _STRATEGY_REGISTRY.get(strategy_key)
    return registration is not None and registration.catalog_visible


def _accepted_proof_blocked_reason(
    entry: StrategyValidationEntry,
    event: StrategyValidationFlagEvent,
) -> str | None:
    """Name the first reason the proof recorded at acceptance no longer verifies, or None if it still holds."""
    snapshot = event.evidence_snapshot
    policy = strategy_validation_policy(entry.strategy_category)
    diagnostics = snapshot.diagnostics
    divergent_gating = sorted(
        category for category, count in event.behavioral_equivalence.gating_divergence_counts.items() if count
    )
    divergent_diagnostics = sorted(
        category
        for category, count in (diagnostics.divergence_counts if diagnostics is not None else {}).items()
        if count
    )
    missing_snapshot_fields = policy.missing_required_fields(snapshot)
    stale_artifact = policy.first_stale_artifact(
        entry,
        settings_check=strategy_settings_file_is_current,
        validator_check=strategy_validator_code_is_current,
        audit_check=strategy_audit_copy_is_current,
    )
    stale_artifact_reason = (
        f"The {stale_artifact[0]} at '{stale_artifact[1]}' no longer matches its recorded hash."
        if stale_artifact is not None
        else "All artifacts required by this strategy category match their recorded hashes."
    )
    checks: tuple[tuple[bool, str], ...] = (
        (
            event.behavioral_equivalence.verdict == "accepted_for_deploy",
            "The recorded behavioral-equivalence verdict is no longer accepted for deploy.",
        ),
        (
            stale_artifact is None,
            stale_artifact_reason,
        ),
        (
            not divergent_gating,
            f"The accepted validation event now records a gating divergence in: {', '.join(divergent_gating)}.",
        ),
        (
            policy.snapshot_matches_entry(entry, snapshot),
            "The current manifest entry no longer matches the proof snapshot recorded at acceptance; "
            "the manifest was edited after acceptance.",
        ),
        (
            diagnostics is not None,
            "The accepted proof is missing its diagnostics record.",
        ),
        (
            diagnostics is not None and not divergent_diagnostics,
            f"The accepted proof's diagnostics now record a divergence in: {', '.join(divergent_diagnostics)}.",
        ),
        (
            not missing_snapshot_fields,
            f"The accepted proof snapshot is missing required field(s): {', '.join(missing_snapshot_fields)}.",
        ),
        (
            entry.deployable,
            "The strategy validation manifest no longer marks this strategy as deployable.",
        ),
    )
    for passed, reason in checks:
        if not passed:
            return reason
    return None


@dataclass(frozen=True)
class _Disposition:
    """The validation facet's launch verdict for one runtime-annotated entry."""

    has_runtime: bool
    evidence_status: EvidenceStatus
    paper_access_state: PaperAccessState
    selectable: bool
    override_explanation: str | None
    blocked_explanation: str | None


def _disposition(
    entry: StrategyValidationEntry,
    event: StrategyValidationFlagEvent,
    *,
    has_runtime: bool,
    paper_access_state: PaperAccessState,
) -> _Disposition:
    """Derive one entry's launch disposition from the validation facet and the runtime fact.

    Precedence, most restrictive first: no runtime blocks regardless of
    behavioral verdict (#1703); a sealed Signal Program whose
    ``(program, account)`` pairing is not canary-allowlisted cannot reach
    Paper at all (#1730); evidence-only is Paper-selectable on the
    human-validated flag alone (#1706); otherwise the accepted proof must
    still re-verify (#1698), or the row demotes to blocked.

    The canary branch keeps ``has_runtime=True`` on purpose: the gate is
    scoped to ``mode == "trade"``, so Dry Run stays available and
    ``_admissible_modes`` still yields ``("dry_run",)``. Selectability is
    account-scoped for exactly the same reason the gate is — the identical
    program is launchable for an allowlisted account and refused for every
    other one, which a global flag cannot express.
    """
    if not has_runtime:
        return _Disposition(
            has_runtime=False,
            evidence_status="blocked",
            paper_access_state="blocked",
            selectable=False,
            override_explanation=None,
            blocked_explanation=NO_RUNTIME_BLOCKED_EXPLANATION,
        )
    if event.behavioral_equivalence.verdict == "evidence_only":
        if paper_access_state == "available":
            return _Disposition(
                has_runtime=True,
                evidence_status="blocked",
                paper_access_state="blocked",
                selectable=False,
                override_explanation=None,
                blocked_explanation=_PAPER_ACCESS_PROOF_BLOCKED_EXPLANATION,
            )
        return _Disposition(
            has_runtime=True,
            evidence_status="evidence_only",
            paper_access_state=paper_access_state,
            selectable=True,
            override_explanation=_EVIDENCE_ONLY_OVERRIDE_EXPLANATION,
            blocked_explanation=None,
        )
    blocked_reason = _accepted_proof_blocked_reason(entry, event)
    if blocked_reason is not None:
        return _Disposition(
            has_runtime=True,
            evidence_status="blocked",
            paper_access_state="blocked",
            selectable=False,
            override_explanation=None,
            blocked_explanation=blocked_reason,
        )
    if paper_access_state == "available":
        return _Disposition(
            has_runtime=True,
            evidence_status="accepted",
            paper_access_state="available",
            selectable=False,
            override_explanation=None,
            blocked_explanation=CANARY_NOT_ALLOWLISTED_BLOCKED_EXPLANATION,
        )
    return _Disposition(
        has_runtime=True,
        evidence_status="accepted",
        paper_access_state=paper_access_state,
        selectable=True,
        override_explanation=None,
        blocked_explanation=None,
    )


def _active_canary_pairings_snapshot() -> frozenset[tuple[str, str]]:
    """Resolve one immutable pairing view for the complete catalog response."""
    source_pairings = canary_admission.CANARY_ADMITTED_PROGRAM_ACCOUNT_PAIRS
    try:
        return source_pairings | canary_admission.active_canary_pairings()
    except canary_admission.CanaryAdmissionLedgerError as exc:
        logger.error("Canary admission catalog snapshot failed closed: %s", exc)
        return source_pairings


def compose_strategy_catalog(
    entries: list[StrategyValidationEntry],
    *,
    account_id: str,
) -> tuple[CatalogEntry, ...]:
    """Compose the strategy catalog from the definition and validation facets.

    Every catalog-visible, currently-validated entry produces exactly one
    row — never silently dropped for lacking a registered runtime (#1703).
    An entry that is missing, invalidated, rejected, or not catalog-visible
    at all produces no row, unchanged from before this slice.
    """
    runtime_keys = supported_alpaca_paper_strategy_keys()
    active_pairings = _active_canary_pairings_snapshot()
    catalog: list[CatalogEntry] = []
    for entry in entries:
        if not _is_catalog_visible(entry.strategy_key):
            continue
        event = entry.current_flag_event
        if entry.validation_state != "validated" or event is None or event.flag != "validated":
            continue
        registration = _STRATEGY_REGISTRY[entry.strategy_key]
        paper_access_state: PaperAccessState
        if registration.signal_program_contract is None:
            paper_access_state = "not_required"
        elif (entry.strategy_key, account_id) in active_pairings:
            paper_access_state = "enabled"
        else:
            paper_access_state = "available"
        disposition = _disposition(
            entry,
            event,
            has_runtime=entry.strategy_key in runtime_keys,
            paper_access_state=paper_access_state,
        )
        snapshot = event.evidence_snapshot
        validation_case_symbol = snapshot.validation_case_symbol or alpaca_paper_strategy_default_symbol(
            entry.strategy_key
        )
        catalog.append(
            CatalogEntry(
                strategy_key=entry.strategy_key,
                label=entry.display_name,
                explanation=entry.description,
                validation_case_symbol=validation_case_symbol,
                has_runtime=disposition.has_runtime,
                evidence_status=disposition.evidence_status,
                paper_access_state=disposition.paper_access_state,
                selectable=disposition.selectable,
                override_explanation=disposition.override_explanation,
                blocked_explanation=disposition.blocked_explanation,
            )
        )
    return tuple(catalog)
