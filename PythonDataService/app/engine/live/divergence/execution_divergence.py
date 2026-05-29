"""Layer A — ``ExecutionDivergenceClassifier``.

Given a matched-ledger row (``ExecutionMatcher`` output) and a
per-category tolerance lookup, emit zero or more ``ExecutionDivergence``
rows. Enum-driven, no I/O. Each output row carries category, severity,
magnitude (bps for slippage, ms for latency, count for missed/extra/
partial, dollar diff for commission), the tolerance that was applied,
and the full source-row identifiers so any flag is traceable to the
artifact rows that produced it (PRD-B user stories 4–7).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from app.engine.live.divergence.common import Severity
from app.engine.live.divergence.execution_matcher import MatchedLedgerRow
from app.research.parity.ibkr_commission import IbkrEquityCommissionModel

__all__ = [
    "ExecutionDivergence",
    "ExecutionDivergenceCategory",
    "ExecutionTolerances",
    "Severity",
    "classify_execution_divergences",
]


class ExecutionDivergenceCategory(StrEnum):
    """Layer A divergence categories (PRD-B). Distinct from the
    backtest-vs-backtest ``DivergenceCategory`` in ``qc_reconciler``."""

    SLIPPAGE = "slippage"
    LATENCY_SUBMIT = "latency_submit"
    LATENCY_FILL = "latency_fill"
    MISSED = "missed"
    EXTRA = "extra"
    PARTIAL = "partial"
    # REJECTED is currently UNREACHABLE — a documented stub, not classified.
    # A broker rejection produces no fill, so from decisions.parquet +
    # executions.parquet alone it is indistinguishable from MISSED. The
    # rejection evidence (broker status / error events) exists at the adapter
    # layer (broker/ibkr/orders.py + models.py error events) but is filtered
    # to fills-only before it reaches an artifact (live_portfolio.py) and the
    # execution artifact is one-row-per-fill (artifacts.py). Distinguishing
    # REJECTED from MISSED requires a future order-lifecycle artifact
    # (order_events.parquet: ts_ms, client_order_id, order_id, perm_id,
    # event_type, status, error_code, error_message). Do NOT synthesise
    # rejection evidence from fill rows — surface the gap instead.
    REJECTED = "rejected"
    COMMISSION_MISSING = "commission_missing"
    COMMISSION_DRIFT = "commission_drift"


@dataclass(frozen=True)
class ExecutionTolerances:
    """Per-category Layer A tolerances. Defaults from PRD-B §Implementation
    Decisions; overridable per-report and recorded verbatim in the JSON
    bundle so day-over-day comparisons are tolerance-anchored."""

    slippage_bps: float = 2.0
    latency_submit_ms: float = 500.0
    latency_fill_ms: float = 2000.0
    missed_count: int = 0
    extra_count: int = 0
    partial_count: int = 0
    commission_atol: Decimal = field(default_factory=lambda: Decimal("0.01"))
    # Commission divergences stay non-gating until the commissionReport
    # callback is observed to fire reliably across a full RTH session
    # (Branch-A vs Branch-B analogue, PRD-B Further Notes). Flip to gating
    # via report config once the wiring stabilises.
    commission_gating: bool = False


@dataclass(frozen=True)
class ExecutionDivergence:
    """One typed execution-quality divergence."""

    category: ExecutionDivergenceCategory
    severity: Severity
    magnitude: float
    applied_tolerance: float
    client_order_id: str | None = None
    perm_id: int | None = None
    exec_id: str | None = None
    bar_close_ms: int | None = None
    detail: str = ""


def classify_execution_divergences(
    row: MatchedLedgerRow,
    tolerances: ExecutionTolerances,
) -> list[ExecutionDivergence]:
    """Emit the divergences implied by one matched-ledger row."""
    divergences: list[ExecutionDivergence] = []
    decision = row.decision
    execution = row.execution

    if row.is_missed:
        divergences.append(
            _divergence(
                ExecutionDivergenceCategory.MISSED,
                Severity.GATING,
                magnitude=1.0,
                applied_tolerance=float(tolerances.missed_count),
                row=row,
            )
        )

    if row.is_extra:
        divergences.append(
            _divergence(
                ExecutionDivergenceCategory.EXTRA,
                Severity.GATING,
                magnitude=1.0,
                applied_tolerance=float(tolerances.extra_count),
                row=row,
            )
        )

    if "partial" in row.flags:
        divergences.append(
            _divergence(
                ExecutionDivergenceCategory.PARTIAL,
                Severity.GATING,
                magnitude=1.0,
                applied_tolerance=float(tolerances.partial_count),
                row=row,
            )
        )

    if decision is not None and execution is not None:
        slippage_bps = (
            abs(execution.fill_price - decision.intended_price)
            / decision.intended_price
            * 10_000
        )
        if slippage_bps > tolerances.slippage_bps:
            divergences.append(
                _divergence(
                    ExecutionDivergenceCategory.SLIPPAGE,
                    Severity.GATING,
                    magnitude=slippage_bps,
                    applied_tolerance=tolerances.slippage_bps,
                    row=row,
                )
            )

        # Submit latency is the decision's own compute-to-submit time, gated
        # by the engine's max_submit_latency_ms.
        if (
            decision.decision_latency_ms is not None
            and decision.decision_latency_ms > tolerances.latency_submit_ms
        ):
            divergences.append(
                _divergence(
                    ExecutionDivergenceCategory.LATENCY_SUBMIT,
                    Severity.GATING,
                    magnitude=float(decision.decision_latency_ms),
                    applied_tolerance=tolerances.latency_submit_ms,
                    row=row,
                )
            )

        # Expected fill time ≈ bar_close_ms (NEXT_BAR_OPEN fills a few seconds
        # past the bar boundary; see reconcile._attach_fills). Actual fill time
        # is execution.ts_ms.
        fill_latency_ms = float(execution.ts_ms - decision.bar_close_ms)
        if fill_latency_ms > tolerances.latency_fill_ms:
            divergences.append(
                _divergence(
                    ExecutionDivergenceCategory.LATENCY_FILL,
                    Severity.GATING,
                    magnitude=fill_latency_ms,
                    applied_tolerance=tolerances.latency_fill_ms,
                    row=row,
                )
            )

        divergences.extend(_classify_commission(row, tolerances))

    return divergences


_COMMISSION_MODEL = IbkrEquityCommissionModel()


def _classify_commission(
    row: MatchedLedgerRow,
    tolerances: ExecutionTolerances,
) -> list[ExecutionDivergence]:
    """COMMISSION_MISSING when the fee was never recorded; COMMISSION_DRIFT
    when a recorded fee differs from the IBKR-tier prediction beyond tolerance.
    Both non-gating until the commissionReport wiring stabilises."""
    execution = row.execution
    assert execution is not None  # caller guards on a matched pair
    severity = Severity.GATING if tolerances.commission_gating else Severity.NON_GATING

    # A null fee is NaN in the parquet — the callback has not back-filled it.
    if math.isnan(execution.fee):
        return [
            _divergence(
                ExecutionDivergenceCategory.COMMISSION_MISSING,
                severity,
                magnitude=1.0,
                applied_tolerance=0.0,
                row=row,
            )
        ]

    predicted = _COMMISSION_MODEL.fee(
        quantity=execution.fill_quantity,
        fill_price=Decimal(str(execution.fill_price)),
    )
    drift = abs(Decimal(str(execution.fee)) - predicted)
    if drift > tolerances.commission_atol:
        return [
            _divergence(
                ExecutionDivergenceCategory.COMMISSION_DRIFT,
                severity,
                magnitude=float(drift),
                applied_tolerance=float(tolerances.commission_atol),
                row=row,
            )
        ]
    return []


def _divergence(
    category: ExecutionDivergenceCategory,
    severity: Severity,
    *,
    magnitude: float,
    applied_tolerance: float,
    row: MatchedLedgerRow,
    detail: str = "",
) -> ExecutionDivergence:
    """Build a divergence, threading the source-row identifiers from the
    ledger row so every flag is traceable to its originating artifact rows."""
    execution = row.execution
    decision = row.decision
    return ExecutionDivergence(
        category=category,
        severity=severity,
        magnitude=magnitude,
        applied_tolerance=applied_tolerance,
        client_order_id=execution.client_order_id if execution is not None else None,
        perm_id=execution.perm_id if execution is not None else None,
        exec_id=execution.exec_id if execution is not None else None,
        bar_close_ms=decision.bar_close_ms if decision is not None else None,
        detail=detail,
    )
