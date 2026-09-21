"""Read contract for the shared symbol picker's listing catalog (ADR 0066).

`GET /api/tickers/catalog` serves this projection — the Frontend's
`VendorSymbolEntry` aliases the generated type — so the closed values here
are the wire contract, not incidental strings: a walk that ever widens the
asset class or the status set must widen these Literals first.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

SymbolCatalogAssetClass = Literal["us_equity"]
"""The only class the walk serves — `market="stocks"` reference rows —
because the lake backfill pipeline can never cover anything else."""

SymbolCatalogStatus = Literal["active", "inactive"]
"""The vendor's listing status. `inactive` is the delisted marker the
backfill panel's explicit toggle gates on."""


class SymbolCatalogEntry(BaseModel):
    """One row of the shared symbol picker's catalog (GET /api/tickers/catalog).

    The membership projection for pickers: a Polygon reference-tickers walk
    projected onto the fields a picker row renders. ``asset_class`` is
    ``us_equity`` for every row the catalog serves — the walk is
    ``market="stocks"`` — and ``status`` carries the vendor's active flag so
    a picker may offer delisted symbols deliberately (the lake backfill
    panel's toggle) without this endpoint deciding membership for it.
    """

    symbol: str
    name: str | None
    asset_class: SymbolCatalogAssetClass
    exchange: str | None
    status: SymbolCatalogStatus
