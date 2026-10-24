"""What the Signal study's tests share: the windows they run, the study they ask for, and the two ways of reading the lake (#3522)."""

from __future__ import annotations

from datetime import date
from typing import Any

from app.lean_sidecar.trading_calendar import expected_sessions
from app.research.signal.engine import SignalEngineReport, run_signal_engine
from app.research.study_inputs import (
    AdmittedMinuteInputs,
    MinuteWindowIdentity,
    admit_minute_inputs,
    bars_as_rows,
    fold_minute_bars,
)
from app.services.signal_study_service import SignalStudyParams, signal_study_identity

#: 2025-02-03: after every window below, so each session is complete and inside the provider's history.
NOW_MS = 1_738_540_800_000
#: Four months, Sep 3 to Dec 31 2024: DST ends Nov 3, Nov 28 and Dec 25 are holidays, Nov 29 and Dec 24 close at 13:00.
LONG = expected_sessions(date(2024, 9, 3), date(2024, 12, 31))
#: Ten sessions across the spring-forward Sunday (Mar 10).
SHORT = expected_sessions(date(2024, 3, 4), date(2024, 3, 15))
EARLY_CLOSES = [date(2024, 11, 29), date(2024, 12, 24)]


def study(window: list[date], *, multiplier: int = 15, session: str = "extended", **over: Any) -> SignalStudyParams:
    return SignalStudyParams(
        "SPY", over.pop("feature_name", "momentum_5m"), window[0], window[-1], "minute", multiplier,
        session_policy=session, **over,  # type: ignore[arg-type]
    )


def lake_rows(params: SignalStudyParams) -> tuple[list[dict], AdmittedMinuteInputs]:
    """The study's bars as it reads them: the identity first (it may refuse the bar length), then admission and the fold."""
    identity = signal_study_identity(params)
    admitted = admit_minute_inputs(identity.inputs, requester="test", now_ms=NOW_MS)
    return bars_as_rows(fold_minute_bars(admitted.window_bars, identity.bar_minutes)), admitted


def ungated_rows(params: SignalStudyParams, bar_minutes: int) -> list[dict]:
    """The same window folded to ``bar_minutes`` without asking the study whether it accepts that length.

    For measuring what the study refuses to run on: the refusal's arithmetic against the bars themselves.
    """
    inputs = MinuteWindowIdentity(
        params.symbol, params.price_adjustment_mode, params.session_policy, params.from_date, params.to_date
    )
    admitted = admit_minute_inputs(inputs, requester="test", now_ms=NOW_MS)
    return bars_as_rows(fold_minute_bars(admitted.window_bars, bar_minutes))


def engine_report(rows: list[dict], params: SignalStudyParams) -> SignalEngineReport:
    return run_signal_engine(
        "SPY", params.feature_name, rows, params.from_date.isoformat(), params.to_date.isoformat(), params.config()
    )
