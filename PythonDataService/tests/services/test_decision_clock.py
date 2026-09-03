"""Decision clock: trigger instants on the calendar (spec §4.4, §4.5)."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.engine.consolidators.trade_bar_consolidator import _floor_to_period_ms
from app.lean_sidecar.trading_calendar import session_close_ms_utc, session_open_ms_utc
from app.schemas.run_admission import StrategyValidationAdmissionFact
from app.services.bot_binding_repository import BrokerBotBinding, alpaca_v1_action_plan
from app.services.decision_clock import (
    decision_timeframe_ms_for_binding,
    next_trigger_ms,
    rth_next_trigger_function,
    rth_trigger_instants,
)
from app.services.signal_program_admission import build_start_program_seal
from app.utils.timestamps import floor_to_period_ms_et, to_ms_utc

_ET = ZoneInfo("America/New_York")
_TF = 15 * 60_000
_REGULAR = date(2026, 9, 2)  # Wednesday, regular session
_EARLY = date(2026, 11, 27)  # day after Thanksgiving: 13:00 ET close
_FRIDAY = date(2026, 9, 4)  # next session is Tue 2026-09-08 (Labor Day 09-07)


def _et(d: date, hour: int, minute: int) -> int:
    return to_ms_utc(datetime(d.year, d.month, d.day, hour, minute, tzinfo=_ET))


def test_shared_floor_matches_the_consolidators_floor_across_dst() -> None:
    for d in (date(2026, 3, 8), date(2026, 11, 1), _REGULAR):
        ts = _et(d, 13, 7)
        assert floor_to_period_ms_et(ts, _TF) == _floor_to_period_ms(ts, timedelta(minutes=15)) == _et(d, 13, 0)


def test_rth_trigger_instants_regular_session() -> None:
    triggers = rth_trigger_instants(_REGULAR, timeframe_ms=_TF)
    assert triggers[0] == _et(_REGULAR, 9, 46)  # 09:30–09:45 bucket fires on the 09:45 minute's close
    assert triggers[-1] == session_close_ms_utc(_REGULAR)  # last bucket: forced flush at the close
    assert len(triggers) == 26


def test_rth_trigger_instants_early_close() -> None:
    triggers = rth_trigger_instants(_EARLY, timeframe_ms=_TF)
    assert triggers[-1] == session_close_ms_utc(_EARLY) == _et(_EARLY, 13, 0)
    assert len(triggers) == 14


def test_next_trigger_after_last_delivered_minute() -> None:
    L = _et(_REGULAR, 15, 0)  # last delivered minute 14:59–15:00 -> the 14:45–15:00 decision is still pending
    assert next_trigger_ms(L, timeframe_ms=_TF, decision_session="rth") == _et(_REGULAR, 15, 1)
    L = _et(_REGULAR, 15, 1)  # the 15:00 minute delivered -> next pending is the 15:00–15:15 decision
    assert next_trigger_ms(L, timeframe_ms=_TF, decision_session="rth") == _et(_REGULAR, 15, 16)
    L = _et(_REGULAR, 15, 59)
    assert next_trigger_ms(L, timeframe_ms=_TF, decision_session="rth") == session_close_ms_utc(_REGULAR)


def test_next_trigger_rolls_to_the_next_session() -> None:
    after_close = session_close_ms_utc(_FRIDAY)
    expected = session_open_ms_utc(date(2026, 9, 8)) + _TF + 60_000
    assert next_trigger_ms(after_close, timeframe_ms=_TF, decision_session="rth") == expected
    pre_market = _et(_REGULAR, 5, 10)
    assert next_trigger_ms(pre_market, timeframe_ms=_TF, decision_session="rth") == _et(_REGULAR, 9, 46)


def test_one_minute_timeframe() -> None:
    L = _et(_REGULAR, 15, 0)
    assert next_trigger_ms(L, timeframe_ms=60_000, decision_session="rth") == _et(_REGULAR, 15, 1)


def test_all_session_is_refused_in_this_slice() -> None:
    with pytest.raises(NotImplementedError):
        next_trigger_ms(_et(_REGULAR, 5, 10), timeframe_ms=_TF, decision_session="all")


def test_rth_next_trigger_function_binds_the_timeframe() -> None:
    """The callable Tasks 7/8 schedule against must carry the timeframe it was built with.

    15:01 is chosen because it is an instant where the two timeframes must
    disagree (15-minute buckets fire next at 15:16, one-minute at 15:02); an
    input where they agree would pass even against an unbound timeframe.
    """
    assert rth_next_trigger_function(_TF)(_et(_REGULAR, 15, 1)) == _et(_REGULAR, 15, 16)
    assert rth_next_trigger_function(60_000)(_et(_REGULAR, 15, 1)) == _et(_REGULAR, 15, 2)


def _binding() -> BrokerBotBinding:
    return BrokerBotBinding(
        strategy_instance_id="decision-clock-test",
        strategy_key="spy_strategy_a",
        broker="alpaca",
        symbol="SPY",
        mode="trade",
        quantity=1,
        action_plan=alpaca_v1_action_plan("SPY"),
        run_id="run-1",
        created_at_ms=0,
    )


def test_decision_timeframe_ms_for_binding_without_a_seal() -> None:
    assert decision_timeframe_ms_for_binding(_binding()) is None


def test_decision_timeframe_ms_for_binding_reads_the_seal() -> None:
    binding = _binding().model_copy(
        update={
            "sealed_account_id": "sim:decision-clock",
            "strategy_params": {},
            "strategy_param_origins": {},
        }
    )
    seal = build_start_program_seal(
        binding,
        StrategyValidationAdmissionFact(
            state="VERIFIED",
            strategy_key="spy_strategy_a",
            evidence_status="accepted",
            event_id="validation-decision-clock-1",
            evidence_snapshot_sha256="d" * 64,
            verified_at_ms=1_787_356_800_000,
            explanation="The exact validation snapshot was re-hashed.",
        ),
        parameter_origins={},
    )
    assert seal is not None
    sealed = binding.model_copy(update={"sealed_program": seal})

    assert decision_timeframe_ms_for_binding(sealed) == _TF
