"""Account Truth refresh boundary with snapshot-cache side effects."""

from __future__ import annotations

from collections.abc import Sequence

from app.broker.ibkr.account_truth import fetch_account_truth
from app.broker.ibkr.client import BrokerError, IbkrClient
from app.broker.ibkr.models import IbkrConnectionHealth
from app.engine.live.account_artifacts import AccountInstanceBinding
from app.schemas.account_truth import AccountTruthEvidenceGap, AccountTruthResponse
from app.services.account_truth_snapshot import (
    AccountTruthSnapshotProvider,
    get_account_truth_snapshot_provider,
)


async def refresh_account_truth_and_update_cache(
    client: IbkrClient,
    *,
    health: IbkrConnectionHealth,
    account_instance_bindings: Sequence[AccountInstanceBinding],
    initial_evidence_gaps: Sequence[AccountTruthEvidenceGap] = (),
    snapshot_provider: AccountTruthSnapshotProvider | None = None,
) -> AccountTruthResponse:
    """Fetch Account Truth and keep the readiness cache in sync with the attempt."""

    provider = snapshot_provider or get_account_truth_snapshot_provider()
    try:
        truth = await fetch_account_truth(
            client,
            health=health,
            account_instance_bindings=account_instance_bindings,
            initial_evidence_gaps=initial_evidence_gaps,
        )
    except BrokerError as exc:
        provider.mark_refresh_failed(
            health.account_id,
            detail=str(exc),
        )
        raise
    provider.remember(truth)
    return truth


__all__ = ["refresh_account_truth_and_update_cache"]
