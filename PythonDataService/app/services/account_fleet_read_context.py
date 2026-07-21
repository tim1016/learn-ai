"""Broker-free account facts shared by one fleet-read request.

Catalog and roll-call are read surfaces.  They may project Account Truth that
was already observed by a broker-facing producer, but they must never refresh
it or otherwise talk to IBKR themselves.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from app.engine.live.fleet import compute_account_identity, compute_fleet_contamination
from app.schemas.live_runs import FleetAccountSummary, FleetContamination
from app.services.account_truth_snapshot import (
    AccountTruthAssessment,
    AccountTruthReadiness,
    AccountTruthReadinessEvidence,
    AccountTruthSnapshot,
    AccountTruthSnapshotProvider,
)
from app.services.fleet_contamination import collect_fleet_position_explanations


@dataclass(frozen=True, slots=True)
class AccountFleetReadContext:
    """One account's cached truth and durable fleet explanation projection."""

    account_id: str
    observed_at_ms: int
    account_truth_readiness: AccountTruthReadiness
    contamination: FleetContamination

    @property
    def account_truth_evidence(self) -> AccountTruthReadinessEvidence | None:
        return self.account_truth_readiness.evidence

    @property
    def account_truth_assessment(self) -> AccountTruthAssessment:
        return self.account_truth_readiness.assessment

    @property
    def fleet_blocks_starts(self) -> bool:
        return self.contamination.policy_blocks_starts


@dataclass(frozen=True, slots=True)
class AccountFleetReadContexts:
    """Request-scoped account contexts, keyed case-insensitively."""

    by_account_id: Mapping[str, AccountFleetReadContext]

    def get(self, account_id: str | None) -> AccountFleetReadContext | None:
        if account_id is None:
            return None
        return self.by_account_id.get(account_id.upper())


def build_account_fleet_read_contexts(
    root: Path,
    account_ids: Iterable[str | None],
    *,
    snapshot_provider: AccountTruthSnapshotProvider,
    observed_at_ms: int,
) -> AccountFleetReadContexts:
    """Project each distinct account from cached evidence without broker I/O."""

    contexts: dict[str, AccountFleetReadContext] = {}
    for raw_account_id in account_ids:
        account_id = raw_account_id.strip() if isinstance(raw_account_id, str) else ""
        if not account_id or account_id.upper() in contexts:
            continue
        evidence = snapshot_provider.get(account_id)
        readiness = AccountTruthReadiness.from_evidence(evidence, now_ms=observed_at_ms)
        net_positions = _cached_net_positions(
            evidence,
            assessment=readiness.assessment,
            account_id=account_id,
            observed_at_ms=observed_at_ms,
        )
        contamination = FleetContamination(
            **compute_fleet_contamination(
                net_positions,
                collect_fleet_position_explanations(root, account_id=account_id),
                policy_blocks_starts=True,
            )
        )
        contexts[account_id.upper()] = AccountFleetReadContext(
            account_id=account_id,
            observed_at_ms=observed_at_ms,
            account_truth_readiness=readiness,
            contamination=contamination,
        )
    return AccountFleetReadContexts(by_account_id=MappingProxyType(contexts))


def compose_fleet_account_read_summary(
    root: Path,
    *,
    requested_account_id: str | None,
    instance_account_ids: Mapping[str, str | None],
    broker_connected_account: str | None,
    broker_account_known: bool,
    snapshot_provider: AccountTruthSnapshotProvider,
    observed_at_ms: int,
) -> FleetAccountSummary:
    """Compose the account row from cached truth, never a broker refresh."""

    identity = compute_account_identity(
        dict(instance_account_ids),
        broker_connected_account,
        broker_account_known=broker_account_known,
    )
    identity_account_id = identity["account_id"]
    resolved_account_id = requested_account_id or (
        identity_account_id if isinstance(identity_account_id, str) else None
    )
    context = build_account_fleet_read_contexts(
        root,
        [resolved_account_id],
        snapshot_provider=snapshot_provider,
        observed_at_ms=observed_at_ms,
    ).get(resolved_account_id)
    contamination = (
        context.contamination
        if context is not None
        else FleetContamination(
            **compute_fleet_contamination(
                None,
                collect_fleet_position_explanations(root, account_id=resolved_account_id),
                policy_blocks_starts=True,
            )
        )
    )
    return FleetAccountSummary(
        account_id=requested_account_id or resolved_account_id,
        account_identity=identity["account_identity"],
        account_identity_reason_codes=identity["account_identity_reason_codes"],
        contamination=contamination,
    )


def _cached_net_positions(
    evidence: AccountTruthReadinessEvidence | None,
    *,
    assessment: AccountTruthAssessment,
    account_id: str,
    observed_at_ms: int,
) -> dict[str, int] | None:
    """Use only a fresh, account-matching positions projection.

    A missing, failed, stale, or source-stale Account Truth observation is not
    replaced by a live read.  ``None`` deliberately reaches the contamination
    projector as an honest unknown.
    """

    if not isinstance(evidence, AccountTruthSnapshot) or evidence.is_stale(observed_at_ms):
        return None
    if evidence.truth.account_id is None or evidence.truth.account_id.upper() != account_id.upper():
        return None
    if assessment.primary_reason_code in {
        "ACCOUNT_TRUTH_STALE",
        "ACCOUNT_TRUTH_REFRESH_FAILED",
        "ACCOUNT_TRUTH_NOT_AVAILABLE",
    } or any(code.startswith("ACCOUNT_TRUTH_SOURCE_") for code in assessment.reason_codes):
        return None
    net_positions: dict[str, int] = {}
    for position in evidence.truth.positions:
        symbol = position.symbol.upper()
        net_positions[symbol] = net_positions.get(symbol, 0) + int(position.quantity)
    return net_positions
