"""Activation cutover plan/apply tests with no broker connection."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

import pytest

from app.broker.alpaca.clerk.active_authority import select_active_clerk_runtime
from app.broker.alpaca.clerk.sqlite.activation import ActivationStore
from app.broker.alpaca.clerk.sqlite.cutover import (
    BrokerCutoverEvidence,
    CutoverRefused,
    apply_cutover,
    plan_cutover,
)
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.contract.models import BrokerAccountSnapshot
from scripts.manage_alpaca_sqlite_clerk import main as recovery_cli

ACCOUNT_ID = "PA-CUTOVER"
PLAN_MS = 1_800_000_000_000


class _Clock:
    def __init__(self, value: int) -> None:
        self.value = value

    def __call__(self) -> int:
        return self.value


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


def _setup(tmp_path: Path) -> tuple[Path, BrokerCutoverEvidence, _Clock]:
    clock = _Clock(PLAN_MS)
    repo = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID,
        artifacts_root=tmp_path,
        clock=clock,
    )
    account_dir = repo.db_path.parent
    repo.close()
    (account_dir / "order_inbox.jsonl").write_text('{"legacy":1}\n', encoding="utf-8")
    (account_dir / "order_journal.jsonl").write_text('{"legacy":1}\n', encoding="utf-8")
    evidence = BrokerCutoverEvidence(
        account_id=ACCOUNT_ID,
        observed_at_ms=PLAN_MS,
        proof_reference="fake-alpaca-account-snapshot",
        positions={"SPY": 0.0},
        open_order_ids=(),
    )
    return account_dir, evidence, clock


def _file_snapshot(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_plan_is_read_only_and_content_addresses_exact_prerequisites(tmp_path: Path) -> None:
    account_dir, evidence, clock = _setup(tmp_path)
    before = _file_snapshot(tmp_path)

    plan = plan_cutover(
        account_id=ACCOUNT_ID,
        artifacts_root=tmp_path,
        broker_evidence=evidence,
        expected_strategy_instance_ids=("spy",),
        stopped_strategy_instance_ids=("spy",),
        max_broker_evidence_age_ms=1_000,
        clock=clock,
    )

    assert _file_snapshot(tmp_path) == before
    assert plan.plan_id == plan.confirmation_token
    assert tuple(item.relative_path for item in plan.legacy_artifacts) == (
        "order_inbox.jsonl",
        "order_journal.jsonl",
    )
    assert (account_dir / "order_journal.jsonl").is_file()


def test_plan_refuses_a_live_uncheckpointed_database_without_writes(tmp_path: Path) -> None:
    clock = _Clock(PLAN_MS)
    repo = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID,
        artifacts_root=tmp_path,
        clock=clock,
    )
    repo.register_strategy_instance(strategy_instance_id="spy", symbol="SPY", config_hash="h1")
    before = _file_snapshot(tmp_path)
    evidence = BrokerCutoverEvidence(
        account_id=ACCOUNT_ID,
        observed_at_ms=PLAN_MS,
        proof_reference="fake-alpaca-account-snapshot",
        positions={},
        open_order_ids=(),
    )

    with pytest.raises(CutoverRefused, match="cleanly stopped, checkpointed"):
        plan_cutover(
            account_id=ACCOUNT_ID,
            artifacts_root=tmp_path,
            broker_evidence=evidence,
            expected_strategy_instance_ids=("spy",),
            stopped_strategy_instance_ids=("spy",),
            max_broker_evidence_age_ms=1_000,
            clock=clock,
        )

    assert _file_snapshot(tmp_path) == before
    repo.close()


def test_apply_rejects_changed_evidence_without_quarantining_or_activating(
    tmp_path: Path,
) -> None:
    account_dir, evidence, clock = _setup(tmp_path)
    plan = plan_cutover(
        account_id=ACCOUNT_ID,
        artifacts_root=tmp_path,
        broker_evidence=evidence,
        expected_strategy_instance_ids=("spy",),
        stopped_strategy_instance_ids=("spy",),
        max_broker_evidence_age_ms=1_000,
        clock=clock,
    )
    changed = BrokerCutoverEvidence(
        account_id=ACCOUNT_ID,
        observed_at_ms=PLAN_MS,
        proof_reference="changed-proof",
        positions={"SPY": 0.0},
        open_order_ids=(),
    )

    with pytest.raises(CutoverRefused, match="changed"):
        apply_cutover(
            plan=plan,
            confirmation_token=plan.confirmation_token,
            artifacts_root=tmp_path,
            broker_evidence=changed,
            expected_strategy_instance_ids=("spy",),
            stopped_strategy_instance_ids=("spy",),
            max_broker_evidence_age_ms=1_000,
            clock=clock,
        )

    assert (account_dir / "order_journal.jsonl").is_file()
    assert ActivationStore(tmp_path / "accounts" / "alpaca").latest(ACCOUNT_ID) is None


def test_apply_activates_sqlite_and_quarantines_exact_legacy_artifacts(tmp_path: Path) -> None:
    account_dir, evidence, clock = _setup(tmp_path)
    plan = plan_cutover(
        account_id=ACCOUNT_ID,
        artifacts_root=tmp_path,
        broker_evidence=evidence,
        expected_strategy_instance_ids=("spy",),
        stopped_strategy_instance_ids=("spy",),
        max_broker_evidence_age_ms=1_000,
        clock=clock,
    )

    receipt = apply_cutover(
        plan=plan,
        confirmation_token=plan.confirmation_token,
        artifacts_root=tmp_path,
        broker_evidence=evidence,
        expected_strategy_instance_ids=("spy",),
        stopped_strategy_instance_ids=("spy",),
        max_broker_evidence_age_ms=1_000,
        clock=clock,
    )

    assert not (account_dir / "order_inbox.jsonl").exists()
    assert not (account_dir / "order_journal.jsonl").exists()
    store = ActivationStore(tmp_path / "accounts" / "alpaca")
    resolved = store.resolve(
        ACCOUNT_ID,
        plan.database.authority_generation,
        plan.database.db_identity_token,
        artifacts_root=tmp_path,
    )
    assert resolved == receipt.activation
    assert (tmp_path / receipt.receipt_reference).is_file()
    quarantine_manifest = tmp_path / receipt.activation.legacy_quarantine_manifest
    quarantine_dir = quarantine_manifest.parent
    assert (quarantine_dir / "order_inbox.jsonl").is_file()
    assert (quarantine_dir / "order_journal.jsonl").is_file()


async def test_successful_fixture_cutover_restarts_with_only_sqlite_authority(
    tmp_path: Path,
) -> None:
    _account_dir, evidence, clock = _setup(tmp_path)
    plan = plan_cutover(
        account_id=ACCOUNT_ID,
        artifacts_root=tmp_path,
        broker_evidence=evidence,
        expected_strategy_instance_ids=(),
        stopped_strategy_instance_ids=(),
        max_broker_evidence_age_ms=1_000,
        clock=clock,
    )
    apply_cutover(
        plan=plan,
        confirmation_token=plan.confirmation_token,
        artifacts_root=tmp_path,
        broker_evidence=evidence,
        expected_strategy_instance_ids=(),
        stopped_strategy_instance_ids=(),
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
        artifacts_root=tmp_path,
        legacy_factory=_legacy_factory,
    )

    assert runtime.authority_kind == "sqlite"
    assert runtime.sqlite_repository is not None
    assert not legacy_constructed
    await runtime.close()


def test_apply_rejects_expired_token_and_non_finite_position(tmp_path: Path) -> None:
    account_dir, evidence, clock = _setup(tmp_path)
    plan = plan_cutover(
        account_id=ACCOUNT_ID,
        artifacts_root=tmp_path,
        broker_evidence=evidence,
        expected_strategy_instance_ids=(),
        stopped_strategy_instance_ids=(),
        max_broker_evidence_age_ms=10_000,
        confirmation_ttl_ms=10,
        clock=clock,
    )
    clock.value += 11
    with pytest.raises(CutoverRefused, match="expired"):
        apply_cutover(
            plan=plan,
            confirmation_token=plan.confirmation_token,
            artifacts_root=tmp_path,
            broker_evidence=evidence,
            expected_strategy_instance_ids=(),
            stopped_strategy_instance_ids=(),
            max_broker_evidence_age_ms=10_000,
            clock=clock,
        )
    assert (account_dir / "order_journal.jsonl").is_file()

    with pytest.raises(CutoverRefused, match="finite"):
        plan_cutover(
            account_id=ACCOUNT_ID,
            artifacts_root=tmp_path,
            broker_evidence=BrokerCutoverEvidence(
                account_id=ACCOUNT_ID,
                observed_at_ms=clock.value,
                proof_reference="bad-position",
                positions={"SPY": float("inf")},
                open_order_ids=(),
            ),
            expected_strategy_instance_ids=(),
            stopped_strategy_instance_ids=(),
            max_broker_evidence_age_ms=10_000,
            clock=clock,
        )


def test_cli_plan_and_apply_require_the_exact_confirmation_token(tmp_path: Path) -> None:
    account_id = "PA-CLI-CUTOVER"
    repo = ClerkSqliteRepository.initialize(account_id=account_id, artifacts_root=tmp_path)
    account_dir = repo.db_path.parent
    repo.close()
    (account_dir / "order_journal.jsonl").write_text("{}\n", encoding="utf-8")
    evidence_path = tmp_path / "broker-evidence.json"
    evidence_path.write_text(
        json.dumps(
            {
                "account_id": account_id,
                "observed_at_ms": time.time_ns() // 1_000_000,
                "proof_reference": "fake-cli-proof",
                "positions": {},
                "open_order_ids": [],
            }
        ),
        encoding="utf-8",
    )
    plan_path = tmp_path / "cutover-plan.json"
    common = [
        "--artifacts-root",
        str(tmp_path),
        "--account-id",
        account_id,
    ]
    evidence_args = [
        "--broker-evidence",
        str(evidence_path),
        "--max-evidence-age-ms",
        "60000",
    ]

    assert recovery_cli([*common, "cutover-plan", *evidence_args, "--output", str(plan_path)]) == 0
    token = json.loads(plan_path.read_text(encoding="utf-8"))["confirmation_token"]
    assert recovery_cli(
        [
            *common,
            "cutover-apply",
            *evidence_args,
            "--plan",
            str(plan_path),
            "--confirmation-token",
            token,
        ]
    ) == 0
    assert ActivationStore(tmp_path / "accounts" / "alpaca").latest(account_id) is not None
    assert not (account_dir / "order_journal.jsonl").exists()
