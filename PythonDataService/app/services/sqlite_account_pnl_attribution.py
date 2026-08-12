"""Active-SQLite C2 account FIFO attribution facade.

This boundary owns authority selection and reader lifetime.  The economic
projection reader remains the sole source of effective fills and delegates all
FIFO arithmetic to ``fifo_pnl.py``.
"""

from __future__ import annotations

from collections.abc import Mapping

from app.broker.alpaca.clerk.active_authority import get_active_clerk_runtime
from app.broker.alpaca.clerk.sqlite.economic_projection import (
    MarketMark,
    SqliteEconomicProjectionReader,
)
from app.broker.alpaca.clerk.sqlite.economic_projection_models import AccountPnlAttribution
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.services.clerk_transaction_projection import ClerkTransactionProjectionUnavailable


def sqlite_account_pnl_attribution(
    *,
    account_id: str,
    from_ms: int,
    to_ms: int,
    marks: Mapping[str, MarketMark] | None = None,
    start_marks: Mapping[str, MarketMark] | None = None,
    position_quantities: Mapping[str, float] | None = None,
) -> AccountPnlAttribution | None:
    """Return C2 only for the selected SQLite authority, otherwise ``None``.

    The ``None`` result is an authority-selection outcome, not an empty P&L
    answer.  Callers must not substitute a legacy journal or broker activity
    feed for active-SQLite attribution.
    """
    clerk = _active_sqlite_clerk(account_id)
    if clerk is None:
        return None
    reader = SqliteEconomicProjectionReader.from_repository(clerk.repository)
    try:
        return reader.account_pnl_attribution(
            from_ms=from_ms,
            to_ms=to_ms,
            marks=marks,
            start_marks=start_marks,
            position_quantities=position_quantities,
        )
    finally:
        reader.close()


def _active_sqlite_clerk(account_id: str) -> SqliteAlpacaClerkFacade | None:
    runtime = get_active_clerk_runtime()
    if runtime is None:
        return None
    if runtime.authority_kind == "unavailable":
        failure = runtime.startup_failure
        if failure is not None and failure.activation_detected and failure.account_id == account_id:
            raise ClerkTransactionProjectionUnavailable(
                "The selected SQLite Clerk authority is unavailable after startup failure."
            )
        return None
    if runtime.authority_kind != "sqlite":
        return None
    clerk = runtime.clerk
    if not isinstance(clerk, SqliteAlpacaClerkFacade):
        raise RuntimeError("Active SQLite Clerk does not expose its read authority")
    if clerk.account_id != account_id:
        raise ValueError("Requested account is not the active SQLite authority")
    return clerk


__all__ = ["sqlite_account_pnl_attribution"]
