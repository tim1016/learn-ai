"""A Deploy that names no budget is refused, typed, before a bot is named or registered (#3216)."""

from __future__ import annotations

import httpx
import pytest
from httpx import ASGITransport

from app.schemas.broker_bots import AlpacaDeploySubmission
from app.services.broker_v2_panel import panel_deploy
from app.services.broker_v2_panel.deploy_submissions import DeploySubmissionLedger
from tests.broker.v2panel.conftest import _BODY
from tests.broker.v2panel.fixtures import ACCT

_BOTS = f"/api/brokers/alpaca/accounts/{ACCT}/bots"
_RECOVERY = f"/api/brokers/alpaca/accounts/{ACCT}/deploy-submissions"
_NO_BUDGET = {key: value for key, value in _BODY.items() if key != "budget"}
_ALLOW_BODY_STRATEGY = frozenset({("ema_crossover_signal", ACCT)})


@pytest.fixture
def deploy_with_the_pairing_admitted(deploy_app, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.services.canary_admission.CANARY_ADMITTED_PROGRAM_ACCOUNT_PAIRS", _ALLOW_BODY_STRATEGY)
    return deploy_app


def _client(fast_app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=fast_app), base_url="http://test")


async def test_a_deploy_with_no_budget_is_refused_with_its_code_and_names_no_bot(
    deploy_with_the_pairing_admitted,
) -> None:
    fast_app, registry = deploy_with_the_pairing_admitted

    async with _client(fast_app) as client:
        refused = await client.post(_BOTS, json=_NO_BUDGET)
        recovered = await client.get(f"{_RECOVERY}/{_NO_BUDGET['submission_key']}")

    assert refused.status_code == 409
    detail = refused.json()["detail"]
    assert detail["reason_code"] == "deploy_budget_required"
    assert detail["outcome"] == "conflict" and detail["receipt_id"] is None
    # Nothing was claimed or started under the key, so the client may use it again.
    assert detail["submission_settled"] is True
    assert registry.deploy_calls == []
    assert not (registry.artifacts_root / "deploy_submissions").exists()
    assert recovered.status_code == 404


async def test_a_resend_with_no_budget_of_a_key_an_earlier_send_claimed_is_not_settled(
    deploy_with_the_pairing_admitted,
) -> None:
    """A key recorded before budgets were required may have started its bot: the refusal leaves it to the recovery read."""
    fast_app, registry = deploy_with_the_pairing_admitted
    request = AlpacaDeploySubmission.model_validate(_NO_BUDGET)
    DeploySubmissionLedger(registry.artifacts_root).claim(
        submission_key=request.submission_key, symbol=request.symbol, strategy_key=request.strategy_key,
        request_fingerprint=panel_deploy._submission_fingerprint(ACCT, request),
        replaces_strategy_instance_id=None, now_ms=1_790_000_000_000,
    )

    async with _client(fast_app) as client:
        refused = await client.post(_BOTS, json=_NO_BUDGET)

    assert refused.status_code == 409
    assert refused.json()["detail"]["reason_code"] == "deploy_budget_required"
    assert refused.json()["detail"]["submission_settled"] is False
    assert registry.deploy_calls == []
