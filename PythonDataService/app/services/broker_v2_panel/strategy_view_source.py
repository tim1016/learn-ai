"""One bot's strategy view, read from what the bot recorded (#2639).

The candles are the bot's own decision bars and the numbers are the ones its
Signal Program computed: decision receipts for the bars this run decided, and
the run's before-start evaluations for the warmup bars behind its "bot
started" line. Nothing here recomputes a strategy indicator.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.broker.alpaca.clerk.sqlite.decision_receipts import (
    MAX_DECISION_RECEIPT_READ,
    QUARANTINE_OUTCOME,
    DecisionReceipt,
)
from app.schemas.decision_explanation import DecisionExplanationRecord
from app.schemas.strategy_view import StrategyViewCandle, StrategyViewResponse
from app.services.broker_v2_panel.panel_data_source import read_run_ledger, selected_panel_authority
from app.services.broker_v2_panel.panel_errors import PanelUnavailableError
from app.services.broker_v2_panel.sqlite_panel_source import (
    SqlitePanelDecisionUnavailable,
    read_sqlite_decision_receipts,
)
from app.services.strategy_view import ResolvedStrategyView, StrategyViewUnavailableError

BEFORE_START_TEXT = "Before start · not acted on"


async def get_strategy_view(broker: str, account_id: str, sid: str) -> StrategyViewResponse:
    """Everything the bot's strategy view draws, for its current (or latest) run."""
    async with selected_panel_authority(broker, account_id, sid) as (_resolved, _registry, binding, facade):
        if facade is None:
            raise PanelUnavailableError(
                "This bot's Clerk is unavailable.",
                detail="Restore the selected authority, then refresh.",
            )
        try:
            view = ResolvedStrategyView.for_settings(
                binding.strategy_key, binding.strategy_params, symbol=binding.symbol
            )
        except StrategyViewUnavailableError as exc:
            raise PanelUnavailableError("This bot's strategy view is unavailable.", detail=str(exc)) from exc
        try:
            receipts = read_sqlite_decision_receipts(broker, sid, limit=MAX_DECISION_RECEIPT_READ, facade=facade)
        except SqlitePanelDecisionUnavailable as exc:
            raise PanelUnavailableError("This bot's decision record is unavailable.", detail=str(exc)) from exc
        run = facade.repository.latest_run(sid)
        before_start = read_run_ledger(
            binding,
            lambda ledger: ledger.before_start_evaluations(run_id=binding.run_id),
            unavailable_action="strategy_view_before_start_unavailable",
        )
    current = run is not None and run.lifecycle_run_id == binding.run_id
    return build_strategy_view(
        view,
        symbol=binding.symbol,
        run_id=binding.run_id,
        run_ids=frozenset({binding.run_id, f"{sid}:{binding.run_id}"}),
        run_started_at_ms=run.started_at_ms if current else None,
        run_stopped_at_ms=run.stopped_at_ms if current else None,
        receipts=receipts or [],
        before_start=before_start,
    )


def build_strategy_view(
    view: ResolvedStrategyView,
    *,
    symbol: str,
    run_id: str,
    run_ids: frozenset[str],
    run_started_at_ms: int | None,
    run_stopped_at_ms: int | None,
    receipts: Sequence[DecisionReceipt],
    before_start: Sequence[DecisionExplanationRecord] | None,
) -> StrategyViewResponse:
    """Merge one run's decisions and before-start evaluations into decision candles.

    ``run_ids`` are the spellings a receipt's facts use for this run: the
    runner's lifecycle id and the Clerk's ``<sid>:<lifecycle>``. A bar this
    run decided is never drawn as before-start, whether or not its receipt
    recorded values: a re-entered run's warmup replays bars it already
    decided, and a bar that closed after the run started was not warmup.
    """
    decided_closes: set[int] = set()
    candles: list[StrategyViewCandle] = []
    unexplained = unshown = 0
    for receipt in receipts:
        if receipt.run_id not in run_ids or receipt.outcome == QUARANTINE_OUTCOME:
            continue
        if receipt.decision_bar_close_ms is not None:
            decided_closes.add(receipt.decision_bar_close_ms)
        record = receipt.explanation
        if record is None:
            if receipt.explanation_unreadable:
                unshown += 1
            else:
                unexplained += 1
            continue
        decided_closes.add(record.bar.end_ms)
        rendered = view.render_or_none(record)
        if rendered is None:
            unshown += 1
            continue
        candles.append(
            StrategyViewCandle(
                **_bar_fields(record),
                phase="decision",
                outcome=receipt.outcome,
                reason_code=receipt.reason_code,
                decision_seq=receipt.seq,
                explanation=rendered,
                gates=view.gate_results(record),
            )
        )
    for record in before_start or ():
        close_ms = record.bar.end_ms
        if close_ms in decided_closes or (run_started_at_ms is not None and close_ms > run_started_at_ms):
            continue
        rendered = view.render_or_none(record)
        if rendered is not None:
            candles.append(
                StrategyViewCandle(
                    **_bar_fields(record),
                    phase="before_start",
                    phase_text=BEFORE_START_TEXT,
                    explanation=rendered,
                    gates=view.gate_results(record),
                )
            )
    candles.sort(key=lambda candle: candle.bar_close_ms)
    return StrategyViewResponse(
        strategy_key=view.strategy_key,
        strategy_name=view.registration.display_name,
        symbol=symbol,
        decision_timeframe_ms=view.decision_timeframe_ms,
        run_id=run_id,
        run_started_at_ms=run_started_at_ms,
        run_stopped_at_ms=run_stopped_at_ms,
        declaration=view.declaration(),
        settings=view.scalar_settings,
        candles=candles,
        unexplained_decision_count=unexplained,
        notices=_notices(before_start, unexplained=unexplained, unshown=unshown),
    )


def _notices(before_start: Sequence[DecisionExplanationRecord] | None, *, unexplained: int, unshown: int) -> list[str]:
    notices: list[str] = []
    if before_start is None:
        notices.append("Bars from before the bot started are unavailable for this run.")
    elif not before_start:
        notices.append("This run saved no bars from before it started.")
    if unexplained:
        noun, verb = ("decision", "was") if unexplained == 1 else ("decisions", "were")
        notices.append(
            f"{unexplained} {noun} in this run {verb} recorded before decisions saved their values: "
            "values not recorded."
        )
    if unshown:
        noun = "decision's values" if unshown == 1 else "decisions' values"
        notices.append(f"{unshown} {noun} could not be shown by this build.")
    return notices


def _bar_fields(record: DecisionExplanationRecord) -> dict[str, float | int]:
    bar = record.bar
    return {
        "bar_start_ms": bar.start_ms,
        "bar_close_ms": bar.end_ms,
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "volume": bar.volume,
    }


__all__ = ["BEFORE_START_TEXT", "build_strategy_view", "get_strategy_view"]
