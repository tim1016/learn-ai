"""Layer B — ``ReplayDivergenceClassifier``.

Given a joined-bar row (``BarSeriesJoiner`` output) plus the replayed
``DecisionRow`` (computed by the Layer B pipeline replaying the
``StrategySpec`` against canonical bars), emit zero or more
``ReplayDivergence`` rows. Enum-driven, no I/O.

Categories quantify how much live-vs-canonical divergence is the data
source rather than execution:
  * ``DATA_DRIFT_{O,H,L,C,V}`` — OHLCV value drift beyond tolerance.
  * ``INDICATOR_STATE_DRIFT`` — same input bars, different indicator output.
  * ``DECISION_DRIFT`` — same indicator state, different signal (a real bug).
  * ``COVERAGE_GAP`` — bar present on one side, missing on the other.
  * ``TRADE_GRAPH_DRIFT`` — cumulative effect on the matched-trade ledger
    (end-of-day, computed from the per-bar divergences elsewhere).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from app.engine.live.artifacts import DecisionRow
from app.engine.live.divergence.bar_series_joiner import JoinedBar
from app.engine.live.divergence.common import Severity


class ReplayDivergenceCategory(StrEnum):
    """Layer B divergence categories (PRD-B)."""

    DATA_DRIFT_O = "data_drift_o"
    DATA_DRIFT_H = "data_drift_h"
    DATA_DRIFT_L = "data_drift_l"
    DATA_DRIFT_C = "data_drift_c"
    DATA_DRIFT_V = "data_drift_v"
    INDICATOR_STATE_DRIFT = "indicator_state_drift"
    DECISION_DRIFT = "decision_drift"
    COVERAGE_GAP = "coverage_gap"
    TRADE_GRAPH_DRIFT = "trade_graph_drift"


@dataclass(frozen=True)
class ReplayTolerances:
    """Per-category Layer B tolerances. Defaults from PRD-B §Implementation
    Decisions; the ``DATA_DRIFT_*`` non-gating posture reflects the
    ADR-locked reality that IBKR paper data is delayed (a noise floor Layer B
    exists to quantify)."""

    bar_value_atol: float = 0.01  # price tolerance for O/H/L/C
    volume_atol: float = 1.0  # share tolerance for V
    indicator_atol: float = 1e-9  # numerical-rigor default for INDICATOR_STATE_DRIFT
    trade_graph_node_atol: int = 1  # allowable diverging ENTER/EXIT nodes


@dataclass(frozen=True)
class ReplayDivergence:
    """One typed replay (data/indicator/decision) divergence."""

    category: ReplayDivergenceCategory
    severity: Severity
    magnitude: float
    applied_tolerance: float
    bar_close_ms: int | None = None
    detail: str = ""


def classify_replay_divergences(
    joined: JoinedBar,
    replayed_decision: DecisionRow | None,
    tolerances: ReplayTolerances,
) -> list[ReplayDivergence]:
    """Emit the divergences implied by one joined-bar + replayed-decision pair."""
    divergences: list[ReplayDivergence] = []
    live = joined.live
    canonical = joined.canonical

    # COVERAGE_GAP — a bar present on one side only. Non-gating: delayed paper
    # data has a coverage noise floor Layer B exists to quantify. With a gap
    # there is nothing to compare, so the per-bar checks below are skipped.
    if joined.gap_side is not None:
        return [
            ReplayDivergence(
                category=ReplayDivergenceCategory.COVERAGE_GAP,
                severity=Severity.NON_GATING,
                magnitude=1.0,
                applied_tolerance=0.0,
                bar_close_ms=joined.bar_close_ms,
                detail=f"bar missing on the {joined.gap_side} side",
            )
        ]

    if live is not None and canonical is not None:
        for category, live_value, canonical_value, atol in (
            (
                ReplayDivergenceCategory.DATA_DRIFT_O,
                live.bar_open,
                canonical.open,
                tolerances.bar_value_atol,
            ),
            (
                ReplayDivergenceCategory.DATA_DRIFT_H,
                live.bar_high,
                canonical.high,
                tolerances.bar_value_atol,
            ),
            (
                ReplayDivergenceCategory.DATA_DRIFT_L,
                live.bar_low,
                canonical.low,
                tolerances.bar_value_atol,
            ),
            (
                ReplayDivergenceCategory.DATA_DRIFT_C,
                live.bar_close,
                canonical.close,
                tolerances.bar_value_atol,
            ),
            (
                ReplayDivergenceCategory.DATA_DRIFT_V,
                live.bar_volume,
                canonical.volume,
                tolerances.volume_atol,
            ),
        ):
            # A field the live run never captured (NULL — the live engine
            # populates only bar_close today) is absent, not a drift. Skip it
            # rather than fabricate a comparison (no forward-fill, no invented
            # value — per .claude/rules/numerical-rigor.md).
            if live_value is None or canonical_value is None:
                continue
            drift = abs(live_value - canonical_value)
            if drift > atol:
                divergences.append(
                    ReplayDivergence(
                        category=category,
                        severity=Severity.NON_GATING,
                        magnitude=drift,
                        applied_tolerance=atol,
                        bar_close_ms=joined.bar_close_ms,
                    )
                )

    # INDICATOR_STATE_DRIFT — same input bars, different indicator output.
    # Gating: a drift here is a warmup or rounding gap that corrupts signals.
    indicator_drifted = False
    if live is not None and replayed_decision is not None:
        for name, live_value in live.indicator_values.items():
            replayed_value = replayed_decision.indicator_values.get(name)
            if replayed_value is None:
                continue
            drift = abs(float(live_value) - float(replayed_value))
            if drift > tolerances.indicator_atol:
                indicator_drifted = True
                divergences.append(
                    ReplayDivergence(
                        category=ReplayDivergenceCategory.INDICATOR_STATE_DRIFT,
                        severity=Severity.GATING,
                        magnitude=drift,
                        applied_tolerance=tolerances.indicator_atol,
                        bar_close_ms=joined.bar_close_ms,
                        detail=f"indicator {name!r} drifted",
                    )
                )

        # DECISION_DRIFT — same indicator state, different signal. Only when
        # the indicators agree: otherwise the signal difference is an expected
        # downstream consequence of INDICATOR_STATE_DRIFT, not its own bug.
        if not indicator_drifted and live.signal != replayed_decision.signal:
            divergences.append(
                ReplayDivergence(
                    category=ReplayDivergenceCategory.DECISION_DRIFT,
                    severity=Severity.GATING,
                    magnitude=1.0,
                    applied_tolerance=0.0,
                    bar_close_ms=joined.bar_close_ms,
                    detail=(
                        "signal differs with matching indicator state — indicates a "
                        "real bug in indicator math; check warmup"
                    ),
                )
            )

    return divergences


_TRADE_GRAPH_SIGNALS = frozenset({"ENTER", "EXIT"})


def classify_trade_graph_drift(
    live_decisions: Sequence[DecisionRow],
    replayed_decisions: Sequence[DecisionRow],
    tolerances: ReplayTolerances,
) -> list[ReplayDivergence]:
    """End-of-day TRADE_GRAPH_DRIFT — the cumulative effect on the matched-trade
    ledger. The trade graph is the set of ``(bar_close_ms, signal)`` ENTER/EXIT
    nodes; drift is the symmetric difference between the live and replayed
    graphs. Gating once it exceeds ``trade_graph_node_atol``.
    """

    def _graph(decisions: Sequence[DecisionRow]) -> set[tuple[int, str]]:
        return {
            (d.bar_close_ms, d.signal)
            for d in decisions
            if d.signal in _TRADE_GRAPH_SIGNALS
        }

    drift_nodes = _graph(live_decisions) ^ _graph(replayed_decisions)
    if len(drift_nodes) <= tolerances.trade_graph_node_atol:
        return []
    return [
        ReplayDivergence(
            category=ReplayDivergenceCategory.TRADE_GRAPH_DRIFT,
            severity=Severity.GATING,
            magnitude=float(len(drift_nodes)),
            applied_tolerance=float(tolerances.trade_graph_node_atol),
            detail=f"{len(drift_nodes)} trade-graph node(s) diverged",
        )
    ]
