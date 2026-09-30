"""The runner's side of a bot's owner-set end (#2607).

The end is the bot's desired state: Deploy records it, the owner can change it
while the bot runs -- with no restart and no seal touched -- and the Clerk
reads it through the runner's end schedule. When the Clerk fences the bot at
its end, the runner stops the bot's process the way the panel's Stop does
after its own STOP, proves that stop from the Clerk's own pass, and records
why. An operator's Stop ends the bot and its end with it; a crash leaves the
end for the Clerk to carry out.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.broker.alpaca.clerk import get_alpaca_clerk, set_alpaca_clerk
from app.broker.alpaca.clerk.models import InstanceCustodyProof
from app.broker.alpaca.clerk.sqlite import recovery_execution
from app.broker.alpaca.clerk.sqlite.recovery_execution import RecoveryExecutionRequest, execute_recovery_action
from app.broker.alpaca.clerk.sqlite.scheduled_end import ScheduledEnd
from app.engine.live.desired_state import DesiredState, DesiredStateRepo, stable_desired_state_path
from app.lean_sidecar.trading_calendar import session_close_ms_utc
from app.schemas.bot_end import BotEnd, BotEndInput
from app.services.bot_binding_repository import BrokerBotBinding
from app.services.bot_carryover import configuration_hash
from app.services.bot_end import BotEndRefused, when_words
from app.services.bot_run_terminal import prove_end_stop_outcome
from app.services.bot_runner import (
    BotTaskRegistry,
    UnknownBotError,
    alpaca_v1_action_plan,
    get_bot_task_registry,
    set_bot_task_registry,
)
from app.utils.timestamps import now_ms_utc
from tests._helpers.bot_runner.custody import _SID, _T0, _custody_proof, _registry
from tests._helpers.bot_runner.doubles import _CustodyClerk, _FakeFeed
from tests._helpers.canary_admission import admit_canary_pairing
from tests._helpers.exit_terms import DEPLOY_EXIT_TERMS
from tests._helpers.session_clock import IN_SESSION_DAY

from ._support import _bar, _wait_for

# The runner suites pin the wall clock at 09:31 ET on IN_SESSION_DAY; the
# default end is that session's close minus one minute.
_END = BotEnd(end_at_ms=session_close_ms_utc(IN_SESSION_DAY) - 60_000, end_action="SELL")  # 15:59 ET


class _Clock:
    """The pinned wall clock, which a test moves forward to a bot's end."""

    def __init__(self) -> None:
        self.offset_ms = 0

    def __call__(self) -> int:
        return now_ms_utc() + self.offset_ms

    def move_to(self, instant_ms: int) -> None:
        self.offset_ms = instant_ms - now_ms_utc()


class _EndClerk(_CustodyClerk):
    """A Clerk whose pass fences every bot the registry names due, as the real pass does.

    ``fences`` off models a pass that ran but reached no run; ``fail_next`` a
    pass that failed. ``passes`` counts its reconciliation passes and
    ``fresh_proofs`` the custody proofs that would each run one more.
    ``published`` off models no pass yet having seen the bot's every transition.
    """

    def __init__(self, registry: BotTaskRegistry, *, fences: bool = True) -> None:
        super().__init__(_custody_proof(exposure={}))
        self.registry = registry
        self.fences = fences
        self.fail_next = False
        self.passes = 0
        self.fresh_proofs = 0
        self.published_reads = 0
        self.published = True

    async def reconcile_once(self):  # type: ignore[override]
        self.passes += 1
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("the pass failed")
        if self.fences:
            for sid, run_id in list(self.active_runs.items()):
                await self.stop_strategy_run(strategy_instance_id=sid, run_id=run_id, reason="scheduled_end")
                self.registry.stop_bot_at_its_end(sid, run_id)
        return await super().reconcile_once()

    async def prove_instance_custody(self, sid: str) -> InstanceCustodyProof:
        self.fresh_proofs += 1
        return self.proof.model_copy(update={"strategy_instance_id": sid})

    async def published_custody(self, sid: str) -> InstanceCustodyProof | None:
        self.published_reads += 1
        if not self.published:
            return None
        return self.proof.model_copy(update={"strategy_instance_id": sid})


def _desired_json(tmp_path: Path, sid: str = _SID) -> dict:
    return json.loads((tmp_path / "live_state" / sid / "desired_state.json").read_text(encoding="utf-8"))


def _binding_bytes(tmp_path: Path, sid: str = _SID) -> bytes:
    return (tmp_path / "live_state" / sid / "strategy_instance.json").read_bytes()


async def _deploy(
    registry: BotTaskRegistry, sid: str = _SID, *, end: BotEnd | None = _END, mode: str = "log_only",
) -> None:
    await registry.deploy(
        exit_terms=DEPLOY_EXIT_TERMS, broker="alpaca", strategy_instance_id=sid, symbol="SPY", end=end, mode=mode,
    )


async def _deployed(tmp_path: Path, clock: _Clock, *, end: BotEnd | None = _END) -> BotTaskRegistry:
    registry = _registry(tmp_path, _FakeFeed([_bar(_T0)], mode="hold"), now_ms=clock)
    await _deploy(registry, end=end)
    return registry


def _end_clerk(registry: BotTaskRegistry, *, fences: bool = True) -> _EndClerk:
    """Install the Clerk bots are deployed on; the suite's own fixture uninstalls it."""
    clerk = _EndClerk(registry, fences=fences)
    set_alpaca_clerk(clerk)
    return clerk


def _sell(end_at_ms: int | None) -> BotEndInput:
    return BotEndInput(end_at_ms=end_at_ms, end_action="SELL")


# ── Deploy records the end ───────────────────────────────────────────────────


async def test_deploy_records_the_owners_end_in_the_bots_desired_state(tmp_path: Path) -> None:
    await _deployed(tmp_path, _Clock())

    desired = _desired_json(tmp_path)
    assert desired["desired_state"] == "RUNNING"
    assert desired["end"] == {"end_at_ms": _END.end_at_ms, "end_action": "SELL", "carried_out_at_ms": None}


async def test_the_end_never_enters_the_binding_or_its_configuration_hash(tmp_path: Path) -> None:
    await _deployed(tmp_path, _Clock())
    with_end = _binding_bytes(tmp_path)

    assert b"end_at_ms" not in with_end
    assert b"end_action" not in with_end


async def test_deploy_with_no_end_records_no_end(tmp_path: Path) -> None:
    registry = await _deployed(tmp_path, _Clock(), end=None)

    assert _desired_json(tmp_path)["end"] is None
    assert registry.pending_ends([_SID]) == []


# ── the owner edits a running bot's end ──────────────────────────────────────


async def test_editing_a_running_bots_end_takes_effect_without_a_restart_and_changes_no_seal(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    registry = await _deployed(tmp_path, clock)
    task = registry._bots[_SID].task
    binding = registry.binding_for_control("alpaca", _SID)
    sealed = (_binding_bytes(tmp_path), configuration_hash(binding), binding.sealed_program, binding.exit_terms)
    later = BotEnd(end_at_ms=_END.end_at_ms - 3_600_000, end_action="KEEP")

    view = await registry.edit_bot_end(
        "alpaca", _SID, BotEndInput(end_at_ms=later.end_at_ms, end_action="KEEP"), updated_by="operator",
    )

    assert view.end_at_ms == later.end_at_ms
    assert view.headline == f"Ends {when_words(later.end_at_ms, now_ms=clock())} · keeps its shares"
    assert registry.pending_ends([_SID]) == [ScheduledEnd(strategy_instance_id=_SID, end=later)]
    # No restart: the same task runs the same run.
    assert registry._bots[_SID].task is task and not task.done()
    assert registry.status("alpaca", _SID).running is True
    # No seal touched: the binding file, its hash, the program seal and the exit terms.
    after = registry.binding_for_control("alpaca", _SID)
    assert (_binding_bytes(tmp_path), configuration_hash(after), after.sealed_program, after.exit_terms) == sealed


async def test_an_edit_can_remove_the_end(tmp_path: Path) -> None:
    registry = await _deployed(tmp_path, _Clock())

    view = await registry.edit_bot_end("alpaca", _SID, _sell(None), updated_by="operator")

    assert view.status == "no_end"
    assert registry.pending_ends([_SID]) == []


async def test_an_edit_is_refused_in_plain_words_when_the_end_is_outside_regular_hours(tmp_path: Path) -> None:
    registry = await _deployed(tmp_path, _Clock())

    with pytest.raises(BotEndRefused, match="The end must fall within regular hours"):
        await registry.edit_bot_end("alpaca", _SID, _sell(_END.end_at_ms + 3_600_000), updated_by="operator")

    assert registry.pending_ends([_SID]) == [ScheduledEnd(strategy_instance_id=_SID, end=_END)]


async def test_an_end_that_has_come_cannot_be_edited(tmp_path: Path) -> None:
    clock = _Clock()
    registry = await _deployed(tmp_path, clock)
    clock.move_to(_END.end_at_ms + 1_000)

    with pytest.raises(BotEndRefused, match="This bot's end has come"):
        await registry.edit_bot_end("alpaca", _SID, _sell(None), updated_by="operator")


async def test_a_stopped_bots_end_cannot_be_edited(tmp_path: Path) -> None:
    registry = await _deployed(tmp_path, _Clock())
    await registry.stop("alpaca", _SID)

    with pytest.raises(BotEndRefused, match="This bot has stopped"):
        await registry.edit_bot_end("alpaca", _SID, _sell(_END.end_at_ms), updated_by="operator")


# ── Stop and crash ───────────────────────────────────────────────────────────


async def test_an_operators_stop_ends_the_bot_and_its_end(tmp_path: Path) -> None:
    """Stopping doesn't sell its shares (#2605's Stop copy); a later scheduled sale would."""
    registry = await _deployed(tmp_path, _Clock())

    await registry.stop("alpaca", _SID)

    assert _desired_json(tmp_path)["end"] is None
    assert registry.pending_ends([_SID]) == []
    assert "a Stop cancels any end" in registry.bot_end("alpaca", _SID).explanation


async def test_a_crash_leaves_the_end_for_the_clerk_to_carry_out(tmp_path: Path) -> None:
    registry = _registry(tmp_path, _FakeFeed([_bar(_T0)], mode="crash", error=RuntimeError("boom")), now_ms=_Clock())
    await _deploy(registry)

    await _wait_for(lambda: not registry.status("alpaca", _SID).running)

    assert _desired_json(tmp_path)["desired_state"] == "STOPPED"
    assert registry.pending_ends([_SID]) == [ScheduledEnd(strategy_instance_id=_SID, end=_END)]
    view = registry.bot_end("alpaca", _SID)
    assert view.status == "scheduled"
    assert view.editable is True


async def test_the_lane_wide_stop_cancels_the_end_a_crash_kept(tmp_path: Path) -> None:
    """#2607 review: a crash records STOPPED and keeps the end for the Clerk. The lane-wide
    Stop -- installation migration's stop-all, lane retirement, the budget cutover -- is the
    operator's Stop, so it ends that end too, though the intent it finds is already STOPPED:
    the end moves with the volume, and the new host's Clerk would sell at the end time."""
    registry = _registry(tmp_path, _FakeFeed([_bar(_T0)], mode="crash", error=RuntimeError("boom")), now_ms=_Clock())
    await _deploy(registry)
    await _wait_for(lambda: not registry.status("alpaca", _SID).running)
    crashed_reason = _desired_json(tmp_path)["reason"]

    outcome = await registry.stop_every_running_bot(updated_by="inkant", reason="lane_stop_all")

    assert registry.pending_ends([_SID]) == []
    assert _desired_json(tmp_path)["end"] is None
    # The intent was STOPPED already: it keeps the crash's record, and the receipt lists no change.
    assert (_desired_json(tmp_path)["desired_state"], _desired_json(tmp_path)["reason"]) == ("STOPPED", crashed_reason)
    assert (outcome.intent_stopped, outcome.refused) == ((), ())
    assert _registry(tmp_path, None).pending_ends([_SID]) == [], "a restart revived the cancelled end"


async def test_the_lane_wide_stop_cancels_the_end_of_an_idle_bot_that_never_stopped(tmp_path: Path) -> None:
    """An idle bot whose intent still says RUNNING (a restart that never resumed it) is recorded
    STOPPED by the lane-wide Stop, and its end is cancelled with it."""
    registry = _registry(tmp_path, None, now_ms=_Clock())
    DesiredStateRepo(stable_desired_state_path(tmp_path, "bot-idle")).set(
        DesiredState.RUNNING, updated_by="earlier-operator", now_ms=_T0, reason="deploy", end=_END,
    )

    outcome = await registry.stop_every_running_bot(updated_by="inkant", reason="lane_stop_all")

    assert registry.pending_ends(["bot-idle"]) == []
    desired = _desired_json(tmp_path, "bot-idle")
    assert (desired["desired_state"], desired["reason"], desired["end"]) == ("STOPPED", "lane_stop_all", None)
    assert [(bot.strategy_instance_id, bot.previous_desired_state) for bot in outcome.intent_stopped] == [
        ("bot-idle", "RUNNING")
    ]


class _PanelFacade:
    """The account facade as the panel's Stop drives it, with the end watch looking right after its STOP.

    The watch runs every few seconds on its own task; this places its look in
    the gap between the Clerk's STOP and the runner's process stop, and lets a
    process stop the look started take the bot's lock first.
    """

    account_id = "paper-account"
    repository = SimpleNamespace(get_command=lambda _command_id: None)

    def __init__(self, clerk: _EndClerk, registry: BotTaskRegistry) -> None:
        self.clerk = clerk
        self.registry = registry

    async def stop_strategy_run(self, *, strategy_instance_id: str, run_id: str, reason: str | None = None):
        await self.clerk.stop_strategy_run(strategy_instance_id=strategy_instance_id, run_id=run_id, reason=reason)
        await self.registry.carry_out_due_ends()
        await asyncio.sleep(0)
        return SimpleNamespace(created=True, command=SimpleNamespace(command_id="cmd:stop", updated_at_ms=_T0))


async def test_the_panels_stop_as_the_end_comes_cancels_the_end_before_its_stop_commits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#2607 review: the owner's Stop sells nothing at the end time. The panel's Stop commits
    the Clerk STOP and then stops the process; an end watch looking in between used to read
    the run as stopped at its end, keep the end, and leave the next pass to sell. The end is
    cancelled before the STOP commits -- durably, so no restart revives it."""
    clock = _Clock()
    registry = _registry(tmp_path, _FakeFeed([_bar(_T0)], mode="hold"), now_ms=clock)
    clerk = _end_clerk(registry, fences=False)
    await _deploy(registry)
    run_id = registry._bots[_SID].binding.run_id
    clock.move_to(_END.end_at_ms + 2_000)  # the end has come; no Clerk pass has fenced the bot yet
    monkeypatch.setattr(
        recovery_execution, "recheck_recovery_action", lambda *_args, **_kwargs: SimpleNamespace(execution_ref=run_id),
    )

    async def context() -> SimpleNamespace:
        return SimpleNamespace(strategy_instance_id=_SID)

    assert get_bot_task_registry() is None
    set_bot_task_registry(registry)
    try:
        await execute_recovery_action(
            _PanelFacade(clerk, registry),  # type: ignore[arg-type]
            request=RecoveryExecutionRequest(
                action_id="stop_bot_decisions", concurrency_token="token", execution_ref=run_id, reason="operator stop",
            ),
            current_context=context,  # type: ignore[arg-type]
        )
    finally:
        set_bot_task_registry(None)
    await asyncio.gather(*registry._end_stop_tasks.values())

    assert registry.pending_ends([_SID]) == []
    outcome = registry.status("alpaca", _SID).duty_outcome
    assert outcome is not None and (outcome.kind, outcome.reason_code) == ("STOPPED", "OPERATOR_STOP")
    assert _registry(tmp_path, None).pending_ends([_SID]) == [], "a restart revived the cancelled end"


async def test_the_end_watch_leaves_a_run_an_operator_stopped_to_that_stop(tmp_path: Path) -> None:
    """#2607 review: only the Clerk's STOP at the end is the end's to finish. A run another
    Stop ended is never re-stopped "at its end" -- which would keep the end for the next
    pass to sell -- and the operator's Stop of the process then cancels it."""
    clock = _Clock()
    registry = _registry(tmp_path, _FakeFeed([_bar(_T0)], mode="hold"), now_ms=clock)
    clerk = _end_clerk(registry, fences=False)
    await _deploy(registry)
    run_id = registry._bots[_SID].binding.run_id
    clock.move_to(_END.end_at_ms + 2_000)

    await clerk.stop_strategy_run(strategy_instance_id=_SID, run_id=run_id, reason="operator stop")
    await registry.carry_out_due_ends()
    await asyncio.sleep(0)
    await registry.stop_after_durable_clerk_stop("alpaca", _SID, updated_by="operator_recovery", reason="op")
    await asyncio.gather(*registry._end_stop_tasks.values())

    assert registry.pending_ends([_SID]) == []
    outcome = registry.status("alpaca", _SID).duty_outcome
    assert outcome is not None and (outcome.kind, outcome.reason_code) == ("STOPPED", "OPERATOR_STOP")


async def test_an_operators_stop_of_a_bot_whose_process_is_gone_cancels_its_end(tmp_path: Path) -> None:
    """#2607 review: Stop is offered while the Clerk run is ACTIVE -- after a crash whose own
    STOP failed, say -- though this runner has no process left to stop. The Stop still
    cancels the end, and durably."""
    registry = _registry(tmp_path, _FakeFeed([_bar(_T0)], mode="crash", error=RuntimeError("boom")), now_ms=_Clock())
    await _deploy(registry)
    await _wait_for(lambda: not registry.status("alpaca", _SID).running)
    assert registry.pending_ends([_SID]) == [ScheduledEnd(strategy_instance_id=_SID, end=_END)]

    with pytest.raises(UnknownBotError):
        await registry.stop_after_durable_clerk_stop("alpaca", _SID, updated_by="operator_recovery", reason="op")

    assert registry.pending_ends([_SID]) == []
    assert _registry(tmp_path, None).pending_ends([_SID]) == [], "a restart revived the cancelled end"


async def test_an_operators_stop_with_no_process_records_the_same_stop_as_the_lane_wide_one(tmp_path: Path) -> None:
    """Every operator Stop makes one record: a bot this runner has no process for, whose intent
    still says RUNNING, is recorded STOPPED with its end cancelled; a bot it never deployed is
    given no desired state."""
    registry = _registry(tmp_path, None, now_ms=_Clock())
    DesiredStateRepo(stable_desired_state_path(tmp_path, "bot-idle")).set(
        DesiredState.RUNNING, updated_by="earlier-operator", now_ms=_T0, reason="deploy", end=_END,
    )

    for sid in ("bot-idle", "never-deployed"):
        with pytest.raises(UnknownBotError):
            await registry.stop_after_durable_clerk_stop("alpaca", sid, updated_by="operator_recovery", reason="op")

    desired = _desired_json(tmp_path, "bot-idle")
    assert (desired["desired_state"], desired["reason"], desired["end"]) == ("STOPPED", "op", None)
    assert DesiredStateRepo(stable_desired_state_path(tmp_path, "never-deployed")).read() is None


async def test_a_stop_goes_on_when_the_end_it_cancels_cannot_be_read(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """The panel's Stop cancels the end before its STOP fences the bot. An unreadable desired
    state has no end the Clerk can carry out, so it never keeps the STOP from landing; it is said."""
    registry = _registry(tmp_path, None)
    broken = stable_desired_state_path(tmp_path, _SID)
    broken.parent.mkdir(parents=True)
    broken.write_text("not json", encoding="utf-8")

    await registry.cancel_end(_SID, updated_by="operator_recovery")

    [said] = [record for record in caplog.records if getattr(record, "action", None) == "bot_end_cancel_unreadable"]
    # A repaired file would carry its end out after all: the repair must clear it.
    assert "clear its end when repairing the file" in said.getMessage()


def test_cancelling_the_end_of_a_bot_with_no_desired_state_writes_none(tmp_path: Path) -> None:
    """A Stop of a bot this runner never deployed leaves no desired state behind to read as RUNNING."""
    repo = DesiredStateRepo(stable_desired_state_path(tmp_path, "never-deployed"))

    assert repo.cancel_end(updated_by="operator_recovery", now_ms=_T0) is None
    assert repo.read() is None


# ── the Clerk's end schedule ─────────────────────────────────────────────────


async def test_the_clerk_stopping_a_bot_at_its_end_stops_its_process_with_the_ends_reason(tmp_path: Path) -> None:
    registry = await _deployed(tmp_path, _Clock())
    managed = registry._bots[_SID]
    run_id = managed.binding.run_id
    # What the Clerk's pass commits before it asks: Stop's STOP, at the bot's end.
    await get_alpaca_clerk().stop_strategy_run(strategy_instance_id=_SID, run_id=run_id, reason="scheduled_end")

    registry.stop_bot_at_its_end(_SID, run_id)

    assert not managed.run_gate.is_set(), "Stop's fence was not raised at once"
    await asyncio.gather(*registry._end_stop_tasks.values())
    view = registry.status("alpaca", _SID)
    assert view.running is False
    assert view.desired_state == "STOPPED"
    assert view.duty_outcome is not None
    assert (view.duty_outcome.kind, view.duty_outcome.reason_code) == ("STOPPED", "SCHEDULED_END")
    # The end is the Clerk's to record carried out, not the Stop's to erase.
    assert registry.pending_ends([_SID]) == [ScheduledEnd(strategy_instance_id=_SID, end=_END)]


async def test_stopping_at_the_end_ignores_a_run_that_is_not_the_bots_current_one(tmp_path: Path) -> None:
    registry = await _deployed(tmp_path, _Clock())

    registry.stop_bot_at_its_end(_SID, "some-older-run")
    await asyncio.sleep(0)

    assert registry.status("alpaca", _SID).running is True
    assert registry._bots[_SID].run_gate.is_set()


async def test_recording_the_end_carried_out_ends_the_pending_end(tmp_path: Path) -> None:
    registry = await _deployed(tmp_path, _Clock())

    registry.record_end_carried_out(ScheduledEnd(strategy_instance_id=_SID, end=_END), at_ms=_END.end_at_ms + 5_000)

    assert registry.pending_ends([_SID]) == []
    assert _desired_json(tmp_path)["end"]["carried_out_at_ms"] == _END.end_at_ms + 5_000
    assert registry.bot_end("alpaca", _SID).status == "ended"


def test_pending_ends_skips_a_bot_it_cannot_read_and_a_bot_with_no_file(tmp_path: Path) -> None:
    registry = _registry(tmp_path, None)
    good = stable_desired_state_path(tmp_path, "good-bot")
    DesiredStateRepo(good).set_end(_END, updated_by="operator", now_ms=1)
    bad = stable_desired_state_path(tmp_path, "bad-bot")
    bad.parent.mkdir(parents=True)
    bad.write_text("not json", encoding="utf-8")

    ends = registry.pending_ends(["bad-bot", "good-bot", "no-file-bot", "../escape"])

    assert ends == [ScheduledEnd(strategy_instance_id="good-bot", end=_END)]


def test_an_unreadable_end_is_said_once_not_on_every_pass(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """#2607 review: the Clerk reads the schedule every 15 s; the broken file is said once, and again when fixed."""
    caplog.set_level(logging.INFO, logger="app.services.bot_runner")
    registry = _registry(tmp_path, None)
    bad = stable_desired_state_path(tmp_path, "bad-bot")
    bad.parent.mkdir(parents=True)
    bad.write_text("not json", encoding="utf-8")

    for _ in range(3):
        registry.pending_ends(["bad-bot"])
    bad.unlink()  # repaired: the bot has no desired state yet
    registry.pending_ends(["bad-bot"])

    actions = [getattr(record, "action", None) for record in caplog.records]
    assert actions.count("bot_end_unreadable") == 1
    assert actions.count("bot_end_readable_again") == 1


def test_bot_end_of_an_unknown_bot_is_refused(tmp_path: Path) -> None:
    with pytest.raises(UnknownBotError):
        _registry(tmp_path, None).bot_end("alpaca", "never-deployed")


# ── the end watch: a running bot's end carried forward from the runner ──────


async def test_the_end_watch_asks_once_per_end_and_again_only_after_a_failed_pass(tmp_path: Path) -> None:
    clock = _Clock()
    registry = _registry(tmp_path, _FakeFeed([_bar(_T0)], mode="hold"), now_ms=clock)
    clerk = _end_clerk(registry, fences=False)
    await _deploy(registry)
    clerk.fail_next = True

    clock.move_to(_END.end_at_ms)
    await registry.carry_out_due_ends()  # fails: asked again
    await registry.carry_out_due_ends()  # runs
    await registry.carry_out_due_ends()  # the periodic sweep's to finish now

    assert clerk.passes == 2


async def test_the_end_watch_asks_for_a_pass_only_once_a_running_bots_end_has_come(tmp_path: Path) -> None:
    clock = _Clock()
    registry = _registry(tmp_path, _FakeFeed([_bar(_T0)], mode="hold"), now_ms=clock)
    clerk = _end_clerk(registry)
    await _deploy(registry)

    await registry.carry_out_due_ends()
    assert clerk.passes == 0

    clock.move_to(_END.end_at_ms)
    await registry.carry_out_due_ends()
    await registry.carry_out_due_ends()  # fenced by the first pass: not asked again

    assert clerk.passes == 1
    await _wait_for(lambda: not registry.status("alpaca", _SID).running)


async def test_bots_ending_on_one_pass_are_proven_by_that_pass_with_no_pass_of_their_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#2607 review: every trading bot on the default end stops in the same minute. Their
    stops read the Clerk's pass that ended them; a reconcile each would be one whole
    account pass per bot -- four Alpaca reads apiece -- at the close."""
    admit_canary_pairing(monkeypatch, "deployment_validation", "paper-account")
    clock = _Clock()
    registry = _registry(tmp_path, _FakeFeed([_bar(_T0)], mode="hold"), now_ms=clock)
    clerk = _end_clerk(registry)
    sids = ["end-bot-a", "end-bot-b", "end-bot-c"]
    for sid in sids:
        await _deploy(registry, sid, mode="trade")

    clock.move_to(_END.end_at_ms)
    await registry.carry_out_due_ends()
    await asyncio.gather(*registry._end_stop_tasks.values())

    assert clerk.passes == 1
    assert clerk.fresh_proofs == 0, "a stop at its end ran a reconciliation pass of its own"
    assert clerk.published_reads == len(sids), "each stop is proven by the pass that ended it"
    for sid in sids:
        outcome = registry.status("alpaca", sid).duty_outcome
        assert outcome is not None and (outcome.kind, outcome.reason_code) == ("STOPPED", "SCHEDULED_END")


async def test_the_end_watch_stops_a_bot_whose_run_the_clerk_already_stopped(tmp_path: Path) -> None:
    """#2607 review: the Clerk's pass stopped the run at its end, but the process stop
    it asked for never came. The watch stops the process -- with no pass of its own --
    rather than leaving a bot running whose every decision the Clerk refuses."""
    clock = _Clock()
    registry = _registry(tmp_path, _FakeFeed([_bar(_T0)], mode="hold"), now_ms=clock)
    clerk = _end_clerk(registry, fences=False)
    await _deploy(registry)
    run_id = registry._bots[_SID].binding.run_id
    await clerk.stop_strategy_run(strategy_instance_id=_SID, run_id=run_id, reason="scheduled_end")

    clock.move_to(_END.end_at_ms + 10_000)
    await registry.carry_out_due_ends()
    await asyncio.gather(*registry._end_stop_tasks.values())

    assert clerk.passes == 0
    view = registry.status("alpaca", _SID)
    assert view.running is False
    assert view.duty_outcome is not None and view.duty_outcome.reason_code == "SCHEDULED_END"


async def test_a_missing_clerk_is_said_once_by_the_end_watch(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """#2607 review: the watch looks every 5 s; an absent Clerk is one warning, not a stack trace per look."""
    clock = _Clock()
    registry = await _deployed(tmp_path, clock)
    set_alpaca_clerk(None)
    clock.move_to(_END.end_at_ms)

    for _ in range(3):
        await registry.carry_out_due_ends()

    failures = [record for record in caplog.records if getattr(record, "action", None) == "bot_end_pass_failed"]
    loud = [record for record in failures if record.levelno >= logging.WARNING]
    assert len(loud) == 1
    assert loud[0].exc_info is None
    assert registry.status("alpaca", _SID).running is True


async def test_one_bot_the_end_watch_cannot_read_never_stalls_the_others(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """#2607 review (#2363's rule): an unexpected failure reading one bot's run -- a locked
    database -- is that bot's, said with its stack; the watch still reaches every bot after it."""
    clock = _Clock()
    registry = _registry(tmp_path, _FakeFeed([_bar(_T0)], mode="hold"), now_ms=clock)
    clerk = _end_clerk(registry, fences=False)
    await _deploy(registry, "bot-a")
    await _deploy(registry, "bot-b")
    real = registry._authority_for

    class _LockedProjector:
        def run_is_active(self, **_kwargs: object) -> bool:
            raise sqlite3.OperationalError("database is locked")

    class _LockedAuthority:
        def lifecycle_projector(self) -> _LockedProjector:
            return _LockedProjector()

    registry._authority_for = (  # type: ignore[method-assign]
        lambda binding: _LockedAuthority() if binding.strategy_instance_id == "bot-a" else real(binding)
    )
    clock.move_to(_END.end_at_ms)

    try:
        await registry.carry_out_due_ends()
    finally:
        registry._authority_for = real  # type: ignore[method-assign]

    assert clerk.passes == 1, "the watch never reached bot-b"
    failed = [record for record in caplog.records if getattr(record, "action", None) == "bot_end_pass_failed"]
    assert [(record.strategy_instance_id, record.levelno) for record in failed] == [("bot-a", logging.ERROR)]
    assert failed[0].exc_info is not None


# ── the proof of a stop at the end ───────────────────────────────────────────


class _PassSequenceClerk:
    """A Clerk whose published passes are ``proofs``, one per look; the last one stays."""

    def __init__(self, proofs: list[InstanceCustodyProof]) -> None:
        self.proofs = proofs
        self.looks = 0

    async def published_custody(self, sid: str) -> InstanceCustodyProof | None:
        self.looks += 1
        return self.proofs.pop(0) if len(self.proofs) > 1 else self.proofs[0]


def _trade_binding() -> BrokerBotBinding:
    return BrokerBotBinding(
        exit_terms=DEPLOY_EXIT_TERMS, strategy_instance_id=_SID, strategy_key="deployment_validation",
        broker="alpaca", symbol="SPY", use_rth=True, mode="trade", quantity=1, carryover_policy="FORBID",
        action_plan=alpaca_v1_action_plan("SPY"), run_id="trade-run-1", created_at_ms=_T0,
    )


async def test_an_end_stops_proof_waits_past_a_pass_that_still_lists_its_sale_as_working(tmp_path: Path) -> None:
    """#2607 review: the pass that ended the bot put in its sale and may publish before the sale
    fills. That pass still lists the order as working; the proof waits for one that doesn't,
    rather than calling a flat bot's custody unprovable."""
    flat = _custody_proof(exposure={})
    clerk = _PassSequenceClerk([flat.model_copy(update={"working_order_refs": ("order:end-sale",)}), flat])
    set_alpaca_clerk(clerk)  # type: ignore[arg-type]

    outcome = await prove_end_stop_outcome(
        _trade_binding(), checkpoint_path=tmp_path / "stop.json", now_ms=lambda: _T0, wait_s=5.0, poll_s=0.0,
    )

    assert outcome == "STOPPED_FLAT"
    assert clerk.looks == 2


async def test_an_end_stops_proof_records_the_last_pass_it_saw_once_its_wait_is_over(tmp_path: Path) -> None:
    """A sale still working when the wait ends is the stop's to record as unproven; the sweeps go on."""
    working = _custody_proof(exposure={"SPY": 1.0}).model_copy(update={"unresolved_intent_refs": ("intent:1",)})
    set_alpaca_clerk(_PassSequenceClerk([working]))  # type: ignore[arg-type]

    outcome = await prove_end_stop_outcome(
        _trade_binding(), checkpoint_path=tmp_path / "stop.json", now_ms=lambda: _T0, wait_s=0.0, poll_s=0.0,
    )

    assert outcome == "STOPPED_CUSTODY_UNPROVABLE"
    assert json.loads((tmp_path / "stop.json").read_text(encoding="utf-8"))["outcome"] == "STOPPED_CUSTODY_UNPROVABLE"


async def test_a_stop_at_its_end_waits_for_its_proof_without_holding_the_bots_operations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#2607 review: the proof waits up to 45 s for the Clerk's pass. A Deploy of the bot takes
    the process-wide graduation fence and then the bot's lock, so a stop that held the lock
    that long held every Deploy and the live cutover behind it. The next operation on the bot
    is answered at once; the proof, when it lands, is still recorded."""
    admit_canary_pairing(monkeypatch, "deployment_validation", "paper-account")
    clock = _Clock()
    registry = _registry(tmp_path, _FakeFeed([_bar(_T0)], mode="hold"), now_ms=clock)
    clerk = _end_clerk(registry)
    await _deploy(registry, mode="trade")
    clerk.published = False  # no pass has seen the bot's every transition yet
    clock.move_to(_END.end_at_ms)

    await registry.carry_out_due_ends()
    await _wait_for(lambda: not registry.status("alpaca", _SID).running)
    with pytest.raises(BotEndRefused, match="This bot's end has come"):
        await asyncio.wait_for(
            registry.edit_bot_end("alpaca", _SID, _sell(None), updated_by="operator"), timeout=2.0,
        )

    clerk.published = True
    await asyncio.gather(*registry._end_stop_tasks.values())
    outcome = registry.status("alpaca", _SID).duty_outcome
    assert outcome is not None and (outcome.kind, outcome.reason_code) == ("STOPPED", "SCHEDULED_END")


async def _stopped_at_its_end_awaiting_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> tuple[BotTaskRegistry, _EndClerk, BrokerBotBinding, list[str]]:
    """A trading bot the Clerk stopped at its end, its stop waiting for its proof with the lock released.

    The last element lists the runs whose outcome was projected over the bot's current one.
    """
    admit_canary_pairing(monkeypatch, "deployment_validation", "paper-account")
    clock = _Clock()
    registry = _registry(tmp_path, _FakeFeed([_bar(_T0)], mode="hold"), now_ms=clock)
    clerk = _end_clerk(registry)
    await _deploy(registry, mode="trade")
    stopped = registry.binding_for_control("alpaca", _SID)
    clerk.published = False
    clock.move_to(_END.end_at_ms)
    await registry.carry_out_due_ends()
    await _wait_for(lambda: not registry.status("alpaca", _SID).running)
    projected: list[str] = []
    monkeypatch.setattr(
        registry._terminal, "replace_provisional_stop",
        lambda binding, **_kwargs: projected.append(binding.run_id),
    )
    return registry, clerk, stopped, projected


async def test_a_stop_at_its_end_proven_after_a_later_run_began_is_recorded_as_the_stopped_runs_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """#2607 review: while the stop waited for its proof with the lock released, a later run of
    the bot was launched. The proof is the stopped run's: it is recorded under that run's id --
    its receipt, and the replay receipt it owes -- where it used to leave the run provisional for
    good, and nothing is projected over the later run."""
    caplog.set_level(logging.INFO, logger="app.services.bot_runner")
    registry, clerk, stopped, projected = await _stopped_at_its_end_awaiting_proof(tmp_path, monkeypatch)
    owed: list[str] = []
    monkeypatch.setattr(registry, "_schedule_run_replay_receipt", lambda binding: owed.append(binding.run_id))

    registry._bindings.record_launch(stopped.model_copy(update={"run_id": "a-later-run"}), launch_reason="deploy")
    clerk.published = True
    await asyncio.gather(*registry._end_stop_tasks.values())

    receipt = registry._bindings.read_outcome(_SID, stopped.run_id)
    assert receipt is not None and (receipt.kind, receipt.reason_code) == ("STOPPED", "SCHEDULED_END")
    assert owed == [stopped.run_id]
    assert projected == []
    assert registry._bindings.read_outcome(_SID, "a-later-run") is None
    assert "bot_end_proof_superseded" in [getattr(record, "action", None) for record in caplog.records]


async def test_a_stop_at_its_end_whose_registration_is_gone_says_its_outcome_stays_provisional(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """No later run began: the bot's registration is gone while its proof was awaited. That is
    said as it is -- not as "a later run began" -- and so is what it leaves: the stopped run's
    outcome stays provisional."""
    registry, clerk, stopped, projected = await _stopped_at_its_end_awaiting_proof(tmp_path, monkeypatch)

    monkeypatch.setattr(registry, "_read_binding", lambda _sid: None)
    clerk.published = True
    await asyncio.gather(*registry._end_stop_tasks.values())

    assert projected == []
    assert registry._bindings.read_outcome(_SID, stopped.run_id) is None
    actions = [getattr(record, "action", None) for record in caplog.records]
    assert "bot_end_proof_superseded" not in actions
    [said] = [record for record in caplog.records if getattr(record, "action", None) == "bot_end_proof_unrecorded"]
    assert "stays provisional" in said.getMessage()


# ── a stopped Dry Run's end ──────────────────────────────────────────────────


def _stopped_dry_run(registry: BotTaskRegistry, tmp_path: Path, *, desired: DesiredState) -> None:
    """A Dry Run whose run ended before its end, and whose account is closed."""
    binding = BrokerBotBinding(
        exit_terms=DEPLOY_EXIT_TERMS, strategy_instance_id="dry-bot", strategy_key="deployment_validation",
        broker="alpaca", symbol="SPY", use_rth=True, mode="dry_run", quantity=1, carryover_policy="FORBID",
        action_plan=alpaca_v1_action_plan("SPY"), run_id="dry-run-1", created_at_ms=_T0,
    )
    registry._bindings.record_launch(binding, launch_reason="deploy")
    DesiredStateRepo(stable_desired_state_path(tmp_path, "dry-bot")).set(
        desired, updated_by="bot_runner", now_ms=_T0, reason="terminal_outcome:CRASHED", end=_END,
    )


async def test_a_stopped_dry_runs_end_is_recorded_when_it_comes(tmp_path: Path) -> None:
    """#2607 review: its run is over and its simulation closed what it held when it
    ended (#2641). No pass of its closed account will come, so the end would read
    as due forever; the watch records it instead."""
    clock = _Clock()
    registry = _registry(tmp_path, None, now_ms=clock)
    _stopped_dry_run(registry, tmp_path, desired=DesiredState.STOPPED)

    await registry.carry_out_due_ends()
    assert registry.pending_ends(["dry-bot"]) == [ScheduledEnd(strategy_instance_id="dry-bot", end=_END)]

    clock.move_to(_END.end_at_ms + 1_000)
    await registry.carry_out_due_ends()

    assert registry.pending_ends(["dry-bot"]) == []
    view = registry.bot_end("alpaca", "dry-bot")
    assert (view.status, view.editable) == ("ended", False)
    assert view.headline.endswith("· sold at its last price")


async def test_a_dry_run_its_owner_wants_running_keeps_its_end_for_its_run(tmp_path: Path) -> None:
    """A restart's restoration is pending: the end is its run's to carry out."""
    clock = _Clock()
    registry = _registry(tmp_path, None, now_ms=clock)
    _stopped_dry_run(registry, tmp_path, desired=DesiredState.RUNNING)

    clock.move_to(_END.end_at_ms + 1_000)
    await registry.carry_out_due_ends()

    assert registry.pending_ends(["dry-bot"]) == [ScheduledEnd(strategy_instance_id="dry-bot", end=_END)]
