"""Options end validation precedes budget review, consent and submission claims."""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.broker.alpaca.clerk.budgets import BudgetUnavailable
from app.lean_sidecar.trading_calendar import CALENDAR_IDENTITY, CALENDAR_PACKAGE_VERSION, session_window_for_date
from app.options_backtest.run import run_sessions
from app.options_bots.golden import GoldenOptionsSetups, SourceRun
from app.options_bots.schedule import first_eligible_session
from app.schemas.bot_end import BotEndInput
from app.schemas.golden_option_setups import GoldenOptionSetupView
from app.schemas.options_deploy import OptionsDeployRequest, OptionsDeploySubmission
from app.services import options_bot_deploy
from app.services.broker_v2_panel import bot_end_panel, budget_deploy, panel_scope
from app.services.broker_v2_panel.deploy_submissions import DeploySubmissionLedger
from tests.options_backtest._market import after_open, leg, setup
from tests.options_backtest._run_data import WED, Day, fake_data, flat, steady
from tests.options_bots.test_golden import MemoryGoldenStore, _approve, _proof

_REQUEST = {
    "execution_mode": "dry_run",
    "order_terms": {"gap_limit_percent": "8", "entry_wait_seconds": 20,
                    "cost_limit_percent": "10", "expiry_close_lead_minutes": 45},
    "exit_terms": {"exit_allowance_bps": 20, "band_multiple": 2, "spread_cap_bps": 50},
}


@pytest.mark.parametrize("request_type", [OptionsDeployRequest, OptionsDeploySubmission])
def test_options_deployment_refuses_explicit_null_and_keeps_no_end_explicit(
    request_type: type[OptionsDeployRequest],
) -> None:
    body = {**_REQUEST, "golden_setup_id": "test-golden"}
    if request_type is OptionsDeploySubmission:
        body["submission_key"] = "explicit-end"
    with pytest.raises(ValidationError, match="end may not be null"):
        request_type.model_validate({**body, "end": None})
    assert request_type.model_validate(body).end is None
    no_end = BotEndInput(end_at_ms=None, end_action="SELL")
    assert request_type.model_validate({**body, "end": no_end}).end == no_end


@pytest.mark.parametrize(
    ("day", "minutes_after_open", "exit_minutes"),
    [(date(2026, 10, 19), 30, 60), (date(2026, 11, 27), 0, 300)],
    ids=["entry-already-passed", "setup-does-not-fit-early-close"],
)
async def test_end_before_next_entry_is_refused_before_funding_or_claiming(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, day: date, minutes_after_open: int, exit_minutes: int,
) -> None:
    written = setup(leg(), exit=after_open(exit_minutes))
    result = run_sessions(written, [WED], fake_data({WED: Day(
        underlying=steady("500"), options={("call", 500): flat("2", 1, 390)},
    )}))
    source = SourceRun("End timing", result, CALENDAR_IDENTITY, CALENDAR_PACKAGE_VERSION)
    module = GoldenOptionsSetups(
        MemoryGoldenStore(), source=lambda _identity: asyncio.sleep(0, result=source),
        prove=lambda run: {**_proof(run), "rules_digest": "a" * 64}, rules_digest=lambda: "a" * 64,
    )
    golden = await module.approve(_approve())
    clock = session_window_for_date(day).open_ms_utc + minutes_after_open * 60_000

    async def account(_broker: str, _account_id: str) -> str:
        return "PA9"

    async def approved(_setup_id: str) -> GoldenOptionSetupView:
        return golden

    def funding_must_not_be_reached(*_args: object, **_kwargs: object) -> None:
        pytest.fail("An unusable deployment end reached funding before it was refused")

    monkeypatch.setattr(panel_scope, "validate_account", account)
    monkeypatch.setattr(options_bot_deploy, "get_ready_setup", approved)
    monkeypatch.setattr(options_bot_deploy, "get_bot_task_registry", lambda: SimpleNamespace(artifacts_root=tmp_path))
    monkeypatch.setattr(options_bot_deploy, "now_ms_utc", lambda: clock)
    monkeypatch.setattr(bot_end_panel, "now_ms_utc", lambda: clock)
    monkeypatch.setattr(budget_deploy, "preview_options_session_budget", funding_must_not_be_reached)
    monkeypatch.setattr(budget_deploy, "resolve_options_session_consent", funding_must_not_be_reached)
    request = OptionsDeployRequest.model_validate({**_REQUEST, "golden_setup_id": golden.id})
    with pytest.raises(BudgetUnavailable, match="next eligible entry"):
        await options_bot_deploy.review_options_bot("alpaca", "PA9", request)
    submission = OptionsDeploySubmission(**request.model_dump(exclude_unset=True), submission_key="unusable-end")
    with pytest.raises(BudgetUnavailable, match="next eligible entry"):
        await options_bot_deploy.deploy_options_bot("alpaca", "PA9", submission)
    assert DeploySubmissionLedger(tmp_path).recorded(
        "unusable-end", request_fingerprint=submission.fingerprint(account="PA9", world="dry_run"),
    ) is None


@pytest.mark.parametrize("choice", ["no_end", "later_end", "at_entry"])
def test_edited_end_must_leave_time_for_an_entry_and_no_end_remains_explicit(choice: str) -> None:
    now = session_window_for_date(date(2026, 10, 19)).open_ms_utc + 30 * 60_000
    eligible = first_eligible_session(setup(leg()), at_ms=now)
    selected = None if choice == "no_end" else eligible.entry_ms if choice == "at_entry" else eligible.close_ms - 45 * 60_000
    request = OptionsDeployRequest.model_validate({
        **_REQUEST, "golden_setup_id": "test-golden", "end": BotEndInput(end_at_ms=selected, end_action="SELL"),
    })
    if choice == "at_entry":
        with pytest.raises(BudgetUnavailable, match="next eligible entry"):
            options_bot_deploy._deployment_end(request, eligible, now_ms=now)
    else:
        resolved = options_bot_deploy._deployment_end(request, eligible, now_ms=now)
        assert (None if resolved is None else resolved.end_at_ms) == selected
