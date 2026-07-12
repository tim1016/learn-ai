from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from app.lean_sidecar.trading_calendar import session_state_at_ms as calendar_session_state_at_ms
from app.lean_sidecar.trading_calendar import session_window_for_date
from app.schemas.broker_capability import SessionCapability, SessionDataCapability
from app.services.session_authority import session_state_at_ms
from app.utils.timestamps import to_ms_utc


def _ny_ms(year: int, month: int, day: int, hour: int, minute: int) -> int:
    return to_ms_utc(datetime(year, month, day, hour, minute, tzinfo=ZoneInfo("America/New_York")))


def _capability() -> SessionDataCapability:
    def session(open_ms: int | None, close_ms: int | None) -> SessionCapability:
        return SessionCapability(
            window_today_open_ms=open_ms,
            window_today_close_ms=close_ms,
            data="live" if open_ms is not None else "none",
            tradeable="yes" if open_ms is not None else "no",
            order_eligible_outside_rth=True,
            evidence_codes=[],
        )

    return SessionDataCapability(
        symbol="SPY",
        con_id=756733,
        account_mode="live",
        account_id="U1234567",
        probed_at_ms=_ny_ms(2026, 6, 23, 12, 0),
        time_zone_id="America/New_York",
        sessions={
            "PRE": session(_ny_ms(2026, 6, 23, 4, 0), _ny_ms(2026, 6, 23, 9, 30)),
            "RTH": session(_ny_ms(2026, 6, 23, 9, 30), _ny_ms(2026, 6, 23, 16, 0)),
            "POST": session(_ny_ms(2026, 6, 23, 16, 0), _ny_ms(2026, 6, 23, 20, 0)),
            "OVERNIGHT": session(_ny_ms(2026, 6, 23, 20, 0), _ny_ms(2026, 6, 24, 4, 0)),
        },
        raw_evidence=[],
    )


@pytest.mark.parametrize(
    ("now_ms", "expected_phase", "expected_next"),
    [
        (_ny_ms(2026, 6, 23, 6, 0), "PRE", _ny_ms(2026, 6, 23, 9, 30)),
        (_ny_ms(2026, 6, 23, 12, 0), "RTH", _ny_ms(2026, 6, 23, 16, 0)),
        (_ny_ms(2026, 6, 23, 18, 0), "POST", _ny_ms(2026, 6, 23, 20, 0)),
        (_ny_ms(2026, 6, 23, 22, 0), "OVERNIGHT", _ny_ms(2026, 6, 24, 4, 0)),
    ],
)
def test_session_state_uses_ibkr_capability_windows(
    now_ms: int,
    expected_phase: str,
    expected_next: int,
) -> None:
    state = session_state_at_ms(now_ms=now_ms, capability=_capability())

    assert state.source == "ibkr_capability"
    assert state.phase == expected_phase
    assert state.next_transition_ms == expected_next
    assert state.permits_strategy_activity is (expected_phase == "RTH")


@pytest.mark.parametrize(
    "now_ms",
    [
        _ny_ms(2026, 6, 23, 9, 30),
        _ny_ms(2026, 6, 23, 12, 0),
        _ny_ms(2026, 6, 23, 15, 59),
    ],
)
def test_session_state_rth_parity_with_nyse_calendar(now_ms: int) -> None:
    state = session_state_at_ms(now_ms=now_ms, capability=_capability())

    assert calendar_session_state_at_ms(now_ms) == "RTH_OPEN"
    assert state.phase == "RTH"


def test_session_state_falls_back_to_nyse_calendar_without_capability() -> None:
    window = session_window_for_date(date(2026, 6, 23))

    state = session_state_at_ms(now_ms=window.open_ms_utc)

    assert state.source == "nyse_calendar"
    assert state.phase == "RTH"
    assert state.next_transition_ms == window.close_ms_utc


def test_session_state_permits_strategy_activity_from_allowed_sessions() -> None:
    state = session_state_at_ms(
        now_ms=_ny_ms(2026, 6, 23, 18, 0),
        capability=_capability(),
        allowed_sessions=("RTH", "POST"),
    )

    assert state.phase == "POST"
    assert state.permits_strategy_activity is True
