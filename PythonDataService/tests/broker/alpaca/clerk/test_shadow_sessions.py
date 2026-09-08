"""One shadow authority's per-ET-day cleanliness journal (ADR 0059 D2)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from app.broker.alpaca.broker import ALPACA_EXTENDED_HOURS_WINDOW
from app.broker.alpaca.clerk.shadow_sessions import (
    SHADOW_SESSIONS_FILENAME,
    ShadowSessionLedger,
    ShadowSessionRecorder,
)
from app.broker.alpaca.clerk.sqlite.reconcile import AccountReconciliationResult
from app.lean_sidecar.trading_calendar import session_open_ms_utc
from app.services.session_authority import declared_session_bounds, et_minute_of_day_ms

DAY = date(2026, 9, 8)
SATURDAY = date(2026, 9, 12)
ACCOUNT = "shadow:9LIVE0001"


class _Clock:
    def __init__(self, now_ms: int) -> None:
        self.now_ms = now_ms

    def __call__(self) -> int:
        return self.now_ms


def _recorder(tmp_path: Path, clock: _Clock) -> tuple[ShadowSessionLedger, ShadowSessionRecorder]:
    ledger = ShadowSessionLedger(artifacts_root=tmp_path, account_id=ACCOUNT)
    return ledger, ShadowSessionRecorder(ledger=ledger, window=ALPACA_EXTENDED_HOURS_WINDOW, clock=clock)


def test_ledger_is_shadow_scoped_and_lives_in_the_custody_directory(tmp_path: Path) -> None:
    ledger = ShadowSessionLedger(artifacts_root=tmp_path, account_id=ACCOUNT)
    assert ledger.path == tmp_path / "accounts" / "alpaca" / ACCOUNT / SHADOW_SESSIONS_FILENAME
    assert ledger.has_rows() is False
    with pytest.raises(ValueError, match="shadow: account identity"):
        ShadowSessionLedger(artifacts_root=tmp_path, account_id="PA0SANITIZED00001")


def test_a_clean_day_opens_then_closes_clean_only_after_the_declared_close(tmp_path: Path) -> None:
    clock = _Clock(et_minute_of_day_ms(DAY, 300))  # 05:00 ET
    ledger, recorder = _recorder(tmp_path, clock)
    clean = AccountReconciliationResult(verdict="clean")
    bounds = declared_session_bounds(DAY, ALPACA_EXTENDED_HOURS_WINDOW)
    assert bounds is not None
    open_ms = session_open_ms_utc(DAY)

    assert recorder.record(clean) is clean
    state = ledger.day_state(open_ms)
    assert (state.opened_at_ms, state.closed_clean, state.non_clean_verdicts) == (clock.now_ms, False, ())

    clock.now_ms = bounds.close_ms - 1
    recorder.record(clean)
    assert ledger.day_state(open_ms).closed_clean is False

    clock.now_ms = bounds.close_ms
    recorder.record(clean)
    recorder.record(clean)  # idempotent: one closed-clean row
    assert ledger.day_state(open_ms).closed_clean is True
    assert [row.kind for row in ledger.rows()] == ["day_opened", "session_closed_clean"]
    assert ledger.completed_session_opens() == (open_ms,)


def test_a_non_clean_pass_taints_the_day_and_is_deduplicated_per_verdict(tmp_path: Path) -> None:
    clock = _Clock(et_minute_of_day_ms(DAY, 720))
    ledger, recorder = _recorder(tmp_path, clock)
    open_ms = session_open_ms_utc(DAY)
    bounds = declared_session_bounds(DAY, ALPACA_EXTENDED_HOURS_WINDOW)
    assert bounds is not None

    recorder.record(AccountReconciliationResult(verdict="stale"))
    recorder.record(AccountReconciliationResult(verdict="stale"))
    recorder.record(AccountReconciliationResult(verdict="position_drift", drifted_symbols=("SPY",)))
    clock.now_ms = bounds.close_ms
    recorder.record(AccountReconciliationResult(verdict="clean"))

    state = ledger.day_state(open_ms)
    assert state.non_clean_verdicts == ("stale", "position_drift")
    assert state.closed_clean is True
    assert ledger.completed_session_opens() == ()
    assert [row.kind for row in ledger.rows()] == ["day_opened", "non_clean", "non_clean", "session_closed_clean"]


def test_passes_outside_a_trading_day_write_nothing(tmp_path: Path) -> None:
    clock = _Clock(et_minute_of_day_ms(SATURDAY, 720))
    ledger, recorder = _recorder(tmp_path, clock)
    recorder.record(AccountReconciliationResult(verdict="clean"))
    assert ledger.rows() == []


def test_a_regular_hours_window_closes_at_the_calendar_close(tmp_path: Path) -> None:
    from app.lean_sidecar.trading_calendar import session_close_ms_utc

    clock = _Clock(et_minute_of_day_ms(DAY, 600))
    ledger = ShadowSessionLedger(artifacts_root=tmp_path, account_id=ACCOUNT)
    recorder = ShadowSessionRecorder(ledger=ledger, window=None, clock=clock)
    recorder.record(AccountReconciliationResult(verdict="clean"))
    clock.now_ms = session_close_ms_utc(DAY)
    recorder.record(AccountReconciliationResult(verdict="clean"))
    assert ledger.day_state(session_open_ms_utc(DAY)).closed_clean is True
