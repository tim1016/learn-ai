from __future__ import annotations

from datetime import date

import pytest

from app.engine.strategy.algorithms import spy_vwap_reversion
from app.engine.strategy.algorithms.spy_vwap_reversion import SpyVwapReversionAlgorithm
from app.lean_sidecar.trading_calendar import SessionWindow


def test_session_bounds_minutes_et_caches_calendar_lookup_by_date(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[date] = []
    trading_day = date(2024, 3, 4)

    def fake_session_window_for_date(d: date) -> SessionWindow:
        calls.append(d)
        return SessionWindow(
            session_date=d,
            open_ms_utc=1_709_562_600_000,
            close_ms_utc=1_709_586_000_000,
        )

    monkeypatch.setattr(
        spy_vwap_reversion,
        "session_window_for_date",
        fake_session_window_for_date,
    )
    algorithm = SpyVwapReversionAlgorithm()

    first = algorithm._session_bounds_minutes_et(trading_day)
    second = algorithm._session_bounds_minutes_et(trading_day)

    assert first == (570, 960)
    assert second == (570, 960)
    assert calls == [trading_day]
