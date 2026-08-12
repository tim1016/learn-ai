"""C3 broker-curve versus active-SQLite FIFO reconciliation.

Formula: ``broker_delta = equity[-1] - equity[0]``;
``local_delta = C2.realized_pnl_total + C2.open_pnl_total -
C2.start_open_pnl_total - C2.fee_total`` when C2 has complete marks, fees,
and execution coverage; and
``residual = broker_delta - local_delta``.
Reference: ``docs/prds/2026-08-12-broker-account-desk-lens-redesign.md``
  Contract C3, and the shared ``DivergenceCategory`` taxonomy in
  ``app.research.parity.qc_reconciler``.
Canonical implementation: this file.
Validated against:
  ``tests/services/test_account_pnl_reconciliation.py``.

When open lots cannot be valued at the requested boundary, or their observed
marks do not align with C1's broker timeframe buckets, C3 reports
``FIXTURE_INSUFFICIENT`` instead of pretending a partial local book proves the
broker curve.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from app.broker.alpaca.clerk.sqlite.economic_projection_models import AccountPnlAttribution
from app.broker.contract.models import BrokerPortfolioHistory
from app.research.parity.qc_reconciler import DivergenceCategory

# Accumulated-P&L default from .claude/rules/numerical-rigor.md.  These names
# are part of C3's numerical provenance and are returned to every consumer.
ACCOUNT_PNL_RECONCILIATION_ATOL = 1e-6
ACCOUNT_PNL_RECONCILIATION_RTOL = 0.0


@dataclass(frozen=True)
class AccountPnlDivergence:
    """One typed explanation for a C3 proof that cannot pass."""

    category: DivergenceCategory
    detail: str


@dataclass(frozen=True)
class AccountPnlReconciliation:
    """The complete, backend-owned C3 comparison result."""

    broker_delta: float
    local_delta: float
    residual: float
    within_tolerance: bool
    atol: float
    rtol: float
    divergences: tuple[AccountPnlDivergence, ...]


def reconcile_broker_curve_to_local_pnl(
    broker_history: BrokerPortfolioHistory,
    local_attribution: AccountPnlAttribution,
    *,
    atol: float = ACCOUNT_PNL_RECONCILIATION_ATOL,
    rtol: float = ACCOUNT_PNL_RECONCILIATION_RTOL,
) -> AccountPnlReconciliation:
    """Compare a C1 broker equity delta with a C2 local FIFO P&L delta.

    Formula: ``residual = (equity[-1] - equity[0]) -
    (realized_pnl_total + end_open_pnl - start_open_pnl - fee_total)`` when
    all open lots are marked and execution and fee coverage are complete.
    Reference: PRD #1460, Contract C3; accumulated-P&L tolerance policy in
      ``.claude/rules/numerical-rigor.md``.
    Canonical implementation: this file.
    Validated against:
      ``tests/services/test_account_pnl_reconciliation.py``.

    An incomplete FIFO book, unknown fees, a non-flat unmarked book, or a mark
    outside C1's corresponding timeframe bucket is not a proof of the equity
    curve. It is classified ``FIXTURE_INSUFFICIENT``; a numerical mismatch
    otherwise uses the taxonomy's ``PNL_DRIFT`` category.
    """
    _validate_tolerances(atol=atol, rtol=rtol)
    local_delta = _local_delta(local_attribution)
    if not broker_history.equity:
        return AccountPnlReconciliation(
            broker_delta=0.0,
            local_delta=local_delta,
            residual=-local_delta,
            within_tolerance=False,
            atol=atol,
            rtol=rtol,
            divergences=(
                AccountPnlDivergence(
                    category=DivergenceCategory.FIXTURE_INSUFFICIENT,
                    detail="Broker portfolio history has no equity points for the requested window.",
                ),
            ),
        )

    broker_delta = broker_history.equity[-1] - broker_history.equity[0]
    residual = broker_delta - local_delta
    divergences: list[AccountPnlDivergence] = []
    if local_attribution.execution_coverage != "complete":
        divergences.append(
            AccountPnlDivergence(
                category=DivergenceCategory.FIXTURE_INSUFFICIENT,
                detail=(
                    "Local SQLite executions do not cover the complete broker account, "
                    "so bot-attributed fills cannot prove the account equity delta."
                ),
            )
        )
    elif local_attribution.fee_fidelity != "reported" or local_attribution.fee_total is None:
        divergences.append(
            AccountPnlDivergence(
                category=DivergenceCategory.FIXTURE_INSUFFICIENT,
                detail="Broker fees are not fully reported for the requested window.",
            )
        )
    elif not local_attribution.marks_complete:
        divergences.append(
            AccountPnlDivergence(
                category=DivergenceCategory.FIXTURE_INSUFFICIENT,
                detail=(
                    "Local FIFO contains open lots without complete marks, so its realized "
                    "P&L cannot prove the broker equity delta."
                ),
            )
        )
    elif not _marks_align_to_curve_boundaries(
        local_attribution,
        curve_start_ms=broker_history.timestamps[0],
        curve_end_ms=broker_history.timestamps[-1],
        timeframe=broker_history.timeframe,
    ):
        divergences.append(
            AccountPnlDivergence(
                category=DivergenceCategory.FIXTURE_INSUFFICIENT,
                detail=(
                    "Local FIFO open-lot marks do not fall within the broker curve's "
                    "corresponding timeframe buckets."
                ),
            )
        )
    elif not math.isclose(broker_delta, local_delta, abs_tol=atol, rel_tol=rtol):
        divergences.append(
            AccountPnlDivergence(
                category=DivergenceCategory.PNL_DRIFT,
                detail=(
                    f"broker_delta={broker_delta:.12g} local_delta={local_delta:.12g} "
                    f"residual={residual:.12g} exceeds atol={atol:.12g}, rtol={rtol:.12g}."
                ),
            )
        )
    return AccountPnlReconciliation(
        broker_delta=broker_delta,
        local_delta=local_delta,
        residual=residual,
        within_tolerance=not divergences,
        atol=atol,
        rtol=rtol,
        divergences=tuple(divergences),
    )


def _validate_tolerances(*, atol: float, rtol: float) -> None:
    """Reject non-finite comparison settings rather than silently weakening C3."""
    for name, value in (("atol", atol), ("rtol", rtol)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{name} must be a finite number")
        if value < 0:
            raise ValueError(f"{name} must be non-negative")


def _local_delta(local_attribution: AccountPnlAttribution) -> float:
    """Return complete C2 delta, or its diagnostic realized subtotal."""
    if (
        local_attribution.marks_complete
        and local_attribution.start_open_pnl_total is not None
        and local_attribution.open_pnl_total is not None
        and local_attribution.fee_total is not None
    ):
        return (
            local_attribution.realized_pnl_total
            + local_attribution.open_pnl_total
            - local_attribution.start_open_pnl_total
            - local_attribution.fee_total
        )
    return local_attribution.realized_pnl_total


def _marks_align_to_curve_boundaries(
    local_attribution: AccountPnlAttribution,
    *,
    curve_start_ms: int,
    curve_end_ms: int,
    timeframe: str,
) -> bool:
    """Require each open-lot mark to fall in its broker-history bucket."""
    bucket_ms = _timeframe_bucket_ms(timeframe)
    if bucket_ms is None:
        return all(
            observed_at_ms == curve_start_ms
            for observed_at_ms in local_attribution.start_mark_observed_at_ms.values()
        ) and all(
            observed_at_ms == curve_end_ms
            for observed_at_ms in local_attribution.mark_observed_at_ms.values()
        )
    return all(
        curve_start_ms <= observed_at_ms < curve_start_ms + bucket_ms
        for observed_at_ms in local_attribution.start_mark_observed_at_ms.values()
    ) and all(
        curve_end_ms <= observed_at_ms < curve_end_ms + bucket_ms
        for observed_at_ms in local_attribution.mark_observed_at_ms.values()
    )


def _timeframe_bucket_ms(timeframe: str) -> int | None:
    """Translate the broker timeframe labels used by the account-history adapter."""
    return {"1Min": 60_000, "1D": 86_400_000}.get(timeframe)


__all__ = [
    "ACCOUNT_PNL_RECONCILIATION_ATOL",
    "ACCOUNT_PNL_RECONCILIATION_RTOL",
    "AccountPnlDivergence",
    "AccountPnlReconciliation",
    "reconcile_broker_curve_to_local_pnl",
]
