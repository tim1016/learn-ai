"""Account-level FIFO attribution and broker-curve proof contracts."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.broker.alpaca.clerk.sqlite.economic_projection_models import AccountPnlAttribution
from app.broker.contract.models import BrokerPortfolioHistory
from app.research.parity.qc_reconciler import DivergenceCategory
from app.services.account_pnl_reconciliation import AccountPnlReconciliation


class FifoAttributionRowResponse(BaseModel):
    """One backend-computed FIFO lot closure; the browser renders it verbatim."""

    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(min_length=1, max_length=64)
    quantity: float
    entry_price: float
    exit_price: float
    opened_at_ms: int = Field(ge=0)
    closed_at_ms: int = Field(ge=0)
    realized_pnl: float
    fee: float | None = None
    entry_strategy_instance_id: str = Field(min_length=1, max_length=128)
    exit_strategy_instance_id: str = Field(min_length=1, max_length=128)


class AccountPnlAttributionResponse(BaseModel):
    """C2 — inclusive-window FIFO attribution from active SQLite folds."""

    model_config = ConfigDict(extra="forbid")

    account_id: str = Field(min_length=1, max_length=64)
    authority_generation: int = Field(ge=0)
    control_revision: int = Field(ge=0)
    from_ms: int = Field(ge=0)
    to_ms: int = Field(ge=0)
    attribution_rows: list[FifoAttributionRowResponse]
    realized_pnl_total: float
    open_pnl_total: float | None
    marks_complete: bool
    mark_observed_at_ms: dict[str, int]

    @classmethod
    def from_projection(cls, projection: AccountPnlAttribution) -> AccountPnlAttributionResponse:
        """Translate the typed SQLite read model without changing its economics."""
        return cls(
            account_id=projection.account_id,
            authority_generation=projection.authority_generation,
            control_revision=projection.control_revision,
            from_ms=projection.from_ms,
            to_ms=projection.to_ms,
            attribution_rows=[
                FifoAttributionRowResponse(
                    symbol=row.symbol,
                    quantity=row.quantity,
                    entry_price=row.entry_price,
                    exit_price=row.exit_price,
                    opened_at_ms=row.opened_at_ms,
                    closed_at_ms=row.closed_at_ms,
                    realized_pnl=row.realized_pnl,
                    fee=row.fee,
                    entry_strategy_instance_id=row.entry_strategy_instance_id,
                    exit_strategy_instance_id=row.exit_strategy_instance_id,
                )
                for row in projection.attribution_rows
            ],
            realized_pnl_total=projection.realized_pnl_total,
            open_pnl_total=projection.open_pnl_total,
            marks_complete=projection.marks_complete,
            mark_observed_at_ms=projection.mark_observed_at_ms,
        )


class AccountPnlDivergenceResponse(BaseModel):
    """One C3 classification, named by the shared reconciliation taxonomy."""

    model_config = ConfigDict(extra="forbid")

    category: DivergenceCategory
    detail: str = Field(min_length=1, max_length=512)


class AccountPnlReconciliationResponse(BaseModel):
    """C3 — broker-curve and local-FIFO delta comparison."""

    model_config = ConfigDict(extra="forbid")

    broker_delta: float
    local_delta: float
    residual: float
    within_tolerance: bool
    atol: float = Field(ge=0)
    rtol: float = Field(ge=0)
    divergences: list[AccountPnlDivergenceResponse]

    @classmethod
    def from_result(
        cls,
        result: AccountPnlReconciliation,
    ) -> AccountPnlReconciliationResponse:
        """Translate the pure C3 result without browser-side arithmetic."""
        return cls(
            broker_delta=result.broker_delta,
            local_delta=result.local_delta,
            residual=result.residual,
            within_tolerance=result.within_tolerance,
            atol=result.atol,
            rtol=result.rtol,
            divergences=[
                AccountPnlDivergenceResponse(category=item.category, detail=item.detail) for item in result.divergences
            ],
        )


class PortfolioHistoryProofResponse(BaseModel):
    """C1 + C2 + C3 bundle for the Trader lens historical scope."""

    model_config = ConfigDict(extra="forbid")

    history: BrokerPortfolioHistory
    attribution: AccountPnlAttributionResponse
    reconciliation: AccountPnlReconciliationResponse
