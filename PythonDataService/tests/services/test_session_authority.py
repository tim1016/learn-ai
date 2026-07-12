from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from app.lean_sidecar.trading_calendar import session_state_at_ms as calendar_session_state_at_ms
from app.lean_sidecar.trading_calendar import session_window_for_date
from app.schemas.broker_capability import SessionCapability, SessionDataCapability
from app.services.session_authority import (
    evaluate_session_submit,
    order_mechanism_sessions_from_capability,
    session_state_at_ms,
)
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


def test_order_mechanism_stays_rth_only_while_extended_placement_disabled() -> None:
    # PRD #1005 safety invariant: even a fully-tradeable, live capability must
    # not open extended placement while the spread-guarded mechanism is off.
    assert order_mechanism_sessions_from_capability(
        _capability(), extended_placement_enabled=False
    ) == ("RTH",)


def test_order_mechanism_without_capability_is_rth_only() -> None:
    assert order_mechanism_sessions_from_capability(
        None, extended_placement_enabled=True
    ) == ("RTH",)


def test_order_mechanism_admits_only_proven_extended_sessions_when_enabled() -> None:
    def session(
        *, tradeable: str, data: str, eligible: bool
    ) -> SessionCapability:
        return SessionCapability(
            window_today_open_ms=_ny_ms(2026, 6, 23, 16, 0),
            window_today_close_ms=_ny_ms(2026, 6, 23, 20, 0),
            data=data,  # type: ignore[arg-type]
            tradeable=tradeable,  # type: ignore[arg-type]
            order_eligible_outside_rth=eligible,
            evidence_codes=[],
        )

    capability = SessionDataCapability(
        symbol="SPY",
        con_id=756733,
        account_mode="live",
        account_id="U1234567",
        probed_at_ms=_ny_ms(2026, 6, 23, 12, 0),
        time_zone_id="America/New_York",
        sessions={
            "PRE": session(tradeable="yes", data="live", eligible=True),
            "RTH": session(tradeable="yes", data="live", eligible=True),
            # POST is entitled but only delayed data — not safe to place on.
            "POST": session(tradeable="yes", data="delayed", eligible=True),
            # OVERNIGHT is not enabled on the account.
            "OVERNIGHT": session(tradeable="needs_enablement", data="none", eligible=False),
        },
        raw_evidence=[],
    )

    ready = order_mechanism_sessions_from_capability(
        capability, extended_placement_enabled=True
    )

    assert ready == ("RTH", "PRE")


@pytest.mark.parametrize(
    ("phase", "allowed", "mechanism", "price_ok", "expected"),
    [
        ("CLOSED", ("RTH",), ("RTH",), True, "session_closed"),
        ("POST", ("RTH",), ("RTH",), True, "strategy_session_not_permitted"),
        ("POST", ("RTH", "POST"), ("RTH",), True, "order_mechanism_not_enabled"),
        ("POST", ("RTH", "POST"), ("RTH", "POST"), False, "extended_limit_price_unavailable"),
        ("RTH", ("RTH",), ("RTH",), True, None),
        ("POST", ("RTH", "POST"), ("RTH", "POST"), True, None),
    ],
)
def test_evaluate_session_submit_branch_table(
    phase: str,
    allowed: tuple[str, ...],
    mechanism: tuple[str, ...],
    price_ok: bool,
    expected: str | None,
) -> None:
    assert (
        evaluate_session_submit(
            phase=phase,  # type: ignore[arg-type]
            allowed_sessions=allowed,  # type: ignore[arg-type]
            order_mechanism_sessions=mechanism,  # type: ignore[arg-type]
            extended_reference_price_ok=price_ok,
        )
        == expected
    )
