"""The shadow operator CLI activates, lists sessions, and writes the receipt only when the gate holds."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.shadow_activation import ShadowActivationStore
from app.broker.alpaca.clerk.shadow_receipt import ShadowReceiptStore
from app.services.alpaca_shadow_reconciliation import (
    ShadowGateEvaluation,
    ShadowSessionVerdict,
    TwinDayReconciliation,
)
from app.services.bot_binding_repository import (
    BrokerBotBinding,
    alpaca_v1_action_plan,
    live_state_binding_repository,
)
from scripts.manage_alpaca_shadow import main

LIVE_ACCOUNT = "9LIVE0001"
SID, TWIN = "s", "t"


def _evaluation(counted: int, required: int) -> ShadowGateEvaluation:
    sessions = tuple(
        ShadowSessionVerdict(
            session_open_ms=1_000 + i,
            state="counted",
            detail="",
            shadow_run_id="run-1",
            reconciliation=TwinDayReconciliation(
                1_000 + i, SID, TWIN, (), (), (), None, None, Decimal("0.01")
            ),
        )
        for i in range(counted)
    )
    return ShadowGateEvaluation(
        live_account_id=LIVE_ACCOUNT,
        strategy_instance_id=SID,
        twin_account_id="PA-TEST",
        twin_strategy_instance_id=TWIN,
        configured_signal_hash="a" * 64,
        required_sessions=required,
        sessions=sessions,
    )


def _record_binding(live_state_root: Path, strategy_instance_id: str) -> None:
    """Put one readable runner binding on disk — what the CLI reads before it judges."""
    live_state_binding_repository(live_state_root).record_launch(
        BrokerBotBinding(
            strategy_instance_id=strategy_instance_id,
            broker="alpaca",
            symbol="SPY",
            mode="trade",
            action_plan=alpaca_v1_action_plan("SPY"),
            run_id=f"{strategy_instance_id}-run-1",
            created_at_ms=1_757_000_000_000,
        ),
        launch_reason="deploy",
    )


def _last_object(capsys: pytest.CaptureFixture[str]) -> dict:
    return json.loads(capsys.readouterr().out.splitlines()[-1])


def test_activate_writes_the_fence_and_is_idempotent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = [
        "--live-account-id",
        LIVE_ACCOUNT,
        "--artifacts-root",
        str(tmp_path),
        "--live-state-root",
        str(tmp_path / "live"),
        "activate",
    ]
    assert main(argv) == 0
    assert main(argv) == 0
    record = ShadowActivationStore(tmp_path).latest(f"shadow:{LIVE_ACCOUNT}")
    assert record is not None and _last_object(capsys)["account_id"] == f"shadow:{LIVE_ACCOUNT}"


def test_receipt_is_written_only_when_the_gate_is_satisfied(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base = [
        "--live-account-id",
        LIVE_ACCOUNT,
        "--artifacts-root",
        str(tmp_path),
        "--live-state-root",
        str(tmp_path / "live"),
    ]
    twin = [
        "--strategy-instance-id",
        SID,
        "--twin-account-id",
        "PA-TEST",
        "--twin-strategy-instance-id",
        TWIN,
        "--required-sessions",
        "2",
    ]
    _record_binding(tmp_path / "live", SID)
    _record_binding(tmp_path / "live", TWIN)

    assert main([*base, "receipt", *twin], evaluate=lambda **_kw: _evaluation(1, 2)) == 2
    assert ShadowReceiptStore(tmp_path).latest(SID) is None
    assert _last_object(capsys)["satisfied"] is False

    assert main([*base, "receipt", *twin], evaluate=lambda **_kw: _evaluation(2, 2)) == 0
    receipt = ShadowReceiptStore(tmp_path).latest(SID)
    assert receipt is not None and len(receipt.sessions) == 2 and receipt.required_sessions == 2
    assert main([*base, "sessions", *twin], evaluate=lambda **_kw: _evaluation(2, 2)) == 0


def test_a_reserved_shadow_identity_is_refused_before_any_work(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = [
        "--live-account-id",
        f"shadow:{LIVE_ACCOUNT}",
        "--artifacts-root",
        str(tmp_path),
        "--live-state-root",
        str(tmp_path / "live"),
        "activate",
    ]
    assert main(argv) == 1
    assert "reserved" in _last_object(capsys)["error"]
    assert ShadowActivationStore(tmp_path).latest(f"shadow:{LIVE_ACCOUNT}") is None


def test_an_unknown_binding_is_an_evidence_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _record_binding(tmp_path / "live", SID)
    argv = [
        "--live-account-id",
        LIVE_ACCOUNT,
        "--artifacts-root",
        str(tmp_path),
        "--live-state-root",
        str(tmp_path / "live"),
        "sessions",
        "--strategy-instance-id",
        SID,
        "--twin-account-id",
        "PA-TEST",
        "--twin-strategy-instance-id",
        TWIN,
        "--required-sessions",
        "2",
    ]

    def _never_called(**_kwargs: object) -> ShadowGateEvaluation:
        raise AssertionError("the gate must not be evaluated without both bindings")

    assert main(argv, evaluate=_never_called) == 1
    assert TWIN in _last_object(capsys)["error"]
