"""Account-scoped Options bot deployment and explicit recovery."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Path

from app.broker.alpaca.clerk.budgets import BudgetUnavailable
from app.broker.alpaca.clerk.option_effects import OptionPlanRefused
from app.options_bots.golden import GoldenSetupError
from app.schemas.broker_bots import SUBMISSION_KEY_PATTERN, BotStatusView
from app.schemas.deployment_budget import (
    BudgetDeployCommandReceipt,
    DeploymentBudgetInput,
    DeploymentBudgetPreview,
    DeploySubmissionUncommitted,
)
from app.schemas.options_deploy import (
    OptionsBotSessionView,
    OptionsDeploymentReview,
    OptionsDeployRequest,
    OptionsDeploySubmission,
    OptionsResumeRequest,
)
from app.services.bot_runner import BotRunnerError
from app.services.broker_v2_panel import panel_deploy, panel_scope
from app.services.broker_v2_panel.deploy_submissions import BotNameUnavailable, DeploySubmissionConflict
from app.services.broker_v2_panel.panel_errors import PanelDataError
from app.services.options_bot_deploy import _runner, deploy_options_bot, review_options_bot

router = APIRouter(prefix="/api/brokers/{broker}/accounts/{account_id}/options-bots", tags=["options-bots"])
SubmissionKey = Annotated[str, Path(pattern=SUBMISSION_KEY_PATTERN)]
_ERRORS = (GoldenSetupError, BudgetUnavailable, BotRunnerError, PanelDataError, BotNameUnavailable, DeploySubmissionConflict, OptionPlanRefused)


def _refusal(error: Exception) -> HTTPException:
    if isinstance(error, GoldenSetupError):
        return HTTPException(error.http_status, {"code": error.code, "message": error.message})
    return HTTPException(getattr(error, "http_status", 409), {
        "message": str(error), "code": getattr(error, "reason_code", None), "detail": getattr(error, "detail", None),
    })


@router.post("/review", response_model=OptionsDeploymentReview)
async def review(
    broker: Literal["alpaca"], account_id: str, body: OptionsDeployRequest,
) -> OptionsDeploymentReview:
    try:
        return await review_options_bot(broker, account_id, body)
    except _ERRORS as exc:
        raise _refusal(exc) from exc


@router.post("/deploy", response_model=BudgetDeployCommandReceipt)
async def deploy(
    broker: Literal["alpaca"], account_id: str, body: OptionsDeploySubmission,
) -> BudgetDeployCommandReceipt:
    try:
        return await deploy_options_bot(broker, account_id, body)
    except _ERRORS as exc:
        raise _refusal(exc) from exc


@router.get("/deploy-submissions/{submission_key}", response_model=BudgetDeployCommandReceipt | DeploySubmissionUncommitted)
async def submission(
    broker: Literal["alpaca"], account_id: str, submission_key: SubmissionKey,
) -> BudgetDeployCommandReceipt | DeploySubmissionUncommitted:
    from app.services.options_bot_deploy import _SENDING

    try:
        await panel_scope.validate_account(broker, account_id)
        pending = _SENDING.get(submission_key)
        if pending is not None and pending.locked():
            return DeploySubmissionUncommitted(
                status="in_flight", submission_key=submission_key, strategy_instance_id=None, claimed_at_ms=None,
                message="This Options bot is being deployed.", explanation="The outcome is not settled yet.",
                next_action="Check this command again; do not start another deployment.",
            )
        result = await panel_deploy.deploy_submission_status(account_id, submission_key)
        if result is None:
            raise HTTPException(404, {
                "code": panel_deploy.SUBMISSION_NEVER_CLAIMED,
                "message": "No Options deployment is recorded under this command.",
            })
        return result
    except _ERRORS as exc:
        raise _refusal(exc) from exc


@router.post("/{sid}/resume", response_model=BotStatusView)
async def resume(
    broker: Literal["alpaca"], account_id: str, sid: str, body: OptionsResumeRequest,
) -> BotStatusView:
    try:
        await panel_scope.validate_account_scope(broker, account_id, sid)
        return await _runner().resume_options_bot(broker, sid, body)
    except _ERRORS as exc:
        raise _refusal(exc) from exc


@router.post("/{sid}/resume-review", response_model=DeploymentBudgetPreview)
async def resume_review(
    broker: Literal["alpaca"], account_id: str, sid: str, body: DeploymentBudgetInput | None = None,
) -> DeploymentBudgetPreview:
    from app.services.options_bot_resume_budget import preview_options_resume_budget

    try:
        await panel_scope.validate_account_scope(broker, account_id, sid)
        binding = _runner().binding_for_control(broker, sid)
        return await preview_options_resume_budget(binding, body)
    except _ERRORS as exc:
        raise _refusal(exc) from exc


@router.get("/{sid}/session", response_model=OptionsBotSessionView)
async def session(broker: Literal["alpaca"], account_id: str, sid: str) -> OptionsBotSessionView:
    from app.services.options_bot_status import options_session_view

    try:
        await panel_scope.validate_account_scope(broker, account_id, sid)
        return options_session_view(_runner(), broker, sid)
    except _ERRORS as exc:
        raise _refusal(exc) from exc
