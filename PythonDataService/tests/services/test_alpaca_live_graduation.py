"""Browser graduation orchestration keeps account-world boundaries explicit."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.broker.alpaca.clerk.active_runtime import ActiveClerkRuntime
from app.services import alpaca_live_graduation as graduation

ACCOUNT = "318420190"


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        mode="live",
        live_loss_fraction=0.1,
        live_loss_usd=200.0,
        live_arming_max_sessions=1,
        live_xh_entry_bps=0.0,
        live_xh_exit_bps=0.0,
    )


@pytest.fixture(autouse=True)
def _safe_live_posture(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(graduation, "resolved_alpaca_settings", _settings)
    monkeypatch.setattr(
        graduation.settings,
        "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL",
        False,
    )
    monkeypatch.setattr(graduation.fleet_settings, "WORKER_SERVICE", "alpaca-live-clerk")


def test_shadow_authority_offers_review_without_claiming_deploy_or_arming(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        graduation,
        "get_active_clerk_runtime",
        lambda: ActiveClerkRuntime(
            authority_kind="shadow",
            account_id=f"shadow:{ACCOUNT}",
            account_authority_kind="shadow",
        ),
    )

    result = graduation.AlpacaLiveGraduationService().status(ACCOUNT)

    assert result.state == "review_available"
    assert result.authority == "shadow"
    assert "No authority changes" in (result.next_action or "")


def test_graduated_authority_names_deploy_then_arm_as_separate_next_steps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        graduation,
        "get_active_clerk_runtime",
        lambda: ActiveClerkRuntime(
            authority_kind="sqlite",
            account_id=ACCOUNT,
            account_authority_kind="real_live",
        ),
    )

    result = graduation.AlpacaLiveGraduationService().status(ACCOUNT)

    assert result.state == "graduated"
    assert result.authority == "live"
    assert result.next_action is not None
    assert "Deploy" in result.next_action
    assert "arming" in result.next_action


def test_graduation_refuses_an_unmanaged_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(graduation.fleet_settings, "WORKER_SERVICE", None)
    monkeypatch.setattr(
        graduation,
        "get_active_clerk_runtime",
        lambda: ActiveClerkRuntime(
            authority_kind="shadow",
            account_id=f"shadow:{ACCOUNT}",
            account_authority_kind="shadow",
        ),
    )

    result = graduation.AlpacaLiveGraduationService().status(ACCOUNT)

    assert result.state == "blocked"
    assert result.restart_managed is False
    assert "supervised worker restart" in result.headline.lower()


def test_graduation_refuses_open_control_before_live_custody(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        graduation.settings,
        "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL",
        True,
    )
    monkeypatch.setattr(
        graduation,
        "get_active_clerk_runtime",
        lambda: ActiveClerkRuntime(
            authority_kind="shadow",
            account_id=f"shadow:{ACCOUNT}",
            account_authority_kind="shadow",
        ),
    )

    result = graduation.AlpacaLiveGraduationService().status(ACCOUNT)

    assert result.state == "blocked"
    assert "Authenticated control" in result.headline

