"""Authority-derived Alpaca lifecycle projection contracts."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest

from app.broker.alpaca.clerk import set_alpaca_clerk
from app.broker.alpaca.clerk.account_authority import synthetic_account_id_for_strategy
from app.broker.alpaca.clerk.active_authority import get_clerk_runtime
from app.broker.alpaca.clerk.sqlite.commands import submit_start_run
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.engine.live.bot_lifecycle_state import (
    BotDutyOutcome,
    BotLifecyclePhase,
    BotLifecycleStateRepo,
    stable_bot_lifecycle_state_path,
)
from app.services.bot_lifecycle_projection import (
    AlpacaLifecycleAuthoritySnapshot,
    AlpacaLifecycleProjector,
    ProjectionStatus,
    SqliteAlpacaLifecycleAuthority,
)
from app.services.bot_runner import BotTaskRegistry
from app.utils.timestamps import now_ms_utc
from tests._helpers.bot_runner.doubles import _FakeFeed, _SqliteRuntimeBroker
from tests._helpers.bot_runner.market import patch_fresh_live_market_liveness
from tests._helpers.canary_admission import admit_canary_pairing


@pytest.fixture(autouse=True)
def _fresh_live_market_liveness(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_fresh_live_market_liveness(monkeypatch)


def _registered_repo(tmp_path: Path) -> ClerkSqliteRepository:
    repo = ClerkSqliteRepository.initialize(
        account_id="PA-TEST",
        artifacts_root=tmp_path / "clerk",
    )
    repo.register_strategy_instance(
        strategy_instance_id="paper-projection",
        symbol="SPY",
        config_hash="config-1",
    )
    return repo


def test_projection_rejects_terminal_claim_while_sqlite_run_is_active(
    tmp_path: Path,
) -> None:
    repo = _registered_repo(tmp_path)
    submit_start_run(
        repo,
        account_id="PA-TEST",
        strategy_instance_id="paper-projection",
        lifecycle_run_id="run-active",
    )
    lifecycle_repo = BotLifecycleStateRepo(
        stable_bot_lifecycle_state_path(tmp_path, "paper-projection")
    )
    lifecycle_repo.set_roster(False, now_ms=10, updated_by="roster-authority")
    projector = AlpacaLifecycleProjector(
        authority=SqliteAlpacaLifecycleAuthority(repo),
        lifecycle_repo_for=lambda _strategy_instance_id: lifecycle_repo,
        require_alpaca_identity=lambda _strategy_instance_id, _sqlite_claim: None,
    )

    result = projector.project_terminal(
        strategy_instance_id="paper-projection",
        now_ms=20,
        updated_by="test",
        reason="process_crashed",
        outcome=BotDutyOutcome(
            kind="CRASHED",
            reason_code="PROCESS_CRASHED",
            recorded_at_ms=20,
            run_id="run-active",
        ),
    )

    assert result.status is ProjectionStatus.AUTHORITY_EXPECTATION_SUPERSEDED
    assert result.terminal_outcome_attached is False
    record = lifecycle_repo.read()
    assert record is not None
    assert record.phase is BotLifecyclePhase.ON_DUTY
    assert record.active_run_id == "run-active"
    assert record.on_roster is False
    assert record.duty_outcome is None
    repo.close()


def test_projection_rejects_terminal_claim_for_run_sqlite_never_committed(
    tmp_path: Path,
) -> None:
    repo = _registered_repo(tmp_path)
    lifecycle_repo = BotLifecycleStateRepo(
        stable_bot_lifecycle_state_path(tmp_path, "paper-projection")
    )
    projector = AlpacaLifecycleProjector(
        authority=SqliteAlpacaLifecycleAuthority(repo),
        lifecycle_repo_for=lambda _strategy_instance_id: lifecycle_repo,
        require_alpaca_identity=lambda _strategy_instance_id, _sqlite_claim: None,
    )

    result = projector.project_terminal(
        strategy_instance_id="paper-projection",
        now_ms=20,
        updated_by="test",
        reason="process_crashed",
        outcome=BotDutyOutcome(
            kind="CRASHED",
            reason_code="PROCESS_CRASHED",
            recorded_at_ms=20,
            run_id="run-missing",
        ),
    )

    assert result.status is ProjectionStatus.AUTHORITY_EXPECTATION_SUPERSEDED
    assert result.terminal_outcome_attached is False
    record = lifecycle_repo.read()
    assert record is not None
    assert record.phase is BotLifecyclePhase.OFF_DUTY
    assert record.duty_outcome is None
    repo.close()


def test_projection_reports_file_cas_refusal_without_overwriting_newer_state(
    tmp_path: Path,
) -> None:
    lifecycle_repo = BotLifecycleStateRepo(
        stable_bot_lifecycle_state_path(tmp_path, "paper-projection")
    )
    lifecycle_repo.set_roster(True, now_ms=10, updated_by="seed")

    class _StaticAuthority:
        def snapshot(
            self,
            strategy_instance_id: str,
            expected_run_id: str | None,
        ) -> AlpacaLifecycleAuthoritySnapshot:
            del strategy_instance_id, expected_run_id
            return AlpacaLifecycleAuthoritySnapshot(
                strategy_instance_exists=True,
                active_run_id=None,
                retired_at_ms=None,
                expected_run_state=None,
                control_revision=1,
            )

    class _RacingRepo:
        def read(self):
            return lifecycle_repo.read()

        def update(self, **kwargs):
            lifecycle_repo.set_roster(False, now_ms=11, updated_by="newer-writer")
            return lifecycle_repo.update(**kwargs)

    projector = AlpacaLifecycleProjector(
        authority=_StaticAuthority(),
        lifecycle_repo_for=lambda _strategy_instance_id: _RacingRepo(),  # type: ignore[arg-type]
        require_alpaca_identity=lambda _strategy_instance_id, _sqlite_claim: None,
    )

    result = projector.refresh(
        strategy_instance_id="paper-projection",
        now_ms=12,
        updated_by="test",
        reason="refresh",
    )

    assert result.status is ProjectionStatus.FILE_CAS_REFUSED
    assert result.refusal_reason == "VERSION_MISMATCH"
    assert result.record is not None
    assert result.record.version == 2
    assert result.record.on_roster is False


def test_projection_retries_when_sqlite_revision_changes_during_file_write(
    tmp_path: Path,
) -> None:
    lifecycle_repo = BotLifecycleStateRepo(
        stable_bot_lifecycle_state_path(tmp_path, "paper-projection")
    )
    snapshots = iter(
        (
            AlpacaLifecycleAuthoritySnapshot(
                strategy_instance_exists=True,
                active_run_id="run-old",
                retired_at_ms=None,
                expected_run_state="MISSING",
                control_revision=10,
            ),
            AlpacaLifecycleAuthoritySnapshot(
                strategy_instance_exists=True,
                active_run_id="run-current",
                retired_at_ms=None,
                expected_run_state="ACTIVE",
                control_revision=11,
            ),
            AlpacaLifecycleAuthoritySnapshot(
                strategy_instance_exists=True,
                active_run_id="run-current",
                retired_at_ms=None,
                expected_run_state="ACTIVE",
                control_revision=11,
            ),
            AlpacaLifecycleAuthoritySnapshot(
                strategy_instance_exists=True,
                active_run_id="run-current",
                retired_at_ms=None,
                expected_run_state="ACTIVE",
                control_revision=11,
            ),
        )
    )

    class _ChangingAuthority:
        def snapshot(
            self,
            strategy_instance_id: str,
            expected_run_id: str | None,
        ) -> AlpacaLifecycleAuthoritySnapshot:
            del strategy_instance_id, expected_run_id
            return next(snapshots)

    projector = AlpacaLifecycleProjector(
        authority=_ChangingAuthority(),
        lifecycle_repo_for=lambda _strategy_instance_id: lifecycle_repo,
        require_alpaca_identity=lambda _strategy_instance_id, _sqlite_claim: None,
    )

    result = projector.project_active(
        strategy_instance_id="paper-projection",
        run_id="run-current",
        now_ms=20,
        updated_by="test",
        reason="activation",
    )

    assert result.status is ProjectionStatus.RECORDED
    record = lifecycle_repo.read()
    assert record is not None
    assert record.active_run_id == "run-current"


@pytest.mark.parametrize("mode", ["trade", "dry_run", "log_only"])
async def test_every_alpaca_mode_commits_sqlite_duty_before_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: Literal["trade", "dry_run", "log_only"],
) -> None:
    repo = ClerkSqliteRepository.initialize(
        account_id="PA-TEST",
        artifacts_root=tmp_path / "clerk",
    )
    broker = _SqliteRuntimeBroker()
    clerk = SqliteAlpacaClerkFacade(repo=repo, read=broker, trade=broker)
    set_alpaca_clerk(clerk)
    registry = BotTaskRegistry(
        tmp_path / "artifacts",
        feed_resolver=lambda: _FakeFeed([], mode="hold"),
        boot_recovery_required=False,
        supported_broker_ids=frozenset({"alpaca"}),
        start_custody_guard=clerk.start_admission_snapshot,
        now_ms=now_ms_utc,
    )
    admit_canary_pairing(monkeypatch, "deployment_validation", "PA-TEST")
    try:
        deployed = await registry.deploy(
            broker="alpaca",
            strategy_instance_id=f"paper-{mode}",
            symbol="SPY",
            mode=mode,
        )

        authority_repo = repo
        if mode == "dry_run":
            runtime = get_clerk_runtime(synthetic_account_id_for_strategy(f"paper-{mode}"))
            assert runtime is not None and runtime.sqlite_repository is not None
            authority_repo = runtime.sqlite_repository
        active = authority_repo.active_run(f"paper-{mode}")
        assert active is not None
        assert active.lifecycle_run_id == deployed.active_run_id

        await registry.stop("alpaca", f"paper-{mode}")
        if mode == "dry_run":
            assert get_clerk_runtime(synthetic_account_id_for_strategy(f"paper-{mode}")) is None
            reopened = ClerkSqliteRepository.open(
                account_id=synthetic_account_id_for_strategy(f"paper-{mode}"),
                artifacts_root=tmp_path / "artifacts",
            )
            try:
                assert reopened.active_run(f"paper-{mode}") is None
            finally:
                reopened.close()
        else:
            assert authority_repo.active_run(f"paper-{mode}") is None
    finally:
        await registry.stop_all()
        set_alpaca_clerk(None)
        repo.close()
