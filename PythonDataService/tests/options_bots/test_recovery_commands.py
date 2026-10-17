"""A retried recovery key always names its original run, even after a later stop."""

from pathlib import Path

import pytest

from app.options_bots.recovery_commands import claim_recovery
from app.schemas.deployment_budget import DeploymentBudgetInput
from app.schemas.options_deploy import OptionsResumeRequest


def test_recovery_retry_keeps_run_and_original_predecessor_across_processes(tmp_path: Path) -> None:
    request = OptionsResumeRequest(idempotency_key="resume-a")
    original = claim_recovery(tmp_path, prior_run_id="first-run", request=request, now_ms=10)
    retry = claim_recovery(tmp_path, prior_run_id="later-run", request=request, now_ms=20)
    assert retry == original
    assert claim_recovery(
        tmp_path, prior_run_id="later-run", request=OptionsResumeRequest(idempotency_key="resume-b"), now_ms=20,
    ).run_id != original.run_id


def test_recovery_key_cannot_change_from_manage_only_to_entry_spending(tmp_path: Path) -> None:
    request = OptionsResumeRequest(idempotency_key="resume-a")
    claim_recovery(tmp_path, prior_run_id="first-run", request=request, now_ms=10)
    with pytest.raises(ValueError, match="different recovery"):
        claim_recovery(tmp_path, prior_run_id="first-run", now_ms=20, request=request.model_copy(update={
            "budget": DeploymentBudgetInput(amount_usd="1000", risk_revision=0),
        }))
