"""Typed price-normalization contract for option mids feeding the IV solver.

Formula: quality_score = 1 − half_spread / mid (per docs/architecture/iv-ownership-research.md §7.9). Two input regimes: live OPRA NBBO mid=(bid+ask)/2, half_spread=(ask−bid)/2; EOD close synthesis (close proxy, spread estimated).
Reference: Internal — docs/architecture/iv-ownership-research.md §4.6 (schema rationale) and §7.9 (quality score formula).
Canonical implementation: app/volatility/price_normalization.py
Validated against: NONE — pending (schema / contract validation owed)

See ``docs/architecture/iv-ownership-research.md`` §4.6 for the schema and
§7.9 for the ``quality_score = 1 - half_spread / mid`` rationale.

The system has two real data regimes for option prices:

1. **Live OPRA NBBO** from Polygon's snapshot endpoint — real bid/ask, real mid.
2. **End-of-day close synthesis** — the only retroactive option on Polygon
   Stocks Starter + Options Starter, where we don't have historical NBBO and
   must infer mid/spread from EOD aggregates.

Architectural commitment: the *call site* declares which regime the data
came from. There is no polymorphic adapter that hides the branch behind a
method name. Every option price that enters the IV solver carries a tag
that says where it came from and how its spread was derived.

`NormalizedOptionPrice` is per-leg (one mid + provenance). `NormalizedOptionQuote`
is per-strike (call-leg + put-leg on the same expiry), the shape that flows
into VIX-style replication.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

PriceSource = Literal[
    "opra_mid",
    "opra_mid_recorded",
    "synthetic_close_proxy",
]
"""Tags the regime the option mid came from. New regimes (e.g. historical
NBBO if Polygon plan upgrades) are added by extending the literal — not by
hiding behind a polymorphic adapter."""


DEFAULT_HALF_SPREAD_RULE = "max($0.05, 0.5%·close)"
"""The half-spread synthesis rule used by the SPY 2024-12-20 golden fixture
build script. Recording the rule text in `NormalizedOptionPrice.half_spread_rule`
keeps the synthesis reproducible without spelunking. Different rules are
allowed; the record-keeping is the load-bearing part."""


TIERED_MONEYNESS_HALF_SPREAD_RULE = (
    "tiered_moneyness: |K-S|/S<0.05→0.5%·S, <0.15→1.0%·S, ≥0.15→2.0%·S; floor $0.05"
)
"""Moneyness-tiered half-spread synthesis rule. Empirical option spreads
widen substantially with distance from spot — flat 0.5%·S is fine ATM
but indefensible at 10Δ where real spreads are ~1–2% of S. The tiered
schedule (Nemes-style) bounds the wing-spread bias without needing
historical NBBO. Default for new synthesis paths going forward;
``DEFAULT_HALF_SPREAD_RULE`` (flat 0.5%·S) is preserved for the SPY
2024-12-20 golden fixture so its reconstructions stay byte-identical.
See ``docs/architecture/iv-research-chat-notes.md`` §5.5."""


def tiered_moneyness_half_spread(
    *,
    close: float,
    strike: float,
    spot: float,
) -> float:
    """Half-spread for the tiered-moneyness rule.

    Tier breakpoints on ``|K-S|/S``:

    - ``< 0.05`` (ATM-ish):     ``max($0.05, 0.005·spot)``
    - ``< 0.15`` (~moderate):   ``max($0.05, 0.010·spot)``
    - ``>= 0.15`` (deep wing):  ``max($0.05, 0.020·spot)``

    Why ``spot`` not ``close`` for the relative term: option premium
    collapses on the wings, so ``0.5%·close`` shrinks toward zero exactly
    where real spreads grow. Anchoring on spot keeps the dollar magnitude
    realistic on far strikes. ``close`` is still passed to support a
    future "tighter-than-tier" override (e.g. an unusually liquid wing).
    """
    if spot <= 0:
        raise ValueError(f"spot must be > 0, got {spot}")
    if strike <= 0:
        raise ValueError(f"strike must be > 0, got {strike}")
    if close < 0:
        raise ValueError(f"close must be >= 0, got {close}")
    rel_dist = abs(strike - spot) / spot
    if rel_dist < 0.05:
        return max(0.05, 0.005 * spot)
    if rel_dist < 0.15:
        return max(0.05, 0.010 * spot)
    return max(0.05, 0.020 * spot)


@dataclass(frozen=True)
class NormalizedOptionPrice:
    """A single option leg's mid price with full provenance.

    Parameters
    ----------
    mid : non-negative; 0.0 represents zero-bid (no quote, used for
        wing-truncation logic in VIX replication).
    source : one of ``PriceSource`` — declared at construction.
    spread_estimate : half-spread in price units; ``None`` if the leg is
        zero-bid or otherwise has no measurable spread.
    spread_synthetic : ``True`` if ``spread_estimate`` came from a rule
        rather than from observed bid/ask.
    half_spread_rule : the rule text (e.g. ``"max($0.05, 0.5%·close)"``).
        Required when ``spread_synthetic`` is ``True``; ``None`` when the
        spread came from real bid/ask.
    quality_score : ``1 - half_spread / mid`` clamped to ``[0, 1]``. A
        wide-spread or zero-mid leg scores low; a tight-spread ATM leg
        scores high.
    """

    mid: float
    source: PriceSource
    spread_estimate: float | None
    spread_synthetic: bool
    half_spread_rule: str | None
    quality_score: float

    def __post_init__(self) -> None:
        if self.mid < 0:
            raise ValueError(f"mid must be >= 0, got {self.mid}")
        if self.spread_estimate is not None and self.spread_estimate < 0:
            raise ValueError(f"spread_estimate must be >= 0, got {self.spread_estimate}")
        if not 0.0 <= self.quality_score <= 1.0:
            raise ValueError(f"quality_score must be in [0, 1], got {self.quality_score}")
        if self.spread_synthetic and self.half_spread_rule is None:
            raise ValueError("spread_synthetic=True requires a half_spread_rule")

    @property
    def is_zero_bid(self) -> bool:
        """Convenience for the VIX-replication wing-truncation rule."""
        return self.mid <= 0.0


def _quality_from_half_spread(mid: float, half_spread: float) -> float:
    """`1 - half_spread / mid`, clamped to [0, 1]. Returns 0.0 for mid <= 0."""
    if mid <= 0:
        return 0.0
    raw = 1.0 - (half_spread / mid)
    return max(0.0, min(1.0, raw))


def from_snapshot_quote(bid: float, ask: float) -> NormalizedOptionPrice:
    """Construct from a real OPRA NBBO bid/ask pair (live snapshot).

    Zero-bid (no buyer), zero-ask (no seller), or inverted (bid > ask)
    input collapses to a quality-zero, mid-zero record so the
    wing-truncation rule in VIX replication still fires correctly. This
    matches the legacy ``OptionQuote.call_mid`` semantics: a leg with no
    bid is not a real quote, even if an ask exists.
    """
    if bid <= 0 or ask <= 0 or bid > ask:
        return NormalizedOptionPrice(
            mid=0.0,
            source="opra_mid",
            spread_estimate=None,
            spread_synthetic=False,
            half_spread_rule=None,
            quality_score=0.0,
        )
    mid = (bid + ask) / 2.0
    half_spread = (ask - bid) / 2.0
    return NormalizedOptionPrice(
        mid=mid,
        source="opra_mid",
        spread_estimate=half_spread,
        spread_synthetic=False,
        half_spread_rule=None,
        quality_score=_quality_from_half_spread(mid, half_spread),
    )


def from_recorded_snapshot(bid: float, ask: float) -> NormalizedOptionPrice:
    """Construct from a recorder-persisted bid/ask pair.

    Same math as ``from_snapshot_quote``; the distinct ``source`` tag lets
    downstream consumers tell live vs. recorded apart in provenance reports
    (e.g. the VRP route emits ``iv_source: 'recorded_internal'`` when this
    is the dominant source).
    """
    base = from_snapshot_quote(bid, ask)
    # Re-emit with the recorded-source tag.
    return NormalizedOptionPrice(
        mid=base.mid,
        source="opra_mid_recorded",
        spread_estimate=base.spread_estimate,
        spread_synthetic=False,
        half_spread_rule=None,
        quality_score=base.quality_score,
    )


def from_eod_close(
    close: float,
    *,
    rule: str = DEFAULT_HALF_SPREAD_RULE,
) -> NormalizedOptionPrice:
    """Construct from an end-of-day close, synthesizing bid/ask via a rule.

    The default rule matches the SPY 2024-12-20 golden-fixture build script:
    ``half_spread = max($0.05, 0.5% · close); zero-bid below $0.05``. A
    contract whose close is below the absolute floor is treated as zero-bid
    (mid=0, quality=0) so wing-truncation behaves as it would on real OPRA.
    """
    if close < 0:
        raise ValueError(f"close must be >= 0, got {close}")
    if close < 0.05:
        return NormalizedOptionPrice(
            mid=0.0,
            source="synthetic_close_proxy",
            spread_estimate=None,
            spread_synthetic=True,
            half_spread_rule=rule,
            quality_score=0.0,
        )
    half_spread = max(0.05, 0.005 * close)
    return NormalizedOptionPrice(
        mid=close,
        source="synthetic_close_proxy",
        spread_estimate=half_spread,
        spread_synthetic=True,
        half_spread_rule=rule,
        quality_score=_quality_from_half_spread(close, half_spread),
    )


def from_eod_close_tiered_moneyness(
    close: float,
    *,
    strike: float,
    spot: float,
    rule: str = TIERED_MONEYNESS_HALF_SPREAD_RULE,
) -> NormalizedOptionPrice:
    """Synthesize a quote from EOD close using a moneyness-tiered half-spread.

    Default for new synthesis paths. The flat-rule ``from_eod_close`` is
    retained for golden-fixture reproducibility; new code that synthesizes
    historical option quotes should use this function instead so deep-OTM
    legs aren't priced with a 0.5%-of-close spread that empirically
    understates wing spreads by an order of magnitude.

    Zero-bid handling matches ``from_eod_close``: if ``close`` is below
    the absolute $0.05 floor the leg is treated as zero-bid (mid=0,
    quality=0).
    """
    if close < 0:
        raise ValueError(f"close must be >= 0, got {close}")
    if close < 0.05:
        return NormalizedOptionPrice(
            mid=0.0,
            source="synthetic_close_proxy",
            spread_estimate=None,
            spread_synthetic=True,
            half_spread_rule=rule,
            quality_score=0.0,
        )
    half_spread = tiered_moneyness_half_spread(
        close=close, strike=strike, spot=spot
    )
    return NormalizedOptionPrice(
        mid=close,
        source="synthetic_close_proxy",
        spread_estimate=half_spread,
        spread_synthetic=True,
        half_spread_rule=rule,
        quality_score=_quality_from_half_spread(close, half_spread),
    )


@dataclass(frozen=True)
class NormalizedOptionQuote:
    """Per-strike normalized prices: call leg + put leg on the same expiry.

    This is what flows into ``replicate_expiry_variance_with_provenance`` —
    same shape as the legacy ``OptionQuote`` but every leg carries a
    ``PriceSource`` tag so the variance-contribution-weighted synthetic share
    can be computed downstream.
    """

    strike: float
    call: NormalizedOptionPrice
    put: NormalizedOptionPrice

    def __post_init__(self) -> None:
        if self.strike <= 0:
            raise ValueError(f"strike must be > 0, got {self.strike}")
