"""Layer A — ``ExecutionMatcher``.

Given a strategy's decisions + executions for one trading day, produce a
matched-ledger of ``(decision | None, execution | None)`` pairs. Pure
function over typed rows — the pipeline reads parquet, the matcher
consumes ``DecisionRow`` / ``ExecutionRow`` (PRD-B Implementation
Decisions → "Modules to build").

Match key is ``client_order_id`` when an order-link maps a decision's
``bar_close_ms`` to it; otherwise the matcher falls back to the composite
``(strategy_instance_id, bar_close_ms, intended_action)`` key resolved as
the chronologically-first execution at or after the bar close — the
as-of merge ``reconcile._attach_fills`` already uses for the SPY 15-min
strategy.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from app.engine.live.artifacts import DecisionRow, ExecutionRow

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MatchedLedgerRow:
    """One ``(decision, execution)`` pair, or a half-pair when one side is missing.

    ``match_basis`` records how the pair was joined (``client_order_id``,
    ``composite_key``, or ``unmatched``). ``flags`` carries matcher-level
    annotations (e.g. ``partial``, ``stale_session``) that the downstream
    classifier consumes.
    """

    decision: DecisionRow | None
    execution: ExecutionRow | None
    match_basis: str
    flags: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_missed(self) -> bool:
        """A decision intended an action but no execution was matched to it."""
        return self.decision is not None and self.execution is None

    @property
    def is_extra(self) -> bool:
        """An execution exists with no decision row to explain it."""
        return self.decision is None and self.execution is not None


def match_executions(
    decisions: Sequence[DecisionRow],
    executions: Sequence[ExecutionRow],
    *,
    session_window: tuple[int, int],
    order_links: Mapping[str, int] | None = None,
) -> list[MatchedLedgerRow]:
    """Match decisions to executions into a per-day ledger.

    ``session_window`` is ``(start_ms, end_ms)`` in ``int64 ms UTC``;
    ``order_links`` maps ``client_order_id -> bar_close_ms`` (the PRD-A
    owned-orders join). Both are supplied by the Layer A pipeline.
    """
    links = order_links or {}
    session_start_ms, _ = session_window
    bar_to_decision = {d.bar_close_ms: d for d in decisions}
    ledger: list[MatchedLedgerRow] = []
    matched_decision_bars: set[int] = set()

    # Layer A is executing-only. Shadow simulated fills are filtered at the
    # input boundary with an explicit reason — never silently — so a shadow
    # run's decisions still surface as MISSED rather than being "satisfied"
    # by a synthesised fill (PRD-B Further Notes → shadow forward-compat).
    shadow = [e for e in executions if e.execution_source == "shadow_sim"]
    if shadow:
        logger.info(
            "ExecutionMatcher: filtered %d shadow_sim fill(s) at input boundary "
            "(Layer A is executing-only); exec_ids=%s",
            len(shadow),
            [e.exec_id for e in shadow],
        )
    executions = [e for e in executions if e.execution_source != "shadow_sim"]

    # Stage 0 — quarantine stale fills. A fill timestamped before the session
    # is a leftover order from a prior run: EXTRA with reason ``stale_session``.
    # It never consumes this session's decisions.
    live_executions: list[ExecutionRow] = []
    for execution in executions:
        if execution.ts_ms < session_start_ms:
            ledger.append(
                MatchedLedgerRow(
                    decision=None,
                    execution=execution,
                    match_basis="unmatched",
                    flags=("stale_session",),
                )
            )
        else:
            live_executions.append(execution)

    # Stage 1 — authoritative ``client_order_id`` match via the owned-orders
    # link. Group fills by their linked bar so a decision filled by >1
    # execution is flagged ``partial`` across every leg.
    fills_by_bar: dict[int, list[ExecutionRow]] = {}
    remaining: list[ExecutionRow] = []
    for execution in live_executions:
        bar_close_ms = links.get(execution.client_order_id)
        decision = bar_to_decision.get(bar_close_ms) if bar_close_ms is not None else None
        if decision is not None and decision.signal != "HOLD":
            fills_by_bar.setdefault(bar_close_ms, []).append(execution)
        else:
            remaining.append(execution)

    for bar_close_ms, fills in fills_by_bar.items():
        decision = bar_to_decision[bar_close_ms]
        matched_decision_bars.add(bar_close_ms)
        flags = ("partial",) if len(fills) > 1 else ()
        for execution in fills:
            ledger.append(
                MatchedLedgerRow(
                    decision=decision,
                    execution=execution,
                    match_basis="client_order_id",
                    flags=flags,
                )
            )

    # Stage 2 — composite-key fallback for signalled decisions left unmatched:
    # earliest unused live fill at/after ``bar_close_ms`` whose direction
    # agrees with ``intended_action``. (strategy_instance_id is implicit — the
    # matcher is called per instance.)
    used_exec_ids: set[str] = {e.exec_id for fills in fills_by_bar.values() for e in fills}
    fallback_candidates = sorted(remaining, key=lambda e: e.ts_ms)
    signalled_unmatched = sorted(
        (
            d
            for d in decisions
            if d.signal != "HOLD" and d.bar_close_ms not in matched_decision_bars
        ),
        key=lambda d: d.bar_close_ms,
    )
    for decision in signalled_unmatched:
        for execution in fallback_candidates:
            if execution.exec_id in used_exec_ids or execution.ts_ms < decision.bar_close_ms:
                continue
            if not _direction_agrees(decision.intended_action, execution.fill_quantity):
                continue
            used_exec_ids.add(execution.exec_id)
            matched_decision_bars.add(decision.bar_close_ms)
            ledger.append(
                MatchedLedgerRow(
                    decision=decision, execution=execution, match_basis="composite_key"
                )
            )
            break

    # Stage 3 — leftovers. Signalled-but-unfilled decisions are MISSED; live
    # fills that matched nothing are EXTRA.
    for decision in decisions:
        if decision.signal == "HOLD" or decision.bar_close_ms in matched_decision_bars:
            continue
        ledger.append(
            MatchedLedgerRow(decision=decision, execution=None, match_basis="unmatched")
        )
    for execution in remaining:
        if execution.exec_id not in used_exec_ids:
            ledger.append(
                MatchedLedgerRow(decision=None, execution=execution, match_basis="unmatched")
            )

    return ledger


def _direction_agrees(intended_action: str, fill_quantity: int) -> bool:
    """A buy intent fills positive quantity; a sell intent fills negative."""
    action = intended_action.upper()
    if action == "BUY":
        return fill_quantity > 0
    if action == "SELL":
        return fill_quantity < 0
    return False
