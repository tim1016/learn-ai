"""A terminal outcome the lifecycle projection refused is said, not dropped (B-2).

The receipt is written before the projection, so a refused projection leaves
the outcome on disk and the duty settle projects it later; what was missing
was any sign that it had been refused.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest

from app.services.bot_binding_repository import BrokerBotBinding, alpaca_v1_action_plan
from app.services.bot_lifecycle_projection import AlpacaLifecycleProjectionResult, ProjectionStatus
from app.services.bot_run_terminal import BotRunTerminalRecorder

_SID = "terminal-log-1"


def _binding() -> BrokerBotBinding:
    return BrokerBotBinding(
        strategy_instance_id=_SID, broker="alpaca", symbol="SPY", run_id="run-1", created_at_ms=1_000,
        action_plan=alpaca_v1_action_plan("SPY"),
    )


def _recorder(status: ProjectionStatus) -> BotRunTerminalRecorder:
    refusal = "VERSION_MISMATCH" if status is ProjectionStatus.FILE_CAS_REFUSED else None
    result = AlpacaLifecycleProjectionResult(
        status=status, record=None, terminal_outcome_attached=False, refusal_reason=refusal,
    )
    evidence: Any = SimpleNamespace(record_terminal=lambda *_args, **_kwargs: result)
    return BotRunTerminalRecorder(
        managed_bots={},
        desired_repo_for=lambda _sid: None,  # type: ignore[arg-type, return-value]
        run_evidence=evidence,
        now_ms=lambda: 2_000,
    )


@pytest.mark.parametrize("status", [ProjectionStatus.FILE_CAS_REFUSED, ProjectionStatus.AUTHORITY_CHANGED])
def test_a_refused_terminal_projection_is_logged(
    caplog: pytest.LogCaptureFixture, status: ProjectionStatus,
) -> None:
    with caplog.at_level(logging.WARNING, logger="app.services.bot_run_terminal"):
        _recorder(status).replace_provisional_stop(_binding(), kind="STOPPED", reason_code="OPERATOR_STOP")

    (record,) = [r for r in caplog.records if getattr(r, "action", None) == "bot_terminal_projection_not_recorded"]
    assert (record.strategy_instance_id, record.run_id, record.projection_status) == (_SID, "run-1", status.value)


def test_a_refused_projection_of_a_finalized_run_is_logged_too(caplog: pytest.LogCaptureFixture) -> None:
    desired = SimpleNamespace(set=lambda *_args, **_kwargs: None)
    recorder = _recorder(ProjectionStatus.AUTHORITY_CHANGED)
    recorder._desired_repo_for = lambda _sid: desired  # type: ignore[assignment, return-value]

    with caplog.at_level(logging.WARNING, logger="app.services.bot_run_terminal"):
        recorder.finalize(_binding(), kind="CRASHED", reason_code="ValueError")

    assert [getattr(r, "action", None) for r in caplog.records].count("bot_terminal_projection_not_recorded") == 1


@pytest.mark.parametrize("status", [ProjectionStatus.RECORDED, ProjectionStatus.AUTHORITY_EXPECTATION_SUPERSEDED])
def test_a_projection_that_answered_is_not_logged_as_refused(
    caplog: pytest.LogCaptureFixture, status: ProjectionStatus,
) -> None:
    with caplog.at_level(logging.WARNING, logger="app.services.bot_run_terminal"):
        _recorder(status).replace_provisional_stop(_binding(), kind="STOPPED", reason_code="OPERATOR_STOP")

    assert not [r for r in caplog.records if getattr(r, "action", None) == "bot_terminal_projection_not_recorded"]
