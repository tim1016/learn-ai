"""Container-restart recovery for the surviving SQLite custody authority.

Acceptance criteria pinned here:
- AC1: after a simulated hard stop with a bot on duty, the boot sweep records
  durable interrupted evidence; the bot is not auto-restarted and never
  renders healthy-on-duty.
- starts remain refused until recovery has completed successfully;
- SQLite recovery runs before runner file projections are repaired;
- interrupted run evidence is made honestly OFF_DUTY without auto-restart;
- unsupported historical IBKR bindings remain outside Alpaca recovery.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.broker.alpaca.clerk import set_alpaca_clerk
from app.broker.alpaca.clerk.sqlite.commands import submit_start_run
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.engine.live.bot_lifecycle_state import (
    BotLifecyclePhase,
    BotLifecycleStateRepo,
    stable_bot_lifecycle_state_path,
)
from app.engine.live.desired_state import (
    DesiredState,
    DesiredStateRepo,
    stable_desired_state_path,
)
from app.services.bot_binding_repository import BrokerBotBinding, alpaca_v1_action_plan
from app.services.bot_boot_recovery import (
    BootAuthorityPreparationError,
    BotBootRecovery,
    BotRecoveryCandidate,
)
from app.services.bot_lifecycle_projection import (
    ActiveSqliteAlpacaLifecycleAuthority,
    AlpacaLifecycleAuthoritySnapshot,
    AlpacaLifecycleProjector,
)
from app.services.bot_runner import (
    BootRecoveryIncompleteError,
    BotTaskRegistry,
)
from tests.services.test_bot_runner import (
    _custody_proof,
    _CustodyClerk,
    _FakeFeed,
    _flat_start_guard,
    _lifecycle_json,
    _SqliteRuntimeBroker,
)

_SID = "alpaca-drill-bot"
_T0 = 1_700_000_000_000


@pytest.fixture(autouse=True)
def _active_runtime() -> None:
    set_alpaca_clerk(_CustodyClerk(_custody_proof(exposure={})))
    yield
    set_alpaca_clerk(None)


def _artifacts_root(tmp_path: Path) -> Path:
    return tmp_path / "artifacts"


def _registry(tmp_path: Path, feed: _FakeFeed | None) -> BotTaskRegistry:
    return BotTaskRegistry(
        _artifacts_root(tmp_path),
        feed_resolver=lambda: feed,
        start_custody_guard=_flat_start_guard,
    )


async def _unexpected_authority_stop(
    strategy_instance_id: str,
    run_id: str,
) -> None:
    raise AssertionError(
        f"unexpected authority stop for {strategy_instance_id}:{run_id}"
    )


# ── AC4: fail-closed start gate ────────────────────────────────────────


async def test_boot_without_sqlite_authority_uses_empty_candidates(
    tmp_path: Path,
) -> None:
    set_alpaca_clerk(None)
    registry = _registry(tmp_path, _FakeFeed([], mode="hold"))

    report = await registry.run_boot_recovery()

    assert report.interrupted_instances == ()


async def test_starts_refused_until_boot_sweep_completes(tmp_path: Path) -> None:
    registry = _registry(tmp_path, _FakeFeed([], mode="hold"))

    with pytest.raises(BootRecoveryIncompleteError):
        await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")

    await registry.run_boot_recovery()
    view = await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")
    assert view.running is True
    await registry.stop("alpaca", _SID)


async def test_failed_authority_preparation_keeps_boot_gate_closed(tmp_path: Path) -> None:
    registry = _registry(tmp_path, _FakeFeed([], mode="hold"))

    async def fail_recovery() -> None:
        raise RuntimeError("authority unavailable")

    with pytest.raises(BootAuthorityPreparationError, match="recover"):
        await registry.run_boot_recovery(recover=fail_recovery)

    with pytest.raises(BootRecoveryIncompleteError):
        await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")


async def test_boot_recovers_sqlite_before_reading_file_projection(tmp_path: Path) -> None:
    artifacts_root = _artifacts_root(tmp_path)
    lifecycle_repo = BotLifecycleStateRepo(
        stable_bot_lifecycle_state_path(artifacts_root, _SID)
    )
    lifecycle_repo.set_phase(
        BotLifecyclePhase.ON_DUTY,
        now_ms=_T0,
        updated_by="pre-crash",
        active_run_id="run-old",
    )
    desired_repo = DesiredStateRepo(
        stable_desired_state_path(artifacts_root, _SID),
        trusted_root=artifacts_root / "live_state",
    )
    events: list[str] = []

    class _RecoveredAuthority:
        def snapshot(
            self,
            strategy_instance_id: str,
            expected_run_id: str | None,
        ) -> AlpacaLifecycleAuthoritySnapshot:
            del strategy_instance_id, expected_run_id
            events.append("authority_projection")
            return AlpacaLifecycleAuthoritySnapshot(
                strategy_instance_exists=True,
                active_run_id=None,
                retired_at_ms=None,
                expected_run_state="STOPPED",
                control_revision=1,
            )

    async def recover() -> None:
        events.append("recover")

    async def reconcile() -> None:
        events.append("reconcile")

    projector = AlpacaLifecycleProjector(
        authority=_RecoveredAuthority(),
        lifecycle_repo_for=lambda _strategy_instance_id: lifecycle_repo,
        require_alpaca_identity=lambda _strategy_instance_id, _sqlite_claim: None,
    )
    await BotBootRecovery(
        artifacts_root,
        lifecycle_repo_for=lambda _strategy_instance_id: lifecycle_repo,
        lifecycle_projector=projector,
        desired_repo_for=lambda _strategy_instance_id: desired_repo,
        recovery_candidates=lambda: (
            BotRecoveryCandidate(_SID, "run-old", sqlite_active=False),
        ),
        stop_authority_run=_unexpected_authority_stop,
        manages_instance=lambda _strategy_instance_id: True,
        is_running=lambda _strategy_instance_id: False,
        now_ms=lambda: _T0 + 1,
    ).run(recover=recover, reconcile=reconcile)

    assert events == [
        "recover",
        "reconcile",
        "authority_projection",
        "authority_projection",
    ]


async def test_boot_reconstructs_missing_projection_from_binding_candidate(
    tmp_path: Path,
) -> None:
    artifacts_root = _artifacts_root(tmp_path)
    lifecycle_repo = BotLifecycleStateRepo(
        stable_bot_lifecycle_state_path(artifacts_root, _SID)
    )
    desired_repo = DesiredStateRepo(
        stable_desired_state_path(artifacts_root, _SID),
        trusted_root=artifacts_root / "live_state",
    )

    class _StoppedAuthority:
        def snapshot(
            self,
            strategy_instance_id: str,
            expected_run_id: str | None,
        ) -> AlpacaLifecycleAuthoritySnapshot:
            assert strategy_instance_id == _SID
            assert expected_run_id == "run-registered"
            return AlpacaLifecycleAuthoritySnapshot(
                strategy_instance_exists=True,
                active_run_id=None,
                retired_at_ms=None,
                expected_run_state="STOPPED",
                control_revision=1,
            )

    projector = AlpacaLifecycleProjector(
        authority=_StoppedAuthority(),
        lifecycle_repo_for=lambda _strategy_instance_id: lifecycle_repo,
        require_alpaca_identity=lambda _strategy_instance_id, _sqlite_claim: None,
    )
    report = await BotBootRecovery(
        artifacts_root,
        lifecycle_repo_for=lambda _strategy_instance_id: lifecycle_repo,
        lifecycle_projector=projector,
        desired_repo_for=lambda _strategy_instance_id: desired_repo,
        recovery_candidates=lambda: (
            BotRecoveryCandidate(_SID, "run-registered", sqlite_active=False),
        ),
        stop_authority_run=_unexpected_authority_stop,
        manages_instance=lambda _strategy_instance_id: True,
        is_running=lambda _strategy_instance_id: False,
        now_ms=lambda: _T0 + 1,
    ).run()

    record = lifecycle_repo.read()
    assert record is not None
    assert record.phase is BotLifecyclePhase.OFF_DUTY
    assert record.duty_outcome is not None
    assert record.duty_outcome.run_id == "run-registered"
    assert desired_repo.read_state() is DesiredState.STOPPED
    assert report.interrupted_instances == (_SID,)


async def test_file_cas_refusal_fails_boot_recovery(tmp_path: Path) -> None:
    artifacts_root = _artifacts_root(tmp_path)
    lifecycle_repo = BotLifecycleStateRepo(
        stable_bot_lifecycle_state_path(artifacts_root, _SID)
    )
    lifecycle_repo.set_phase(
        BotLifecyclePhase.ON_DUTY,
        now_ms=_T0,
        updated_by="pre-crash",
        active_run_id="run-raced",
    )
    desired_repo = DesiredStateRepo(
        stable_desired_state_path(artifacts_root, _SID),
        trusted_root=artifacts_root / "live_state",
    )

    class _StoppedAuthority:
        def snapshot(
            self,
            strategy_instance_id: str,
            expected_run_id: str | None,
        ) -> AlpacaLifecycleAuthoritySnapshot:
            assert strategy_instance_id == _SID
            assert expected_run_id == "run-raced"
            return AlpacaLifecycleAuthoritySnapshot(
                strategy_instance_exists=True,
                active_run_id=None,
                retired_at_ms=None,
                expected_run_state="STOPPED",
                control_revision=1,
            )

    class _RacingLifecycleRepo:
        def read(self):
            return lifecycle_repo.read()

        def update(self, **kwargs):
            lifecycle_repo.set_roster(False, now_ms=_T0 + 1, updated_by="racer")
            return lifecycle_repo.update(**kwargs)

    projector = AlpacaLifecycleProjector(
        authority=_StoppedAuthority(),
        lifecycle_repo_for=lambda _strategy_instance_id: _RacingLifecycleRepo(),  # type: ignore[arg-type]
        require_alpaca_identity=lambda _strategy_instance_id, _sqlite_claim: None,
    )
    recovery = BotBootRecovery(
        artifacts_root,
        lifecycle_repo_for=lambda _strategy_instance_id: lifecycle_repo,
        lifecycle_projector=projector,
        desired_repo_for=lambda _strategy_instance_id: desired_repo,
        recovery_candidates=lambda: (
            BotRecoveryCandidate(_SID, "run-raced", sqlite_active=False),
        ),
        stop_authority_run=_unexpected_authority_stop,
        manages_instance=lambda _strategy_instance_id: True,
        is_running=lambda _strategy_instance_id: False,
        now_ms=lambda: _T0 + 2,
    )

    with pytest.raises(BootAuthorityPreparationError, match="FILE_CAS_REFUSED"):
        await recovery.run()


async def test_authority_retry_exhaustion_fails_boot_recovery(tmp_path: Path) -> None:
    artifacts_root = _artifacts_root(tmp_path)
    lifecycle_repo = BotLifecycleStateRepo(
        stable_bot_lifecycle_state_path(artifacts_root, _SID)
    )
    lifecycle_repo.set_phase(
        BotLifecyclePhase.ON_DUTY,
        now_ms=_T0,
        updated_by="pre-crash",
        active_run_id="run-unstable",
    )
    desired_repo = DesiredStateRepo(
        stable_desired_state_path(artifacts_root, _SID),
        trusted_root=artifacts_root / "live_state",
    )

    class _AlwaysChangingAuthority:
        def __init__(self) -> None:
            self.revision = 0

        def snapshot(
            self,
            strategy_instance_id: str,
            expected_run_id: str | None,
        ) -> AlpacaLifecycleAuthoritySnapshot:
            assert strategy_instance_id == _SID
            assert expected_run_id == "run-unstable"
            self.revision += 1
            return AlpacaLifecycleAuthoritySnapshot(
                strategy_instance_exists=True,
                active_run_id=None,
                retired_at_ms=None,
                expected_run_state="STOPPED",
                control_revision=self.revision,
            )

    projector = AlpacaLifecycleProjector(
        authority=_AlwaysChangingAuthority(),
        lifecycle_repo_for=lambda _strategy_instance_id: lifecycle_repo,
        require_alpaca_identity=lambda _strategy_instance_id, _sqlite_claim: None,
    )
    recovery = BotBootRecovery(
        artifacts_root,
        lifecycle_repo_for=lambda _strategy_instance_id: lifecycle_repo,
        lifecycle_projector=projector,
        desired_repo_for=lambda _strategy_instance_id: desired_repo,
        recovery_candidates=lambda: (
            BotRecoveryCandidate(_SID, "run-unstable", sqlite_active=False),
        ),
        stop_authority_run=_unexpected_authority_stop,
        manages_instance=lambda _strategy_instance_id: True,
        is_running=lambda _strategy_instance_id: False,
        now_ms=lambda: _T0 + 2,
    )

    with pytest.raises(BootAuthorityPreparationError, match="AUTHORITY_CHANGED"):
        await recovery.run()


async def test_boot_reconstructs_sqlite_start_committed_before_binding(
    tmp_path: Path,
) -> None:
    repo = ClerkSqliteRepository.initialize(
        account_id="PA-TEST",
        artifacts_root=tmp_path / "sqlite-clerk",
    )
    repo.register_strategy_instance(
        strategy_instance_id=_SID,
        symbol="SPY",
        config_hash="config-1",
    )
    submit_start_run(
        repo,
        account_id="PA-TEST",
        strategy_instance_id=_SID,
        lifecycle_run_id="run-committed-before-binding",
    )
    broker = _SqliteRuntimeBroker()
    set_alpaca_clerk(SqliteAlpacaClerkFacade(repo=repo, read=broker, trade=broker))
    registry = BotTaskRegistry(
        _artifacts_root(tmp_path),
        feed_resolver=lambda: _FakeFeed([], mode="hold"),
        supported_broker_ids=frozenset({"alpaca"}),
        start_custody_guard=_flat_start_guard,
    )

    try:
        report = await registry.run_boot_recovery()

        assert report.interrupted_instances == (_SID,)
        assert repo.active_run(_SID) is None
        record = registry._lifecycle_repo(_SID).read()
        assert record is not None
        assert record.phase is BotLifecyclePhase.OFF_DUTY
        assert record.duty_outcome is not None
        assert record.duty_outcome.run_id == "run-committed-before-binding"
        assert registry._desired_repo(_SID).read_state() is DesiredState.STOPPED
    finally:
        set_alpaca_clerk(None)
        repo.close()


# ── AC1: interrupted evidence, no auto-restart, never healthy ──────────


async def test_boot_sweep_records_interrupted_evidence_and_never_restarts(
    tmp_path: Path,
) -> None:
    feed = _FakeFeed([], mode="hold")
    registry = _registry(tmp_path, feed)
    await registry.run_boot_recovery()
    await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")
    assert _lifecycle_json(_artifacts_root(tmp_path), _SID)["phase"] == "ON_DUTY"
    # Simulated hard stop: the process dies — tasks vanish, files survive.
    # A hard kill never runs the supervisor's finalizer, so suppress it
    # (finalized=True makes it a no-op) before tearing the task down.
    registry._bots[_SID].finalized = True
    registry._bots[_SID].task.cancel()
    await asyncio.sleep(0)  # evidence stays stale ON_DUTY, as after a kill
    rebooted = _registry(tmp_path, feed)
    report = await rebooted.run_boot_recovery()

    assert report.interrupted_instances == (_SID,)
    lifecycle = _lifecycle_json(_artifacts_root(tmp_path), _SID)
    assert lifecycle["phase"] == "OFF_DUTY"
    assert lifecycle["duty_outcome"]["kind"] == "EXITED_UNVERIFIED"
    assert lifecycle["duty_outcome"]["reason_code"] == "INTERRUPTED_BY_RESTART"

    view = rebooted.status("alpaca", _SID)
    assert view.running is False  # never rendered healthy, never auto-restarted
    assert view.phase == "OFF_DUTY"
    assert view.desired_state == "STOPPED"


async def test_boot_sweep_does_not_report_a_superseded_interruption(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    artifacts_root = _artifacts_root(tmp_path)
    lifecycle_path = stable_bot_lifecycle_state_path(artifacts_root, _SID)

    lifecycle_repo = BotLifecycleStateRepo(lifecycle_path)
    lifecycle_repo.set_phase(
        BotLifecyclePhase.ON_DUTY,
        now_ms=_T0,
        updated_by="old-run",
        active_run_id="run-old",
    )
    desired_repo = DesiredStateRepo(
        stable_desired_state_path(artifacts_root, _SID),
        trusted_root=artifacts_root / "live_state",
    )
    caplog.set_level("WARNING", logger="app.services.bot_boot_recovery")
    clerk = _CustodyClerk(_custody_proof(exposure={}))
    clerk.active_runs[_SID] = "run-new"
    clerk.known_runs.add((_SID, "run-new"))
    set_alpaca_clerk(clerk)
    projector = AlpacaLifecycleProjector(
        authority=ActiveSqliteAlpacaLifecycleAuthority(),
        lifecycle_repo_for=lambda _strategy_instance_id: lifecycle_repo,
        require_alpaca_identity=lambda _strategy_instance_id, _sqlite_claim: None,
    )

    report = await BotBootRecovery(
        artifacts_root,
        lifecycle_repo_for=lambda _strategy_instance_id: lifecycle_repo,
        lifecycle_projector=projector,
        desired_repo_for=lambda _strategy_instance_id: desired_repo,
        recovery_candidates=lambda: (
            BotRecoveryCandidate(_SID, "run-old", sqlite_active=False),
        ),
        stop_authority_run=_unexpected_authority_stop,
        manages_instance=lambda _strategy_instance_id: True,
        is_running=lambda _strategy_instance_id: False,
        now_ms=lambda: _T0 + 2,
    ).run()

    assert report.interrupted_instances == ()
    current = lifecycle_repo.read()
    assert current is not None
    assert current.active_run_id == "run-new"
    assert desired_repo.read() is None
    assert all(
        getattr(record, "action", None) != "boot_sweep_interrupted"
        for record in caplog.records
    )


async def test_boot_sweep_repairs_interrupted_bot_stranded_as_running(
    tmp_path: Path,
) -> None:
    feed = _FakeFeed([], mode="hold")
    registry = _registry(tmp_path, feed)
    await registry.run_boot_recovery()
    await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")
    registry._bots[_SID].finalized = True
    registry._bots[_SID].task.cancel()
    await asyncio.sleep(0)

    first_reboot = _registry(tmp_path, feed)
    await first_reboot.run_boot_recovery()
    first_reboot._desired_repo(_SID).set(
        DesiredState.RUNNING,
        updated_by="legacy_boot_sweep",
        now_ms=_T0,
        reason="legacy_interrupted_state",
    )
    assert first_reboot.status("alpaca", _SID).desired_state == "RUNNING"

    second_reboot = _registry(tmp_path, feed)
    report = await second_reboot.run_boot_recovery()

    assert report.interrupted_instances == ()
    view = second_reboot.status("alpaca", _SID)
    assert view.phase == "OFF_DUTY"
    assert view.desired_state == "STOPPED"
    assert view.duty_outcome is not None
    assert view.duty_outcome.reason_code == "INTERRUPTED_BY_RESTART"


async def test_boot_sweep_repairs_service_shutdown_stranded_as_running(
    tmp_path: Path,
) -> None:
    feed = _FakeFeed([], mode="hold")
    registry = _registry(tmp_path, feed)
    await registry.run_boot_recovery()
    await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")

    await registry.stop_all()
    assert registry.status("alpaca", _SID).desired_state == "RUNNING"

    rebooted = _registry(tmp_path, feed)
    report = await rebooted.run_boot_recovery()

    assert report.interrupted_instances == ()
    view = rebooted.status("alpaca", _SID)
    assert view.phase == "OFF_DUTY"
    assert view.desired_state == "STOPPED"
    assert view.duty_outcome is not None
    assert view.duty_outcome.reason_code == "SERVICE_SHUTDOWN"


async def test_boot_sweep_repairs_service_shutdown_stranded_as_paused(
    tmp_path: Path,
) -> None:
    feed = _FakeFeed([], mode="hold")
    registry = _registry(tmp_path, feed)
    await registry.run_boot_recovery()
    await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")
    await registry.pause("alpaca", _SID)
    await registry.stop_all()
    assert registry.status("alpaca", _SID).desired_state == "PAUSED"

    rebooted = _registry(tmp_path, feed)
    await rebooted.run_boot_recovery()

    view = rebooted.status("alpaca", _SID)
    assert view.phase == "OFF_DUTY"
    assert view.desired_state == "STOPPED"
    assert view.duty_outcome is not None
    assert view.duty_outcome.reason_code == "SERVICE_SHUTDOWN"


async def test_boot_sweep_skips_bots_bound_to_unsupported_broker(
    tmp_path: Path,
) -> None:
    """Registry with supported_broker_ids={"alpaca"} must not touch IBKR bots."""
    feed = _FakeFeed([], mode="hold")
    # Seed one retained IBKR binding without asking the Alpaca runner to own it.
    registry = BotTaskRegistry(
        _artifacts_root(tmp_path),
        feed_resolver=lambda: feed,
        supported_broker_ids=frozenset({"alpaca"}),
        start_custody_guard=_flat_start_guard,
    )
    registry._bindings.record_launch(
        BrokerBotBinding(
            strategy_instance_id=_SID,
            broker="ibkr",
            symbol="SPY",
            action_plan=alpaca_v1_action_plan("SPY"),
            run_id="ibkr-run",
            created_at_ms=_T0,
        ),
        launch_reason="deploy",
    )
    registry._lifecycle_repo(_SID).set_phase(
        BotLifecyclePhase.ON_DUTY,
        now_ms=_T0,
        updated_by="ibkr-host-daemon",
        active_run_id="ibkr-run",
    )
    assert _lifecycle_json(_artifacts_root(tmp_path), _SID)["phase"] == "ON_DUTY"
    rebooted = BotTaskRegistry(
        _artifacts_root(tmp_path),
        feed_resolver=lambda: feed,
        supported_broker_ids=frozenset({"alpaca"}),
        start_custody_guard=_flat_start_guard,
    )
    assert rebooted.binding_for_control("ibkr", _SID).broker == "ibkr"
    assert rebooted._manages_boot_recovery(_SID) is False
    report = await rebooted.run_boot_recovery()

    # Sweep must skip the ibkr-tagged bot: no interrupted evidence, lifecycle
    # left untouched as ON_DUTY.
    assert report.interrupted_instances == ()
    assert _lifecycle_json(_artifacts_root(tmp_path), _SID)["phase"] == "ON_DUTY"


async def test_boot_sweep_refuses_corrupt_binding_for_sqlite_active_run(
    tmp_path: Path,
) -> None:
    """Conflicting unreadable plane evidence keeps authority recovery closed."""
    feed = _FakeFeed([], mode="hold")
    registry = BotTaskRegistry(
        _artifacts_root(tmp_path),
        feed_resolver=lambda: feed,
        supported_broker_ids=frozenset({"alpaca"}),
        start_custody_guard=_flat_start_guard,
    )
    await registry.run_boot_recovery()
    await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")
    registry._bots[_SID].finalized = True
    registry._bots[_SID].task.cancel()
    await asyncio.sleep(0)

    binding_path = _artifacts_root(tmp_path) / "live_state" / _SID / "strategy_instance.json"
    binding_path.write_text("{not-json", encoding="utf-8")

    rebooted = BotTaskRegistry(
        _artifacts_root(tmp_path),
        feed_resolver=lambda: feed,
        supported_broker_ids=frozenset({"alpaca"}),
    )
    with pytest.raises(BootAuthorityPreparationError, match="candidate enumeration"):
        await rebooted.run_boot_recovery()
    assert _lifecycle_json(_artifacts_root(tmp_path), _SID)["phase"] == "ON_DUTY"
