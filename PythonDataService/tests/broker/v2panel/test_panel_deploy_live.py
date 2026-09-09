"""On the real-live authority the deploy path offers `live`, never `paper` or `shadow` (ADR 0059 D11, slice 7 R7)."""

from __future__ import annotations

import pytest

from app.schemas.broker_bots import AlpacaPaperDeployRequest, AlpacaPaperDeployView, AlpacaPaperSizingSelection
from app.services.broker_v2_panel.panel_deploy import _require_alpaca_deploy_request
from app.services.broker_v2_panel.panel_errors import PanelRunnerError
from app.services.broker_v2_panel.paper_deploy_service import build_alpaca_paper_deploy_view
from tests._helpers.canary_admission import admit_canary_pairing
from tests.broker.v2panel.test_panel_deploy_shadow import (
    _STRATEGY_KEY,
    LIVE_ACCT,
    _clerk_status,
    _entries,
    _live_account,
)


def _live_view(monkeypatch: pytest.MonkeyPatch) -> AlpacaPaperDeployView:
    admit_canary_pairing(monkeypatch, _STRATEGY_KEY, LIVE_ACCT)
    return build_alpaca_paper_deploy_view(
        _live_account(), _clerk_status(LIVE_ACCT), _entries(), symbol="SPY", custody_world="real_live"
    )


def test_the_live_world_offers_live_and_dry_run_only(monkeypatch: pytest.MonkeyPatch) -> None:
    view = _live_view(monkeypatch)
    assert view.account_mode == "live"
    assert view.account_label == f"Alpaca live · {LIVE_ACCT}"
    assert {mode.mode: mode.availability for mode in view.execution_modes} == {"dry_run": "available", "live": "available"}
    assert all("paper" not in strategy.admissible_modes and "shadow" not in strategy.admissible_modes for strategy in view.strategies)
    assert any("live" in strategy.admissible_modes for strategy in view.strategies)
    assert view.eligibility.eligible is True


def test_the_live_view_names_real_money_and_the_arming_step(monkeypatch: pytest.MonkeyPatch) -> None:
    view = _live_view(monkeypatch)
    live = next(mode for mode in view.execution_modes if mode.mode == "live")
    assert "real-money" in live.explanation and "arm" in live.explanation
    row = next(check for check in view.readiness_checks if check.gate_id == "broker.account_posture")
    assert row.label == "Live account posture"
    assert "paper" not in row.headline.lower()


def test_a_live_request_passes_the_mode_offer_check(monkeypatch: pytest.MonkeyPatch) -> None:
    view = _live_view(monkeypatch)
    strategy = next(s for s in view.strategies if s.selectable)
    request = AlpacaPaperDeployRequest(
        strategy_instance_id="ema-live-1",
        strategy_key=strategy.strategy_key,
        symbol="SPY",
        execution_mode="live",
        sizing=AlpacaPaperSizingSelection(preset="safe_canary", quantity=1),
        carryover_policy="FORBID",
        evidence_override=None,
    )
    assert _require_alpaca_deploy_request(view, request) is not None
    with pytest.raises(PanelRunnerError, match="not available on this account"):
        _require_alpaca_deploy_request(view, request.model_copy(update={"execution_mode": "shadow"}))
