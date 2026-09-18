"""Browser graduation orchestration keeps account-world boundaries explicit."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.broker.alpaca.clerk.active_runtime import ActiveClerkRuntime
from app.broker.alpaca.clerk.sqlite.cutover import BrokerCutoverEvidence
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


def _evidence(*, positions: dict[str, float], open_order_ids: tuple[str, ...]) -> BrokerCutoverEvidence:
    return BrokerCutoverEvidence(
        account_id=ACCOUNT,
        account_mode="live",
        observed_at_ms=1_000,
        proof_reference="alpaca-account-read:fixture",
        positions=positions,
        open_order_ids=open_order_ids,
    )


def _fake_plan(evidence: BrokerCutoverEvidence) -> SimpleNamespace:
    return SimpleNamespace(
        plan_id="a" * 64,
        confirmation_token="b" * 64,
        account_id=ACCOUNT,
        created_at_ms=1_000,
        expires_at_ms=1_000 + 300_000,
        broker_evidence=evidence,
        runner_roster=(),
    )


async def _returns(value: object) -> object:
    return value


async def test_apply_re_observes_broker_state_instead_of_replaying_prepares_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: apply() must call the broker again, not replay prepare()'s snapshot.

    ``apply_cutover`` refuses when its ``broker_evidence`` argument disagrees with the
    plan's stored evidence — but only if the caller supplies an independently observed
    apply-time snapshot. Before this fix, ``_apply_domain_plan`` echoed
    ``plan.broker_evidence`` straight back, so that check always compared the plan
    against itself and could never catch a position or order opened during the
    five-minute confirmation window.
    """
    monkeypatch.setattr(
        graduation,
        "get_active_clerk_runtime",
        lambda: ActiveClerkRuntime(
            authority_kind="shadow",
            account_id=f"shadow:{ACCOUNT}",
            account_authority_kind="shadow",
        ),
    )
    flat_evidence = _evidence(positions={}, open_order_ids=())
    drifted_evidence = _evidence(positions={"AAPL": 5.0}, open_order_ids=())
    observed = iter([flat_evidence, drifted_evidence])
    monkeypatch.setattr(
        graduation.AlpacaLiveGraduationService,
        "_capture_broker_evidence",
        lambda self, account_id: _returns(next(observed)),
    )
    monkeypatch.setattr(
        graduation.AlpacaLiveGraduationService,
        "_prepare_domain_plan",
        lambda self, account_id, evidence: (_fake_plan(evidence), "backup/ref.tar"),
    )
    monkeypatch.setattr(
        graduation.AlpacaLiveGraduationService,
        "_read_persisted_plan",
        lambda self, account_id, plan_id: (_fake_plan(flat_evidence), Path("backup/ref.tar")),
    )
    applied_with: list[BrokerCutoverEvidence] = []

    def _fake_apply_domain_plan(self, plan, backup_path, confirmation_token, broker_evidence):
        applied_with.append(broker_evidence)
        return SimpleNamespace(
            receipt_reference="evidence/cutover-receipt.json",
            activation=SimpleNamespace(activated_at_ms=2_000),
        )

    monkeypatch.setattr(
        graduation.AlpacaLiveGraduationService,
        "_apply_domain_plan",
        _fake_apply_domain_plan,
    )

    service = graduation.AlpacaLiveGraduationService()
    plan_view = await service.prepare(ACCOUNT)
    await service.apply(
        ACCOUNT,
        plan_id=plan_view.plan_id,
        confirmation_token=plan_view.confirmation_token,
    )

    assert applied_with == [drifted_evidence]

