"""Tests for broker cancel recovery guards."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.broker.ibkr.orders import OrderRefusedError
from app.engine.live.account_artifacts import AccountFreezeEvidence, write_account_freeze
from app.routers import broker


def test_raw_order_id_cancel_refuses_active_account_freeze(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live_runs_root = tmp_path / "live_runs"
    live_runs_root.mkdir()
    monkeypatch.setattr(
        broker,
        "get_settings",
        lambda: SimpleNamespace(live_runs_root=str(live_runs_root)),
    )
    write_account_freeze(
        tmp_path,
        AccountFreezeEvidence(
            account_id="DU1234567",
            reason="restart_intensity.threshold_breached",
            source="account_restart_intensity",
            recorded_at_ms=1_780_000_002_000,
            operator_next_step="STOP_RESTARTING_AND_RECOVER_ACCOUNT",
        ),
    )

    with pytest.raises(OrderRefusedError) as exc_info:
        broker._raise_if_account_frozen_for_raw_cancel(
            SimpleNamespace(connected_account="DU1234567")
        )
    message = str(exc_info.value)
    assert "account freeze is active" in message
    assert "restart_intensity.threshold_breached" in message
    assert "STOP_RESTARTING_AND_RECOVER_ACCOUNT" in message


def test_raw_order_id_cancel_skips_guard_without_connected_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_settings() -> None:
        raise AssertionError("settings should not be read without a connected account")

    monkeypatch.setattr(broker, "get_settings", fail_settings)

    broker._raise_if_account_frozen_for_raw_cancel(
        SimpleNamespace(connected_account=None)
    )


def test_raw_order_id_cancel_fails_closed_when_freeze_state_unreadable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live_runs_root = tmp_path / "live_runs"
    live_runs_root.mkdir()
    monkeypatch.setattr(
        broker,
        "get_settings",
        lambda: SimpleNamespace(live_runs_root=str(live_runs_root)),
    )
    account_root = tmp_path / "accounts" / "DU1234567"
    account_root.mkdir(parents=True)
    (account_root / "unresolved_exposure.flag").write_text("{not-json", encoding="utf-8")

    with pytest.raises(OrderRefusedError, match="is not readable"):
        broker._raise_if_account_frozen_for_raw_cancel(
            SimpleNamespace(connected_account="DU1234567")
        )
