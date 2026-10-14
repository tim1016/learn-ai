"""Registry / Clerk ordering: the durable Clerk STOP must commit before
task cancellation, and the runner stays daemon-free and container-scoped
by construction.

Split from ``tests/services/test_bot_runner.py`` (issue #1737, seam 1).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.broker.alpaca.clerk import set_alpaca_clerk
from app.marketdata.feed import ContinuityPolicy
from app.schemas.traded_asset_acknowledgement import UntestedTradedAssetAcknowledgement
from tests._helpers.bot_runner.custody import _SID, _T0, _custody_proof, _registry, admission_guard_for
from tests._helpers.bot_runner.doubles import _FakeFeed
from tests._helpers.canary_admission import admit_canary_pairing
from tests._helpers.exit_terms import DEPLOY_EXIT_TERMS

from ._support import _bar, _OrderingClerk, _wait_for


class _StopOrderingFeed(_FakeFeed):
    def __init__(self, clerk: _OrderingClerk) -> None:
        super().__init__([], mode="hold")
        self._clerk = clerk
        self.cancelled_after_stop = False

    async def stream_bars(
        self,
        symbol: str,
        *,
        use_rth: bool = True,
        continuity: ContinuityPolicy | None = None,
    ):
        del symbol, use_rth, continuity
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled_after_stop = self._clerk.stop_committed.is_set()
            raise
        if False:
            yield


@pytest.mark.asyncio
async def test_trade_run_registration_precedes_order_capable_task_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clerk = _OrderingClerk(_custody_proof(exposure={}))
    feed = _StopOrderingFeed(clerk)
    registry = _registry(
        tmp_path,
        feed,
        start_custody_guard=admission_guard_for(clerk),
    )
    set_alpaca_clerk(clerk)
    admit_canary_pairing(monkeypatch, "deployment_validation", "paper-account")
    try:
        deployed = await registry.deploy(
            exit_terms=DEPLOY_EXIT_TERMS, broker="alpaca",
            strategy_instance_id=_SID,
            symbol="SPY",
            mode="trade",
        )

        assert clerk.registered_runs == [deployed.active_run_id]
        assert not clerk.registration_saw_bot_task
        await registry.stop("alpaca", _SID)
    finally:
        set_alpaca_clerk(None)


@pytest.mark.asyncio
async def test_the_owners_acknowledgement_of_an_untested_asset_is_sealed_with_the_bot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#2912: a bot that listens to SPY and trades QQQ keeps what the owner accepted, in its record and under its seal."""
    acknowledgement = UntestedTradedAssetAcknowledgement(
        acknowledgement="I_ACCEPT_UNTESTED_TRADED_ASSET_RISK",
        reason="QQQ has followed SPY closely enough to try this.",
    )
    clerk = _OrderingClerk(_custody_proof(exposure={}))
    registry = _registry(tmp_path, _StopOrderingFeed(clerk), start_custody_guard=admission_guard_for(clerk))
    set_alpaca_clerk(clerk)
    admit_canary_pairing(monkeypatch, "deployment_validation", "paper-account")
    try:
        await registry.deploy_with_admission(
            exit_terms=DEPLOY_EXIT_TERMS, broker="alpaca",
            strategy_instance_id=_SID,
            symbol="SPY",
            traded_symbol="QQQ",
            mode="trade",
            traded_asset_acknowledgement=acknowledgement,
        )

        binding = registry.binding_for_control("alpaca", _SID)
        assert binding.traded_asset_acknowledgement == acknowledgement
        assert binding.sealed_program is not None
        assert binding.sealed_program.traded_asset_acknowledgement == acknowledgement
        stored = tmp_path / "live_state" / _SID
        for record in ("strategy_instance.json", "sealed_program_v2.json"):
            assert json.loads((stored / record).read_text())["traded_asset_acknowledgement"] == acknowledgement.model_dump()
        await registry.stop("alpaca", _SID)
    finally:
        set_alpaca_clerk(None)


@pytest.mark.asyncio
async def test_stop_commits_clerk_stop_before_task_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clerk = _OrderingClerk(_custody_proof(exposure={}))
    feed = _StopOrderingFeed(clerk)
    registry = _registry(
        tmp_path,
        feed,
        start_custody_guard=admission_guard_for(clerk),
    )
    set_alpaca_clerk(clerk)
    admit_canary_pairing(monkeypatch, "deployment_validation", "paper-account")
    try:
        deployed = await registry.deploy(
            exit_terms=DEPLOY_EXIT_TERMS, broker="alpaca",
            strategy_instance_id=_SID,
            symbol="SPY",
            mode="trade",
        )

        await registry.stop("alpaca", _SID)

        assert clerk.stopped_runs == [deployed.active_run_id]
        assert feed.cancelled_after_stop
    finally:
        set_alpaca_clerk(None)


@pytest.mark.asyncio
async def test_quiesce_after_clerk_stop_does_not_commit_a_second_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clerk = _OrderingClerk(_custody_proof(exposure={}))
    feed = _StopOrderingFeed(clerk)
    registry = _registry(
        tmp_path,
        feed,
        start_custody_guard=admission_guard_for(clerk),
    )
    set_alpaca_clerk(clerk)
    admit_canary_pairing(monkeypatch, "deployment_validation", "paper-account")
    try:
        deployed = await registry.deploy(
            exit_terms=DEPLOY_EXIT_TERMS, broker="alpaca",
            strategy_instance_id=_SID,
            symbol="SPY",
            mode="trade",
        )
        assert deployed.active_run_id is not None

        # Model the recovery flow: the SQLite authority already committed the
        # durable STOP before entering the registry. The registry must reap the
        # task *after* that commit, without authoring a second STOP.
        clerk.stop_committed.set()

        await registry.stop_after_durable_clerk_stop(
            "alpaca",
            _SID,
            lifecycle_run_id=deployed.active_run_id,
            updated_by="operator_recovery",
            reason="sqlite_recovery_stop_bot_decisions",
        )

        assert clerk.stopped_runs == []
        assert feed.cancelled_after_stop
        assert registry.status("alpaca", _SID).running is False
    finally:
        set_alpaca_clerk(None)


@pytest.mark.asyncio
async def test_failed_clerk_stop_closes_run_gate_without_cancelling_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clerk = _OrderingClerk(_custody_proof(exposure={}))
    feed = _StopOrderingFeed(clerk)
    registry = _registry(
        tmp_path,
        feed,
        start_custody_guard=admission_guard_for(clerk),
    )
    set_alpaca_clerk(clerk)
    admit_canary_pairing(monkeypatch, "deployment_validation", "paper-account")
    try:
        await registry.deploy(
            exit_terms=DEPLOY_EXIT_TERMS, broker="alpaca",
            strategy_instance_id=_SID,
            symbol="SPY",
            mode="trade",
        )
        clerk.fail_stop = True

        with pytest.raises(RuntimeError, match="durable STOP failed"):
            await registry.stop("alpaca", _SID)

        managed = registry._bots[_SID]
        assert not managed.run_gate.is_set()
        assert not managed.task.done()
        assert not feed.cancelled_after_stop

        clerk.fail_stop = False
        await registry.stop("alpaca", _SID)
    finally:
        set_alpaca_clerk(None)


@pytest.mark.asyncio
async def test_stop_all_commits_each_trade_run_before_task_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clerk = _OrderingClerk(_custody_proof(exposure={}))
    feed = _StopOrderingFeed(clerk)
    registry = _registry(
        tmp_path,
        feed,
        start_custody_guard=admission_guard_for(clerk),
    )
    set_alpaca_clerk(clerk)
    admit_canary_pairing(monkeypatch, "deployment_validation", "paper-account")
    try:
        deployed = await registry.deploy(
            exit_terms=DEPLOY_EXIT_TERMS, broker="alpaca",
            strategy_instance_id=_SID,
            symbol="SPY",
            mode="trade",
        )

        await registry.stop_all()

        assert clerk.stopped_runs == [deployed.active_run_id]
        assert feed.cancelled_after_stop
    finally:
        set_alpaca_clerk(None)


@pytest.mark.asyncio
async def test_stop_all_preserves_operator_intent(tmp_path: Path) -> None:
    feed = _FakeFeed([], mode="hold")
    registry = _registry(tmp_path, feed)
    await registry.deploy(exit_terms=DEPLOY_EXIT_TERMS, broker="alpaca", strategy_instance_id=_SID, symbol="SPY")

    await registry.stop_all()
    await _wait_for(lambda: _SID not in registry._bots)

    view = registry.status("alpaca", _SID)
    assert view.running is False
    assert view.duty_outcome is not None
    assert view.duty_outcome.kind == "STOPPED"
    assert view.duty_outcome.reason_code == "SERVICE_SHUTDOWN"
    # Operator intent untouched: the bot still WANTS to run after a restart.
    assert view.desired_state == "RUNNING"


@pytest.mark.asyncio
async def test_all_artifacts_are_written_under_the_container_root(tmp_path: Path) -> None:
    from app.broker.alpaca.clerk.account_authority import paper_evidence_account_id_for_strategy

    feed = _FakeFeed([_bar(_T0)], mode="hold")
    registry = _registry(tmp_path, feed)
    await registry.deploy(exit_terms=DEPLOY_EXIT_TERMS, broker="alpaca", strategy_instance_id=_SID, symbol="SPY")
    await registry.stop("alpaca", _SID)

    written = sorted(
        p.relative_to(tmp_path).as_posix()
        for p in tmp_path.rglob("*")
        # .lock sidecars belong to the canonical repos' advisory-lock protocol.
        if p.is_file() and p.suffix != ".lock"
    )
    # "deployment_validation" is a registered Signal Program (issue #1730
    # Slice 5): every deploy now also writes the v2 seal
    # (sealed_program_v2.json) and, since its build proves PROVEN, the
    # per-run program-build evidence record -- both new, correct artifacts
    # for a sealed program's deploy, not present for a legacy compatibility
    # strategy.
    #
    # Direction 2 (run-scoped replay proof): a real-paper trade run now
    # retains its exact source bars in the instance-scoped
    # ``paper:<sid>`` evidence ledger (the WAL-mode sqlite file plus its
    # -wal/-shm sidecars), mirroring Dry Run's ``sim:<sid>`` scoping. The
    # ledger stays open for the run's lifetime exactly as Dry Run's does,
    # so the sidecars are the expected on-disk shape.
    assert written == [
        f"accounts/alpaca/{paper_evidence_account_id_for_strategy(_SID)}/source_bars.sqlite3",
        f"accounts/alpaca/{paper_evidence_account_id_for_strategy(_SID)}/source_bars.sqlite3-shm",
        f"accounts/alpaca/{paper_evidence_account_id_for_strategy(_SID)}/source_bars.sqlite3-wal",
        f"live_state/{_SID}/current_run.json",
        f"live_state/{_SID}/desired_state.json",
        f"live_state/{_SID}/lifecycle_state.json",
        f"live_state/{_SID}/program_build_evidence/{registry.binding_for_control('alpaca', _SID).run_id}.json",
        f"live_state/{_SID}/run_outcomes/{registry.binding_for_control('alpaca', _SID).run_id}.json",
        f"live_state/{_SID}/runs/{registry.binding_for_control('alpaca', _SID).run_id}.json",
        f"live_state/{_SID}/sealed_program_v2.json",
        f"live_state/{_SID}/strategy_instance.json",
    ]


class _YieldingStopClerk(_OrderingClerk):
    """A Clerk whose STOP commit waits on a contended intake fence: it yields to the loop."""

    async def stop_strategy_run(
        self, *, strategy_instance_id: str, run_id: str, reason: str | None = None,
    ) -> None:
        await super().stop_strategy_run(strategy_instance_id=strategy_instance_id, run_id=run_id, reason=reason)
        await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_stop_all_stops_every_bot_when_a_cancelled_bots_task_reaps_itself_mid_shutdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F-A-1: the first bot's task unwinds and leaves the bot dict while the second bot's STOP awaits."""
    clerk = _YieldingStopClerk(_custody_proof(exposure={}))
    registry = _registry(tmp_path, _FakeFeed([_bar(_T0)], mode="hold"))
    set_alpaca_clerk(clerk)
    admit_canary_pairing(monkeypatch, "deployment_validation", "paper-account")
    try:
        first = await registry.deploy(
            exit_terms=DEPLOY_EXIT_TERMS, broker="alpaca", strategy_instance_id=_SID, symbol="SPY", mode="trade",
        )
        second = await registry.deploy(
            exit_terms=DEPLOY_EXIT_TERMS, broker="alpaca", strategy_instance_id=f"{_SID}-2", symbol="SPY", mode="trade",
        )

        await registry.stop_all()

        assert sorted(clerk.stopped_runs) == sorted([first.active_run_id, second.active_run_id])
    finally:
        set_alpaca_clerk(None)


@pytest.mark.asyncio
async def test_stop_all_cancels_a_stop_at_a_bots_end_that_outlives_its_wait(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F-A-3: a stop waiting out its custody proof is abandoned, not left pending past shutdown."""
    from app.services import bot_runner

    monkeypatch.setattr(bot_runner, "_STOP_TIMEOUT_S", 0.05)
    registry = _registry(tmp_path, _FakeFeed([], mode="hold"))
    waiting_for_proof = asyncio.get_running_loop().create_task(asyncio.sleep(3600), name="bot-end:straggler")
    registry._end_stop_tasks["straggler"] = waiting_for_proof

    await registry.stop_all()

    assert waiting_for_proof.cancelled()
