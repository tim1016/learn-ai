"""Proof seams for bounded post-activation runner-catalog quarantine."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.sqlite import catalog_quarantine
from app.broker.alpaca.clerk.sqlite.catalog_quarantine import (
    CatalogQuarantineRefused,
    apply_catalog_quarantine,
    plan_catalog_quarantine,
)
from app.broker.alpaca.clerk.sqlite.database_verification import DatabaseVerification
from app.engine.live.account_registry import (
    AccountInstanceBinding,
    bot_order_namespace_for_instance,
)
from app.services.bot_binding_repository import StrategyInstanceRecord, alpaca_v1_action_plan
from tests._helpers.legacy_ibkr_artifacts import (
    write_historical_account_binding,
)

ACCOUNT_ID = "PATEST"


def _verification() -> DatabaseVerification:
    return DatabaseVerification(
        account_id=ACCOUNT_ID,
        authority_generation=3,
        db_identity_token="a" * 32,
        schema_version=1,
        control_revision=17,
        transition_count=4,
        last_sequence=4,
        last_row_hash="b" * 64,
    )


def _binding(root: Path, sid: str, *, broker: str = "alpaca") -> Path:
    instance_dir = root / "live_state" / sid
    instance_dir.mkdir(parents=True)
    record = StrategyInstanceRecord(
        strategy_instance_id=sid,
        strategy_key="deployment_validation",
        broker=broker,
        symbol="SPY",
        use_rth=True,
        mode="trade",
        quantity=1,
        carryover_policy="FORBID",
        action_plan=alpaca_v1_action_plan("SPY"),
        configuration_hash="c" * 64,
        created_at_ms=1_700_000_000_000,
    )
    (instance_dir / "strategy_instance.json").write_text(
        record.model_dump_json(),
        encoding="utf-8",
    )
    (instance_dir / "desired_state.json").write_text("{}", encoding="utf-8")
    return instance_dir


def _legacy_v1_binding(root: Path, sid: str) -> Path:
    instance_dir = root / "live_state" / sid
    instance_dir.mkdir(parents=True)
    (instance_dir / "broker_binding.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "strategy_instance_id": sid,
                "broker": "alpaca",
                "symbol": "SPY",
                "use_rth": True,
                "mode": "trade",
                "quantity": 1,
                "run_id": f"run-{sid}",
                "created_at_ms": 1_700_000_000_000,
            }
        ),
        encoding="utf-8",
    )
    return instance_dir


def _register_account_binding(
    root: Path,
    sid: str,
    *,
    account_id: str = ACCOUNT_ID,
) -> None:
    write_historical_account_binding(
        root,
        AccountInstanceBinding(
            account_id=account_id,
            strategy_instance_id=sid,
            run_id=f"run-{sid}",
            bot_order_namespace=bot_order_namespace_for_instance(sid),
            lifecycle_state="ACTIVE",
            recorded_at_ms=1_700_000_000_000,
            source="catalog-quarantine.test",
        ),
    )


@pytest.fixture()
def activated_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> catalog_quarantine._ActiveRegistry:
    active = catalog_quarantine._ActiveRegistry(
        database=_verification(),
        strategy_instance_ids=("current-sqlite",),
    )
    monkeypatch.setattr(
        catalog_quarantine,
        "_read_active_registry",
        lambda _root, _account: active,
    )
    monkeypatch.setattr(catalog_quarantine, "now_ms_utc", lambda: 1_700_000_000_000)
    return active


def test_plan_apply_moves_only_unregistered_alpaca_bindings(
    tmp_path: Path,
    activated_registry: catalog_quarantine._ActiveRegistry,
) -> None:
    runner_root = tmp_path / "runner"
    current = _binding(runner_root, "current-sqlite")
    legacy_a = _legacy_v1_binding(runner_root, "legacy-a")
    legacy_b = _binding(runner_root, "legacy-b")
    ibkr = _binding(runner_root, "ibkr-retained", broker="ibkr")
    for sid in ("current-sqlite", "legacy-a", "legacy-b", "ibkr-retained"):
        _register_account_binding(runner_root, sid)
    unbound = runner_root / "live_state" / "unbound-evidence"
    unbound.mkdir()
    (unbound / "lifecycle_state.json").write_text("{}", encoding="utf-8")

    plan = plan_catalog_quarantine(
        artifacts_root=tmp_path / "clerk",
        runner_artifacts_root=runner_root,
        account_id=ACCOUNT_ID,
        max_candidates=2,
        max_total_bytes=100_000,
    )
    receipt = apply_catalog_quarantine(
        plan=plan,
        confirmation_token=plan.confirmation_token,
        artifacts_root=tmp_path / "clerk",
        runner_artifacts_root=runner_root,
    )

    assert [item.strategy_instance_id for item in plan.candidates] == ["legacy-a", "legacy-b"]
    assert receipt.quarantined_count == 2
    assert current.is_dir()
    assert ibkr.is_dir()
    assert unbound.is_dir()
    assert not legacy_a.exists()
    assert not legacy_b.exists()
    quarantine = runner_root / receipt.quarantine_reference
    assert (quarantine / "legacy-a").is_dir()
    assert (quarantine / "legacy-b").is_dir()
    assert json.loads((quarantine / "manifest.json").read_text(encoding="utf-8"))["state"] == "applied"
    assert (quarantine / "receipt.json").is_file()


def test_apply_refuses_changed_catalog_without_moving_anything(
    tmp_path: Path,
    activated_registry: catalog_quarantine._ActiveRegistry,
) -> None:
    runner_root = tmp_path / "runner"
    legacy = _binding(runner_root, "legacy-a")
    _register_account_binding(runner_root, "legacy-a")
    plan = plan_catalog_quarantine(
        artifacts_root=tmp_path / "clerk",
        runner_artifacts_root=runner_root,
        account_id=ACCOUNT_ID,
        max_candidates=1,
        max_total_bytes=100_000,
    )
    (legacy / "desired_state.json").write_text('{"changed":true}', encoding="utf-8")

    with pytest.raises(CatalogQuarantineRefused, match="evidence changed"):
        apply_catalog_quarantine(
            plan=plan,
            confirmation_token=plan.confirmation_token,
            artifacts_root=tmp_path / "clerk",
            runner_artifacts_root=runner_root,
        )

    assert legacy.is_dir()


def test_apply_rolls_back_an_earlier_move_when_a_later_move_fails(
    tmp_path: Path,
    activated_registry: catalog_quarantine._ActiveRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner_root = tmp_path / "runner"
    legacy_a = _binding(runner_root, "legacy-a")
    legacy_b = _binding(runner_root, "legacy-b")
    _register_account_binding(runner_root, "legacy-a")
    _register_account_binding(runner_root, "legacy-b")
    plan = plan_catalog_quarantine(
        artifacts_root=tmp_path / "clerk",
        runner_artifacts_root=runner_root,
        account_id=ACCOUNT_ID,
        max_candidates=2,
        max_total_bytes=100_000,
    )
    real_replace = catalog_quarantine.os.replace

    def fail_second_move(source: Path, destination: Path) -> None:
        if Path(source) == legacy_b:
            raise OSError("injected second-move failure")
        real_replace(source, destination)

    monkeypatch.setattr(catalog_quarantine.os, "replace", fail_second_move)

    with pytest.raises(OSError, match="injected second-move failure"):
        apply_catalog_quarantine(
            plan=plan,
            confirmation_token=plan.confirmation_token,
            artifacts_root=tmp_path / "clerk",
            runner_artifacts_root=runner_root,
        )

    assert legacy_a.is_dir()
    assert legacy_b.is_dir()
    quarantine = runner_root / "legacy-catalog-quarantine" / plan.plan_id
    assert not (quarantine / "legacy-a").exists()
    assert not (quarantine / "legacy-b").exists()
    assert not (quarantine / "manifest.json").exists()

    monkeypatch.setattr(
        catalog_quarantine,
        "now_ms_utc",
        lambda: plan.expires_at_ms + 1,
    )
    with pytest.raises(CatalogQuarantineRefused, match="plan expired"):
        apply_catalog_quarantine(
            plan=plan,
            confirmation_token=plan.confirmation_token,
            artifacts_root=tmp_path / "clerk",
            runner_artifacts_root=runner_root,
        )


def test_plan_scopes_candidates_to_the_selected_account(
    tmp_path: Path,
    activated_registry: catalog_quarantine._ActiveRegistry,
) -> None:
    runner_root = tmp_path / "runner"
    selected = _binding(runner_root, "selected-account-bot")
    other = _binding(runner_root, "other-account-bot")
    _register_account_binding(runner_root, "selected-account-bot")
    _register_account_binding(
        runner_root,
        "other-account-bot",
        account_id="PAOTHER",
    )

    plan = plan_catalog_quarantine(
        artifacts_root=tmp_path / "clerk",
        runner_artifacts_root=runner_root,
        account_id=ACCOUNT_ID,
        max_candidates=2,
        max_total_bytes=100_000,
    )

    assert [item.strategy_instance_id for item in plan.candidates] == ["selected-account-bot"]
    assert selected.is_dir()
    assert other.is_dir()


def test_expired_plan_cannot_resume_without_a_durably_moved_artifact(
    tmp_path: Path,
    activated_registry: catalog_quarantine._ActiveRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner_root = tmp_path / "runner"
    legacy = _binding(runner_root, "legacy-a")
    _register_account_binding(runner_root, "legacy-a")
    plan = plan_catalog_quarantine(
        artifacts_root=tmp_path / "clerk",
        runner_artifacts_root=runner_root,
        account_id=ACCOUNT_ID,
        max_candidates=1,
        max_total_bytes=100_000,
    )
    real_replace = catalog_quarantine.os.replace

    def interrupt_before_first_move(source: Path, destination: Path) -> None:
        if Path(source) == legacy:
            raise KeyboardInterrupt
        real_replace(source, destination)

    monkeypatch.setattr(
        catalog_quarantine.os,
        "replace",
        interrupt_before_first_move,
    )
    with pytest.raises(KeyboardInterrupt):
        apply_catalog_quarantine(
            plan=plan,
            confirmation_token=plan.confirmation_token,
            artifacts_root=tmp_path / "clerk",
            runner_artifacts_root=runner_root,
        )

    quarantine = runner_root / "legacy-catalog-quarantine" / plan.plan_id
    assert (quarantine / "manifest.json").is_file()
    assert legacy.is_dir()

    monkeypatch.setattr(catalog_quarantine.os, "replace", real_replace)
    monkeypatch.setattr(
        catalog_quarantine,
        "now_ms_utc",
        lambda: plan.expires_at_ms + 1,
    )
    with pytest.raises(CatalogQuarantineRefused, match="plan expired"):
        apply_catalog_quarantine(
            plan=plan,
            confirmation_token=plan.confirmation_token,
            artifacts_root=tmp_path / "clerk",
            runner_artifacts_root=runner_root,
        )


def test_resume_preserves_manifest_when_an_earlier_artifact_is_already_moved(
    tmp_path: Path,
    activated_registry: catalog_quarantine._ActiveRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner_root = tmp_path / "runner"
    legacy_a = _binding(runner_root, "legacy-a")
    legacy_b = _binding(runner_root, "legacy-b")
    _register_account_binding(runner_root, "legacy-a")
    _register_account_binding(runner_root, "legacy-b")
    plan = plan_catalog_quarantine(
        artifacts_root=tmp_path / "clerk",
        runner_artifacts_root=runner_root,
        account_id=ACCOUNT_ID,
        max_candidates=2,
        max_total_bytes=100_000,
    )
    real_replace = catalog_quarantine.os.replace

    def interrupt_second_move(source: Path, destination: Path) -> None:
        if Path(source) == legacy_b:
            raise KeyboardInterrupt
        real_replace(source, destination)

    monkeypatch.setattr(catalog_quarantine.os, "replace", interrupt_second_move)
    with pytest.raises(KeyboardInterrupt):
        apply_catalog_quarantine(
            plan=plan,
            confirmation_token=plan.confirmation_token,
            artifacts_root=tmp_path / "clerk",
            runner_artifacts_root=runner_root,
        )

    quarantine = runner_root / "legacy-catalog-quarantine" / plan.plan_id
    assert not legacy_a.exists()
    assert legacy_b.is_dir()
    assert (quarantine / "legacy-a").is_dir()
    assert (quarantine / "manifest.json").is_file()

    def fail_resumed_second_move(source: Path, destination: Path) -> None:
        if Path(source) == legacy_b:
            raise OSError("injected resumed move failure")
        real_replace(source, destination)

    monkeypatch.setattr(catalog_quarantine.os, "replace", fail_resumed_second_move)
    with pytest.raises(OSError, match="injected resumed move failure"):
        apply_catalog_quarantine(
            plan=plan,
            confirmation_token=plan.confirmation_token,
            artifacts_root=tmp_path / "clerk",
            runner_artifacts_root=runner_root,
        )
    assert (quarantine / "legacy-a").is_dir()
    assert (quarantine / "manifest.json").is_file()

    monkeypatch.setattr(catalog_quarantine.os, "replace", real_replace)
    monkeypatch.setattr(
        catalog_quarantine,
        "now_ms_utc",
        lambda: plan.expires_at_ms + 1,
    )
    receipt = apply_catalog_quarantine(
        plan=plan,
        confirmation_token=plan.confirmation_token,
        artifacts_root=tmp_path / "clerk",
        runner_artifacts_root=runner_root,
    )
    assert receipt.quarantined_count == 2
    assert not legacy_b.exists()


def test_apply_refuses_a_symlinked_quarantine_root_before_moving(
    tmp_path: Path,
    activated_registry: catalog_quarantine._ActiveRegistry,
) -> None:
    runner_root = tmp_path / "runner"
    legacy = _binding(runner_root, "legacy-a")
    _register_account_binding(runner_root, "legacy-a")
    plan = plan_catalog_quarantine(
        artifacts_root=tmp_path / "clerk",
        runner_artifacts_root=runner_root,
        account_id=ACCOUNT_ID,
        max_candidates=1,
        max_total_bytes=100_000,
    )
    redirected = tmp_path / "redirected-quarantine"
    redirected.mkdir()
    (runner_root / "legacy-catalog-quarantine").symlink_to(
        redirected,
        target_is_directory=True,
    )

    with pytest.raises(CatalogQuarantineRefused, match="symbolic link"):
        apply_catalog_quarantine(
            plan=plan,
            confirmation_token=plan.confirmation_token,
            artifacts_root=tmp_path / "clerk",
            runner_artifacts_root=runner_root,
        )

    assert legacy.is_dir()
    assert not any(redirected.iterdir())


def test_apply_refuses_a_symlinked_artifact_destination_before_moving(
    tmp_path: Path,
    activated_registry: catalog_quarantine._ActiveRegistry,
) -> None:
    runner_root = tmp_path / "runner"
    legacy = _binding(runner_root, "legacy-a")
    _register_account_binding(runner_root, "legacy-a")
    plan = plan_catalog_quarantine(
        artifacts_root=tmp_path / "clerk",
        runner_artifacts_root=runner_root,
        account_id=ACCOUNT_ID,
        max_candidates=1,
        max_total_bytes=100_000,
    )
    quarantine_dir = runner_root / "legacy-catalog-quarantine" / plan.plan_id
    quarantine_dir.mkdir(parents=True)
    redirected = tmp_path / "redirected-artifact"
    redirected.mkdir()
    (quarantine_dir / "legacy-a").symlink_to(
        redirected,
        target_is_directory=True,
    )

    with pytest.raises(CatalogQuarantineRefused, match="symbolic link"):
        apply_catalog_quarantine(
            plan=plan,
            confirmation_token=plan.confirmation_token,
            artifacts_root=tmp_path / "clerk",
            runner_artifacts_root=runner_root,
        )

    assert legacy.is_dir()
    assert not any(redirected.iterdir())


def test_plan_refuses_candidate_set_larger_than_operator_bound(
    tmp_path: Path,
    activated_registry: catalog_quarantine._ActiveRegistry,
) -> None:
    runner_root = tmp_path / "runner"
    _binding(runner_root, "legacy-a")
    _binding(runner_root, "legacy-b")
    _register_account_binding(runner_root, "legacy-a")
    _register_account_binding(runner_root, "legacy-b")

    with pytest.raises(CatalogQuarantineRefused, match="operator-declared bounds"):
        plan_catalog_quarantine(
            artifacts_root=tmp_path / "clerk",
            runner_artifacts_root=runner_root,
            account_id=ACCOUNT_ID,
            max_candidates=1,
            max_total_bytes=100_000,
        )
