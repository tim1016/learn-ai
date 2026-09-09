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
from app.schemas.broker_bots import (
    AlpacaPaperDeployReadinessCheck,
    AlpacaPaperDeployReceipt,
    AlpacaPaperDeployRequest,
    AlpacaPaperDeployView,
    BotStatusView,
)
from app.schemas.operator_blocker import AccountOperatorPosture
from app.schemas.run_admission import RunAdmissionDecision
from app.schemas.strategy_validation import StrategyValidationEntry
from app.services.broker_v2_panel import panel_deploy
from app.services.broker_v2_panel.panel_deploy import _require_alpaca_deploy_request
from app.services.broker_v2_panel.panel_errors import PanelRunnerError, PanelUnavailableError
from app.services.broker_v2_panel.paper_deploy_service import (
    build_alpaca_paper_deploy_receipt,
    build_alpaca_paper_deploy_view,
    resolve_deploy_strategy_params,
)
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
_BINDING_AT_MS = 1_700_000_000_000
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


def _paper_view(monkeypatch: pytest.MonkeyPatch) -> AlpacaPaperDeployView:
    admit_canary_pairing(monkeypatch, _STRATEGY_KEY, ACCT)
    return build_alpaca_paper_deploy_view(
        account_snapshot(),
        _clerk_status(ACCT),
        _entries(),
        symbol="SPY",
        custody_world="real_paper",
    )


def test_real_paper_world_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    view = _paper_view(monkeypatch)

    assert view.account_mode == "paper"
    assert view.account_label == f"Alpaca paper · {ACCT}"
    assert {mode.mode for mode in view.execution_modes} == {"dry_run", "paper", "live"}
    assert any("paper" in strategy.admissible_modes for strategy in view.strategies)


def _account_posture_row(view: AlpacaPaperDeployView) -> AlpacaPaperDeployReadinessCheck:
    return next(check for check in view.readiness_checks if check.gate_id == "broker.account_posture")


def test_shadow_view_never_calls_the_live_account_a_paper_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The page must not tell an operator that a real-money account is paper."""
    view = _shadow_view(monkeypatch)
    row = _account_posture_row(view)

    assert row.label == "Shadow account posture"
    assert row.headline == "The Alpaca shadow account is active and readable."
    assert row.evidence_summary == (
        f"Alpaca live account {LIVE_ACCT}, read through its shadow authority, reports status ACTIVE."
    )
    assert view.eligibility.headline == (
        "This Alpaca account is eligible for a Clerk-governed shadow deployment."
    )
    prose = (
        row.label,
        row.headline,
        row.explanation,
        row.evidence_summary,
        view.eligibility.headline,
        view.eligibility.explanation,
    )
    assert not any("paper" in sentence.lower() for sentence in prose)
    # The wire tokens the Frontend consumes are deliberately not world-scoped.
    assert row.gate_id == "broker.account_posture"
    assert view.eligibility.reason_code == "ALPACA_PAPER_DEPLOY_READY"


def test_real_paper_view_still_names_the_paper_account(monkeypatch: pytest.MonkeyPatch) -> None:
    """Characterization pin: the real-paper world's prose stays byte-identical."""
    view = _paper_view(monkeypatch)
    row = _account_posture_row(view)

    assert row.label == "Paper account posture"
    assert row.headline == "The Alpaca paper account is active and tradable."
    assert row.explanation == (
        "The server resolved the selected paper account and found no broker trading block."
    )
    assert row.evidence_summary == f"Alpaca paper account {ACCT} reports status ACTIVE."
    assert view.eligibility.headline == (
        "This Alpaca paper account is eligible for a Clerk-governed deployment."
    )
    assert view.eligibility.explanation == (
        "The operator may choose Clerk-governed paper execution or a zero-broker-write Dry Run before launch."
    )


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


def _bot() -> BotStatusView:
    return BotStatusView(
        strategy_instance_id=SID,
        strategy_key=_STRATEGY_KEY,
        broker="alpaca",
        symbol="SPY",
        mode="trade",
        quantity=2,
        running=True,
        phase="ON_DUTY",
        desired_state="RUNNING",
        active_run_id="run-test",
        duty_outcome=None,
        binding_created_at_ms=_BINDING_AT_MS,
        last_transition_at_ms=None,
    )


def _admission() -> RunAdmissionDecision:
    return RunAdmissionDecision(
        operation="START",
        allowed=True,
        reason_code="START_ADMITTED",
        explanation="The Clerk and bot registry admit Start.",
        next_step=None,
        strategy_instance_id=SID,
        proposed_run_id="run-test",
        configuration_hash="a" * 64,
        account_id=LIVE_ACCT,
        evaluated_at_ms=_BINDING_AT_MS,
        fact_ages_ms={
            "program_build": 0,
            "runtime": 0,
            "process": 0,
            "market_data": 0,
            "market_liveness": 0,
            "clerk": 0,
        },
        evidence_refs=("test-admission",),
    )


def _receipt(view: AlpacaPaperDeployView, execution_mode: str) -> AlpacaPaperDeployReceipt:
    request = _request(execution_mode)
    return build_alpaca_paper_deploy_receipt(
        broker="alpaca",
        view=view,
        request=request,
        bot=_bot(),
        admission=_admission(),
        resolved_params=resolve_deploy_strategy_params(
            request.strategy_key, request.symbol, dict(request.parameters)
        ),
    )


def test_shadow_receipt_names_the_shadow_world_not_paper(monkeypatch: pytest.MonkeyPatch) -> None:
    """A shadow deploy lands on a real-money account; its receipt must say so."""
    receipt = _receipt(_shadow_view(monkeypatch), "shadow")

    assert receipt.message == f"{SID} is on duty in Alpaca shadow."
    assert receipt.explanation == (
        "The deployment binding is durable; the shadow Clerk synthesizes every fill "
        "against this live account's real reads and submits nothing (ADR 0059 D2)."
    )
    assert receipt.next_action == "Open the bot panel and verify the first synthesized shadow receipt."
    prose = (receipt.message, receipt.explanation, receipt.next_action)
    assert not any("paper" in sentence.lower() for sentence in prose)


def test_paper_receipt_copy_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Characterization pin: the real-paper receipt's three sentences stay byte-identical."""
    admit_canary_pairing(monkeypatch, _STRATEGY_KEY, ACCT)
    view = build_alpaca_paper_deploy_view(
        account_snapshot(),
        _clerk_status(ACCT),
        _entries(),
        symbol="SPY",
        custody_world="real_paper",
    )

    receipt = _receipt(view, "paper")

    assert receipt.message == f"{SID} is on duty in Alpaca paper."
    assert receipt.explanation == (
        "The deployment binding is durable and all strategy effects are owned by the Alpaca Clerk."
    )
    assert receipt.next_action == "Open the production bot panel and verify the first Clerk receipt."


@pytest.mark.asyncio
async def test_live_account_with_no_shadow_world_is_still_refused(
    deploy_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fast_app, _registry = deploy_app
    monkeypatch.setattr(get_broker_registry().resolve("alpaca"), "account", _live_account())
    monkeypatch.setattr(panel_deploy, "primary_custody_world", lambda: None)

    with pytest.raises(PanelUnavailableError, match=r"Alpaca account deployment is refused\.") as refused:
        await panel_deploy.get_alpaca_paper_deploy_view("alpaca", LIVE_ACCT)

    assert refused.value.detail is not None
    assert "No Alpaca authority is installed" in refused.value.detail

    async with httpx.AsyncClient(transport=ASGITransport(app=fast_app), base_url="http://test") as client:
        response = await client.post(
            f"/api/brokers/alpaca/accounts/{LIVE_ACCT}/bots",
            json={**_BODY, "strategy_instance_id": SID},
        )

    assert response.status_code == 503


@pytest.mark.asyncio
async def test_shadow_authority_refuses_a_paper_account_by_name(
    deploy_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A shadow authority only admits a live account; the refusal names the shadow world, not `real_paper`."""
    monkeypatch.setattr(panel_deploy, "primary_custody_world", lambda: "shadow")

    with pytest.raises(PanelUnavailableError, match=r"Alpaca account deployment is refused\.") as refused:
        await panel_deploy.get_alpaca_paper_deploy_view("alpaca", ACCT)

    assert refused.value.detail is not None
    assert "The active shadow authority does not admit" in refused.value.detail
