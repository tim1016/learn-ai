"""Activation cutover plan/apply tests with no broker connection."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from app.broker.alpaca.clerk.active_authority import select_active_clerk_runtime
from app.broker.alpaca.clerk.sqlite import cutover as cutover_module
from app.broker.alpaca.clerk.sqlite.activation import ActivationStore
from app.broker.alpaca.clerk.sqlite.cutover import (
    BrokerCutoverEvidence,
    CutoverRefused,
    apply_cutover,
    initialize_cutover_authority,
    plan_cutover,
)
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.contract.models import BrokerAccountSnapshot
from app.engine.live.desired_state import DesiredState, DesiredStateRecord
from tests.broker.alpaca.clerk.sqlite.cutover_test_support import (
    PLAN_MS,
)
from tests.broker.alpaca.clerk.sqlite.cutover_test_support import (
    CutoverTestClock as _Clock,
)
from tests.broker.alpaca.clerk.sqlite.cutover_test_support import (
    write_stopped_runner_bot as _write_stopped_runner_bot,
)

ACCOUNT_ID = "PA-CUTOVER"


class _StartupBroker:
    broker_id = "alpaca"

    async def get_account(self) -> BrokerAccountSnapshot:
        return BrokerAccountSnapshot(
            broker="alpaca",
            account_id=ACCOUNT_ID,
            account_mode="paper",
            account_status="ACTIVE",
            currency="USD",
            cash=1_000.0,
            equity=1_000.0,
            buying_power=2_000.0,
            portfolio_value=1_000.0,
            long_market_value=0.0,
            short_market_value=0.0,
            pattern_day_trader=False,
            trading_blocked=False,
            account_blocked=False,
            created_at_ms=1,
            observed_at_ms=PLAN_MS,
        )

    async def list_orders(self, **_kwargs: Any) -> list:
        return []

    async def list_positions(self) -> list:
        return []

    async def submit(self, *_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("startup must not submit")

    async def cancel(self, _order_id: str) -> None:
        raise AssertionError("startup must not cancel")

    async def get_order_by_client_order_id(self, _client_order_id: str) -> None:
        return None


def _setup(
    tmp_path: Path,
) -> tuple[Path, Path, Path, BrokerCutoverEvidence, _Clock]:
    clock = _Clock(PLAN_MS)
    clerk_root = tmp_path / "clerk"
    runner_root = tmp_path / "runner"
    _write_stopped_runner_bot(runner_root)
    account_dir = clerk_root / "accounts" / "alpaca" / ACCOUNT_ID
    account_dir.mkdir(parents=True)
    (account_dir / "order_inbox.jsonl").write_text('{"legacy":1}\n', encoding="utf-8")
    (account_dir / "order_journal.jsonl").write_text('{"legacy":1}\n', encoding="utf-8")
    legacy_bot_dir = clerk_root / "accounts" / ACCOUNT_ID / "bots" / "spy"
    legacy_bot_dir.mkdir(parents=True)
    (legacy_bot_dir / "decision_journal.jsonl").write_text(
        '{"legacy":1}\n', encoding="utf-8"
    )
    evidence = BrokerCutoverEvidence(
        account_id=ACCOUNT_ID,
        account_mode="paper",
        observed_at_ms=PLAN_MS,
        proof_reference="fake-alpaca-account-snapshot",
        positions={"SPY": 0.0},
        open_order_ids=(),
    )
    initialize_cutover_authority(
        account_id=ACCOUNT_ID,
        artifacts_root=clerk_root,
        runner_artifacts_root=runner_root,
        broker_evidence=evidence,
        max_broker_evidence_age_ms=1_000,
        clock=clock,
    )
    return clerk_root, account_dir, runner_root, evidence, clock


def _file_snapshot(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_initialize_creates_inactive_generation_from_fresh_proven_stopped_state(
    tmp_path: Path,
) -> None:
    clock = _Clock(PLAN_MS)
    clerk_root = tmp_path / "clerk"
    runner_root = tmp_path / "runner"
    _write_stopped_runner_bot(runner_root)
    account_dir = clerk_root / "accounts" / "alpaca" / ACCOUNT_ID
    account_dir.mkdir(parents=True)
    (account_dir / "order_journal.jsonl").write_text(
        '{"legacy":1}\n', encoding="utf-8"
    )
    evidence = BrokerCutoverEvidence(
        account_id=ACCOUNT_ID,
        account_mode="paper",
        observed_at_ms=PLAN_MS,
        proof_reference="fake-alpaca-account-snapshot",
        positions={},
        open_order_ids=(),
    )

    receipt = initialize_cutover_authority(
        account_id=ACCOUNT_ID,
        artifacts_root=clerk_root,
        runner_artifacts_root=runner_root,
        broker_evidence=evidence,
        max_broker_evidence_age_ms=1_000,
        clock=clock,
    )

    assert receipt.database.authority_generation == 1
    assert tuple(item.strategy_instance_id for item in receipt.runner_roster) == ("spy",)
    assert (clerk_root / receipt.receipt_reference).is_file()
    assert (account_dir / "clerk.db").is_file()
    assert (account_dir / "order_journal.jsonl").is_file()
    assert ActivationStore(clerk_root / "accounts" / "alpaca").latest(ACCOUNT_ID) is None


def test_initialize_refuses_running_roster_without_creating_authority(
    tmp_path: Path,
) -> None:
    clerk_root = tmp_path / "clerk"
    runner_root = tmp_path / "runner"
    _write_stopped_runner_bot(
        runner_root,
        strategy_instance_id="spy",
        desired_state=DesiredState.RUNNING,
        legacy_binding=True,
    )
    evidence = BrokerCutoverEvidence(
        account_id=ACCOUNT_ID,
        account_mode="paper",
        observed_at_ms=PLAN_MS,
        proof_reference="fake-alpaca-account-snapshot",
        positions={},
        open_order_ids=(),
    )

    with pytest.raises(CutoverRefused, match="not durably STOPPED"):
        initialize_cutover_authority(
            account_id=ACCOUNT_ID,
            artifacts_root=clerk_root,
            runner_artifacts_root=runner_root,
            broker_evidence=evidence,
            max_broker_evidence_age_ms=1_000,
            clock=_Clock(PLAN_MS),
        )

    assert not (clerk_root / "accounts" / "alpaca" / ACCOUNT_ID / "clerk.db").exists()
    assert ActivationStore(clerk_root / "accounts" / "alpaca").latest(ACCOUNT_ID) is None


def test_initialize_refuses_empty_runner_roster_without_creating_authority(
    tmp_path: Path,
) -> None:
    clerk_root = tmp_path / "clerk"
    runner_root = tmp_path / "runner"
    (runner_root / "live_state").mkdir(parents=True)
    account_dir = clerk_root / "accounts" / "alpaca" / ACCOUNT_ID
    account_dir.mkdir(parents=True)
    (account_dir / "order_journal.jsonl").write_text("{}\n", encoding="utf-8")
    evidence = BrokerCutoverEvidence(
        account_id=ACCOUNT_ID,
        account_mode="paper",
        observed_at_ms=PLAN_MS,
        proof_reference="fake-alpaca-account-snapshot",
        positions={},
        open_order_ids=(),
    )

    with pytest.raises(CutoverRefused, match="no governed Alpaca bots"):
        initialize_cutover_authority(
            account_id=ACCOUNT_ID,
            artifacts_root=clerk_root,
            runner_artifacts_root=runner_root,
            broker_evidence=evidence,
            max_broker_evidence_age_ms=1_000,
            clock=_Clock(PLAN_MS),
        )

    assert not (account_dir / "clerk.db").exists()
    assert ActivationStore(clerk_root / "accounts" / "alpaca").latest(ACCOUNT_ID) is None


def test_initialize_refuses_tampered_normalized_runner_binding(
    tmp_path: Path,
) -> None:
    clerk_root = tmp_path / "clerk"
    runner_root = tmp_path / "runner"
    instance_dir = _write_stopped_runner_bot(runner_root)
    account_dir = clerk_root / "accounts" / "alpaca" / ACCOUNT_ID
    account_dir.mkdir(parents=True)
    (account_dir / "order_journal.jsonl").write_text("{}\n", encoding="utf-8")
    instance_path = instance_dir / "strategy_instance.json"
    payload = json.loads(instance_path.read_text(encoding="utf-8"))
    payload["broker"] = "ibkr"
    instance_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CutoverRefused, match="configuration hash is invalid"):
        initialize_cutover_authority(
            account_id=ACCOUNT_ID,
            artifacts_root=clerk_root,
            runner_artifacts_root=runner_root,
            broker_evidence=BrokerCutoverEvidence(
                account_id=ACCOUNT_ID,
                account_mode="paper",
                observed_at_ms=PLAN_MS,
                proof_reference="fake-alpaca-account-snapshot",
                positions={},
                open_order_ids=(),
            ),
            max_broker_evidence_age_ms=1_000,
            clock=_Clock(PLAN_MS),
        )

    assert not (account_dir / "clerk.db").exists()


def test_initialize_ignores_a_valid_non_alpaca_binding_in_the_shared_runner_root(
    tmp_path: Path,
) -> None:
    clerk_root = tmp_path / "clerk"
    runner_root = tmp_path / "runner"
    _write_stopped_runner_bot(runner_root, strategy_instance_id="spy")
    _write_stopped_runner_bot(
        runner_root,
        strategy_instance_id="ibkr-spy",
        broker="ibkr",
    )
    account_dir = clerk_root / "accounts" / "alpaca" / ACCOUNT_ID
    account_dir.mkdir(parents=True)
    (account_dir / "order_journal.jsonl").write_text("{}\n", encoding="utf-8")

    receipt = initialize_cutover_authority(
        account_id=ACCOUNT_ID,
        artifacts_root=clerk_root,
        runner_artifacts_root=runner_root,
        broker_evidence=BrokerCutoverEvidence(
            account_id=ACCOUNT_ID,
            account_mode="paper",
            observed_at_ms=PLAN_MS,
            proof_reference="fake-alpaca-account-snapshot",
            positions={},
            open_order_ids=(),
        ),
        max_broker_evidence_age_ms=1_000,
        clock=_Clock(PLAN_MS),
    )

    assert tuple(item.strategy_instance_id for item in receipt.runner_roster) == (
        "spy",
    )


def test_initialize_refuses_broken_binding_symlink(tmp_path: Path) -> None:
    clerk_root = tmp_path / "clerk"
    runner_root = tmp_path / "runner"
    instance_dir = _write_stopped_runner_bot(runner_root)
    account_dir = clerk_root / "accounts" / "alpaca" / ACCOUNT_ID
    account_dir.mkdir(parents=True)
    (account_dir / "order_journal.jsonl").write_text("{}\n", encoding="utf-8")
    instance_path = instance_dir / "strategy_instance.json"
    instance_path.unlink()
    instance_path.symlink_to(instance_dir / "missing-strategy-instance.json")

    with pytest.raises(CutoverRefused, match="must not be a symbolic link"):
        initialize_cutover_authority(
            account_id=ACCOUNT_ID,
            artifacts_root=clerk_root,
            runner_artifacts_root=runner_root,
            broker_evidence=BrokerCutoverEvidence(
                account_id=ACCOUNT_ID,
                account_mode="paper",
                observed_at_ms=PLAN_MS,
                proof_reference="fake-alpaca-account-snapshot",
                positions={},
                open_order_ids=(),
            ),
            max_broker_evidence_age_ms=1_000,
            clock=_Clock(PLAN_MS),
        )

    assert not (account_dir / "clerk.db").exists()


def test_initialize_refuses_wrong_clerk_root_without_creating_authority(
    tmp_path: Path,
) -> None:
    clerk_root = tmp_path / "wrong-clerk-root"
    runner_root = tmp_path / "runner"
    _write_stopped_runner_bot(runner_root)

    with pytest.raises(CutoverRefused, match="no legacy authority artifacts"):
        initialize_cutover_authority(
            account_id=ACCOUNT_ID,
            artifacts_root=clerk_root,
            runner_artifacts_root=runner_root,
            broker_evidence=BrokerCutoverEvidence(
                account_id=ACCOUNT_ID,
                account_mode="paper",
                observed_at_ms=PLAN_MS,
                proof_reference="fake-alpaca-account-snapshot",
                positions={},
                open_order_ids=(),
            ),
            max_broker_evidence_age_ms=1_000,
            clock=_Clock(PLAN_MS),
        )

    assert not (
        clerk_root / "accounts" / "alpaca" / ACCOUNT_ID / "clerk.db"
    ).exists()


def test_initialize_refuses_non_paper_broker_evidence(tmp_path: Path) -> None:
    clerk_root = tmp_path / "clerk"
    runner_root = tmp_path / "runner"
    _write_stopped_runner_bot(runner_root)

    with pytest.raises(CutoverRefused, match="paper account"):
        initialize_cutover_authority(
            account_id=ACCOUNT_ID,
            artifacts_root=clerk_root,
            runner_artifacts_root=runner_root,
            broker_evidence=BrokerCutoverEvidence(
                account_id=ACCOUNT_ID,
                account_mode="live",
                observed_at_ms=PLAN_MS,
                proof_reference="wrong-mode-account",
                positions={},
                open_order_ids=(),
            ),
            max_broker_evidence_age_ms=1_000,
            clock=_Clock(PLAN_MS),
        )

    assert not (clerk_root / "accounts" / "alpaca" / ACCOUNT_ID / "clerk.db").exists()


def test_initialize_resumes_verified_inactive_generation_after_receipt_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clerk_root = tmp_path / "clerk"
    runner_root = tmp_path / "runner"
    _write_stopped_runner_bot(runner_root)
    account_dir = clerk_root / "accounts" / "alpaca" / ACCOUNT_ID
    account_dir.mkdir(parents=True)
    (account_dir / "order_journal.jsonl").write_text("{}\n", encoding="utf-8")
    evidence = BrokerCutoverEvidence(
        account_id=ACCOUNT_ID,
        account_mode="paper",
        observed_at_ms=PLAN_MS,
        proof_reference="fake-alpaca-account-snapshot",
        positions={},
        open_order_ids=(),
    )
    real_atomic_write_json = cutover_module.atomic_write_json

    def _fail_receipt(path: Path, payload: dict[str, Any]) -> str:
        if path.name.startswith("initialization-g1-"):
            raise OSError("injected receipt publication failure")
        return real_atomic_write_json(path, payload)

    monkeypatch.setattr(cutover_module, "atomic_write_json", _fail_receipt)
    with pytest.raises(OSError, match="injected receipt"):
        initialize_cutover_authority(
            account_id=ACCOUNT_ID,
            artifacts_root=clerk_root,
            runner_artifacts_root=runner_root,
            broker_evidence=evidence,
            max_broker_evidence_age_ms=1_000,
            clock=_Clock(PLAN_MS),
        )

    assert (account_dir / "clerk.db").is_file()
    assert ActivationStore(clerk_root / "accounts" / "alpaca").latest(ACCOUNT_ID) is None

    with pytest.raises(CutoverRefused, match="differs from its durable intent"):
        initialize_cutover_authority(
            account_id=ACCOUNT_ID,
            artifacts_root=clerk_root,
            runner_artifacts_root=runner_root,
            broker_evidence=BrokerCutoverEvidence(
                account_id=ACCOUNT_ID,
                account_mode="paper",
                observed_at_ms=PLAN_MS,
                proof_reference="changed-after-initialization-failure",
                positions={},
                open_order_ids=(),
            ),
            max_broker_evidence_age_ms=1_000,
            clock=_Clock(PLAN_MS),
        )

    monkeypatch.setattr(
        cutover_module,
        "atomic_write_json",
        real_atomic_write_json,
    )
    receipt = initialize_cutover_authority(
        account_id=ACCOUNT_ID,
        artifacts_root=clerk_root,
        runner_artifacts_root=runner_root,
        broker_evidence=evidence,
        max_broker_evidence_age_ms=1_000,
        clock=_Clock(PLAN_MS),
    )
    repeated = initialize_cutover_authority(
        account_id=ACCOUNT_ID,
        artifacts_root=clerk_root,
        runner_artifacts_root=runner_root,
        broker_evidence=evidence,
        max_broker_evidence_age_ms=1_000,
        clock=_Clock(PLAN_MS),
    )

    assert repeated == receipt
    assert receipt.receipt_reference.endswith(
        f"initialization-g1-{receipt.database.db_identity_token}.json"
    )


def test_initialize_refuses_unrelated_empty_generation_without_cutover_intent(
    tmp_path: Path,
) -> None:
    clock = _Clock(PLAN_MS)
    clerk_root = tmp_path / "clerk"
    runner_root = tmp_path / "runner"
    _write_stopped_runner_bot(runner_root)
    repository = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID,
        artifacts_root=clerk_root,
        clock=clock,
    )
    account_dir = repository.db_path.parent
    repository.close()
    (account_dir / "order_journal.jsonl").write_text("{}\n", encoding="utf-8")

    with pytest.raises(CutoverRefused, match="no durable cutover initialization intent"):
        initialize_cutover_authority(
            account_id=ACCOUNT_ID,
            artifacts_root=clerk_root,
            runner_artifacts_root=runner_root,
            broker_evidence=BrokerCutoverEvidence(
                account_id=ACCOUNT_ID,
                account_mode="paper",
                observed_at_ms=PLAN_MS,
                proof_reference="fake-alpaca-account-snapshot",
                positions={},
                open_order_ids=(),
            ),
            max_broker_evidence_age_ms=1_000,
            clock=clock,
        )

    with pytest.raises(CutoverRefused, match="initialization intent"):
        plan_cutover(
            account_id=ACCOUNT_ID,
            artifacts_root=clerk_root,
            runner_artifacts_root=runner_root,
            broker_evidence=BrokerCutoverEvidence(
                account_id=ACCOUNT_ID,
                account_mode="paper",
                observed_at_ms=PLAN_MS,
                proof_reference="fake-alpaca-account-snapshot",
                positions={},
                open_order_ids=(),
            ),
            max_broker_evidence_age_ms=1_000,
            clock=clock,
        )

    assert ActivationStore(clerk_root / "accounts" / "alpaca").latest(ACCOUNT_ID) is None


def test_initialize_refuses_a_prepared_generation_that_was_later_used(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock(PLAN_MS)
    clerk_root = tmp_path / "clerk"
    runner_root = tmp_path / "runner"
    _write_stopped_runner_bot(runner_root)
    account_dir = clerk_root / "accounts" / "alpaca" / ACCOUNT_ID
    account_dir.mkdir(parents=True)
    (account_dir / "order_journal.jsonl").write_text("{}\n", encoding="utf-8")
    evidence = BrokerCutoverEvidence(
        account_id=ACCOUNT_ID,
        account_mode="paper",
        observed_at_ms=PLAN_MS,
        proof_reference="fake-alpaca-account-snapshot",
        positions={},
        open_order_ids=(),
    )
    real_atomic_write_json = cutover_module.atomic_write_json

    def _fail_receipt(path: Path, payload: dict[str, Any]) -> str:
        if path.name.startswith("initialization-g1-"):
            raise OSError("injected receipt publication failure")
        return real_atomic_write_json(path, payload)

    monkeypatch.setattr(cutover_module, "atomic_write_json", _fail_receipt)
    with pytest.raises(OSError, match="injected receipt"):
        initialize_cutover_authority(
            account_id=ACCOUNT_ID,
            artifacts_root=clerk_root,
            runner_artifacts_root=runner_root,
            broker_evidence=evidence,
            max_broker_evidence_age_ms=1_000,
            clock=clock,
        )
    monkeypatch.setattr(cutover_module, "atomic_write_json", real_atomic_write_json)
    repository = ClerkSqliteRepository.open(
        account_id=ACCOUNT_ID,
        artifacts_root=clerk_root,
        clock=clock,
    )
    repository.register_strategy_instance(
        strategy_instance_id="spy",
        symbol="SPY",
        config_hash="configured-before-cutover",
    )
    repository.close()

    with pytest.raises(CutoverRefused, match="unused SQLite authority"):
        initialize_cutover_authority(
            account_id=ACCOUNT_ID,
            artifacts_root=clerk_root,
            runner_artifacts_root=runner_root,
            broker_evidence=evidence,
            max_broker_evidence_age_ms=1_000,
            clock=clock,
        )


def test_plan_derives_roster_and_refuses_a_configured_running_bot(tmp_path: Path) -> None:
    clerk_root, account_dir, runner_root, evidence, clock = _setup(tmp_path)
    _write_stopped_runner_bot(
        runner_root,
        strategy_instance_id="qqq",
        desired_state=DesiredState.RUNNING,
    )
    before = _file_snapshot(tmp_path)

    with pytest.raises(CutoverRefused, match=r"qqq.*not durably STOPPED"):
        plan_cutover(
            account_id=ACCOUNT_ID,
            artifacts_root=clerk_root,
            runner_artifacts_root=runner_root,
            broker_evidence=evidence,
            max_broker_evidence_age_ms=1_000,
            clock=clock,
        )

    assert _file_snapshot(tmp_path) == before
    assert (account_dir / "order_journal.jsonl").is_file()


def test_plan_is_read_only_and_content_addresses_exact_prerequisites(tmp_path: Path) -> None:
    clerk_root, account_dir, runner_root, evidence, clock = _setup(tmp_path)
    before = _file_snapshot(tmp_path)

    plan = plan_cutover(
        account_id=ACCOUNT_ID,
        artifacts_root=clerk_root,
        runner_artifacts_root=runner_root,
        broker_evidence=evidence,
        max_broker_evidence_age_ms=1_000,
        clock=clock,
    )

    assert _file_snapshot(tmp_path) == before
    assert plan.plan_id == plan.confirmation_token
    assert tuple(item.relative_path for item in plan.legacy_artifacts) == (
        f"accounts/{ACCOUNT_ID}/bots",
        f"accounts/alpaca/{ACCOUNT_ID}/order_inbox.jsonl",
        f"accounts/alpaca/{ACCOUNT_ID}/order_journal.jsonl",
    )
    assert tuple(item.strategy_instance_id for item in plan.runner_roster) == ("spy",)
    assert (account_dir / "order_journal.jsonl").is_file()


def test_apply_rejects_changed_durable_roster_before_activation(tmp_path: Path) -> None:
    clerk_root, account_dir, runner_root, evidence, clock = _setup(tmp_path)
    plan = plan_cutover(
        account_id=ACCOUNT_ID,
        artifacts_root=clerk_root,
        runner_artifacts_root=runner_root,
        broker_evidence=evidence,
        max_broker_evidence_age_ms=1_000,
        clock=clock,
    )
    desired_path = runner_root / "live_state" / "spy" / "desired_state.json"
    desired_path.write_text(
        DesiredStateRecord(
            desired_state=DesiredState.STOPPED,
            updated_at_ms=PLAN_MS - 1,
            updated_by="operator",
            reason="changed after planning",
        ).model_dump_json(),
        encoding="utf-8",
    )

    with pytest.raises(CutoverRefused, match="roster changed"):
        apply_cutover(
            plan=plan,
            confirmation_token=plan.confirmation_token,
            artifacts_root=clerk_root,
            runner_artifacts_root=runner_root,
            broker_evidence=evidence,
            max_broker_evidence_age_ms=1_000,
            clock=clock,
        )

    assert (account_dir / "order_journal.jsonl").is_file()
    assert ActivationStore(clerk_root / "accounts" / "alpaca").latest(ACCOUNT_ID) is None


def test_apply_rejects_changed_initialization_receipt_before_activation(
    tmp_path: Path,
) -> None:
    clerk_root, account_dir, runner_root, evidence, clock = _setup(tmp_path)
    plan = plan_cutover(
        account_id=ACCOUNT_ID,
        artifacts_root=clerk_root,
        runner_artifacts_root=runner_root,
        broker_evidence=evidence,
        max_broker_evidence_age_ms=1_000,
        clock=clock,
    )
    receipt_path = (
        account_dir
        / "cutover-evidence"
        / f"initialization-g1-{plan.database.db_identity_token}.json"
    )
    receipt_payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt_path.write_text(json.dumps(receipt_payload, indent=2), encoding="utf-8")

    with pytest.raises(CutoverRefused, match="evidence changed after cutover planning"):
        apply_cutover(
            plan=plan,
            confirmation_token=plan.confirmation_token,
            artifacts_root=clerk_root,
            runner_artifacts_root=runner_root,
            broker_evidence=evidence,
            max_broker_evidence_age_ms=1_000,
            clock=clock,
        )

    assert (account_dir / "order_journal.jsonl").is_file()
    assert ActivationStore(clerk_root / "accounts" / "alpaca").latest(ACCOUNT_ID) is None


def test_plan_refuses_a_live_uncheckpointed_database_without_writes(tmp_path: Path) -> None:
    clock = _Clock(PLAN_MS)
    clerk_root = tmp_path / "clerk"
    runner_root = tmp_path / "runner"
    _write_stopped_runner_bot(runner_root)
    repo = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID,
        artifacts_root=clerk_root,
        clock=clock,
    )
    repo.register_strategy_instance(strategy_instance_id="spy", symbol="SPY", config_hash="h1")
    before = _file_snapshot(tmp_path)
    evidence = BrokerCutoverEvidence(
        account_id=ACCOUNT_ID,
        account_mode="paper",
        observed_at_ms=PLAN_MS,
        proof_reference="fake-alpaca-account-snapshot",
        positions={},
        open_order_ids=(),
    )

    with pytest.raises(CutoverRefused, match="cleanly stopped, checkpointed"):
        plan_cutover(
            account_id=ACCOUNT_ID,
            artifacts_root=clerk_root,
            runner_artifacts_root=runner_root,
            broker_evidence=evidence,
            max_broker_evidence_age_ms=1_000,
            clock=clock,
        )

    assert _file_snapshot(tmp_path) == before
    repo.close()


def test_apply_rejects_changed_evidence_without_quarantining_or_activating(
    tmp_path: Path,
) -> None:
    clerk_root, account_dir, runner_root, evidence, clock = _setup(tmp_path)
    plan = plan_cutover(
        account_id=ACCOUNT_ID,
        artifacts_root=clerk_root,
        runner_artifacts_root=runner_root,
        broker_evidence=evidence,
        max_broker_evidence_age_ms=1_000,
        clock=clock,
    )
    changed = BrokerCutoverEvidence(
        account_id=ACCOUNT_ID,
        account_mode="paper",
        observed_at_ms=PLAN_MS,
        proof_reference="changed-proof",
        positions={"SPY": 0.0},
        open_order_ids=(),
    )

    with pytest.raises(CutoverRefused, match="changed"):
        apply_cutover(
            plan=plan,
            confirmation_token=plan.confirmation_token,
            artifacts_root=clerk_root,
            runner_artifacts_root=runner_root,
            broker_evidence=changed,
            max_broker_evidence_age_ms=1_000,
            clock=clock,
        )

    assert (account_dir / "order_journal.jsonl").is_file()
    assert ActivationStore(clerk_root / "accounts" / "alpaca").latest(ACCOUNT_ID) is None


def test_apply_activates_sqlite_and_quarantines_exact_legacy_artifacts(tmp_path: Path) -> None:
    clerk_root, account_dir, runner_root, evidence, clock = _setup(tmp_path)
    plan = plan_cutover(
        account_id=ACCOUNT_ID,
        artifacts_root=clerk_root,
        runner_artifacts_root=runner_root,
        broker_evidence=evidence,
        max_broker_evidence_age_ms=1_000,
        clock=clock,
    )

    receipt = apply_cutover(
        plan=plan,
        confirmation_token=plan.confirmation_token,
        artifacts_root=clerk_root,
        runner_artifacts_root=runner_root,
        broker_evidence=evidence,
        max_broker_evidence_age_ms=1_000,
        clock=clock,
    )

    assert not (account_dir / "order_inbox.jsonl").exists()
    assert not (account_dir / "order_journal.jsonl").exists()
    store = ActivationStore(clerk_root / "accounts" / "alpaca")
    resolved = store.resolve(
        ACCOUNT_ID,
        plan.database.authority_generation,
        plan.database.db_identity_token,
        artifacts_root=clerk_root,
    )
    assert resolved == receipt.activation
    assert (clerk_root / receipt.receipt_reference).is_file()
    quarantine_manifest = clerk_root / receipt.activation.legacy_quarantine_manifest
    quarantine_dir = quarantine_manifest.parent
    assert (
        quarantine_dir / "accounts" / "alpaca" / ACCOUNT_ID / "order_inbox.jsonl"
    ).is_file()
    assert (
        quarantine_dir / "accounts" / "alpaca" / ACCOUNT_ID / "order_journal.jsonl"
    ).is_file()
    assert (
        quarantine_dir
        / "accounts"
        / ACCOUNT_ID
        / "bots"
        / "spy"
        / "decision_journal.jsonl"
    ).is_file()


async def test_successful_fixture_cutover_restarts_with_only_sqlite_authority(
    tmp_path: Path,
) -> None:
    clerk_root, _account_dir, runner_root, evidence, clock = _setup(tmp_path)
    plan = plan_cutover(
        account_id=ACCOUNT_ID,
        artifacts_root=clerk_root,
        runner_artifacts_root=runner_root,
        broker_evidence=evidence,
        max_broker_evidence_age_ms=1_000,
        clock=clock,
    )
    apply_cutover(
        plan=plan,
        confirmation_token=plan.confirmation_token,
        artifacts_root=clerk_root,
        runner_artifacts_root=runner_root,
        broker_evidence=evidence,
        max_broker_evidence_age_ms=1_000,
        clock=clock,
    )
    legacy_constructed = False

    def _legacy_factory() -> Any:
        nonlocal legacy_constructed
        legacy_constructed = True
        raise AssertionError("activated restart must not construct the legacy Clerk")

    broker = _StartupBroker()
    runtime = await select_active_clerk_runtime(
        read=broker,
        trade=broker,
        artifacts_root=clerk_root,
        legacy_factory=_legacy_factory,
    )

    assert runtime.authority_kind == "sqlite"
    assert runtime.sqlite_repository is not None
    assert not legacy_constructed
    await runtime.close()


def test_apply_rejects_expired_token_and_non_finite_position(tmp_path: Path) -> None:
    clerk_root, account_dir, runner_root, evidence, clock = _setup(tmp_path)
    plan = plan_cutover(
        account_id=ACCOUNT_ID,
        artifacts_root=clerk_root,
        runner_artifacts_root=runner_root,
        broker_evidence=evidence,
        max_broker_evidence_age_ms=10_000,
        confirmation_ttl_ms=10,
        clock=clock,
    )
    clock.value += 11
    with pytest.raises(CutoverRefused, match="expired"):
        apply_cutover(
            plan=plan,
            confirmation_token=plan.confirmation_token,
            artifacts_root=clerk_root,
            runner_artifacts_root=runner_root,
            broker_evidence=evidence,
            max_broker_evidence_age_ms=10_000,
            clock=clock,
        )
    assert (account_dir / "order_journal.jsonl").is_file()

    with pytest.raises(CutoverRefused, match="finite"):
        plan_cutover(
            account_id=ACCOUNT_ID,
            artifacts_root=clerk_root,
            runner_artifacts_root=runner_root,
            broker_evidence=BrokerCutoverEvidence(
                account_id=ACCOUNT_ID,
                account_mode="paper",
                observed_at_ms=clock.value,
                proof_reference="bad-position",
                positions={"SPY": float("inf")},
                open_order_ids=(),
            ),
            max_broker_evidence_age_ms=10_000,
            clock=clock,
        )
