"""A Deploy that names no budget fails request validation before a bot is named (#3216)."""

from __future__ import annotations

import httpx
import pytest
from httpx import ASGITransport

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


async def test_a_deploy_with_no_budget_is_refused_by_the_wire_schema_and_names_no_bot(
    deploy_with_the_pairing_admitted,
) -> None:
    fast_app, registry = deploy_with_the_pairing_admitted

    async with _client(fast_app) as client:
        refused = await client.post(_BOTS, json=_NO_BUDGET)
        recovered = await client.get(f"{_RECOVERY}/{_NO_BUDGET['submission_key']}")

    assert refused.status_code == 422
    assert any(error["loc"] == ["body", "budget"] for error in refused.json()["detail"])
    assert registry.deploy_calls == []
    assert not (registry.artifacts_root / "deploy_submissions").exists()
    assert recovered.status_code == 404


async def test_a_resend_with_no_budget_does_not_change_a_previously_claimed_key(
    deploy_with_the_pairing_admitted,
) -> None:
    """Schema validation cannot renew or erase a key recorded before budgets were required."""
    fast_app, registry = deploy_with_the_pairing_admitted
    ledger = DeploySubmissionLedger(registry.artifacts_root)
    claimed = ledger.claim(
        submission_key=_NO_BUDGET["submission_key"], account_id=ACCT,
        symbol=_NO_BUDGET["symbol"], strategy_key=_NO_BUDGET["strategy_key"],
        request_fingerprint="legacy-no-budget-fingerprint",
        replaces_strategy_instance_id=None, now_ms=1_790_000_000_000,
    )

    async with _client(fast_app) as client:
        refused = await client.post(_BOTS, json=_NO_BUDGET)

    assert refused.status_code == 422
    assert ledger.by_key(_NO_BUDGET["submission_key"]) == claimed
    assert registry.deploy_calls == []
