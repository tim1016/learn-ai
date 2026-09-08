"""On a shadow authority the deploy path offers `shadow`, never `paper` (ADR 0059 D2).

A live Alpaca account is deployable only through the Shadow Account Authority:
the view reports the live ``account_mode`` honestly, labels itself a shadow
account, and offers exactly the two modes that submit nothing to real money.
A request naming a mode the view does not offer is refused before any
mode-tiered gate runs, and a live account with no shadow world installed is
refused outright.
"""

from __future__ import annotations

import httpx
import pytest
from httpx import ASGITransport

from app.broker.alpaca.clerk.models import ChannelHealth, ClerkStatus, HoldState
from app.broker.contract.models import BrokerAccountSnapshot
from app.broker.contract.registry import get_broker_registry
from app.schemas.broker_bots import AlpacaPaperDeployRequest, AlpacaPaperDeployView
from app.schemas.operator_blocker import AccountOperatorPosture
from app.schemas.strategy_validation import StrategyValidationEntry
from app.services.broker_v2_panel import panel_deploy
from app.services.broker_v2_panel.panel_deploy import _require_alpaca_deploy_request
from app.services.broker_v2_panel.panel_errors import PanelRunnerError, PanelUnavailableError
from app.services.broker_v2_panel.paper_deploy_service import build_alpaca_paper_deploy_view
from app.services.strategy_validation_manifest import (
    load_strategy_validation_entries,
    strategy_registry_seeds,
)
from app.utils.timestamps import now_ms_utc
from tests._helpers.canary_admission import admit_canary_pairing
from tests.broker.v2panel.conftest import _BODY, account_snapshot
from tests.broker.v2panel.fixtures import ACCT, SID

LIVE_ACCT = "9LIVE0001"
_STRATEGY_KEY = "ema_crossover_signal"
_HEALTHY_POSTURE = AccountOperatorPosture(
    condition=None,
    account_desk=None,
    fleet_roster=None,
    status_headline="Account Clerk custody is healthy",
    status_detail=None,
)


def _entries() -> list[StrategyValidationEntry]:
    """The one committed, currently-accepted entry the deploy fixtures use."""
    return [
        entry
        for entry in load_strategy_validation_entries(strategy_registry_seeds())
        if entry.strategy_key == _STRATEGY_KEY
    ]


def _clerk_status(account_id: str) -> ClerkStatus:
    """A healthy Clerk read: no freeze, no hold, both channels fresh.

    Observation time is read from the clock because the view under test
    stamps itself with ``now_ms_utc()`` and judges channel staleness against
    that stamp -- the same shape ``conftest.deploy_app``'s fake uses.
    """
    observed_at_ms = now_ms_utc()
    return ClerkStatus(
        broker="alpaca",
        account_id=account_id,
        hold=HoldState(active=False),
        outstanding_intents=0,
        observed_at_ms=observed_at_ms,
        channel_healths=[
            ChannelHealth(stream="market_data", healthy=True, connected=True, observed_at_ms=observed_at_ms),
            ChannelHealth(stream="execution", healthy=True, connected=True, observed_at_ms=observed_at_ms),
        ],
        operator_posture=_HEALTHY_POSTURE,
    )


def _live_account() -> BrokerAccountSnapshot:
    return account_snapshot(account_id=LIVE_ACCT, account_mode="live")


def _shadow_view(monkeypatch: pytest.MonkeyPatch) -> AlpacaPaperDeployView:
    admit_canary_pairing(monkeypatch, _STRATEGY_KEY, LIVE_ACCT)
    return build_alpaca_paper_deploy_view(
        _live_account(),
        _clerk_status(LIVE_ACCT),
        _entries(),
        symbol="SPY",
        custody_world="shadow",
    )


def test_shadow_world_authors_shadow_and_dry_run_only(monkeypatch: pytest.MonkeyPatch) -> None:
    view = _shadow_view(monkeypatch)

    assert view.account_mode == "live"
    assert view.account_label == f"Alpaca shadow · {LIVE_ACCT}"
    offered = {mode.mode: mode.availability for mode in view.execution_modes}
    assert offered == {"dry_run": "available", "shadow": "available", "live": "planned"}
    assert all("paper" not in strategy.admissible_modes for strategy in view.strategies)
    assert any("shadow" in strategy.admissible_modes for strategy in view.strategies)
    assert view.eligibility.eligible is True  # account_ready admits the shadow world


def test_real_paper_world_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    admit_canary_pairing(monkeypatch, _STRATEGY_KEY, ACCT)

    view = build_alpaca_paper_deploy_view(
        account_snapshot(),
        _clerk_status(ACCT),
        _entries(),
        symbol="SPY",
        custody_world="real_paper",
    )

    assert view.account_mode == "paper"
    assert view.account_label == f"Alpaca paper · {ACCT}"
    assert {mode.mode for mode in view.execution_modes} == {"dry_run", "paper", "live"}
    assert any("paper" in strategy.admissible_modes for strategy in view.strategies)


def _request(execution_mode: str) -> AlpacaPaperDeployRequest:
    return AlpacaPaperDeployRequest.model_validate({**_BODY, "execution_mode": execution_mode})


def test_deploy_request_for_a_mode_the_view_does_not_offer_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    view = _shadow_view(monkeypatch)

    with pytest.raises(PanelRunnerError) as refused:
        _require_alpaca_deploy_request(view, _request("paper"))

    assert refused.value.http_status == 409
    assert "not available on this account" in str(refused.value)


def test_shadow_request_passes_the_gate_paper_passes_today(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shadow request clears the same preflight a paper request clears."""
    view = _shadow_view(monkeypatch)

    resolved = _require_alpaca_deploy_request(view, _request("shadow"))

    assert "symbol" not in resolved.effective
    assert resolved.origins


@pytest.mark.asyncio
async def test_live_account_with_no_shadow_world_is_still_refused(
    deploy_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fast_app, _registry = deploy_app
    monkeypatch.setattr(get_broker_registry().resolve("alpaca"), "account", _live_account())
    monkeypatch.setattr(panel_deploy, "primary_custody_world", lambda: None)

    with pytest.raises(PanelUnavailableError, match=r"Alpaca live-account deployment is refused\."):
        await panel_deploy.get_alpaca_paper_deploy_view("alpaca", LIVE_ACCT)

    async with httpx.AsyncClient(transport=ASGITransport(app=fast_app), base_url="http://test") as client:
        response = await client.post(
            f"/api/brokers/alpaca/accounts/{LIVE_ACCT}/bots",
            json={**_BODY, "strategy_instance_id": SID},
        )

    assert response.status_code == 503
