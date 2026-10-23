"""Operator estimates from the canonical indicator and calendar requirements.

Formula: remaining bars = max(0, required - available); session estimate =
  ceil(remaining / whole decision bars per regular calendar session).
Reference: issue #2857; the history guarantee in engine/strategy/registry.py.
Canonical implementation: this file for estimates; indicator counts and the
  calendar guarantee are called, never reimplemented.
Validated against: tests/services/test_indicator_spawning.py.
"""

from __future__ import annotations

from collections.abc import Mapping

from app.engine.strategy.params import decision_timeframe_ms_for
from app.engine.strategy.registry import _STRATEGY_REGISTRY, _regular_session_ms, _warmup_lookback_days_holding
from app.marketdata.feed import MAX_WARMUP_LOOKBACK_DAYS
from app.schemas.indicator_convergence import IndicatorProgress, SpawningView


def _remaining_words(bars: int, timeframe_ms: int) -> tuple[str, int | None]:
    per_session = _regular_session_ms() // timeframe_ms
    if not per_session:
        return f"needs {bars} more bars of {timeframe_ms // 60_000} minutes before it decides", None
    sessions = -(-bars // per_session)
    return f"needs about {sessions} more trading sessions of live bars before it decides", sessions


def spawning_view(progress: IndicatorProgress | None, *, running: bool) -> SpawningView | None:
    if progress is None or progress.converged or not running:
        return None
    words, sessions = _remaining_words(max(0, progress.bars_needed - progress.bars_done), progress.decision_timeframe_ms)
    return SpawningView(
        **progress.model_dump(), label="Spawning", sessions_left=sessions,
        explanation=f"{progress.bars_done} of {progress.bars_needed} indicator bars settled; {words}. No orders are placed while spawning.",
    )


def deploy_spawn_estimate(strategy_key: str, params: Mapping[str, object]) -> str:
    registration = _STRATEGY_REGISTRY[strategy_key]
    contract = registration.signal_program_contract
    if contract is None:
        raise ValueError("A convergence estimate requires a Signal Program")
    validated = registration.param_schema.model_validate(params)
    needed = max((item.bars_to_converge or 0 for item in contract.resolved_signals(validated)), default=0)
    width = decision_timeframe_ms_for(validated, qualified_ms=contract.decision_timeframe_ms)
    # Invert the same sealed history guarantee, without a second calendar formula.
    lower, upper = 0, needed
    while lower < upper:
        middle = (lower + upper + 1) // 2
        days = _warmup_lookback_days_holding(middle, width)
        if days is not None and days <= MAX_WARMUP_LOOKBACK_DAYS:
            lower = middle
        else:
            upper = middle - 1
    remaining = needed - lower
    if not remaining:
        return "Ready to decide at start when history is complete."
    words, _ = _remaining_words(remaining, width)
    return f"Spawning: {words}. Spawn progress is lost if the bot stops."
