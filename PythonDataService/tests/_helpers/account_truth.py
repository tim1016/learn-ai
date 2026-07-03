"""Shared Account Truth test fixture factories."""

from __future__ import annotations

from app.broker.ibkr.account_truth_freshness import ACCOUNT_TRUTH_SOURCE_FRESHNESS_SPECS
from app.schemas.account_truth import AccountTruthSourceFreshness


def fresh_account_truth_source_freshness(generated_at_ms: int) -> list[AccountTruthSourceFreshness]:
    """Return one fresh source-freshness row for every canonical Account Truth source."""

    return [
        AccountTruthSourceFreshness(
            source=spec.source,
            label=spec.label,
            status="fresh",
            severity=spec.severity,
            fetched_at_ms=generated_at_ms,
            age_ms=0,
            hard_ttl_ms=spec.hard_ttl_ms,
            reason_code=None,
            message=f"{spec.label} evidence is fresh.",
        )
        for spec in ACCOUNT_TRUTH_SOURCE_FRESHNESS_SPECS
    ]
