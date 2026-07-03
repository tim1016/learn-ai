"""Account Truth source freshness policy and row projection."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from app.broker.ibkr.models import (
    IbkrAccountSummary,
    IbkrConnectionHealth,
    IbkrOpenOrder,
    IbkrOrderEvent,
    IbkrPositionsSnapshot,
)
from app.schemas.account_truth import (
    AccountTruthEvidenceGap,
    AccountTruthSeverity,
    AccountTruthSourceFreshness,
    AccountTruthSourceName,
)

ACCOUNT_TRUTH_SOURCE_FRESHNESS_TTL_MS = 60_000


@dataclass(frozen=True)
class AccountTruthSourceFreshnessSpec:
    source: AccountTruthSourceName
    label: str
    severity: AccountTruthSeverity
    hard_ttl_ms: int = ACCOUNT_TRUTH_SOURCE_FRESHNESS_TTL_MS


ACCOUNT_TRUTH_SOURCE_FRESHNESS_SPECS: tuple[AccountTruthSourceFreshnessSpec, ...] = (
    AccountTruthSourceFreshnessSpec("broker_connection", "Broker connection", "critical"),
    AccountTruthSourceFreshnessSpec("account_summary", "Account summary", "critical"),
    AccountTruthSourceFreshnessSpec("positions", "Positions", "critical"),
    AccountTruthSourceFreshnessSpec("open_orders", "Open orders", "critical"),
    AccountTruthSourceFreshnessSpec("completed_orders", "Completed orders", "warning"),
    AccountTruthSourceFreshnessSpec("executions", "Executions", "warning"),
)
ACCOUNT_TRUTH_SOURCE_NAMES: tuple[AccountTruthSourceName, ...] = tuple(
    spec.source for spec in ACCOUNT_TRUTH_SOURCE_FRESHNESS_SPECS
)
_SOURCE_SPEC_BY_NAME = {spec.source: spec for spec in ACCOUNT_TRUTH_SOURCE_FRESHNESS_SPECS}


def compose_account_truth_source_freshness(
    *,
    health: IbkrConnectionHealth,
    account: IbkrAccountSummary | None,
    positions_snapshot: IbkrPositionsSnapshot | None,
    open_orders: Sequence[IbkrOpenOrder],
    completed_orders: Sequence[IbkrOpenOrder],
    executions: Sequence[IbkrOrderEvent],
    evidence_gaps: Sequence[AccountTruthEvidenceGap],
    checked_at_ms: int,
) -> list[AccountTruthSourceFreshness]:
    """Build one backend-authored freshness verdict for each Account Truth source."""

    gap_by_source = {gap.source: gap for gap in evidence_gaps}
    return [
        _source_freshness_row(
            "broker_connection",
            fetched_at_ms=health.fetched_at_ms,
            gap=None,
            checked_at_ms=checked_at_ms,
        ),
        _source_freshness_row(
            "account_summary",
            fetched_at_ms=account.fetched_at_ms if account is not None else None,
            gap=gap_by_source.get("account_summary"),
            checked_at_ms=checked_at_ms,
        ),
        _source_freshness_row(
            "positions",
            fetched_at_ms=positions_snapshot.fetched_at_ms if positions_snapshot is not None else None,
            gap=gap_by_source.get("positions"),
            checked_at_ms=checked_at_ms,
            force_stale_message=_positions_cache_fallback_message(positions_snapshot),
        ),
        _source_freshness_row(
            "open_orders",
            fetched_at_ms=_order_sequence_fetched_at_ms(open_orders, checked_at_ms),
            gap=gap_by_source.get("open_orders"),
            checked_at_ms=checked_at_ms,
        ),
        _source_freshness_row(
            "completed_orders",
            fetched_at_ms=_order_sequence_fetched_at_ms(completed_orders, checked_at_ms),
            gap=gap_by_source.get("completed_orders"),
            checked_at_ms=checked_at_ms,
        ),
        _source_freshness_row(
            "executions",
            fetched_at_ms=_execution_source_fetched_at_ms(executions, checked_at_ms),
            gap=gap_by_source.get("executions"),
            checked_at_ms=checked_at_ms,
        ),
    ]


def critical_source_freshness_blocks(
    source_freshness: Sequence[AccountTruthSourceFreshness],
    *,
    checked_at_ms: int | None = None,
) -> tuple[AccountTruthSourceFreshness, ...]:
    """Return critical missing/stale source rows, including absent expected rows."""

    return tuple(
        row
        for row in normalize_source_freshness(source_freshness, checked_at_ms=checked_at_ms)
        if _SOURCE_SPEC_BY_NAME[row.source].severity == "critical"
        and row.status in {"missing", "stale"}
    )


def normalize_source_freshness(
    source_freshness: Sequence[AccountTruthSourceFreshness],
    *,
    checked_at_ms: int | None = None,
) -> tuple[AccountTruthSourceFreshness, ...]:
    """Fill absent rows and canonicalize severity/reason fields from the source spec."""

    rows_by_source = {row.source: row for row in source_freshness}
    rows: list[AccountTruthSourceFreshness] = []
    for spec in ACCOUNT_TRUTH_SOURCE_FRESHNESS_SPECS:
        row = rows_by_source.get(spec.source)
        if row is None:
            rows.append(
                _source_freshness_row(
                    spec.source,
                    fetched_at_ms=None,
                    gap=None,
                    checked_at_ms=0,
                )
            )
            continue
        updates: dict[str, object] = {}
        if row.severity != spec.severity:
            updates["severity"] = spec.severity
        if row.hard_ttl_ms != spec.hard_ttl_ms:
            updates["hard_ttl_ms"] = spec.hard_ttl_ms
        if row.status != "fresh" and row.reason_code is None:
            updates["reason_code"] = _reason_code(row.source, row.status)
        updated_row = row.model_copy(update=updates) if updates else row
        rows.append(
            _recheck_source_age(updated_row, checked_at_ms=checked_at_ms)
            if checked_at_ms is not None
            else updated_row
        )
    return tuple(rows)


def _source_freshness_row(
    source: AccountTruthSourceName,
    *,
    fetched_at_ms: int | None,
    gap: AccountTruthEvidenceGap | None,
    checked_at_ms: int,
    force_stale_message: str | None = None,
) -> AccountTruthSourceFreshness:
    spec = _SOURCE_SPEC_BY_NAME[source]
    if gap is not None or fetched_at_ms is None:
        return AccountTruthSourceFreshness(
            source=source,
            label=spec.label,
            status="missing",
            severity=spec.severity,
            fetched_at_ms=None,
            age_ms=None,
            hard_ttl_ms=spec.hard_ttl_ms,
            reason_code=_reason_code(source, "missing"),
            message=gap.message if gap is not None else f"{spec.label} evidence is unavailable.",
        )

    age_ms = max(0, checked_at_ms - fetched_at_ms)
    if force_stale_message is not None:
        return _stale_source_freshness_row(
            source,
            fetched_at_ms=fetched_at_ms,
            age_ms=None,
            message=force_stale_message,
        )
    if age_ms > spec.hard_ttl_ms:
        return _stale_source_freshness_row(
            source,
            fetched_at_ms=fetched_at_ms,
            age_ms=age_ms,
            message=None,
        )

    return AccountTruthSourceFreshness(
        source=source,
        label=spec.label,
        status="fresh",
        severity=spec.severity,
        fetched_at_ms=fetched_at_ms,
        age_ms=age_ms,
        hard_ttl_ms=spec.hard_ttl_ms,
        reason_code=None,
        message=f"{spec.label} evidence is fresh.",
    )


def _recheck_source_age(
    row: AccountTruthSourceFreshness,
    *,
    checked_at_ms: int,
) -> AccountTruthSourceFreshness:
    spec = _SOURCE_SPEC_BY_NAME[row.source]
    if row.fetched_at_ms is None:
        return row
    age_ms = max(0, checked_at_ms - row.fetched_at_ms)
    if row.status != "fresh":
        if row.age_ms is None:
            return row
        return row.model_copy(update={"age_ms": age_ms})
    if age_ms <= spec.hard_ttl_ms:
        return row.model_copy(update={"age_ms": age_ms})
    return _stale_source_freshness_row(
        row.source,
        fetched_at_ms=row.fetched_at_ms,
        age_ms=age_ms,
        message=None,
    )


def _stale_source_freshness_row(
    source: AccountTruthSourceName,
    *,
    fetched_at_ms: int | None,
    age_ms: int | None,
    message: str | None,
) -> AccountTruthSourceFreshness:
    spec = _SOURCE_SPEC_BY_NAME[source]
    return AccountTruthSourceFreshness(
        source=source,
        label=spec.label,
        status="stale",
        severity=spec.severity,
        fetched_at_ms=fetched_at_ms,
        age_ms=age_ms,
        hard_ttl_ms=spec.hard_ttl_ms,
        reason_code=_reason_code(source, "stale"),
        message=message
        or (
            f"{spec.label} evidence is {age_ms} ms old; hard freshness threshold is {spec.hard_ttl_ms} ms."
            if age_ms is not None
            else f"{spec.label} evidence age is unknown; hard freshness threshold is {spec.hard_ttl_ms} ms."
        ),
    )


def _positions_cache_fallback_message(
    positions_snapshot: IbkrPositionsSnapshot | None,
) -> str | None:
    if positions_snapshot is None or not positions_snapshot.used_cache_fallback:
        return None
    return (
        "Positions evidence came from the synchronized IBKR cache after "
        "reqPositionsAsync timed out; live broker positions cannot be proven."
    )


def _reason_code(source: AccountTruthSourceName, status: str) -> str:
    return f"ACCOUNT_TRUTH_SOURCE_{status.upper()}_{source.upper()}"


def _order_sequence_fetched_at_ms(
    rows: Sequence[IbkrOpenOrder],
    checked_at_ms: int,
) -> int:
    return max((row.fetched_at_ms for row in rows), default=checked_at_ms)


def _execution_source_fetched_at_ms(
    rows: Sequence[IbkrOrderEvent],
    checked_at_ms: int,
) -> int:
    return max((row.ts_ms for row in rows), default=checked_at_ms)


__all__ = [
    "ACCOUNT_TRUTH_SOURCE_FRESHNESS_SPECS",
    "ACCOUNT_TRUTH_SOURCE_NAMES",
    "compose_account_truth_source_freshness",
    "critical_source_freshness_blocks",
    "normalize_source_freshness",
]
