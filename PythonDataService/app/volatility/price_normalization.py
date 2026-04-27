"""Typed price-normalization contract for option mids feeding the IV solver.

Step A of the IV-ownership plan (`docs/architecture/iv-ownership-plan.md`).

The system has two real data regimes for option prices:

1. **Live OPRA NBBO** from Polygon's snapshot endpoint — real bid/ask, real mid.
2. **End-of-day close synthesis** — the only retroactive option on Polygon
   Stocks Starter + Options Starter, where we don't have historical NBBO and
   must infer mid/spread from EOD aggregates.

The plan's architectural commitment (Round 1 rebuttal): the *call site*
declares which regime the data came from. There is no polymorphic adapter
that hides the branch behind a method name. Every option price that enters
the IV solver carries a tag that says where it came from and how its
spread was derived.

`NormalizedOptionPrice` is per-leg (one mid + provenance). `NormalizedOptionQuote`
is per-strike (call-leg + put-leg on the same expiry), the shape that flows
into VIX-style replication.

See `docs/architecture/iv-ownership-decisions.md` for the rationale on
`quality_score = 1 - half_spread / mid` (Q5).
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
