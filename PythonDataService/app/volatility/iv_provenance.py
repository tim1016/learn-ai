"""Typed provenance for IV30 outputs.

See ``docs/architecture/iv-ownership-research.md`` §4.6 for the consolidated
schema rationale and §7.3 for why count-share *and* variance-share are both
recorded.

`IvSource` and `IvProvenance` describe the **derived** volatility — distinct
from `PriceSource` and `NormalizedOptionPrice` (in `price_normalization.py`)
which describe the **inputs** to the IV solver. The separation prevents the
"single `polygon_computed_iv` enum" category violation: a chain can be 100%
real OPRA mids (`PriceSource`) but still be solved by either our internal
solver or by trusting Polygon's IV field (`IvSource`). The repo's
sovereignty commitment is to never store Polygon's IV as authoritative —
the recorder always recomputes — but the enum allows for diagnostic
comparison.

The two operationally important fields are:

- ``variance_contribution_synthetic``: weighted by VIX-replication
  contribution, not by raw count (Round 3 issue #2). A chain whose ATM is
  real OPRA but whose deep wings are synthetic-close-proxy can have a high
  ``synthetic_count_share`` but a low ``variance_contribution_synthetic``,
  because ATM dominates the integration weight. The latter is what the
  signal generator should gate on.
- ``strike_coverage_score``: how far into the wings the chain extends
  before the two-consecutive-zero-bid truncation rule fires, expressed as
  a fraction of `5σ` average across calls/puts. Surface this so debugging
  knows when to look at wing truncation as the cause of replication
  disagreement.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from app.volatility.price_normalization import PriceSource

IvSource = Literal[
    "internal_solver",
    "polygon_field",
]
"""Where the IV number came from. ``internal_solver`` is our 3-tier
Newton/QL/Brent chain; ``polygon_field`` is Polygon's snapshot-included IV.
The plan explicitly forbids storing ``polygon_field`` as a production IV
value — it exists in the enum for diagnostic comparison only."""


@dataclass(frozen=True)
class IvProvenance:
    """Provenance for an IV30 (or any chain-replication-derived IV) output.

    Parameters
    ----------
    iv_source : ``"internal_solver"`` for everything we ship in production.
    price_source_mix : share-by-count of each ``PriceSource`` across legs
        actually used in the integration. Sums to 1.0 (or 0.0 if no legs
        were used, which would be a separate failure).
    variance_contribution_synthetic : share of VIX-replication variance
        contribution that came from ``synthetic_close_proxy`` legs. The
        operational metric for "how much of this IV is built on synthesis."
    strike_coverage_score : ``min(1, avg_wings_in_sigma / 5)`` across
        calls/puts. 1.0 means the chain extends 5σ or more on both sides
        before the wing-truncation rule fires.
    per_strike_contributions : opt-in via ``debug=True``. Each entry has
        ``{strike, kind, dK, Q, c_i, active_leg_source, active_leg_synthetic}``.
        ``None`` by default — the production hot path doesn't pay the
        list-allocation cost.
    """

    iv_source: IvSource
    price_source_mix: dict[PriceSource, float]
    variance_contribution_synthetic: float
    strike_coverage_score: float
    per_strike_contributions: list[dict] | None = field(default=None)

    def __post_init__(self) -> None:
        if not 0.0 <= self.variance_contribution_synthetic <= 1.0 + 1e-9:
            raise ValueError(
                f"variance_contribution_synthetic must be in [0, 1], "
                f"got {self.variance_contribution_synthetic}"
            )
        if not 0.0 <= self.strike_coverage_score <= 1.0 + 1e-9:
            raise ValueError(
                f"strike_coverage_score must be in [0, 1], "
                f"got {self.strike_coverage_score}"
            )
        if self.price_source_mix:
            total = sum(self.price_source_mix.values())
            if not (abs(total - 1.0) < 1e-6 or abs(total) < 1e-9):
                raise ValueError(
                    f"price_source_mix must sum to 1.0 or 0.0, got {total}"
                )
            for share in self.price_source_mix.values():
                if share < -1e-9 or share > 1.0 + 1e-9:
                    raise ValueError(
                        f"price_source_mix shares must be in [0, 1], got {share}"
                    )
