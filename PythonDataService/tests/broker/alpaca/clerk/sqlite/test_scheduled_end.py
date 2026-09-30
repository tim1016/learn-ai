"""The Clerk carries out a bot's owner-set end on its reconciliation pass (#2607).

At or after ``end_at_ms`` the pass fences the bot's run with Stop's STOP,
cancels its working entries, sells what it holds at market inside the regular
session when the owner chose SELL, and records the end carried out. It does so
for a running bot and for one whose run already died -- the owner-scheduled
exception to #2504 Q3's "a dead run never sells by itself". An end the Clerk
finds after the close (it was down at the time) sells at the next open, never
after hours.

Every clock is a fake ``int64 ms UTC`` clock set from an ET wall clock; the
session boundaries come from the canonical calendar.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from app.broker.alpaca.clerk import set_alpaca_clerk
from app.broker.alpaca.clerk.active_authority import ActiveClerkRuntime, set_active_clerk_runtime
from app.broker.alpaca.clerk.recovery_reduction import UNPRICEABLE_RECOVERY, RecoveryPricing
from app.broker.alpaca.clerk.sqlite import scheduled_end
from app.broker.alpaca.clerk.sqlite.commands import submit_start_run, submit_stop_run
from app.broker.alpaca.clerk.sqlite.exit import accept_exit
from app.broker.alpaca.clerk.sqlite.exit_resolution import (
    EXIT_REDRIVE_DECISION_PREFIX,
    SCHEDULED_END_DECISION_PREFIX,
    resolve_exit,
)
from app.broker.alpaca.clerk.sqlite.facts import ExitAcceptedFacts
from app.broker.alpaca.clerk.sqlite.intake_fence import ReentrantAsyncLock
from app.broker.alpaca.clerk.sqlite.reconcile import reconcile_account
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository, ExecutionLeaseLost
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.alpaca.clerk.sqlite.scheduled_end import (
    SCHEDULED_END_REASON,
    EndSaleWaiting,
    ScheduledEnd,
    end_sales_waiting_for_open,
    install_bot_end_schedule,
)
from app.broker.alpaca.clerk.trade_evidence import SqliteTradeUpdateEvidenceSink
from app.broker.contract.errors import BrokerUnavailable
from app.broker.contract.models import BrokerOrder, BrokerOrderEvent, BrokerOrderLeg, OrderSide, OrderType
from app.engine.live.desired_state import DesiredState, DesiredStateRepo, stable_desired_state_path
from app.routers import alpaca_clerk_sqlite
from app.schemas.bot_end import BotEnd
from app.services.bot_runner import BotTaskRegistry, set_bot_task_registry
from app.services.broker_v2_panel import lane_summary
from app.utils.timestamps import to_ms_utc
from tests._helpers.bot_runner.custody import _custody_proof
from tests._helpers.bot_runner.custody import _registry as _runner_registry
from tests._helpers.bot_runner.doubles import _CustodyClerk, _FakeFeed
from tests._helpers.bot_runner.market import patch_fresh_live_market_liveness
from tests._helpers.exit_terms import DEPLOY_EXIT_TERMS
from tests.broker.alpaca.clerk.sqlite.conftest import (
    _AssertingNoReconciler,
    _broker_order_fixture,
    _broker_position_fixture,
    _clock_at,
    _FakeReadPort,
    _FakeTradePort,
    _make_held_position,
    _walk_clock_to,
)
from tests.broker.alpaca.clerk.sqlite.test_exit_send_session import _live_touch
from tests.broker.alpaca.clerk.sqlite.test_two_bots_one_symbol import _wash_trade_rejection

ACCOUNT_ID = "PA-END"
SID = "end-bot"
RUN_ID = "run-1"
_ET = ZoneInfo("America/New_York")
_WEDNESDAY = date(2026, 9, 30)
_THURSDAY = date(2026, 10, 1)


def _at(day: date, hour: int, minute: int = 0, second: int = 0) -> int:
    return to_ms_utc(datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=_ET))


_END_MS = _at(_WEDNESDAY, 15, 59)


class _Schedule:
    """The runner's side of the end, as the Clerk sees it through the port."""

    def __init__(self, *ends: ScheduledEnd) -> None:
        self.ends = {end.strategy_instance_id: end for end in ends}
        self.stopped: list[tuple[str, str]] = []
        self.carried_out: list[tuple[ScheduledEnd, int]] = []

    def pending_ends(self, strategy_instance_ids: Sequence[str]) -> list[ScheduledEnd]:
        return [self.ends[sid] for sid in strategy_instance_ids if sid in self.ends]

    def stop_bot_at_its_end(self, strategy_instance_id: str, lifecycle_run_id: str) -> None:
        self.stopped.append((strategy_instance_id, lifecycle_run_id))

    def record_end_carried_out(self, end: ScheduledEnd, *, at_ms: int) -> None:
        self.carried_out.append((end, at_ms))
        self.ends.pop(end.strategy_instance_id)


class _Market(_FakeTradePort):
    """Alpaca for one bot: its filled entry, and each order sent, looked up exactly."""

    def __init__(self) -> None:
        super().__init__()
        self.sent: dict[str, BrokerOrder] = {}

    async def submit(self, leg: BrokerOrderLeg, *, client_order_id: str) -> BrokerOrder:
        order = await super().submit(leg, client_order_id=client_order_id)
        self.sent[client_order_id] = order
        return order

    async def get_order_by_client_order_id(self, client_order_id: str) -> BrokerOrder | None:
        self.lookup_calls.append(client_order_id)
        if client_order_id in self.sent:
            return self.sent[client_order_id]
        return _broker_order_fixture(
            client_order_id, status="filled", quantity=10.0, filled_quantity=10.0, filled_avg_price=100.0,
        ).model_copy(update={"order_id": f"bo-{client_order_id}"})

    async def fill(self, repo: ClerkSqliteRepository, client_order_id: str) -> None:
        """Alpaca fills the sale: the order reads filled and its slice arrives on trade_updates."""
        filled = self.sent[client_order_id].model_copy(update={
            "status": "filled", "filled_quantity": 10.0, "filled_avg_price": 101.0,
        })
        self.sent[client_order_id] = filled
        sink = SqliteTradeUpdateEvidenceSink(repo=repo, intake=ReentrantAsyncLock(), reconciler=_AssertingNoReconciler())
        await sink.record_lifecycle_event(
            client_order_id=client_order_id,
            event=BrokerOrderEvent(
                event_type="fill", occurred_at_ms=repo.clock(), price=101.0, quantity=10, execution_id="end-sale",
            ),
            event_key="execution:end-sale",
            order=filled,
            recovery_source=None,
            recovery_window_limit=None,
        )


@pytest.fixture
def repo(tmp_path: Path) -> Iterator[ClerkSqliteRepository]:
    """A bot holding 10 SPY, bought that morning, its run still active."""
    r = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=_clock_at(_at(_WEDNESDAY, 10)), lease_ttl_ms=300_000,
    )
    r.register_strategy_instance(strategy_instance_id=SID, symbol="SPY", config_hash="h1")
    submit_start_run(r, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id=RUN_ID)
    yield r
    r.close()


@pytest.fixture
def schedule() -> Iterator[_Schedule]:
    installed = _Schedule()
    install_bot_end_schedule(installed)
    yield installed
    install_bot_end_schedule(None)


def _end(action: str = "SELL", end_at_ms: int = _END_MS) -> ScheduledEnd:
    return ScheduledEnd(strategy_instance_id=SID, end=BotEnd(end_at_ms=end_at_ms, end_action=action))


async def _hold_ten(repo: ClerkSqliteRepository) -> str:
    return await _make_held_position(repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, run_id=RUN_ID)


async def _pass(
    repo: ClerkSqliteRepository, trade: _FakeTradePort, *, held: float = 10.0,
    pricing: RecoveryPricing = UNPRICEABLE_RECOVERY,
):
    positions = [_broker_position_fixture("SPY", quantity=held)] if held else []
    return await reconcile_account(
        repo, read=_FakeReadPort(positions=positions), trade=trade, trigger="AUTOMATIC",
        intake=ReentrantAsyncLock(), pricing=pricing,
    )


def _stop_reason(repo: ClerkSqliteRepository) -> str | None:
    stopped = repo.last_strategy_transition(strategy_instance_id=SID, transition_kind="RUN_STOPPED")
    return None if stopped is None else json.loads(stopped["facts_json"]).get("operator_reason")


def _end_sales(repo: ClerkSqliteRepository) -> list[str]:
    rows = repo._conn.execute(
        "SELECT effect_operation_id FROM effect_operations WHERE strategy_instance_id = ? AND kind = 'EXIT'",
        (SID,),
    ).fetchall()
    return [row[0] for row in rows if SCHEDULED_END_DECISION_PREFIX in row[0]]


def _sold_market(trade: _FakeTradePort) -> list[BrokerOrderLeg]:
    return [
        leg for leg in trade.submitted_legs
        if leg.side == OrderSide.SELL and leg.order_type == OrderType.MARKET and not leg.extended_hours
    ]


# ── a running bot ────────────────────────────────────────────────────────────


async def test_a_running_bot_at_its_end_with_sell_ends_flat_by_a_market_order(
    repo: ClerkSqliteRepository, schedule: _Schedule,
) -> None:
    await _hold_ten(repo)
    schedule.ends[SID] = _end("SELL")
    _walk_clock_to(repo, _at(_WEDNESDAY, 15, 59, 5))
    market = _Market()

    await _pass(repo, market)

    assert repo.active_run(SID) is None
    assert _stop_reason(repo) == SCHEDULED_END_REASON
    assert schedule.stopped == [(SID, RUN_ID)]
    assert [(leg.side, leg.quantity) for leg in _sold_market(market)] == [(OrderSide.SELL, 10)]
    assert [end for end, _at_ms in schedule.carried_out] == [_end("SELL")]

    [sale] = market.sent
    await market.fill(repo, sale)
    await _pass(repo, market, held=0)

    assert repo.position(SID, "SPY") == 0
    assert len(_sold_market(market)) == 1, "the end sold twice"


async def test_a_running_bot_at_its_end_with_keep_ends_stopped_and_still_holding(
    repo: ClerkSqliteRepository, schedule: _Schedule,
) -> None:
    await _hold_ten(repo)
    schedule.ends[SID] = _end("KEEP")
    _walk_clock_to(repo, _at(_WEDNESDAY, 15, 59, 5))
    market = _Market()

    await _pass(repo, market)

    assert repo.active_run(SID) is None
    assert _stop_reason(repo) == SCHEDULED_END_REASON
    assert schedule.stopped == [(SID, RUN_ID)]
    assert market.submitted_legs == []
    assert repo.position(SID, "SPY") == 10
    assert [end for end, _at_ms in schedule.carried_out] == [_end("KEEP")]


async def test_nothing_happens_before_the_end(repo: ClerkSqliteRepository, schedule: _Schedule) -> None:
    await _hold_ten(repo)
    schedule.ends[SID] = _end("SELL")
    _walk_clock_to(repo, _at(_WEDNESDAY, 15, 58, 59))
    market = _Market()

    await _pass(repo, market)

    assert repo.active_run(SID) is not None
    assert schedule.stopped == []
    assert market.submitted_legs == []
    assert schedule.carried_out == []


async def test_a_second_pass_never_sells_twice(repo: ClerkSqliteRepository, schedule: _Schedule) -> None:
    await _hold_ten(repo)
    schedule.ends[SID] = _end("SELL")
    _walk_clock_to(repo, _at(_WEDNESDAY, 15, 59, 5))
    market = _Market()
    await _pass(repo, market)
    schedule.ends[SID] = _end("SELL")  # as if the carried-out record had not landed

    _walk_clock_to(repo, _at(_WEDNESDAY, 15, 59, 20))
    await _pass(repo, market)

    assert len(_sold_market(market)) == 1
    assert len(_end_sales(repo)) == 1
    assert schedule.stopped == [(SID, RUN_ID)]


async def test_the_end_fences_the_run_even_when_alpaca_cannot_be_read(
    repo: ClerkSqliteRepository, schedule: _Schedule,
) -> None:
    """The fence is Stop's local STOP: it never waits on the broker. Nothing
    is sold, and the end stays pending until a pass can see the account."""
    await _hold_ten(repo)
    schedule.ends[SID] = _end("SELL")
    _walk_clock_to(repo, _at(_WEDNESDAY, 15, 59, 5))

    class _Unreadable(_FakeReadPort):
        async def list_positions(self):  # type: ignore[override]
            raise BrokerUnavailable("alpaca is down", broker="alpaca")

    market = _Market()
    result = await reconcile_account(
        repo, read=_Unreadable(), trade=market, trigger="AUTOMATIC",
        intake=ReentrantAsyncLock(), pricing=UNPRICEABLE_RECOVERY,
    )

    assert result.verdict == "stale"
    assert repo.active_run(SID) is None
    assert schedule.stopped == [(SID, RUN_ID)]
    assert market.submitted_legs == []
    assert schedule.carried_out == []


async def test_the_bots_own_working_exit_is_left_to_finish(
    repo: ClerkSqliteRepository, schedule: _Schedule,
) -> None:
    """A sale the bot already sent goes on; the end sells only what that sale leaves."""
    entry_ref = await _hold_ten(repo)
    accepted = accept_exit(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, decision_id="own-exit",
        lifecycle_run_id=RUN_ID, entry_order_ref=entry_ref,
    )
    assert accepted.effect_operation_id is not None
    market = _Market()
    _walk_clock_to(repo, _at(_WEDNESDAY, 15, 58, 30))
    await resolve_exit(repo, effect_operation_id=accepted.effect_operation_id, trade=market, pricing=UNPRICEABLE_RECOVERY)
    assert len(market.submitted_legs) == 1  # the bot's own sale, still working
    schedule.ends[SID] = _end("SELL")
    _walk_clock_to(repo, _at(_WEDNESDAY, 15, 59, 5))

    await _pass(repo, market)

    assert repo.active_run(SID) is None
    assert len(market.submitted_legs) == 1, "a second sale raced the bot's own"
    assert _end_sales(repo) == []
    assert schedule.carried_out == []


# ── a crashed bot ────────────────────────────────────────────────────────────


async def test_a_crashed_run_is_ended_the_same_way(repo: ClerkSqliteRepository, schedule: _Schedule) -> None:
    """Its run already stopped when its runner died; the Clerk still sells at its end."""
    await _hold_ten(repo)
    submit_stop_run(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id=RUN_ID, operator_reason="runner_gone",
    )
    schedule.ends[SID] = _end("SELL")
    _walk_clock_to(repo, _at(_WEDNESDAY, 15, 59, 5))
    market = _Market()

    await _pass(repo, market)

    assert _stop_reason(repo) == "runner_gone"
    assert schedule.stopped == []  # no run to fence, no process to stop
    assert [(leg.side, leg.quantity) for leg in _sold_market(market)] == [(OrderSide.SELL, 10)]
    assert [end for end, _at_ms in schedule.carried_out] == [_end("SELL")]

    [sale] = market.sent
    await market.fill(repo, sale)
    await _pass(repo, market, held=0)

    assert repo.position(SID, "SPY") == 0


async def test_a_crashed_run_that_keeps_is_left_holding(repo: ClerkSqliteRepository, schedule: _Schedule) -> None:
    await _hold_ten(repo)
    submit_stop_run(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id=RUN_ID, operator_reason="runner_gone",
    )
    schedule.ends[SID] = _end("KEEP")
    _walk_clock_to(repo, _at(_WEDNESDAY, 15, 59, 5))
    market = _Market()

    await _pass(repo, market)

    assert market.submitted_legs == []
    assert repo.position(SID, "SPY") == 10
    assert [end for end, _at_ms in schedule.carried_out] == [_end("KEEP")]


# ── an end missed while the Clerk was down ───────────────────────────────────


async def test_an_end_missed_while_the_clerk_was_down_sells_at_the_next_open_never_after_hours(
    repo: ClerkSqliteRepository, schedule: _Schedule,
) -> None:
    """Found at 17:00, after the close: nothing goes out after hours or in the
    pre-market, even with a live quote and an exit allowance that would price an
    extended-hours limit for any other exit; at the open it sells at market."""
    await _hold_ten(repo)
    submit_stop_run(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id=RUN_ID,
        operator_reason="service_restart_recovery",
    )
    schedule.ends[SID] = _end("SELL")
    market = _Market()
    pricing = _live_touch()

    _walk_clock_to(repo, _at(_WEDNESDAY, 17))
    await _pass(repo, market, pricing=pricing)

    assert market.submitted_legs == [], "the missed end sold after hours"
    [sale] = _end_sales(repo)
    effect = repo.effect_operation(sale)
    assert effect is not None and effect.state not in ("succeeded", "failed", "rejected")
    hold = repo.last_strategy_transition(strategy_instance_id=SID, transition_kind="EXIT_MARKET_HOLD")
    assert hold is not None and hold["summary_code"] == "SCHEDULED_END_WAITS_FOR_OPEN"
    assert "never goes out after hours" in json.loads(hold["facts_json"])["explanation"]
    # The owner is told, and the end counts as carried out: the sale is the EXIT's now.
    assert [end for end, _at_ms in schedule.carried_out] == [_end("SELL")]

    _walk_clock_to(repo, _at(_THURSDAY, 5))
    await _pass(repo, market, pricing=pricing)
    assert market.submitted_legs == [], "the missed end sold in the pre-market"

    _walk_clock_to(repo, _at(_THURSDAY, 9, 30, 5))
    await _pass(repo, market, pricing=pricing)

    assert [(leg.side, leg.quantity) for leg in _sold_market(market)] == [(OrderSide.SELL, 10)]
    assert len(market.submitted_legs) == 1


async def test_an_end_found_in_the_last_seconds_before_the_close_waits_for_the_open(
    repo: ClerkSqliteRepository, schedule: _Schedule,
) -> None:
    """A market order that could reach Alpaca after the close is never sent: it would queue for the open."""
    await _hold_ten(repo)
    schedule.ends[SID] = _end("SELL")
    _walk_clock_to(repo, _at(_WEDNESDAY, 15, 59, 58))
    market = _Market()

    await _pass(repo, market, pricing=_live_touch())

    assert market.submitted_legs == []
    assert len(_end_sales(repo)) == 1
    assert repo.effect_operation(_end_sales(repo)[0]).state not in ("succeeded", "failed", "rejected")


# ── the step never fails the account's reconciliation ────────────────────────


async def test_an_end_schedule_that_cannot_be_read_never_fails_the_pass(
    repo: ClerkSqliteRepository, schedule: _Schedule, caplog: pytest.LogCaptureFixture,
) -> None:
    await _hold_ten(repo)
    _walk_clock_to(repo, _at(_WEDNESDAY, 15, 59, 5))

    def unreadable(_sids: Sequence[str]) -> list[ScheduledEnd]:
        raise RuntimeError("a desired-state read broke")

    schedule.pending_ends = unreadable  # type: ignore[method-assign]
    result = await _pass(repo, _Market())

    assert result.verdict == "clean"
    assert repo.active_run(SID) is not None
    assert any(getattr(record, "action", None) == "scheduled_end_schedule_unreadable" for record in caplog.records)


# ── the step without an installed schedule ───────────────────────────────────


async def test_a_pass_with_no_schedule_installed_ends_nothing(repo: ClerkSqliteRepository) -> None:
    await _hold_ten(repo)
    _walk_clock_to(repo, _at(_WEDNESDAY, 15, 59, 5) + int(timedelta(minutes=1).total_seconds() * 1000))
    market = _Market()

    await _pass(repo, market)

    assert repo.active_run(SID) is not None
    assert market.submitted_legs == []


# ── a sale that sells only in regular hours, however it is re-driven ────────


def _accepted(repo: ClerkSqliteRepository, effect_operation_id: str) -> ExitAcceptedFacts:
    row = repo.first_effect_transition(effect_operation_id=effect_operation_id, transition_kind="EXIT_ACCEPTED")
    assert row is not None
    return ExitAcceptedFacts.from_facts_json(row["facts_json"])


async def test_a_refused_end_sale_is_redriven_as_a_market_order_at_the_next_open_never_after_hours(
    repo: ClerkSqliteRepository, schedule: _Schedule,
) -> None:
    """#2607 review: Alpaca refuses the sale at 15:59; the watchdog's re-drive of it
    sells only as the sale itself does -- a market order in the regular session --
    never an extended-hours limit priced after the close or in the pre-market,
    however live the quote. So the end's "ended" words stay true."""
    await _hold_ten(repo)
    schedule.ends[SID] = _end("SELL")
    _walk_clock_to(repo, _at(_WEDNESDAY, 15, 59, 5))
    refusing = _FakeTradePort(submit_error=_wash_trade_rejection())

    await _pass(repo, refusing)

    [sale] = _end_sales(repo)
    assert repo.effect_operation(sale).state == "failed"
    assert _accepted(repo, sale).regular_session_only is True
    assert [end for end, _at_ms in schedule.carried_out] == [_end("SELL")]

    redrive = _FakeTradePort()
    pricing = _live_touch()
    for instant in (
        _at(_WEDNESDAY, 16, 1), _at(_WEDNESDAY, 16, 6), _at(_WEDNESDAY, 19, 30),
        _at(_THURSDAY, 4, 5), _at(_THURSDAY, 8), _at(_THURSDAY, 9, 29),
    ):
        _walk_clock_to(repo, instant)
        await _pass(repo, redrive, pricing=pricing)
    assert redrive.submitted_legs == [], "the refused end sale was re-driven outside the regular session"

    _walk_clock_to(repo, _at(_THURSDAY, 9, 30, 5))
    await _pass(repo, redrive, pricing=pricing)

    [leg] = redrive.submitted_legs
    assert (leg.side, leg.quantity, leg.order_type, leg.extended_hours) == (OrderSide.SELL, 10, OrderType.MARKET, False)
    [redriven] = [
        row[0] for row in repo._conn.execute(
            "SELECT effect_operation_id FROM effect_operations WHERE strategy_instance_id = ? AND kind = 'EXIT'", (SID,),
        ).fetchall() if EXIT_REDRIVE_DECISION_PREFIX in row[0]
    ]
    assert _accepted(repo, redriven).regular_session_only is True


async def test_an_end_found_after_a_half_day_close_waits_for_the_next_sessions_open(
    repo: ClerkSqliteRepository, schedule: _Schedule,
) -> None:
    """Found at 13:00:30 on a half-day, inside its post-market: nothing goes out
    that afternoon, the weekend or Monday's pre-market; Monday's open sells at market."""
    half_day, monday = date(2026, 11, 27), date(2026, 11, 30)
    await _hold_ten(repo)
    submit_stop_run(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id=RUN_ID, operator_reason="runner_gone",
    )
    schedule.ends[SID] = _end("SELL", end_at_ms=_at(half_day, 12, 59))
    market = _Market()
    pricing = _live_touch()

    for instant in (_at(half_day, 13, 0, 30), _at(date(2026, 11, 28), 11), _at(monday, 4, 0, 5)):
        _walk_clock_to(repo, instant)
        await _pass(repo, market, pricing=pricing)
    assert market.submitted_legs == []

    _walk_clock_to(repo, _at(monday, 9, 30, 5))
    await _pass(repo, market, pricing=pricing)

    assert [(leg.order_type, leg.extended_hours) for leg in market.submitted_legs] == [(OrderType.MARKET, False)]


# ── the owner's Stop in the minute of the end ────────────────────────────────


def test_an_operators_stop_after_the_end_stopped_the_run_is_the_stop_already_committed(
    repo: ClerkSqliteRepository,
) -> None:
    """#2607 review: the owner's Stop landing in the minute the Clerk stopped the run
    at its end names the same run under another reason. It is that STOP, never a conflict."""
    first = submit_stop_run(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id=RUN_ID,
        operator_reason=SCHEDULED_END_REASON,
    )

    second = submit_stop_run(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id=RUN_ID, operator_reason="operator_stop",
    )

    assert second.created is False
    assert second.command.command_id == first.command.command_id
    assert _stop_reason(repo) == SCHEDULED_END_REASON


async def test_an_operators_stop_as_the_end_comes_cancels_the_sale(
    repo: ClerkSqliteRepository, schedule: _Schedule,
) -> None:
    """#2607 review: the owner's Stop cancels the end -- Stop keeps the shares -- so a
    Stop landing while the pass carries the end out leaves nothing to sell."""
    await _hold_ten(repo)
    schedule.ends[SID] = _end("SELL")

    def owners_stop_lands(strategy_instance_id: str, lifecycle_run_id: str) -> None:
        schedule.stopped.append((strategy_instance_id, lifecycle_run_id))
        schedule.ends.pop(strategy_instance_id)  # the owner's Stop clears the bot's end

    schedule.stop_bot_at_its_end = owners_stop_lands  # type: ignore[method-assign]
    _walk_clock_to(repo, _at(_WEDNESDAY, 15, 59, 5))
    market = _Market()

    await _pass(repo, market)

    assert repo.active_run(SID) is None
    assert market.submitted_legs == []
    assert _end_sales(repo) == []
    assert repo.position(SID, "SPY") == 10


async def test_the_raw_stop_route_cancels_the_end_so_the_pass_at_the_end_sells_nothing(
    repo: ClerkSqliteRepository, tmp_path: Path,
) -> None:
    """#2664: the raw ``runs/stop`` route (fleet op ``custody_runs_stop``) is an operator's
    Stop too. It used to commit its STOP and leave a SELL end pending, so the Clerk's pass at
    the end time sold the shares the operator meant to keep. It now cancels the end, durably,
    through the runner that keeps it, before its STOP commits."""
    await _hold_ten(repo)
    runner = _runner_registry(tmp_path / "runner", None)
    desired = DesiredStateRepo(stable_desired_state_path(tmp_path / "runner", SID))
    desired.set(DesiredState.RUNNING, updated_by="deploy", now_ms=repo.clock(), reason="deploy", end=_end("SELL").end)
    port = _Market()
    facade = SqliteAlpacaClerkFacade(repo=repo, read=_FakeReadPort(), trade=port, account_mode="paper")
    app = FastAPI()
    app.include_router(alpaca_clerk_sqlite.router)
    set_active_clerk_runtime(ActiveClerkRuntime(authority_kind="sqlite", clerk=facade))
    set_bot_task_registry(runner)
    install_bot_end_schedule(runner)
    try:
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            stop = await client.post(
                f"/api/alpaca-clerk-sqlite/accounts/{ACCOUNT_ID}/bots/{SID}/runs/stop",
                json={"lifecycle_run_id": RUN_ID, "operator_reason": "operator stop"},
            )
        _walk_clock_to(repo, _at(_WEDNESDAY, 15, 59, 5))
        await _pass(repo, port)
    finally:
        install_bot_end_schedule(None)
        set_bot_task_registry(None)
        set_active_clerk_runtime(None)

    assert stop.status_code == 202
    assert _stop_reason(repo) == "operator stop"
    assert port.submitted_legs == []
    assert _end_sales(repo) == []
    assert repo.position(SID, "SPY") == 10
    assert runner.pending_ends([SID]) == []
    # The runner has no process for the bot; the operator's Stop is still recorded.
    record = desired.read()
    assert record is not None and (record.desired_state, record.end) == (DesiredState.STOPPED, None)


async def test_the_raw_stop_route_naming_another_run_leaves_the_running_ones_end(
    repo: ClerkSqliteRepository, tmp_path: Path,
) -> None:
    """#2664 review: a raw Stop naming a run that is not the bot's active one -- a stale id, or
    a lost-response retry arriving after a redeploy -- stops nothing of the running run, so it
    cancels nothing of its end either: the pass at the end still sells."""
    await _hold_ten(repo)
    runner = _runner_registry(tmp_path / "runner", None)
    desired = DesiredStateRepo(stable_desired_state_path(tmp_path / "runner", SID))
    desired.set(DesiredState.RUNNING, updated_by="deploy", now_ms=repo.clock(), reason="deploy", end=_end("SELL").end)
    facade = SqliteAlpacaClerkFacade(repo=repo, read=_FakeReadPort(), trade=_Market(), account_mode="paper")
    app = FastAPI()
    app.include_router(alpaca_clerk_sqlite.router)
    set_active_clerk_runtime(ActiveClerkRuntime(authority_kind="sqlite", clerk=facade))
    set_bot_task_registry(runner)
    try:
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            stop = await client.post(
                f"/api/alpaca-clerk-sqlite/accounts/{ACCOUNT_ID}/bots/{SID}/runs/stop",
                json={"lifecycle_run_id": "an-earlier-run", "operator_reason": "operator stop"},
            )
    finally:
        set_bot_task_registry(None)
        set_active_clerk_runtime(None)

    assert stop.status_code == 404
    assert repo.active_run(SID) is not None
    assert runner.pending_ends([SID]) == [ScheduledEnd(strategy_instance_id=SID, end=_end("SELL").end)]


async def test_the_raw_stop_route_leaves_the_end_of_a_bot_this_account_does_not_run(
    repo: ClerkSqliteRepository, tmp_path: Path,
) -> None:
    """#2664: a raw Stop naming a bot this account's authority has no registration for -- a Dry
    Run's, which runs in its own simulator -- is refused, and cancels no end on its way."""
    runner = _runner_registry(tmp_path / "runner", None)
    desired = DesiredStateRepo(stable_desired_state_path(tmp_path / "runner", "dry-run-bot"))
    desired.set(DesiredState.RUNNING, updated_by="deploy", now_ms=repo.clock(), reason="deploy", end=_end("SELL").end)
    facade = SqliteAlpacaClerkFacade(repo=repo, read=_FakeReadPort(), trade=_Market(), account_mode="paper")
    app = FastAPI()
    app.include_router(alpaca_clerk_sqlite.router)
    set_active_clerk_runtime(ActiveClerkRuntime(authority_kind="sqlite", clerk=facade))
    set_bot_task_registry(runner)
    try:
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            stop = await client.post(
                f"/api/alpaca-clerk-sqlite/accounts/{ACCOUNT_ID}/bots/dry-run-bot/runs/stop",
                json={"lifecycle_run_id": RUN_ID, "operator_reason": "operator stop"},
            )
    finally:
        set_bot_task_registry(None)
        set_active_clerk_runtime(None)

    assert stop.status_code == 404
    assert runner.pending_ends(["dry-run-bot"]) == [
        ScheduledEnd(strategy_instance_id="dry-run-bot", end=_end("SELL").end)
    ]


_LIVE_SID = "live-bot"
_LIVE_STOP_PATH = f"/api/alpaca-clerk-sqlite/accounts/{ACCOUNT_ID}/bots/{_LIVE_SID}/runs/stop"


@pytest.fixture
def runner_duty_clerk(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """What the runner's suites give a bot they deploy: live market facts and a duty Clerk of its own."""
    patch_fresh_live_market_liveness(monkeypatch)
    set_alpaca_clerk(_CustodyClerk(_custody_proof(exposure={})))
    yield
    set_alpaca_clerk(None)


async def _deployed_in_the_runner(repo: ClerkSqliteRepository, root: Path) -> tuple[BotTaskRegistry, str]:
    """A bot whose process runs in the runner, its SELL end pending, and its run's id.

    The runner deploys it through the duty Clerk its suites give it, on the wall clock they
    pin, before the end. The bot is registered at the account's authority, whose run the
    caller starts under the runner's run id.
    """
    runner = _runner_registry(root, _FakeFeed([], mode="hold"))
    deployed = await runner.deploy(
        exit_terms=DEPLOY_EXIT_TERMS, broker="alpaca", strategy_instance_id=_LIVE_SID, symbol="SPY",
        end=_end("SELL").end,
    )
    assert deployed.active_run_id is not None and runner.status("alpaca", _LIVE_SID).running
    repo.register_strategy_instance(strategy_instance_id=_LIVE_SID, symbol="SPY", config_hash="h1")
    return runner, deployed.active_run_id


@asynccontextmanager
async def _the_raw_route(repo: ClerkSqliteRepository, runner: BotTaskRegistry) -> AsyncIterator[httpx.AsyncClient]:
    """A client of the raw route over ``repo``'s authority, with ``runner`` as the process's bot runner.

    That authority is then the process's one Clerk, the runner's included. On the way out the
    runner shuts down while it is still installed, as a service shutdown would.
    """
    facade = SqliteAlpacaClerkFacade(repo=repo, read=_FakeReadPort(), trade=_Market(), account_mode="paper")
    app = FastAPI()
    app.include_router(alpaca_clerk_sqlite.router)
    set_active_clerk_runtime(ActiveClerkRuntime(authority_kind="sqlite", clerk=facade))
    set_bot_task_registry(runner)
    try:
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client
    finally:
        await runner.stop_all()
        set_bot_task_registry(None)
        set_active_clerk_runtime(None)


@pytest.mark.usefixtures("runner_duty_clerk")
async def test_the_raw_stop_route_stops_the_bots_process_and_records_the_operators_stop(
    repo: ClerkSqliteRepository, tmp_path: Path,
) -> None:
    """#2664: the raw Stop fenced the run at the Clerk but left the bot's process in the runner
    consuming bars, its intent still RUNNING. Once its STOP is durable it now stops the process,
    as the panel's Stop does, and records the operator's Stop: the intent STOPPED, the end
    cancelled -- the process stop revives nothing the Stop cancelled."""
    runner, run_id = await _deployed_in_the_runner(repo, tmp_path / "runner")
    submit_start_run(repo, account_id=ACCOUNT_ID, strategy_instance_id=_LIVE_SID, lifecycle_run_id=run_id)

    async with _the_raw_route(repo, runner) as client:
        stop = await client.post(_LIVE_STOP_PATH, json={"lifecycle_run_id": run_id, "operator_reason": "operator stop"})
        status = runner.status("alpaca", _LIVE_SID)

    assert stop.status_code == 202
    assert repo.active_run(_LIVE_SID) is None
    assert status.running is False
    assert status.duty_outcome is not None
    assert (status.duty_outcome.kind, status.duty_outcome.reason_code) == ("STOPPED", "OPERATOR_STOP")
    assert runner.pending_ends([_LIVE_SID]) == []
    record = DesiredStateRepo(stable_desired_state_path(tmp_path / "runner", _LIVE_SID)).read()
    assert record is not None
    assert (record.desired_state, record.updated_by, record.reason, record.end) == (
        DesiredState.STOPPED, "operator_runs_stop", "operator stop", None,
    )


@pytest.mark.usefixtures("runner_duty_clerk")
async def test_the_raw_stop_route_naming_another_run_leaves_the_running_process_alone(
    repo: ClerkSqliteRepository, tmp_path: Path,
) -> None:
    """#2664: only a Stop of the bot's active run stops its process. A Stop naming a run the
    bot never had is refused; a lost-response retry of an earlier run's Stop, arriving after
    a redeploy, is replayed -- 202, that earlier STOP. Neither touches the running process,
    its intent or its end."""
    runner, run_id = await _deployed_in_the_runner(repo, tmp_path / "runner")
    submit_start_run(repo, account_id=ACCOUNT_ID, strategy_instance_id=_LIVE_SID, lifecycle_run_id="an-earlier-run")
    submit_stop_run(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=_LIVE_SID, lifecycle_run_id="an-earlier-run",
        operator_reason="operator stop",
    )
    submit_start_run(repo, account_id=ACCOUNT_ID, strategy_instance_id=_LIVE_SID, lifecycle_run_id=run_id)

    async with _the_raw_route(repo, runner) as client:
        refused = await client.post(
            _LIVE_STOP_PATH, json={"lifecycle_run_id": "a-run-it-never-had", "operator_reason": "operator stop"},
        )
        replayed = await client.post(
            _LIVE_STOP_PATH, json={"lifecycle_run_id": "an-earlier-run", "operator_reason": "operator stop"},
        )
        active = repo.active_run(_LIVE_SID)
        running = runner.status("alpaca", _LIVE_SID).running
        pending = runner.pending_ends([_LIVE_SID])
        record = DesiredStateRepo(stable_desired_state_path(tmp_path / "runner", _LIVE_SID)).read()

    assert refused.status_code == 404
    assert replayed.status_code == 202
    assert active is not None and active.lifecycle_run_id == run_id
    assert running is True
    assert pending == [ScheduledEnd(strategy_instance_id=_LIVE_SID, end=_end("SELL").end)]
    assert record is not None and record.desired_state is DesiredState.RUNNING


async def test_a_lost_execution_lease_while_finishing_an_end_fails_the_pass(
    repo: ClerkSqliteRepository, schedule: _Schedule, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#2607 review: as in the fence and in every other write, a lost lease is the pass's failure, never one bot's."""
    await _hold_ten(repo)
    schedule.ends[SID] = _end("SELL")
    _walk_clock_to(repo, _at(_WEDNESDAY, 15, 59, 5))

    def lease_lost(*_args: object, **_kwargs: object) -> bool:
        raise ExecutionLeaseLost("the execution lease lapsed", account_id=ACCOUNT_ID)

    monkeypatch.setattr(scheduled_end, "_carried_out", lease_lost)

    with pytest.raises(ExecutionLeaseLost):
        await _pass(repo, _Market())


# ── the attention bell: a sale waiting for the open ──────────────────────────


async def test_a_sale_waiting_for_the_open_rings_the_attention_bell_until_it_is_sent(
    repo: ClerkSqliteRepository, schedule: _Schedule, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Owner decision 2026-09-29 ("Alert bell too"): the waiting sale is one bell line,
    naming the bot, the symbol and the open it goes out at; it clears once sent."""
    await _hold_ten(repo)
    submit_stop_run(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id=RUN_ID,
        operator_reason="service_restart_recovery",
    )
    schedule.ends[SID] = _end("SELL")
    market = _Market()
    monkeypatch.setattr(lane_summary, "get_active_clerk_runtime", lambda: SimpleNamespace(
        sqlite_repository=repo, clerk=None, startup_failure=None, selected_account_authority_kind="real_paper",
    ))

    _walk_clock_to(repo, _at(_WEDNESDAY, 17))
    await _pass(repo, market, pricing=_live_touch())

    assert end_sales_waiting_for_open(repo) == [EndSaleWaiting(strategy_instance_id=SID, symbol="SPY", quantity=10.0)]
    [line] = [
        item for item in (await lane_summary.lane_attention_read()).items
        if item.reason_code == "SCHEDULED_END_WAITS_FOR_OPEN"
    ]
    assert line.condition_id == f"end-sale-waits:{SID}"
    assert line.reason_code == "SCHEDULED_END_WAITS_FOR_OPEN"
    assert (line.kind, line.severity, line.symbol, line.action.destination) == ("exit", "warning", "SPY", "bot")
    assert line.headline == (
        f"{SID} reached its end while the market was closed. "
        "Its sale of 10 SPY goes out at the open, Thu Oct 1, 09:30 ET."
    )

    _walk_clock_to(repo, _at(_THURSDAY, 9, 30, 5))
    await _pass(repo, market, pricing=_live_touch())

    assert len(_sold_market(market)) == 1
    assert end_sales_waiting_for_open(repo) == []
    assert not [
        item for item in (await lane_summary.lane_attention_read()).items
        if item.reason_code == "SCHEDULED_END_WAITS_FOR_OPEN"
    ]
