"""The runner's side of a bot's owner-set end (#2607).

The end is the bot's desired state: Deploy records it, the owner can change it
while the bot runs -- with no restart and no seal touched -- and the Clerk
reads it through the runner's end schedule. When the Clerk fences the bot at
its end, the runner stops the bot's process the way the panel's Stop does
after its own STOP, and records why. An operator's Stop ends the bot and its
end with it; a crash leaves the end for the Clerk to carry out.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.broker.alpaca.clerk import get_alpaca_clerk, set_alpaca_clerk
from app.broker.alpaca.clerk.sqlite.scheduled_end import ScheduledEnd
from app.engine.live.desired_state import DesiredStateRepo, stable_desired_state_path
from app.lean_sidecar.trading_calendar import session_close_ms_utc
from app.schemas.bot_end import BotEnd, BotEndInput
from app.services.bot_carryover import configuration_hash
from app.services.bot_end import BotEndRefused
from app.services.bot_runner import BotTaskRegistry, UnknownBotError
from app.utils.timestamps import now_ms_utc
from tests._helpers.bot_runner.custody import _SID, _T0, _custody_proof, _registry
from tests._helpers.bot_runner.doubles import _CustodyClerk, _FakeFeed
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


def _desired_json(tmp_path: Path, sid: str = _SID) -> dict:
    return json.loads((tmp_path / "live_state" / sid / "desired_state.json").read_text(encoding="utf-8"))


def _binding_bytes(tmp_path: Path, sid: str = _SID) -> bytes:
    return (tmp_path / "live_state" / sid / "strategy_instance.json").read_bytes()


async def _deployed(tmp_path: Path, clock: _Clock, *, end: BotEnd | None = _END) -> BotTaskRegistry:
    registry = _registry(tmp_path, _FakeFeed([_bar(_T0)], mode="hold"), now_ms=clock)
    await registry.deploy(
        exit_terms=DEPLOY_EXIT_TERMS, broker="alpaca", strategy_instance_id=_SID, symbol="SPY", end=end,
    )
    return registry


# ── Deploy records the end ───────────────────────────────────────────────────


async def test_deploy_records_the_owners_end_in_the_bots_desired_state(tmp_path: Path) -> None:
    await _deployed(tmp_path, _Clock())

    desired = _desired_json(tmp_path)
    assert desired["desired_state"] == "RUNNING"
    assert desired["end_at_ms"] == _END.end_at_ms
    assert desired["end_action"] == "SELL"
    assert desired["end_carried_out_at_ms"] is None


async def test_the_end_never_enters_the_binding_or_its_configuration_hash(tmp_path: Path) -> None:
    await _deployed(tmp_path, _Clock())
    with_end = _binding_bytes(tmp_path)

    assert b"end_at_ms" not in with_end
    assert b"end_action" not in with_end


async def test_deploy_with_no_end_records_no_end(tmp_path: Path) -> None:
    registry = await _deployed(tmp_path, _Clock(), end=None)

    assert _desired_json(tmp_path)["end_at_ms"] is None
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
    assert view.headline == "Ends today 14:59 ET · keeps its shares"
    assert registry.pending_ends([_SID]) == [ScheduledEnd(strategy_instance_id=_SID, end=later)]
    # No restart: the same task runs the same run.
    assert registry._bots[_SID].task is task and not task.done()
    assert registry.status("alpaca", _SID).running is True
    # No seal touched: the binding file, its hash, the program seal and the exit terms.
    after = registry.binding_for_control("alpaca", _SID)
    assert (_binding_bytes(tmp_path), configuration_hash(after), after.sealed_program, after.exit_terms) == sealed


async def test_an_edit_can_remove_the_end(tmp_path: Path) -> None:
    registry = await _deployed(tmp_path, _Clock())

    view = await registry.edit_bot_end("alpaca", _SID, BotEndInput(end_at_ms=None), updated_by="operator")

    assert view.status == "no_end"
    assert registry.pending_ends([_SID]) == []


async def test_an_edit_is_refused_in_plain_words_when_the_end_is_outside_regular_hours(tmp_path: Path) -> None:
    registry = await _deployed(tmp_path, _Clock())

    with pytest.raises(BotEndRefused, match="The end must fall within regular hours"):
        await registry.edit_bot_end(
            "alpaca", _SID, BotEndInput(end_at_ms=_END.end_at_ms + 3_600_000), updated_by="operator",
        )

    assert registry.pending_ends([_SID]) == [ScheduledEnd(strategy_instance_id=_SID, end=_END)]


async def test_an_end_that_has_come_cannot_be_edited(tmp_path: Path) -> None:
    clock = _Clock()
    registry = await _deployed(tmp_path, clock)
    clock.move_to(_END.end_at_ms + 1_000)

    with pytest.raises(BotEndRefused, match="This bot's end has come"):
        await registry.edit_bot_end("alpaca", _SID, BotEndInput(end_at_ms=None), updated_by="operator")


async def test_a_stopped_bots_end_cannot_be_edited(tmp_path: Path) -> None:
    registry = await _deployed(tmp_path, _Clock())
    await registry.stop("alpaca", _SID)

    with pytest.raises(BotEndRefused, match="This bot has stopped"):
        await registry.edit_bot_end("alpaca", _SID, BotEndInput(end_at_ms=_END.end_at_ms), updated_by="operator")


# ── Stop and crash ───────────────────────────────────────────────────────────


async def test_an_operators_stop_ends_the_bot_and_its_end(tmp_path: Path) -> None:
    """Stopping doesn't sell its shares (#2605's Stop copy); a later scheduled sale would."""
    registry = await _deployed(tmp_path, _Clock())

    await registry.stop("alpaca", _SID)

    assert _desired_json(tmp_path)["end_at_ms"] is None
    assert registry.pending_ends([_SID]) == []


async def test_a_crash_leaves_the_end_for_the_clerk_to_carry_out(tmp_path: Path) -> None:
    registry = _registry(tmp_path, _FakeFeed([_bar(_T0)], mode="crash", error=RuntimeError("boom")), now_ms=_Clock())
    await registry.deploy(
        exit_terms=DEPLOY_EXIT_TERMS, broker="alpaca", strategy_instance_id=_SID, symbol="SPY", end=_END,
    )

    await _wait_for(lambda: not registry.status("alpaca", _SID).running)

    assert _desired_json(tmp_path)["desired_state"] == "STOPPED"
    assert registry.pending_ends([_SID]) == [ScheduledEnd(strategy_instance_id=_SID, end=_END)]
    view = registry.bot_end("alpaca", _SID)
    assert view.status == "scheduled"
    assert view.editable is True


# ── the Clerk's end schedule ─────────────────────────────────────────────────


async def test_the_clerk_stopping_a_bot_at_its_end_stops_its_process_with_the_ends_reason(tmp_path: Path) -> None:
    registry = await _deployed(tmp_path, _Clock())
    managed = registry._bots[_SID]
    run_id = managed.binding.run_id
    # What the Clerk's pass commits before it asks: Stop's STOP, at the bot's end.
    await get_alpaca_clerk().stop_strategy_run(strategy_instance_id=_SID, run_id=run_id, reason="scheduled_end")

    registry.stop_bot_at_its_end(_SID, run_id)

    assert not managed.run_gate.is_set(), "Stop's fence was not raised at once"
    await asyncio.gather(*registry._end_stop_tasks)
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
    assert _desired_json(tmp_path)["end_carried_out_at_ms"] == _END.end_at_ms + 5_000
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


def test_bot_end_of_an_unknown_bot_is_refused(tmp_path: Path) -> None:
    with pytest.raises(UnknownBotError):
        _registry(tmp_path, None).bot_end("alpaca", "never-deployed")


# ── the end watch: a running bot's Clerk is asked for a pass at its end ─────


class _EndClerk(_CustodyClerk):
    """A Clerk whose pass fences every bot the registry names due, as the real pass does.

    ``fences`` off models a pass that ran but reached no run; ``fail_next`` a
    pass that failed.
    """

    def __init__(self, registry: BotTaskRegistry, *, fences: bool = True) -> None:
        super().__init__(_custody_proof(exposure={}))
        self.registry = registry
        self.fences = fences
        self.fail_next = False
        self.passes = 0

    async def reconcile_once(self):  # type: ignore[override]
        self.passes += 1
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("the pass failed")
        if self.fences:
            for sid, managed in list(self.registry._bots.items()):
                self.registry.stop_bot_at_its_end(sid, managed.binding.run_id)
        return await super().reconcile_once()


async def test_the_end_watch_asks_once_per_end_and_again_only_after_a_failed_pass(tmp_path: Path) -> None:
    clock = _Clock()
    registry = await _deployed(tmp_path, clock)
    previous = get_alpaca_clerk()
    clerk = _EndClerk(registry, fences=False)
    clerk.fail_next = True
    set_alpaca_clerk(clerk)
    try:
        clock.move_to(_END.end_at_ms)
        await registry.carry_out_due_ends()  # fails: asked again
        await registry.carry_out_due_ends()  # runs
        await registry.carry_out_due_ends()  # the periodic sweep's to finish now

        assert clerk.passes == 2
    finally:
        set_alpaca_clerk(previous)


async def test_the_end_watch_asks_for_a_pass_only_once_a_running_bots_end_has_come(tmp_path: Path) -> None:
    clock = _Clock()
    registry = await _deployed(tmp_path, clock)
    previous = get_alpaca_clerk()
    clerk = _EndClerk(registry)
    set_alpaca_clerk(clerk)
    try:
        await registry.carry_out_due_ends()
        assert clerk.passes == 0

        clock.move_to(_END.end_at_ms)
        await registry.carry_out_due_ends()
        await registry.carry_out_due_ends()  # fenced by the first pass: not asked again

        assert clerk.passes == 1
        await _wait_for(lambda: not registry.status("alpaca", _SID).running)
    finally:
        set_alpaca_clerk(previous)
