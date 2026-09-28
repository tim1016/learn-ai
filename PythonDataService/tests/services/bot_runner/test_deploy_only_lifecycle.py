"""#2541: terminal trading needs fresh consent, while custody stays recoverable."""
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from app.broker.alpaca.clerk import set_alpaca_clerk
from app.broker.v2panel.vocabulary import ACTION_IDS
from app.routers import broker_v2_panel
from app.schemas.broker_v2_panel import PanelActionRequest
from app.services.bot_runner import BotTaskRegistry, RunAdmissionRefusedError
from tests._helpers.bot_runner.custody import _SID, _custody_proof, _registry
from tests._helpers.bot_runner.doubles import _CustodyClerk, _FakeFeed
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
