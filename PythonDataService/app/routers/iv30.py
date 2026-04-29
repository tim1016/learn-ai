"""Live IV30 endpoints — Step C of the IV-ownership plan.

Two routes that pull a real Polygon snapshot, run our solver / replication
pipeline through the Step A / Step B contracts, and return an IV30 number
with full ``IvProvenance``:

- ``POST /api/edge/iv30/vix-style`` — CBOE VIX 2019 whitepaper formula on
  the surviving chain. The architecturally important demonstration:
  every leg is real OPRA, so ``variance_contribution_synthetic`` should be
  ~0 and the price source mix should be ~100% ``opra_mid``.
- ``POST /api/edge/iv30/parametric`` — variance-time interpolation between
  the two expiries straddling 30 calendar days, using the IV at the ATM
  strike (closest to forward) on each expiry as the σ input. Uses
  ``iv30_atm_50d`` from ``features_realtime.iv30_constructor``.

The response carries ``r`` and ``q`` from ``rate_dividend_service`` so the
UI can show the inputs that drove the number.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import pandas as pd
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.engine.edge.features_realtime.iv30_constructor import iv30_atm_50d
from app.services.polygon_client import PolygonClientService
from app.services.rate_dividend_service import get_rate_and_dividend
from app.volatility.iv_provenance import IvProvenance
from app.volatility.price_normalization import (
    NormalizedOptionPrice,
    NormalizedOptionQuote,
    from_snapshot_quote,
)
from app.volatility.solver import implied_volatility
from app.volatility.vix_replication import vix_style_iv30_with_provenance

router = APIRouter(prefix="/api/edge/iv30", tags=["edge-iv30"])
logger = logging.getLogger(__name__)

polygon_client = PolygonClientService()


# ── Request / response models ───────────────────────────────────────────────


class Iv30LiveRequest(BaseModel):
    symbol: str = Field(..., description="Underlying ticker (e.g. SPY).")
    target_calendar_days: int = Field(
        30, ge=1, le=180, description="Target constant-maturity in calendar days."
    )
    debug: bool = Field(
        False,
        description="If True, response includes per-strike contributions for the VIX-style route.",
    )


class IvProvenancePayload(BaseModel):
    """Snake-case JSON projection of ``IvProvenance``."""

    iv_source: str
    price_source_mix: dict[str, float]
    variance_contribution_synthetic: float
    strike_coverage_score: float
    per_strike_contributions: list[dict] | None = None


def _provenance_to_payload(prov: IvProvenance) -> IvProvenancePayload:
    return IvProvenancePayload(
        iv_source=prov.iv_source,
        price_source_mix=dict(prov.price_source_mix),
        variance_contribution_synthetic=prov.variance_contribution_synthetic,
        strike_coverage_score=prov.strike_coverage_score,
        per_strike_contributions=prov.per_strike_contributions,
    )


class Iv30LiveResponse(BaseModel):
    """Live IV30 with provenance.

    ``method`` distinguishes the two routes so a single client can plot
    both overlays without disambiguating from the URL.
    """

    symbol: str
    method: str  # "vix_style" | "parametric"
    target_calendar_days: int
    iv30_act365: float
    spot: float
    rate: float
    dividend_yield: float
    rate_source: str
    dividend_source: str
    expiries_used_calendar_days: list[int]
    iv_provenance: IvProvenancePayload
    snapshot_ts_ms: int


# ── Polygon → NormalizedOptionQuote conversion ──────────────────────────────


def _parse_expiration_to_days(expiration_iso: str, asof: datetime) -> int:
    """Calendar days from ``asof`` to the contract expiration."""
    expiry = datetime.fromisoformat(expiration_iso)
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=UTC)
    return max(0, (expiry.date() - asof.date()).days)


def _normalized_quotes_by_expiry(
    contracts: list[dict],
    asof: datetime,
) -> dict[int, list[NormalizedOptionQuote]]:
    """Group Polygon snapshot contracts by expiry days, pair call/put per
    strike, and wrap as ``NormalizedOptionQuote`` tagged ``opra_mid``.

    Contracts whose ``last_quote`` is missing are treated as zero-bid via
    ``from_snapshot_quote(0, 0)`` so the wing-truncation rule still fires
    correctly downstream.
    """
    by_expiry: dict[int, dict[float, dict[str, NormalizedOptionPrice]]] = {}
    for c in contracts:
        exp_iso = c.get("expiration_date")
        strike = c.get("strike_price")
        ctype = c.get("contract_type")
        if not exp_iso or strike is None or ctype not in ("call", "put"):
            continue
        try:
            days = _parse_expiration_to_days(exp_iso, asof)
        except (ValueError, TypeError):
            continue
        if days <= 0:
            continue
        lq = c.get("last_quote") or {}
        bid = float(lq.get("bid", 0.0)) if lq.get("bid") is not None else 0.0
        ask = float(lq.get("ask", 0.0)) if lq.get("ask") is not None else 0.0
        leg = from_snapshot_quote(bid, ask)
        by_expiry.setdefault(days, {}).setdefault(float(strike), {})[ctype] = leg

    result: dict[int, list[NormalizedOptionQuote]] = {}
    for days, by_strike in by_expiry.items():
        quotes = []
        for strike, legs in sorted(by_strike.items()):
            call = legs.get("call") or from_snapshot_quote(0.0, 0.0)
            put = legs.get("put") or from_snapshot_quote(0.0, 0.0)
            quotes.append(NormalizedOptionQuote(strike=strike, call=call, put=put))
        if quotes:
            result[days] = quotes
    return result


def _pick_straddle_pair(by_expiry: dict[int, list], target_days: int) -> tuple[int, int]:
    """Two expiries straddling ``target_days``. Raises HTTP 400 if none exist."""
    expiries = sorted(by_expiry.keys())
    below = [d for d in expiries if d <= target_days]
    above = [d for d in expiries if d > target_days]
    if not below or not above:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"No expiries straddle {target_days} days. Available: {expiries}",
        )
    return max(below), min(above)


# ── Routes ──────────────────────────────────────────────────────────────────


@router.post("/vix-style", response_model=Iv30LiveResponse)
async def iv30_vix_style(req: Iv30LiveRequest) -> Iv30LiveResponse:
    """Live VIX-style IV30 from a fresh Polygon snapshot.

    The acceptance criterion (plan §5.C): on a normal trading day, this
    returns within 50 bps of the published CBOE VIX index value when
    called against SPY. The provenance object reports
    ``variance_contribution_synthetic ≈ 0`` and ``opra_mid`` ≈ 100% mix.
    """
    snapshot = polygon_client.list_snapshot_options_chain(underlying_asset=req.symbol)
    underlying = snapshot.get("underlying") or {}
    contracts = snapshot.get("contracts") or []

    spot = float(underlying.get("price") or 0.0)
    if spot <= 0:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Polygon snapshot returned no spot for {req.symbol}")
    if not contracts:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Polygon snapshot returned no contracts for {req.symbol}")

    asof = datetime.now(tz=UTC)
    rd = get_rate_and_dividend(
        ticker=req.symbol, spot_price=spot, polygon=polygon_client, dte_days=req.target_calendar_days
    )

    by_expiry = _normalized_quotes_by_expiry(contracts, asof)
    if len(by_expiry) < 2:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"Need at least two expiries with quotes; got {len(by_expiry)}",
        )

    t1_days, t2_days = _pick_straddle_pair(by_expiry, req.target_calendar_days)
    sigma, prov = vix_style_iv30_with_provenance(
        by_expiry[t1_days],
        by_expiry[t2_days],
        rate1=rd.rate,
        T1_calendar_days=t1_days,
        rate2=rd.rate,
        T2_calendar_days=t2_days,
        target_calendar_days=req.target_calendar_days,
        debug=req.debug,
    )

    return Iv30LiveResponse(
        symbol=req.symbol,
        method="vix_style",
        target_calendar_days=req.target_calendar_days,
        iv30_act365=float(sigma),
        spot=spot,
        rate=rd.rate,
        dividend_yield=rd.dividend_yield,
        rate_source=rd.source_rate,
        dividend_source=rd.source_dividend,
        expiries_used_calendar_days=[t1_days, t2_days],
        iv_provenance=_provenance_to_payload(prov),
        snapshot_ts_ms=int(asof.timestamp() * 1000),
    )


@router.post("/parametric", response_model=Iv30LiveResponse)
async def iv30_parametric(req: Iv30LiveRequest) -> Iv30LiveResponse:
    """Live parametric IV30 — variance-time interpolation between the ATM
    σ values of the two straddling expiries.

    Lighter-weight than VIX-style: only one strike per expiry contributes
    (the ATM call). The provenance reflects that: 100% ``opra_mid`` if the
    ATM legs have real quotes, with ``strike_coverage_score = 0`` because
    no wing is sampled.
    """
    snapshot = polygon_client.list_snapshot_options_chain(underlying_asset=req.symbol)
    underlying = snapshot.get("underlying") or {}
    contracts = snapshot.get("contracts") or []

    spot = float(underlying.get("price") or 0.0)
    if spot <= 0:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Polygon snapshot returned no spot for {req.symbol}")
    if not contracts:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Polygon snapshot returned no contracts for {req.symbol}")

    asof = datetime.now(tz=UTC)
    rd = get_rate_and_dividend(
        ticker=req.symbol, spot_price=spot, polygon=polygon_client, dte_days=req.target_calendar_days
    )

    by_expiry = _normalized_quotes_by_expiry(contracts, asof)
    if len(by_expiry) < 2:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"Need at least two expiries with quotes; got {len(by_expiry)}",
        )

    iv_by_expiry: dict[int, float] = {}
    legs_used: list[NormalizedOptionPrice] = []
    for days, quotes in by_expiry.items():
        atm = _select_atm_strike_for_parametric(quotes, spot)
        if atm is None:
            continue
        call_leg = atm.call
        if call_leg.is_zero_bid:
            continue
        result = implied_volatility(
            option_price=call_leg.mid,
            spot=spot,
            strike=atm.strike,
            ttm=days / 365.0,
            rate=rd.rate,
            dividend=rd.dividend_yield,
            is_call=True,
        )
        if result.iv is None:
            continue
        iv_by_expiry[days] = float(result.iv)
        legs_used.append(call_leg)

    if len(iv_by_expiry) < 2:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"Could not solve IV for at least two expiries; got {len(iv_by_expiry)}",
        )

    series = pd.Series(iv_by_expiry).sort_index()
    sigma = iv30_atm_50d(series, target_days=req.target_calendar_days)
    if sigma is None:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "iv30_atm_50d returned None — should not happen with two valid expiries",
        )

    # Provenance for the parametric path: count-share by source across the
    # ATM call legs that contributed; coverage is 0 (no wings sampled).
    from collections import Counter

    src_counts = Counter(leg.source for leg in legs_used)
    total = sum(src_counts.values())
    price_source_mix = {src: count / total for src, count in src_counts.items()}
    n_synthetic = sum(1 for leg in legs_used if leg.spread_synthetic)
    vcs = n_synthetic / len(legs_used) if legs_used else 0.0
    prov = IvProvenance(
        iv_source="internal_solver",
        price_source_mix=price_source_mix,
        variance_contribution_synthetic=vcs,
        # Both wing-coverage and single-strike-domination are diagnostics
        # of the chain-replication integral; not meaningful for the
        # parametric ATM-only path. 0.0 signals "not applicable" the same
        # way strike_coverage_score=0.0 does.
        strike_coverage_score=0.0,
        max_single_strike_share=0.0,
        per_strike_contributions=None,
    )

    expiries_used = sorted(iv_by_expiry.keys())
    return Iv30LiveResponse(
        symbol=req.symbol,
        method="parametric",
        target_calendar_days=req.target_calendar_days,
        iv30_act365=float(sigma),
        spot=spot,
        rate=rd.rate,
        dividend_yield=rd.dividend_yield,
        rate_source=rd.source_rate,
        dividend_source=rd.source_dividend,
        expiries_used_calendar_days=expiries_used,
        iv_provenance=_provenance_to_payload(prov),
        snapshot_ts_ms=int(asof.timestamp() * 1000),
    )


def _select_atm_strike_for_parametric(
    quotes: list[NormalizedOptionQuote], spot: float
) -> NormalizedOptionQuote | None:
    """Pick the strike closest to spot whose call leg has a real quote."""
    best, best_dist = None, float("inf")
    for nq in quotes:
        if nq.call.is_zero_bid:
            continue
        dist = abs(nq.strike - spot)
        if dist < best_dist:
            best_dist, best = dist, nq
    return best
