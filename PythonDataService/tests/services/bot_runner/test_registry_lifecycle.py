"""``BotTaskRegistry`` core lifecycle: deploy, stop, crash, kill, and
retire, with durable evidence asserted from the raw artifact files.

Split from ``tests/services/test_bot_runner.py`` (issue #1737).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.broker.alpaca.clerk.active_protocol import ClerkAdmissionSnapshotStaleError
from app.broker.alpaca.clerk.models import (
    AccountFreezeState,
    ClerkCustodySnapshot,
    CustodyCountFact,
    CustodyExposureFact,
)
from app.engine.live.account_artifacts import RestartIntensityPolicy
from app.engine.live.desired_state import DesiredState
from app.marketdata.feed import ContinuityPolicy, FeedHealth, MarketDataBar, MarketDataFeedError
from app.schemas.broker_bots import BotProcessFact
from app.services import bot_runner as bot_runner_module
from app.services.bot_runner import (
    BootRecoveryIncompleteError,
    BotAlreadyRunningError,
    BotTaskRegistry,
    InvalidStrategyInstanceIdError,
    MarketDataFeedUnavailableError,
    RecoveryUncertainError,
    RestartIntensityRefusedError,
    RunAdmissionRefusedError,
    UnknownBotError,
)
from app.services.bot_runner_errors import (
    BotRunnerError,
)
from tests._helpers.bot_runner.custody import (
    _SID,
    _T0,
    _flat_custody_snapshot,
    _flat_start_guard,
    _lifecycle_json,
    _registry,
)
from tests._helpers.bot_runner.doubles import _FakeFeed
from tests._helpers.bot_runner.ema_parity import admit_lean_parity_settings_for_start_admission

from ._support import _RTH_MS, _bar, _current_run_json, _strategy_instance_json, _wait_for

_CLOSED_MS = 1_700_096_400_000


def _desired_json(tmp_path: Path, sid: str = _SID) -> dict:
    path = tmp_path / "live_state" / sid / "desired_state.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _run_json(tmp_path: Path, run_id: str, sid: str = _SID) -> dict:
    path = tmp_path / "live_state" / sid / "runs" / f"{run_id}.json"
    return json.loads(path.read_text(encoding="utf-8"))


class _StaleFeed(_FakeFeed):
    def health(self, symbol: str | None = None) -> FeedHealth:
        return super().health(symbol).model_copy(update={"stale": True, "reason": "No recent closed bar."})


class _ActiveStaleFeed(_StaleFeed):
    def health(self, symbol: str | None = None) -> FeedHealth:
        return super().health(symbol).model_copy(update={"active_subscription_count": 1})


class _CancellationSuppressingFeed(_FakeFeed):
    """Simulates a strategy coroutine whose task never terminates on cancel.

    ``stream_bars`` swallows exactly one ``CancelledError`` at its
    ``await`` suspension point and keeps looping, so ``task.cancel()``
    never actually finishes the task — reproducing the P0-3 audit
    finding without a real external dependency.
    """

    def __init__(self, bars: list[MarketDataBar]) -> None:
        super().__init__(bars, mode="hold")
        self.cancellation_suppressed = False

    async def stream_bars(
        self,
        symbol: str,
        *,
        use_rth: bool = True,
        continuity: ContinuityPolicy | None = None,
    ):
        del continuity
        for bar in self._bars:
            self.bars_consumed += 1
            yield bar
        while True:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                if self.cancellation_suppressed:
                    raise
                self.cancellation_suppressed = True


@asynccontextmanager
async def _rth_start_guard(sid: str):
    yield _flat_custody_snapshot(sid, observed_at_ms=_RTH_MS)


@asynccontextmanager
async def _closed_start_guard(sid: str):
    yield _flat_custody_snapshot(sid, observed_at_ms=_CLOSED_MS)


@asynccontextmanager
async def _changing_start_guard(_sid: str):
    raise ClerkAdmissionSnapshotStaleError("test evidence race")
    yield  # pragma: no cover - required to type this as an async context manager


@pytest.mark.asyncio
async def test_deploy_produces_running_task_and_durable_on_duty_evidence(tmp_path: Path) -> None:
    feed = _FakeFeed([_bar(_T0)], mode="hold")
    registry = _registry(tmp_path, feed)

    view = await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")

    assert view.running is True
    assert view.phase == "ON_DUTY"
    assert view.desired_state == "RUNNING"
    assert view.broker == "alpaca"
    assert view.active_run_id is not None
    assert _strategy_instance_json(tmp_path)["sealed_account_id"] == "paper-account"

    # Durable evidence readable WITHOUT the runner (raw files).
    lifecycle = _lifecycle_json(tmp_path)
    assert lifecycle["phase"] == "ON_DUTY"
    assert lifecycle["active_run_id"] == view.active_run_id
    desired = _desired_json(tmp_path)
    assert desired["desired_state"] == "RUNNING"
    instance = _strategy_instance_json(tmp_path)
    assert instance["broker"] == "alpaca"
    assert instance["symbol"] == "SPY"
    assert instance["mode"] == "log_only"
    assert instance["quantity"] == 1
    current = _current_run_json(tmp_path)
    assert current["run_id"] == view.active_run_id
    assert isinstance(_run_json(tmp_path, current["run_id"])["started_at_ms"], int)

    await registry.stop("alpaca", _SID)


@pytest.mark.asyncio
async def test_process_fact_requires_current_registry_liveness_proof(tmp_path: Path) -> None:
    feed = _FakeFeed([], mode="hold")
    registry = _registry(tmp_path, feed)
    view = await registry.deploy(
        broker="alpaca",
        strategy_instance_id=_SID,
        symbol="SPY",
    )

    running = registry.process_fact("alpaca", _SID)

    assert running.strategy_instance_id == _SID
    assert running.run_id == view.active_run_id
    assert running.process_identity == f"in-process-task:{view.active_run_id}"
    assert running.state == "RUNNING"
    assert running.registry_generation
    assert running.observed_at_ms > 0

    replacement_registry = _registry(tmp_path, feed)
    unknown = replacement_registry.process_fact("alpaca", _SID)

    assert unknown.run_id == view.active_run_id
    assert unknown.process_identity is None
    assert unknown.state == "UNKNOWN"
    assert unknown.registry_generation != running.registry_generation

    await registry.stop("alpaca", _SID)
    exited = replacement_registry.process_fact("alpaca", _SID)
    assert exited.run_id == view.active_run_id
    assert exited.process_identity is None
    assert exited.state == "EXITED"


def test_process_fact_rejects_unemittable_starting_state() -> None:
    with pytest.raises(ValidationError):
        BotProcessFact(
            strategy_instance_id=_SID,
            run_id="run-1",
            process_identity="in-process-task:run-1",
            state="STARTING",
            registry_generation="registry-1",
            observed_at_ms=_T0,
        )


@pytest.mark.asyncio
async def test_start_preview_and_execution_share_the_same_admission_policy(
    tmp_path: Path,
) -> None:
    feed = _ActiveStaleFeed([], mode="hold", observed_at_ms=_RTH_MS)
    registry = BotTaskRegistry(
        tmp_path,
        feed_resolver=lambda: feed,
        restart_policy=RestartIntensityPolicy(threshold=100),
        now_ms=lambda: _RTH_MS,
        boot_recovery_required=False,
        start_custody_guard=_rth_start_guard,
    )

    preview = await registry.preview_start_admission(
        broker="alpaca",
        strategy_instance_id=_SID,
        symbol="SPY",
    )

    assert preview.allowed is False
    assert preview.reason_code == "MARKET_DATA_STALE"
    with pytest.raises(MarketDataFeedUnavailableError) as refused:
        await registry.deploy(
            broker="alpaca",
            strategy_instance_id=_SID,
            symbol="SPY",
        )
    assert refused.value.admission_decision is not None
    assert refused.value.admission_decision.reason_code == preview.reason_code
    assert not (tmp_path / "live_state" / _SID / "broker_binding.json").exists()


@pytest.mark.asyncio
async def test_start_allows_idle_connected_feed_to_establish_subscription(
    tmp_path: Path,
) -> None:
    feed = _StaleFeed([], mode="hold", observed_at_ms=_RTH_MS)
    registry = BotTaskRegistry(
        tmp_path,
        feed_resolver=lambda: feed,
        restart_policy=RestartIntensityPolicy(threshold=100),
        now_ms=lambda: _RTH_MS,
        boot_recovery_required=False,
        start_custody_guard=_rth_start_guard,
    )

    preview = await registry.preview_start_admission(
        broker="alpaca",
        strategy_instance_id=_SID,
        symbol="SPY",
    )

    assert preview.allowed is True
    started = await registry.deploy(
        broker="alpaca",
        strategy_instance_id=_SID,
        symbol="SPY",
    )
    assert started.running is True
    await registry.stop("alpaca", _SID)


@pytest.mark.asyncio
async def test_start_does_not_call_expected_rth_silence_a_stalled_feed(
    tmp_path: Path,
) -> None:
    feed = _ActiveStaleFeed([], mode="hold", observed_at_ms=_CLOSED_MS)
    registry = BotTaskRegistry(
        tmp_path,
        feed_resolver=lambda: feed,
        restart_policy=RestartIntensityPolicy(threshold=100),
        now_ms=lambda: _CLOSED_MS,
        boot_recovery_required=False,
        start_custody_guard=_closed_start_guard,
    )

    preview = await registry.preview_start_admission(
        broker="alpaca",
        strategy_instance_id=_SID,
        symbol="SPY",
    )

    assert preview.allowed is True


@pytest.mark.asyncio
async def test_start_preview_and_execution_share_boot_recovery_refusal(
    tmp_path: Path,
) -> None:
    registry = BotTaskRegistry(
        tmp_path,
        feed_resolver=lambda: _FakeFeed([], mode="hold"),
        restart_policy=RestartIntensityPolicy(threshold=100),
        start_custody_guard=_flat_start_guard,
    )

    preview = await registry.preview_start_admission(
        broker="alpaca",
        strategy_instance_id=_SID,
        symbol="SPY",
    )

    assert preview.allowed is False
    assert preview.reason_code == "BOOT_RECOVERY_INCOMPLETE"
    with pytest.raises(BootRecoveryIncompleteError) as refused:
        await registry.deploy(
            broker="alpaca",
            strategy_instance_id=_SID,
            symbol="SPY",
        )
    assert refused.value.admission_decision is not None
    assert refused.value.admission_decision.reason_code == preview.reason_code


@pytest.mark.asyncio
async def test_start_preview_and_execution_share_unresolved_recovery_refusal(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path, _FakeFeed([], mode="hold"))

    async def no_op() -> None:
        return None

    async def one_unresolved_intent(subject_id: str | None) -> int:
        # Subject-aware on purpose: a double that answered 1 for every subject
        # would flatten the very distinction #1793 introduced, and this test
        # would keep passing if the gate regressed to an account-wide read.
        return 1 if subject_id in (None, f"bot:{_SID}") else 0

    await registry.run_boot_recovery(
        recover=no_op,
        reconcile=no_op,
        unresolved_intents_probe=one_unresolved_intent,
    )

    preview = await registry.preview_start_admission(
        broker="alpaca",
        strategy_instance_id=_SID,
        symbol="SPY",
    )

    assert preview.allowed is False
    assert preview.reason_code == "RECOVERY_UNCERTAIN"
    with pytest.raises(RecoveryUncertainError) as refused:
        await registry.deploy(
            broker="alpaca",
            strategy_instance_id=_SID,
            symbol="SPY",
        )
    assert refused.value.admission_decision is not None
    assert refused.value.admission_decision.reason_code == preview.reason_code


@pytest.mark.asyncio
async def test_one_bots_unresolved_intent_does_not_refuse_a_sibling(
    tmp_path: Path,
) -> None:
    """#1793: the amplifier that turned one stuck EXIT into a 50-bot freeze.

    The gate is per-bot; the count it read was per-account, so a single
    unresolved intent refused Start and Resume fleet-wide. The probe is now
    asked about the requesting bot's own custody subject.
    """
    registry = _registry(tmp_path, _FakeFeed([], mode="hold"))
    frozen_sid, healthy_sid = "alpaca-frozen-1", "alpaca-healthy-1"
    asked: list[str | None] = []

    async def no_op() -> None:
        return None

    async def only_the_frozen_bot(subject_id: str | None) -> int:
        asked.append(subject_id)
        return 1 if subject_id == f"bot:{frozen_sid}" else 0

    await registry.run_boot_recovery(
        recover=no_op,
        reconcile=no_op,
        unresolved_intents_probe=only_the_frozen_bot,
    )

    frozen = await registry.preview_start_admission(
        broker="alpaca", strategy_instance_id=frozen_sid, symbol="SPY"
    )
    healthy = await registry.preview_start_admission(
        broker="alpaca", strategy_instance_id=healthy_sid, symbol="SPY"
    )

    # The bot that owns the unresolved intent is still refused by it.
    assert frozen.allowed is False
    assert frozen.reason_code == "RECOVERY_UNCERTAIN"

    # Its sibling is not. Before #1793 this was RECOVERY_UNCERTAIN too.
    assert healthy.reason_code != "RECOVERY_UNCERTAIN"

    # Boot recovery reads account-wide (it is a summary of the whole
    # authority); both admission decisions read one subject each. Asserting
    # the whole call log keeps that distinction from silently collapsing.
    assert asked == [None, f"bot:{frozen_sid}", f"bot:{healthy_sid}"]


@pytest.mark.asyncio
async def test_start_preview_and_execution_share_restart_intensity_refusal(
    tmp_path: Path,
) -> None:
    registry = _registry(
        tmp_path,
        _FakeFeed([], mode="hold"),
        policy=RestartIntensityPolicy(threshold=1, window_ms=300_000),
    )

    preview = await registry.preview_start_admission(
        broker="alpaca",
        strategy_instance_id=_SID,
        symbol="SPY",
    )

    assert preview.allowed is False
    assert preview.reason_code == "RESTART_INTENSITY_EXCEEDED"
    with pytest.raises(RestartIntensityRefusedError) as refused:
        await registry.deploy(
            broker="alpaca",
            strategy_instance_id=_SID,
            symbol="SPY",
        )
    assert refused.value.admission_decision is not None
    assert refused.value.admission_decision.reason_code == preview.reason_code


@pytest.mark.asyncio
async def test_start_refuses_cleanly_when_clerk_evidence_never_stabilizes(
    tmp_path: Path,
) -> None:
    registry = BotTaskRegistry(
        tmp_path,
        feed_resolver=lambda: _FakeFeed([], mode="hold"),
        restart_policy=RestartIntensityPolicy(threshold=100),
        boot_recovery_required=False,
        start_custody_guard=_changing_start_guard,
    )

    with pytest.raises(RunAdmissionRefusedError, match="stable Clerk custody"):
        await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")

    assert not (tmp_path / "live_state" / _SID / "broker_binding.json").exists()


@pytest.mark.asyncio
async def test_start_timestamps_activation_after_custody_reconciliation(
    tmp_path: Path,
) -> None:
    clock = {"now": _T0}

    @asynccontextmanager
    async def delayed_custody_guard(sid: str):
        clock["now"] = _T0 + 10_000
        yield _flat_custody_snapshot(sid, observed_at_ms=clock["now"])

    registry = BotTaskRegistry(
        tmp_path,
        feed_resolver=lambda: _FakeFeed([], mode="hold", observed_at_ms=_T0 + 10_000),
        restart_policy=RestartIntensityPolicy(threshold=100),
        now_ms=lambda: clock["now"],
        boot_recovery_required=False,
        start_custody_guard=delayed_custody_guard,
    )

    started = await registry.deploy_with_admission(
        broker="alpaca",
        strategy_instance_id=_SID,
        symbol="SPY",
    )

    assert started.admission.evaluated_at_ms == _T0 + 10_000
    run_id = _current_run_json(tmp_path)["run_id"]
    assert _run_json(tmp_path, run_id)["started_at_ms"] == started.admission.evaluated_at_ms
    await registry.stop("alpaca", _SID)


@pytest.mark.asyncio
async def test_deployed_bot_consumes_bars_and_logs_decisions(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    feed = _FakeFeed([_bar(_T0), _bar(_T0 + 60_000)], mode="hold")
    registry = _registry(tmp_path, feed)

    with caplog.at_level("INFO", logger="app.services.bot_runtime"):
        await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")
        await _wait_for(lambda: feed.bars_consumed == 2)
        await registry.stop("alpaca", _SID)

    decisions = [r for r in caplog.records if getattr(r, "action", None) == "bot_decision"]
    assert len(decisions) == 2
    assert decisions[0].decision == "HOLD"
    assert decisions[0].bar_start_ms == _T0


@pytest.mark.asyncio
async def test_deploy_while_running_is_refused(tmp_path: Path) -> None:
    feed = _FakeFeed([], mode="hold")
    registry = _registry(tmp_path, feed)
    await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")

    with pytest.raises(BotAlreadyRunningError):
        await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")

    await registry.stop("alpaca", _SID)


@pytest.mark.asyncio
async def test_deploy_after_stop_with_changed_configuration_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The second deploy below intentionally switches to "ema_crossover_signal"
    # with a changed symbol, to prove it's refused on the SID conflict --
    # not on this signal program's own (unrelated) parameters gate. See the
    # helper's docstring.
    admit_lean_parity_settings_for_start_admission(monkeypatch)
    feed = _FakeFeed([], mode="hold")
    registry = _registry(tmp_path, feed)
    await registry.deploy_with_admission(
        broker="alpaca",
        strategy_instance_id=_SID,
        symbol="SPY",
    )
    original_binding_bytes = (
        tmp_path / "live_state" / _SID / "strategy_instance.json"
    ).read_bytes()

    await registry.stop(broker="alpaca", strategy_instance_id=_SID)

    with pytest.raises(RunAdmissionRefusedError) as excinfo:
        await registry.deploy_with_admission(
            broker="alpaca",
            strategy_instance_id=_SID,
            strategy_key="ema_crossover_signal",
            symbol="QQQ",
        )
    assert excinfo.value.admission_decision.reason_code == "STRATEGY_INSTANCE_ALREADY_EXISTS"

    unchanged_binding_bytes = (
        tmp_path / "live_state" / _SID / "strategy_instance.json"
    ).read_bytes()
    assert unchanged_binding_bytes == original_binding_bytes


@pytest.mark.asyncio
async def test_deploy_without_feed_is_typed_503(tmp_path: Path) -> None:
    registry = _registry(tmp_path, None)

    with pytest.raises(MarketDataFeedUnavailableError):
        await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")


@pytest.mark.asyncio
async def test_deploy_rejects_unsafe_strategy_instance_id(tmp_path: Path) -> None:
    registry = _registry(tmp_path, _FakeFeed([], mode="hold"))

    with pytest.raises(InvalidStrategyInstanceIdError):
        await registry.deploy(broker="alpaca", strategy_instance_id="../escape", symbol="SPY")


@pytest.mark.asyncio
async def test_stop_writes_durable_intent_and_off_duty_evidence(tmp_path: Path) -> None:
    feed = _FakeFeed([_bar(_T0)], mode="hold")
    registry = _registry(tmp_path, feed)
    await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")

    view = await registry.stop("alpaca", _SID, reason="drill")

    assert view.running is False
    assert view.phase == "OFF_DUTY"
    assert view.desired_state == "STOPPED"
    assert view.duty_outcome is not None
    assert view.duty_outcome.kind == "STOPPED"
    assert view.duty_outcome.reason_code == "OPERATOR_STOP"

    # Raw artifacts agree without the runner.
    assert _desired_json(tmp_path)["desired_state"] == "STOPPED"
    lifecycle = _lifecycle_json(tmp_path)
    assert lifecycle["phase"] == "OFF_DUTY"
    assert lifecycle["active_run_id"] is None
    assert lifecycle["duty_outcome"]["kind"] == "STOPPED"


@pytest.mark.asyncio
async def test_stop_of_unknown_bot_is_404(tmp_path: Path) -> None:
    registry = _registry(tmp_path, _FakeFeed([], mode="hold"))

    with pytest.raises(UnknownBotError):
        await registry.stop("alpaca", "never-deployed")


@pytest.mark.asyncio
async def test_stop_does_not_finalize_or_reap_a_task_that_survives_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.services.bot_runner._STOP_TIMEOUT_S", 0.05)
    feed = _CancellationSuppressingFeed(bars=[])
    registry = _registry(tmp_path, feed=feed)
    await registry.deploy_with_admission(
        broker="alpaca",
        strategy_instance_id=_SID,
        strategy_key="deployment_validation",
        symbol="SPY",
        mode="log_only",
    )

    status = await registry.stop(broker="alpaca", strategy_instance_id=_SID)

    assert feed.cancellation_suppressed is True
    # The task must still be tracked — not reaped — while it's alive.
    assert _SID in registry._bots
    assert registry._bots[_SID].task.done() is False
    # status() must honestly report the bot as still running, since the
    # task is still alive; desired_state carries the STOPPED intent.
    assert status.running is True
    assert registry.desired_state(_SID) is DesiredState.STOPPED


@pytest.mark.asyncio
async def test_desired_state_reports_durable_intent(tmp_path: Path) -> None:
    registry = _registry(tmp_path, _FakeFeed([], mode="hold"))
    await registry.deploy_with_admission(
        broker="alpaca",
        strategy_instance_id=_SID,
        strategy_key="deployment_validation",
        symbol="SPY",
        mode="log_only",
    )
    assert registry.desired_state(_SID) is DesiredState.RUNNING

    await registry.stop(broker="alpaca", strategy_instance_id=_SID)
    assert registry.desired_state(_SID) is DesiredState.STOPPED


@pytest.mark.asyncio
async def test_crash_records_typed_evidence_and_reaps(tmp_path: Path) -> None:
    feed = _FakeFeed([_bar(_T0)], mode="crash", error=RuntimeError("boom"))
    registry = _registry(tmp_path, feed)
    await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")

    await _wait_for(lambda: not registry.status("alpaca", _SID).running)

    view = registry.status("alpaca", _SID)
    assert view.running is False  # reaped — never rendered healthy
    assert view.phase == "OFF_DUTY"
    assert view.duty_outcome is not None
    assert view.duty_outcome.kind == "CRASHED"
    assert view.duty_outcome.reason_code == "RuntimeError"
    # A terminal crash is fail-closed.  Leaving RUNNING behind strands the
    # off-duty bot because the panel's proof-gated Start path requires STOPPED.
    assert view.desired_state == "STOPPED"
    assert _desired_json(tmp_path)["desired_state"] == "STOPPED"

    lifecycle = _lifecycle_json(tmp_path)
    assert lifecycle["duty_outcome"]["kind"] == "CRASHED"


@pytest.mark.asyncio
async def test_feed_death_records_feed_death_crash(tmp_path: Path) -> None:
    feed = _FakeFeed([_bar(_T0)], mode="crash", error=MarketDataFeedError("gateway lost"))
    registry = _registry(tmp_path, feed)
    await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")

    await _wait_for(lambda: not registry.status("alpaca", _SID).running)

    view = registry.status("alpaca", _SID)
    assert view.duty_outcome is not None
    assert view.duty_outcome.kind == "CRASHED"
    assert view.duty_outcome.reason_code == "FEED_DEATH"
    assert view.desired_state == "STOPPED"


@pytest.mark.asyncio
async def test_count_complete_interruption_keeps_the_run_running(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """#1921: a reconnect the feed recovered from is not a duty outcome.

    The companion of ``test_feed_death_records_feed_death_crash`` above: a
    connection loss the feed survived -- every minute still accounted for --
    must leave the bot on duty, where before #1921 any interruption reached
    the runner as ``FEED_DEATH``.

    The wait is on the runner's own decision for the recovered bar, not on
    ``feed.bars_consumed``: the double increments that before it yields, so
    waiting on it can pass while the runner is still upstream of the bar and
    has had no chance to die on it.
    """
    feed = _FakeFeed([_bar(_T0), _bar(_T0 + 60_000)], mode="interrupt")
    registry = _registry(tmp_path, feed)

    with caplog.at_level("INFO", logger="app.services.bot_runtime"):
        await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")

        await _wait_for(
            lambda: len([r for r in caplog.records if getattr(r, "action", None) == "bot_decision"]) == 2
        )

    decided = [r for r in caplog.records if getattr(r, "action", None) == "bot_decision"]
    assert [r.bar_start_ms for r in decided] == [_T0, _T0 + 60_000]
    view = registry.status("alpaca", _SID)
    assert view.running is True
    assert view.duty_outcome is None
    await registry.stop("alpaca", _SID)


@pytest.mark.asyncio
async def test_kill_without_stop_intent_is_exited_unverified(tmp_path: Path) -> None:
    feed = _FakeFeed([], mode="hold")
    registry = _registry(tmp_path, feed)
    await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")

    managed_task = registry._bots[_SID].task
    managed_task.cancel()  # a kill: no stop intent recorded
    await asyncio.wait({managed_task})
    await _wait_for(lambda: _SID not in registry._bots)

    view = registry.status("alpaca", _SID)
    assert view.running is False
    assert view.duty_outcome is not None
    assert view.duty_outcome.kind == "EXITED_UNVERIFIED"
    assert view.duty_outcome.reason_code == "CANCELLED_WITHOUT_STOP_INTENT"
    assert view.desired_state == "STOPPED"


@pytest.mark.asyncio
async def test_bar_stream_end_is_exited_unverified(tmp_path: Path) -> None:
    feed = _FakeFeed([_bar(_T0)], mode="finite")
    registry = _registry(tmp_path, feed)
    await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")

    await _wait_for(lambda: not registry.status("alpaca", _SID).running)

    view = registry.status("alpaca", _SID)
    assert view.duty_outcome is not None
    assert view.duty_outcome.kind == "EXITED_UNVERIFIED"
    assert view.duty_outcome.reason_code == "BAR_STREAM_ENDED"
    assert view.desired_state == "STOPPED"


class _CustodyThatAcquiresExposure:
    """Flat while the bot deploys, exposed by the time Retire is clicked.

    Models the race the commit-time re-proof exists for: a fill lands between
    the panel authoring an enabled Retire and the operator clicking it.
    """

    def __init__(self) -> None:
        self.exposed = False

    @asynccontextmanager
    async def __call__(self, sid: str) -> AsyncIterator[ClerkCustodySnapshot]:
        flat = _flat_custody_snapshot(sid)
        if not self.exposed:
            yield flat
            return
        yield flat.model_copy(
            update={
                "exposure": CustodyExposureFact(
                    state="non_zero", positions={"APPL": 3.0}
                ),
                "working_orders": CustodyCountFact(state="non_zero", count=1),
            }
        )


@pytest.mark.asyncio
async def test_retire_reproves_custody_and_refuses_to_strand_exposure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retirement is irreversible, so it re-proves its own preconditions.

    The panel's guard refuses exposure and working orders, but that decision
    is authored at presentation time and the operator clicks later. Between
    those two moments a fill can land. Re-checking only `running` at the
    commit would retire a registration that still holds custody -- and there
    is no undo. The committing operation answers the same shared rule again
    against a freshly reconciled snapshot (#1778, S5).
    """
    feed = _FakeFeed([_bar(_T0)], mode="crash", error=RuntimeError("boom"))
    custody = _CustodyThatAcquiresExposure()
    registry = _registry(tmp_path, feed, start_custody_guard=custody)
    await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")
    await _wait_for(lambda: not registry.status("alpaca", _SID).running)

    # The registration outlived its strategy: deployable when created, its key
    # since removed. That is what makes Retire eligible at all -- so reaching
    # the custody guard proves the commit-time re-proof, not an earlier
    # refusal.
    monkeypatch.setattr(bot_runner_module, "_STRATEGY_REGISTRY", {})
    custody.exposed = True

    with pytest.raises(BotRunnerError) as blocked:
        await registry.retire("alpaca", _SID, updated_by="operator")

    assert "custody" in str(blocked.value).lower()
    assert registry.status("alpaca", _SID).phase != "RETIRED"


@pytest.mark.asyncio
async def test_unresolved_intent_mid_sweep_evaluation_reports_wait_not_intervene(
    tmp_path: Path,
) -> None:
    """#1808: with the sweep's evaluation observation wired, an unresolved
    intent during post-outage settling refuses Start as
    ``RECOVERY_SWEEP_EVALUATING`` (wait) rather than ``RECOVERY_UNCERTAIN``
    (intervene). The refusal is still a refusal — only the prose changes."""
    from app.broker.alpaca.clerk.models import RecoveryEvaluationObservation
    from app.services.bot_runner_errors import RecoverySweepEvaluatingError
    from app.services.bot_start_admission import RECOVERY_EVALUATION_WINDOW_MS

    registry = _registry(tmp_path, _FakeFeed([], mode="hold"))

    async def no_op() -> None:
        return None

    async def one_unresolved_intent(subject_id: str | None) -> int:
        return 1 if subject_id in (None, f"bot:{_SID}") else 0

    now = bot_runner_module.now_ms_utc()
    evaluating = RecoveryEvaluationObservation(
        evaluation_started_at_ms=now,
        last_pass_completed_at_ms=None,
    )

    await registry.run_boot_recovery(
        recover=no_op,
        reconcile=no_op,
        unresolved_intents_probe=one_unresolved_intent,
        recovery_evaluation=lambda: evaluating,
    )

    preview = await registry.preview_start_admission(
        broker="alpaca",
        strategy_instance_id=_SID,
        symbol="SPY",
    )

    assert preview.allowed is False
    assert preview.reason_code == "RECOVERY_SWEEP_EVALUATING"
    with pytest.raises(RecoverySweepEvaluatingError) as refused:
        await registry.deploy(
            broker="alpaca",
            strategy_instance_id=_SID,
            symbol="SPY",
        )
    assert refused.value.admission_decision is not None
    assert refused.value.admission_decision.reason_code == preview.reason_code

    # Once the evaluation window has lapsed with the intent still unresolved,
    # the same wiring reports the intervene state — the wait-state cannot
    # outlive the sweep evaluation that produced it.
    expired = RecoveryEvaluationObservation(
        evaluation_started_at_ms=now - RECOVERY_EVALUATION_WINDOW_MS - 1,
        last_pass_completed_at_ms=now - RECOVERY_EVALUATION_WINDOW_MS,
    )
    await registry.run_boot_recovery(
        recover=no_op,
        reconcile=no_op,
        unresolved_intents_probe=one_unresolved_intent,
        recovery_evaluation=lambda: expired,
    )

    settled = await registry.preview_start_admission(
        broker="alpaca",
        strategy_instance_id=_SID,
        symbol="SPY",
    )
    assert settled.reason_code == "RECOVERY_UNCERTAIN"


class _CustodyThatFreezes:
    """A freeze lands between the panel arming Archive and the operator clicking.

    Under a freeze the Clerk cannot observe the broker, so it reports zero
    exposure because it knows nothing -- not because the bot is flat. Archive's
    enabling proof *is* that reading, so a snapshot it cannot vouch for must
    refuse the command rather than satisfy it.
    """

    def __init__(self) -> None:
        self.frozen = False

    @asynccontextmanager
    async def __call__(self, sid: str) -> AsyncIterator[ClerkCustodySnapshot]:
        flat = _flat_custody_snapshot(sid)
        if not self.frozen:
            yield flat
            return
        yield flat.model_copy(
            update={
                "freeze": AccountFreezeState(
                    active=True,
                    category="ACCOUNT_STATE_UNPROVABLE",
                    explanation="Broker observation is unavailable.",
                    next_step="Restore broker observation and reconcile.",
                    observed_at_ms=_RTH_MS,
                )
            }
        )


@pytest.mark.asyncio
async def test_archive_reproves_custody_and_refuses_to_strand_exposure(
    tmp_path: Path,
) -> None:
    """Archive is irreversible, so it re-proves its own precondition.

    Same race as retire's: the panel authors an enabled Archive against a
    projected snapshot and the operator clicks later. A fill landing in
    between must refuse the command, not be stranded by it.
    """
    feed = _FakeFeed([_bar(_T0)], mode="crash", error=RuntimeError("boom"))
    custody = _CustodyThatAcquiresExposure()
    registry = _registry(tmp_path, feed, start_custody_guard=custody)
    await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")
    await _wait_for(lambda: not registry.status("alpaca", _SID).running)

    custody.exposed = True

    with pytest.raises(BotRunnerError) as blocked:
        await registry.archive("alpaca", _SID, updated_by="operator")

    assert "custody" in str(blocked.value).lower()
    assert registry.status("alpaca", _SID).phase != "RETIRED"


@pytest.mark.asyncio
async def test_archive_refuses_when_the_clerk_cannot_prove_flatness(
    tmp_path: Path,
) -> None:
    """A freeze at commit time refuses even though exposure reads zero."""
    feed = _FakeFeed([_bar(_T0)], mode="crash", error=RuntimeError("boom"))
    custody = _CustodyThatFreezes()
    registry = _registry(tmp_path, feed, start_custody_guard=custody)
    await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")
    await _wait_for(lambda: not registry.status("alpaca", _SID).running)

    custody.frozen = True

    with pytest.raises(BotRunnerError) as blocked:
        await registry.archive("alpaca", _SID, updated_by="operator")

    assert "prove" in str(blocked.value).lower()
    assert registry.status("alpaca", _SID).phase != "RETIRED"
