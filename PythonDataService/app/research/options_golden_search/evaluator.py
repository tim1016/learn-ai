"""Lean, sequential Options search evaluation on the ordinary replay (#3332).

Formula: cash_next = cash_before + canonical daily P&L, only after a complete observed session.
Reference: #3332 fixed-quantity cash carry; app/options_backtest/run.py canonical run adapter.
Canonical implementation: replay.py for session money/decisions; this file only coordinates research evidence.
Validated against: tests/research/options_golden_search/test_evaluator.py.

No cash or slippage ladders run implicitly. Approval separately materializes the complete ordinary source run.
An incomplete session or missing held-minute mark stops before any synthetic settlement can fund another day.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, localcontext

from app.options_backtest import fills
from app.options_backtest.capital_evidence import CapitalSession, capital_evidence
from app.options_backtest.data import RunData
from app.options_backtest.fees import fee_for_policy
from app.options_backtest.observation import MinuteObservation
from app.options_backtest.replay import replay_session
from app.options_backtest.results import Fill, SessionStatus, SkipReason
from app.options_backtest.run import RunStopped, load_session_market, validate_run_sessions
from app.options_backtest.session_result import SessionPins
from app.options_backtest.setup import OptionsBacktestSetup
from app.research.options_golden_search.metrics import QualificationMetrics, SessionEvidence, summarize_evidence
from app.research.options_golden_search.protocol import Window
from app.utils.session_anchors import et_date_at_ms


@dataclass(frozen=True)
class EvaluationResult:
    sessions: tuple[SessionEvidence, ...]
    pins: tuple[SessionPins, ...]
    metrics: QualificationMetrics
    scheduled_sessions: int


def evaluate_setup(
    setup: OptionsBacktestSetup, window: Window, data: RunData, *,
    risk_free_daily_return: float = 0, fee_multiplier: Decimal = Decimal(1),
    should_stop: Callable[[], bool] | None = None,
    on_progress: Callable[[int], None] | None = None,
) -> EvaluationResult:
    """Replay one frozen scenario and window, preserving canonical decisions and exact cash."""
    if not fee_multiplier.is_finite() or fee_multiplier < 1:
        raise ValueError("fee stress multiplier must be finite and at least one")
    days = tuple(et_date_at_ms(instant) for instant in window.session_ms)
    validate_run_sessions(setup, days)
    base_fee = fee_for_policy(setup.fees)

    def fee_for(fill: Fill) -> Decimal:
        fee = base_fee(fill)
        with localcontext(fills.EXACT):
            return fee * fee_multiplier

    cash = setup.starting_cash_usd
    kept: list[SessionEvidence] = []
    pins: list[SessionPins] = []
    observed_minutes = 0
    for index, day in enumerate(days):
        if should_stop is not None and should_stop():
            raise RunStopped(index)
        market, session_pins = load_session_market(setup, day, data)
        if market.session_open_ms != window.session_ms[index]:
            raise ValueError("window session anchors differ from the canonical calendar")
        observations: list[MinuteObservation] = []

        def observe(
            point: MinuteObservation, *, points: list[MinuteObservation] = observations, sessions_done: int = index,
        ) -> None:
            nonlocal observed_minutes
            if should_stop is not None and should_stop():
                raise RunStopped(sessions_done)
            points.append(point)
            observed_minutes += 1
            if on_progress is not None:
                on_progress(observed_minutes)

        outcome = replay_session(setup, market, cash_usd=cash, fee_for=fee_for, observer=observe)
        kept.append(SessionEvidence(market.session_open_ms, market.session_close_ms, cash, outcome, tuple(observations)))
        pins.append(session_pins)
        capital = capital_evidence((CapitalSession.of_outcome(market.session_open_ms, outcome),), starting_cash=cash)
        if (
            outcome.status is SessionStatus.INCOMPLETE
            or outcome.skip_reason is SkipReason.UNDERLYING_DID_NOT_TRADE
            or any(point.missing_leg_ids for point in observations)
            or capital.ended_by is not None
        ):
            break
        with localcontext(fills.EXACT):
            cash += outcome.pnl_usd
    metrics = summarize_evidence(
        kept, starting_capital_usd=setup.starting_cash_usd,
        annualization_sessions=window.annualization_sessions, risk_free_daily_return=risk_free_daily_return,
    )
    return EvaluationResult(tuple(kept), tuple(pins), metrics, len(days))
