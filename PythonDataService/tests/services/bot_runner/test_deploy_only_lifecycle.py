"""#2541: terminal trading needs fresh consent, while custody stays recoverable."""
import asyncio
import inspect
from pathlib import Path
from typing import get_args

import httpx
import pytest
from fastapi import FastAPI

from app.broker.alpaca.clerk import set_alpaca_clerk
from app.broker.v2panel.vocabulary import ACTION_IDS
from app.routers import broker_v2_panel
from app.schemas.broker_v2_panel import PanelActionRequest
from app.schemas.run_admission import StartRuntimeAdmissionFact
from app.services import bot_runner
from app.services.bot_clerk_lifecycle import ActiveClerkUnavailableError
from app.services.bot_run_evidence import ACTIVATION_FAILED_STOP_REASON_CODE
from app.services.bot_runner import BotTaskRegistry, RunAdmissionRefusedError
from app.services.bot_runner_errors import ActivationFailedCleanupProvenError
from app.services.broker_v2_panel.outcome_copy import outcome_card_copy, outcome_headline
from tests._helpers.bot_runner.custody import _SID, _custody_proof, _registry
from tests._helpers.bot_runner.doubles import _CustodyClerk, _FakeFeed
from tests._helpers.bot_runner.market import patch_fresh_live_market_liveness
from tests._helpers.canary_admission import admit_canary_pairing
from tests._helpers.exit_terms import DEPLOY_EXIT_TERMS


@pytest.mark.parametrize('action_id', ['pause', 'continue', 'resume'])
@pytest.mark.parametrize('suffix', ['actions', 'actions/quiesce'])
async def test_removed_action_cannot_cross_the_command_boundary(
    action_id: str, suffix: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = FastAPI()
    app.include_router(broker_v2_panel.router)
    calls = []

    async def action(broker: str, account_id: str, sid: str, request: PanelActionRequest):
        calls.append(request)
        raise AssertionError('A removed lifecycle command reached the real router dispatch')

    monkeypatch.setattr(broker_v2_panel, '_run_action', action)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post(f'/api/brokers/alpaca/accounts/account/bots/stopped/{suffix}', json={
            'action_id': action_id, 'idempotency_key': 'removed-command',
            'revision': 1, 'concurrency_token': 'stale-tab-token',
        })
    assert response.status_code == 422
    assert not calls
    assert action_id not in ACTION_IDS


def test_registry_has_no_alternate_restart_or_pause_capability() -> None:
    for method in ('resume_existing', 'resume_existing_with_admission', 'preview_resume_admission', 'pause', 'continue_paused'):
        assert not hasattr(BotTaskRegistry, method)


def test_registry_has_no_restart_intensity_state(tmp_path: Path) -> None:
    """Every Deploy is a fresh identity with fresh consent and nothing restarts
    on its own, so no restart remains to throttle: a check that cannot fire."""
    registry = _registry(tmp_path, _FakeFeed([], mode='hold'))
    assert 'restart_policy' not in inspect.signature(BotTaskRegistry).parameters
    for name in ('_start_history', '_restart_policy', '_enforce_restart_intensity', '_projected_start_count', '_starts_in_window'):
        assert not hasattr(registry, name)
    assert 'RESTART_INTENSITY_EXCEEDED' not in get_args(StartRuntimeAdmissionFact.model_fields['state'].annotation)


async def test_stopped_identity_cannot_redeploy_and_fresh_deploy_keeps_old_receipts(tmp_path: Path) -> None:
    registry = _registry(tmp_path, _FakeFeed([], mode='hold'))
    set_alpaca_clerk(_CustodyClerk(_custody_proof(exposure={})))
    try:
        first = await registry.deploy(exit_terms=DEPLOY_EXIT_TERMS, broker='alpaca', strategy_instance_id=_SID, symbol='SPY')
        await registry.stop('alpaca', _SID)
        historical = registry.status('alpaca', _SID)
        old_binding = registry.binding_for_control('alpaca', _SID)
        with pytest.raises(RunAdmissionRefusedError):
            await registry.deploy(exit_terms=DEPLOY_EXIT_TERMS, broker='alpaca', strategy_instance_id=_SID, symbol='SPY')
        new = await registry.deploy(exit_terms=DEPLOY_EXIT_TERMS, broker='alpaca', strategy_instance_id='fresh-deployment', symbol='SPY')
        assert new.strategy_instance_id != first.strategy_instance_id
        assert new.active_run_id != first.active_run_id
        assert registry.status('alpaca', _SID) == historical
        assert registry.binding_for_control('alpaca', _SID) == old_binding
        await registry.stop('alpaca', 'fresh-deployment')
    finally:
        await registry.stop_all()
        set_alpaca_clerk(None)


async def test_unrecorded_launch_stops_and_reaps_the_task_it_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#2550 review: a Deploy whose launch record fails commits STOP and releases
    the budget; the supervise task it already started must not outlive that."""
    registry = _registry(tmp_path, _FakeFeed([], mode='hold'))
    clerk = _CustodyClerk(_custody_proof(exposure={}))
    set_alpaca_clerk(clerk)
    started: list[asyncio.Task[None]] = []

    async def launch_not_recorded(binding) -> None:
        started.append(registry._bots[binding.strategy_instance_id].task)
        raise ActiveClerkUnavailableError('The budget-backed deployment authority is unavailable.')

    monkeypatch.setattr(bot_runner, 'commit_deploy_launch', launch_not_recorded)
    try:
        with pytest.raises(ActivationFailedCleanupProvenError):
            await registry.deploy(exit_terms=DEPLOY_EXIT_TERMS, broker='alpaca', strategy_instance_id=_SID, symbol='SPY')

        (task,) = started
        assert task.cancelled()
        assert _SID not in registry._bots
        assert clerk.stopped_runs == [registry.binding_for_control('alpaca', _SID).run_id]
        assert registry.status('alpaca', _SID).running is False
        # #2559: run history names the failed launch, not an operator's stop —
        # the compensation runs through the normal Stop, but nobody stopped it.
        lifecycle = registry._lifecycle_repo(_SID).read()
        assert lifecycle is not None and lifecycle.duty_outcome is not None
        assert lifecycle.duty_outcome.kind == "FAILED_LAUNCH"
        assert lifecycle.duty_outcome.reason_code == ACTIVATION_FAILED_STOP_REASON_CODE
        # ...and History words that recorded outcome as a failed launch, not
        # "Stopped by you" (#2661 review).
        outcome = lifecycle.duty_outcome
        assert outcome_headline(outcome.kind, outcome.reason_code, flattened=False) == "Failed to launch"
    finally:
        await registry.stop_all()
        set_alpaca_clerk(None)


@pytest.mark.parametrize(
    ("launch_recorded", "kind", "headline"),
    [(False, "FAILED_LAUNCH", "Failed to launch"), (True, "STOPPED", "Stopped by you")],
    ids=["failed_launch", "operator_stop"],
)
async def test_a_trade_run_records_who_ended_it_beside_its_custody_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, launch_recorded: bool, kind: str, headline: str,
) -> None:
    """#2667: in trade mode the reason is the Clerk's custody proof -- the
    panel's next step and the canary verdict read it -- so a failed launch
    used to read as an operator's stop. The kind now says who ended the run;
    an operator's Stop reads as before."""
    patch_fresh_live_market_liveness(monkeypatch)
    admit_canary_pairing(monkeypatch, 'deployment_validation', 'paper-account')
    monkeypatch.setattr(bot_runner, 'canary_gate_applies', lambda **_: True)
    registry = _registry(tmp_path, _FakeFeed([], mode='hold'))
    clerk = _CustodyClerk(_custody_proof(exposure={'SPY': 1.0}))
    set_alpaca_clerk(clerk)

    async def launch_not_recorded(_binding) -> None:
        raise ActiveClerkUnavailableError('The budget-backed deployment authority is unavailable.')

    if not launch_recorded:
        monkeypatch.setattr(bot_runner, 'commit_deploy_launch', launch_not_recorded)
    try:
        deploy = registry.deploy(
            exit_terms=DEPLOY_EXIT_TERMS, broker='alpaca', strategy_instance_id=_SID, symbol='SPY', mode='trade',
        )
        if launch_recorded:
            await deploy
            await registry.stop('alpaca', _SID)
        else:
            with pytest.raises(ActivationFailedCleanupProvenError):
                await deploy

        lifecycle = registry._lifecycle_repo(_SID).read()
        assert lifecycle is not None and lifecycle.duty_outcome is not None
        outcome = lifecycle.duty_outcome
        assert (outcome.kind, outcome.reason_code) == (kind, 'STOP_REQUIRES_FLATTEN')
        assert outcome_headline(outcome.kind, outcome.reason_code, flattened=False) == headline
        label, explanation = outcome_card_copy(outcome.kind, outcome.reason_code)
        assert label == ('Failed to launch' if not launch_recorded else 'Stopped; flatten required')
        assert explanation.endswith('Use Flatten to resolve that exposure.')
        # The canary verdict is keyed off the proof, as before.
        assert outcome.canary_rollback is not None
        assert (outcome.canary_rollback.stop_outcome, outcome.canary_rollback.allowed) == ('STOP_REQUIRES_FLATTEN', False)
    finally:
        await registry.stop_all()
        set_alpaca_clerk(None)
