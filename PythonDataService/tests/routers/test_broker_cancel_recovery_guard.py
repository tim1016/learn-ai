"""Tests for broker cancel recovery guards."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.broker.ibkr.models import IbkrConnectionHealth
from app.broker.ibkr.orders import OrderRefusedError
from app.engine.live.account_artifacts import AccountFreezeEvidence, write_account_freeze
from app.routers import broker


def _health(account_id: str | None = "DU1234567") -> IbkrConnectionHealth:
    return IbkrConnectionHealth(
        mode="paper",
        host="127.0.0.1",
        port=4002,
        client_id=7,
        connected=True,
        account_id=account_id,
        is_paper=True,
        fetched_at_ms=1_780_000_000_000,
        connection_state="connected",
        last_transition_ms=1_780_000_000_000,
    )


def _client(health: IbkrConnectionHealth) -> SimpleNamespace:
    return SimpleNamespace(health=lambda: health)


def _cancel_row(
    *,
    enabled: bool,
    reason_code: str | None,
    detail: str,
) -> SimpleNamespace:
    return SimpleNamespace(
        fact_kind="open_order",
        order_id=42,
        cancel_action=SimpleNamespace(
            enabled=enabled,
            reason_code=reason_code,
            detail=detail,
        ),
    )


def _patch_cancel_truth_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    account_freeze_active: bool | None,
    row: SimpleNamespace,
) -> None:
    live_runs_root = tmp_path / "live_runs"
    live_runs_root.mkdir(exist_ok=True)
    monkeypatch.setattr(
        broker,
        "get_settings",
        lambda: SimpleNamespace(live_runs_root=str(live_runs_root)),
    )
    monkeypatch.setattr(broker, "get_monitor", lambda: None)
    monkeypatch.setattr(
        broker,
        "load_account_instance_registry_evidence",
        lambda **_: SimpleNamespace(bindings=[], evidence_gaps=[]),
    )

    async def fake_fetch_account_truth(*_, **kwargs) -> SimpleNamespace:
        if account_freeze_active is not None:
            assert kwargs["account_freeze_active"] is account_freeze_active
        return SimpleNamespace(orders=[row])

    monkeypatch.setattr(broker, "fetch_account_truth", fake_fetch_account_truth)


@pytest.mark.asyncio
async def test_order_id_cancel_refuses_active_account_freeze(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    _patch_cancel_truth_dependencies(
        monkeypatch,
        tmp_path,
        account_freeze_active=True,
        row=_cancel_row(
            enabled=False,
            reason_code="ACCOUNT_FROZEN",
            detail="Account recovery is frozen.",
        ),
    )

    with pytest.raises(OrderRefusedError) as exc_info:
        await broker._raise_if_account_truth_blocks_cancel(_client(_health()), 42)
    message = str(exc_info.value)
    assert "ACCOUNT_FROZEN" in message
    assert "Account recovery is frozen" in message


@pytest.mark.asyncio
async def test_order_id_cancel_skips_freeze_read_without_connected_account(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_cancel_truth_dependencies(
        monkeypatch,
        tmp_path,
        account_freeze_active=False,
        row=_cancel_row(
            enabled=True,
            reason_code=None,
            detail="Sends an IBKR cancel request for this live open order.",
        ),
    )
    monkeypatch.setattr(
        broker,
        "read_account_freeze",
        lambda *_: pytest.fail("freeze state should not be read without an account id"),
    )

    await broker._raise_if_account_truth_blocks_cancel(_client(_health(None)), 42)


@pytest.mark.asyncio
async def test_order_id_cancel_fails_closed_when_freeze_state_unreadable(
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
    monkeypatch.setattr(broker, "get_monitor", lambda: None)
    monkeypatch.setattr(
        broker,
        "fetch_account_truth",
        lambda *_args, **_kwargs: pytest.fail(
            "unreadable freeze state must fail before Account Truth fetch"
        ),
    )

    with pytest.raises(OrderRefusedError, match="is not readable"):
        await broker._raise_if_account_truth_blocks_cancel(_client(_health()), 42)


@pytest.mark.asyncio
async def test_order_id_cancel_refuses_backend_disabled_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_cancel_truth_dependencies(
        monkeypatch,
        tmp_path,
        account_freeze_active=False,
        row=_cancel_row(
            enabled=False,
            reason_code="FOREIGN_OR_UNCLAIMED",
            detail="Foreign orders require adoption.",
        ),
    )

    with pytest.raises(OrderRefusedError) as exc_info:
        await broker._raise_if_account_truth_blocks_cancel(_client(_health()), 42)

    assert "FOREIGN_OR_UNCLAIMED" in str(exc_info.value)
    assert "Foreign orders require adoption" in str(exc_info.value)
