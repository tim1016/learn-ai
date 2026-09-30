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
from collections.abc import Iterator, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.broker.alpaca.clerk.recovery_reduction import UNPRICEABLE_RECOVERY, RecoveryPricing
from app.broker.alpaca.clerk.sqlite.commands import submit_start_run, submit_stop_run
from app.broker.alpaca.clerk.sqlite.exit import accept_exit
from app.broker.alpaca.clerk.sqlite.exit_resolution import SCHEDULED_END_DECISION_PREFIX, resolve_exit
from app.broker.alpaca.clerk.sqlite.intake_fence import ReentrantAsyncLock
from app.broker.alpaca.clerk.sqlite.reconcile import reconcile_account
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.scheduled_end import (
    SCHEDULED_END_REASON,
    ScheduledEnd,
    install_bot_end_schedule,
)
from app.broker.alpaca.clerk.trade_evidence import SqliteTradeUpdateEvidenceSink
from app.broker.contract.errors import BrokerUnavailable
from app.broker.contract.models import BrokerOrder, BrokerOrderEvent, BrokerOrderLeg, OrderSide, OrderType
from app.schemas.bot_end import BotEnd
from app.utils.timestamps import to_ms_utc
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


# ── the step without an installed schedule ───────────────────────────────────


async def test_a_pass_with_no_schedule_installed_ends_nothing(repo: ClerkSqliteRepository) -> None:
    await _hold_ten(repo)
    _walk_clock_to(repo, _at(_WEDNESDAY, 15, 59, 5) + int(timedelta(minutes=1).total_seconds() * 1000))
    market = _Market()

    await _pass(repo, market)

    assert repo.active_run(SID) is not None
    assert market.submitted_legs == []
