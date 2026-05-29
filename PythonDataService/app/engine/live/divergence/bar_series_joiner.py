"""Layer B — ``BarSeriesJoiner``.

Join a live-decision bar series (PRD-A schema: ``bar_source`` +
``bar_open/high/low/close/volume`` populated) against a canonical Polygon
bar series for the same session, keyed on ``bar_close_ms`` (``int64 ms
UTC``). Produces a per-bar comparison handling three cases: matched,
live-only (canonical missing → ``COVERAGE_GAP`` candidate), canonical-only
(live missing → ``COVERAGE_GAP`` candidate).

No silent forward-fill or interpolation — a missing bar surfaces as a
half-join, never a synthesised value (``.claude/rules/numerical-rigor.md``).
Pure function.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from app.engine.live.artifacts import DecisionRow


@dataclass(frozen=True)
class CanonicalBar:
    """One canonical Polygon OHLCV bar. ``bar_close_ms`` is ``int64 ms UTC``."""

    bar_close_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class JoinedBar:
    """One per-bar comparison row. ``gap_side`` names the MISSING side
    (``"live"`` or ``"canonical"``) for a coverage gap, or ``None`` when
    both sides are present."""

    bar_close_ms: int
    live: DecisionRow | None
    canonical: CanonicalBar | None
    gap_side: str | None


def join_bar_series(
    live_decisions: Sequence[DecisionRow],
    canonical_bars: Sequence[CanonicalBar],
) -> list[JoinedBar]:
    """Join the two series on ``bar_close_ms``, sorted ascending."""
    live_by_bar = {d.bar_close_ms: d for d in live_decisions}
    canonical_by_bar = {b.bar_close_ms: b for b in canonical_bars}

    joined: list[JoinedBar] = []
    for bar_close_ms in sorted(set(live_by_bar) | set(canonical_by_bar)):
        live = live_by_bar.get(bar_close_ms)
        canonical = canonical_by_bar.get(bar_close_ms)
        if live is not None and canonical is not None:
            gap_side = None
        elif canonical is None:
            gap_side = "canonical"
        else:
            gap_side = "live"
        joined.append(
            JoinedBar(
                bar_close_ms=bar_close_ms,
                live=live,
                canonical=canonical,
                gap_side=gap_side,
            )
        )
    return joined
