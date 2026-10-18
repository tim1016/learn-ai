"""Read-only market adapters for the options position desk; no orders or lake writes."""

from __future__ import annotations

import asyncio
import logging
from datetime import date
from decimal import Decimal
from functools import lru_cache
from threading import Lock

from app.config import settings
from app.data_lake.option_listing import build_option_listing_zip_bytes, parse_option_listing_zip
from app.data_lake.polygon_option_contracts import fetch_option_contracts
from app.options.deadlines import past_last_session
from app.options_backtest.lake_data import LakeRunData
from app.options_backtest.market import PickRefused
from app.options_backtest.picking import ListedContract, Listing, expiry_for, pick_contract
from app.options_backtest.position_preview import PreviewUnavailable, expiry_view, historical_preview, resolved_leg
from app.schemas.options_position_preview import PositionPreviewRequest, PositionPreviewResponse
from app.services.polygon_client import PolygonClientService
from app.utils.session_anchors import et_date_at_ms
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)
_listing_lock = Lock()
_snapshot_lock = Lock()


@lru_cache(maxsize=32)
def _read_snapshot(underlying: str, expiry_ms: int, refresh_bucket: int) -> dict:
    # One read per expiry per 15 seconds, shared across rapid leg edits. Snapshot times
    # remain the vendor's; the cache's lifetime never changes their evidence.
    return PolygonClientService().list_snapshot_options_chain(underlying, expiration_ms=expiry_ms)


@lru_cache(maxsize=8)
def _read_listing(underlying: str, today: date, refresh_bucket: int) -> Listing:
    rows = asyncio.run(fetch_option_contracts(underlying, today, settings.POLYGON_API_KEY, budget_s=30))
    payload = build_option_listing_zip_bytes(underlying, today, rows)
    classified = parse_option_listing_zip(payload, underlying, today)
    return Listing(
        underlying,
        today,
        tuple(
            ListedContract(row.contract, row.shares_per_contract, row.standard)
            for row in classified
            if row.contract is not None and row.shares_per_contract is not None
        ),
        True,
    )


def _listing(underlying: str, today: date, refresh_bucket: int) -> Listing:
    # Check the cache after acquiring the lock: edits arriving during the first
    # paginated fetch share its result rather than starting another full fetch.
    with _listing_lock:
        return _read_listing(underlying, today, refresh_bucket)


def _snapshot(underlying: str, expiry_ms: int, refresh_bucket: int) -> dict:
    with _snapshot_lock:
        return _read_snapshot(underlying, expiry_ms, refresh_bucket)


def current_preview(request: PositionPreviewRequest, *, now_ms: int) -> PositionPreviewResponse:
    """Use the existing Options Lab vendor snapshot, keeping its quote evidence and delays visible."""
    today = et_date_at_ms(now_ms)
    listing = _listing(request.setup.underlying, today, now_ms // 60_000)
    listing = Listing(
        listing.underlying,
        today,
        tuple(row for row in listing.contracts if not past_last_session(row.contract.expiry_date, now_ms)),
        listing.complete,
    )
    by_expiry: dict[int, dict] = {}
    spot: Decimal | None = None
    for leg in request.setup.legs:
        expiries = {
            row.contract.expiry_date for row in listing.contracts if row.is_pickable and row.contract.right == leg.right
        }
        expiry = expiry_for(leg.expiry, listed=expiries, trading_date=today)
        if expiry is None:
            raise PreviewUnavailable(f"Leg {leg.leg_id}: no unexpired listed expiry matches its rule.")
        expiry_ms = next(row.contract.expiry_ms for row in listing.contracts if row.contract.expiry_date == expiry)
        if expiry_ms not in by_expiry:
            by_expiry[expiry_ms] = _snapshot(request.setup.underlying, expiry_ms, now_ms // 15_000)
            price = Decimal(str(by_expiry[expiry_ms].get("underlying", {}).get("price", 0)))
            if not price.is_finite() or price <= 0:
                raise PreviewUnavailable("The current chain has no valid underlying price.")
            if spot is None:
                spot = price
    if spot is None:
        raise PreviewUnavailable("Add a position leg first.")
    legs = []
    for leg in request.setup.legs:
        picked = pick_contract(leg, listing=listing, trading_date=today, underlying_price=spot)
        if isinstance(picked, PickRefused):
            raise PreviewUnavailable(f"Leg {leg.leg_id}: {picked.detail}")
        snapshot = by_expiry[picked.contract.expiry_ms]
        row = next(
            (
                row
                for row in snapshot["contracts"]
                if str(row.get("ticker", "")).removeprefix("O:") == picked.contract.occ_symbol
            ),
            None,
        )
        quote = (row or {}).get("last_quote") or {}
        bid, ask = quote.get("bid"), quote.get("ask")
        stamp = quote.get("last_updated")
        if bid is None or ask is None or type(stamp) is not int:
            raise PreviewUnavailable(
                f"{picked.contract.occ_symbol}: a timestamped two-sided quote is unavailable. Last trades and invented premiums are not substituted."
            )
        bid, ask = Decimal(str(bid)), Decimal(str(ask))
        if not bid.is_finite() or not ask.is_finite() or not 0 <= bid <= ask or ask <= 0:
            raise PreviewUnavailable(f"{picked.contract.occ_symbol}: its quote is invalid or crossed.")
        # Polygon's snapshot quote last_updated is nanoseconds since epoch. This
        # is its ingestion boundary, before anything reaches the preview wire.
        stamp_ms = stamp // 1_000_000
        if not 0 < stamp_ms <= now_ms:
            raise PreviewUnavailable(f"{picked.contract.occ_symbol}: the quote timestamp is invalid or in the future.")
        legs.append(
            resolved_leg(
                leg.leg_id,
                leg.side,
                leg.contracts,
                picked.contract,
                ask if leg.side == "buy" else bid,
                stamp_ms,
                f"Polygon snapshot {'ask' if leg.side == 'buy' else 'bid'} · may be delayed",
            )
        )
    prices, curves, risk = expiry_view(legs, spot)
    notes = [
        "Expiry payoff using displayed ask prices for buys and bid prices for sells. Quotes may be delayed or from different instants; this is not a fill or a profit forecast. Fees, slippage and trading rules are excluded."
    ]
    if risk is None:
        notes.append("These legs have different expiries. Align their expiries to display a single expiry payoff.")
    return PositionPreviewResponse(
        source="current",
        mode="position",
        at_ms=now_ms,
        spot_usd=str(spot),
        legs=legs,
        prices_usd=prices,
        curves=curves,
        risk=risk,
        notes=notes,
    )


class PositionPreviewService:
    async def preview(self, request: PositionPreviewRequest) -> PositionPreviewResponse:
        if request.source == "historical":
            return await asyncio.to_thread(historical_preview, request, LakeRunData(request.setup.underlying))
        return await asyncio.to_thread(current_preview, request, now_ms=now_ms_utc())


_service = PositionPreviewService()


def get_position_preview_service() -> PositionPreviewService:
    return _service
