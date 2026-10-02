"""A saved backtest's strategy view, replayed from the run's own record (#2639 D13).

The run is run again exactly as it ran -- its engine, fills, closing-bar rule,
evaluation boundary, settings and bars (``replay_engine_run``) -- and each
decision it stages is kept. The replay is shown as the run's own only when its
bars still match the data receipt the run recorded and it reproduces the run's
stored trades; otherwise, and for a run that cannot be replayed exactly at all
(a LEAN run, or one whose strategy has changed since), the view refuses and
says why. Nothing about the decisions is stored with the run.
"""

from __future__ import annotations

import json
import math
from collections import deque

from pydantic import ValidationError

from app.engine.data.trade_bar import TradeBar
from app.engine.strategy.base import LoggedTrade
from app.engine.strategy.registry import strategy_program_version
from app.engine.strategy.signal_program import SignalDecision
from app.research.backtest_runs.evidence_provenance import RunEvidenceProvenance
from app.research.backtest_runs.repository import RunDetail
from app.schemas.decision_explanation import DecisionExplanationRecord
from app.schemas.engine_backtest import EngineBacktestRequest
from app.schemas.strategy_view import StrategyViewCandle, StrategyViewResponse
from app.services.engine_backtest_service import (
    SavedRunNotReplayable,
    replay_availability_hash,
    replay_engine_run,
)
from app.services.strategy_view import ResolvedStrategyView, StrategyViewUnavailableError
from app.utils.session_anchors import et_date_at_ms

# A display read must stay a size a browser can draw; a longer run shows its latest candles.
MAX_STRATEGY_VIEW_CANDLES = 10_000
_OUTCOME = {"ENTER": "enter_intent", "EXIT": "exit_intent", "HOLD": "no_action"}
# Prices are compared at the indicator default (numerical-rigor.md).
_PRICE_ATOL = 1e-9


def build_backtest_run_strategy_view(run: RunDetail) -> StrategyViewResponse:
    """The run's strategy view, or ``SavedRunNotReplayable`` naming why it cannot be shown. Blocking."""
    if run.source != "engine":
        raise SavedRunNotReplayable(
            "This is a LEAN run. Its decisions are LEAN's own, and this view replays the Python engine."
        )
    current = strategy_program_version(run.strategy_name)
    if run.program_version is None:
        raise SavedRunNotReplayable("This run recorded no program version, so it cannot be replayed exactly.")
    if run.program_version != current:
        raise SavedRunNotReplayable(
            f"The strategy has changed since this run: it ran {run.program_version}, and this build has {current}."
        )
    request = _request_from_run(run)
    try:
        view = ResolvedStrategyView.for_settings(run.strategy_name, request.params, symbol=run.symbol)
    except StrategyViewUnavailableError as exc:
        raise SavedRunNotReplayable(str(exc)) from exc

    _require_the_runs_bars(run, request)
    kept: deque[tuple[TradeBar, SignalDecision]] = deque(maxlen=MAX_STRATEGY_VIEW_CANDLES)
    staged_count = 0

    def record(bar: TradeBar, decision: SignalDecision) -> None:
        nonlocal staged_count
        staged_count += 1
        kept.append((bar, decision))

    _require_the_runs_trades(run, replay_engine_run(request, record=record))

    evaluation_start_ms = None if request.warmup_from_date is None else run.start_ms
    provenance = _provenance(run)
    # Decisions the run's closing-bar rule set aside (#2607).
    skipped = set() if provenance is None else {skip.bar_close_ms for skip in provenance.closing_bar_skips}
    candles: list[StrategyViewCandle] = []
    for bar, decision in kept:
        explained = DecisionExplanationRecord.from_decision(bar, decision)
        if explained is None:
            continue
        if evaluation_start_ms is not None and bar.end_ms <= evaluation_start_ms:
            candle = view.candle(explained, phase="before_start")
        elif bar.end_ms in skipped:
            candle = view.candle(explained, phase="decision", outcome="blocked", reason_code="CLOSING_BAR_SKIPPED")
        else:
            candle = view.candle(explained, phase="decision", outcome=_OUTCOME[explained.signal])
        if candle is not None:
            candles.append(candle)
    notices: list[str] = []
    if not candles:
        notices.append("No decision bars were found for this run.")
    elif staged_count > len(kept):
        notices.append(f"This run has {staged_count} decision bars; the latest {len(kept)} are shown.")
    # The run's own first and last evaluated bars bound it, in whatever session it read.
    evaluated = [candle for candle in candles if candle.phase == "decision"]
    return view.response(
        symbol=run.symbol,
        run_id=f"backtest-run:{run.id}",
        run_started_at_ms=evaluated[0].bar_start_ms if evaluated else run.start_ms,
        run_stopped_at_ms=evaluated[-1].bar_close_ms if evaluated else run.end_ms,
        candles=candles,
        notices=notices,
    )


def _request_from_run(run: RunDetail) -> EngineBacktestRequest:
    """The run's own backtest request, from what it recorded: read-only, never saved again."""
    if run.data_policy_json is None:
        raise SavedRunNotReplayable("This run recorded no data policy, so its bars cannot be replayed.")
    if run.timespan not in ("minute", "daily"):
        raise SavedRunNotReplayable(f"This run read '{run.timespan}' bars, which cannot be replayed.")
    execution = json.loads(run.execution_config_json) if run.execution_config_json else {}
    compatibility_profile = execution.get("compatibility_profile")
    # A compatibility run charges its pinned IBKR fees and recorded no flat commission; naming one is refused.
    commission = {} if compatibility_profile is not None else {"commission_per_order": run.commission_per_order or 0.0}
    try:
        return EngineBacktestRequest(
            strategy_name=run.strategy_name,
            fill_mode=run.fill_mode,
            **commission,
            slippage_per_share=execution.get("slippage_per_share") or 0.0,
        from_date=et_date_at_ms(run.start_ms).isoformat(),
        to_date=et_date_at_ms(run.end_ms).isoformat(),
            warmup_from_date=execution.get("warmup_from_date"),
            compatibility_profile=compatibility_profile,
            save_study=False,
            summary_only=True,
            initial_cash=run.initial_cash,
            params=json.loads(run.parameters_json),
            resolution=run.timespan,
            auto_fetch=False,
            data_policy=json.loads(run.data_policy_json),
        )
    except ValidationError as exc:
        raise SavedRunNotReplayable(f"This run's recorded settings no longer form a backtest: {exc}") from exc


def _provenance(run: RunDetail) -> RunEvidenceProvenance | None:
    if run.evidence_provenance_json is None:
        return None
    return RunEvidenceProvenance.model_validate_json(run.evidence_provenance_json)


def _require_the_runs_bars(run: RunDetail, request: EngineBacktestRequest) -> None:
    """Refuse a replay whose bars cannot be shown to be the ones the run read.

    The run recorded the lake state it ran against (``data_availability_hash``);
    the same window is materialized again and must hash the same. A run that
    recorded none cannot prove its bars, so it is not replayed.
    """
    provenance = _provenance(run)
    receipt = None if provenance is None else provenance.data_availability_hash
    if receipt is None:
        raise SavedRunNotReplayable(
            "This run recorded no data receipt, so its bars cannot be shown to be the ones it read."
        )
    if replay_availability_hash(request) != receipt:
        raise SavedRunNotReplayable("The lake's bars for this run have changed since it ran.")


def _require_the_runs_trades(run: RunDetail, trades: list[LoggedTrade]) -> None:
    """Refuse a replay that did not reproduce the run's own trades."""
    if len(trades) != run.total_trades:
        raise SavedRunNotReplayable(
            f"Replaying this run made {len(trades)} trades, not its {run.total_trades}, "
            "so the bars or the strategy have changed since it ran."
        )
    for stored in run.trades:
        replayed = trades[stored.trade_number - 1]
        same = (
            replayed.entry_time_ms == stored.entry_ms
            and replayed.exit_time_ms == stored.exit_ms
            and int(replayed.quantity) == int(stored.quantity)
            and math.isclose(float(replayed.entry_price), stored.entry_price, rel_tol=0, abs_tol=_PRICE_ATOL)
            and math.isclose(float(replayed.exit_price), stored.exit_price, rel_tol=0, abs_tol=_PRICE_ATOL)
        )
        if not same:
            raise SavedRunNotReplayable(
                f"Replaying this run changed its trade {stored.trade_number}, "
                "so the bars or the strategy have changed since it ran."
            )


__all__ = ["MAX_STRATEGY_VIEW_CANDLES", "build_backtest_run_strategy_view"]
