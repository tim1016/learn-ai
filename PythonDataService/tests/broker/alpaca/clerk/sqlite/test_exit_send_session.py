"""#2440 (owner decision #2431): whether an EXIT's reducing leg may go out is decided when it is sent.

The deciding program shapes a leg for the instant it decided. The reduction
can be created and sent later — the entry's cancellation defers it, an outage
or a restart delays it — and by then the session may have ended. A market DAY
leg sent after the regular close is queued by Alpaca for the next open; that
is never what an EXIT means. So every EXIT — a program's and a recovery's
alike — passes one send-time rule: a leg that can still go out is sent; one
that cannot is re-priced for the session open now (an extended-hours limit off
the live touch in PRE/POST); and when nothing can price it, or its order
already carries a broker identity, nothing is sent and the operator sees the
``EXIT_NOT_FLAT`` episode.

Reproduces Codex finding R1 (``research/codex-2419``,
``docs/references/codex-review-2419.md``): a program EXIT accepted at 15:59
and first driven at 16:01 used to submit a market DAY SELL with
``extended_hours=False``.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.broker.alpaca.clerk.program_leg import LegShape, ProgramLegPolicy
from app.broker.alpaca.clerk.recovery_reduction import (
    UNPRICEABLE_RECOVERY,
    RecoveryPricing,
)
from app.broker.alpaca.clerk.sqlite.commands import submit_start_run
from app.broker.alpaca.clerk.sqlite.exit import accept_exit, accept_recovery_exit
from app.broker.alpaca.clerk.sqlite.exit_resolution import resolve_exit
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty import EXIT_NOT_FLAT_REASON_CODE
from app.broker.alpaca.marketable_limit import ExtendedHoursAllowances
from app.broker.contract.capabilities import ExtendedHoursWindow
from app.broker.contract.errors import BrokerUnavailable
from app.broker.contract.models import OrderSide, OrderType, TimeInForce
from app.schemas.market_liveness import TopOfBookQuote
from app.utils.timestamps import to_ms_utc
from tests.broker.alpaca.clerk.sqlite.conftest import _clock_at, _walk_clock_to
from tests.broker.alpaca.clerk.sqlite.test_exit import (
    ACCOUNT_ID,
    RUN_ID,
    SID,
    _broker_order,
    _FakeTrade,
    _make_entry,
)

_ET = ZoneInfo("America/New_York")
_WEDNESDAY = date(2026, 9, 2)
_EARLY_CLOSE_DAY = date(2026, 11, 27)  # the day after Thanksgiving: the close is 13:00
_POLICY = ProgramLegPolicy(
    window=ExtendedHoursWindow(open_minute_et=4 * 60, close_minute_et=20 * 60),
    allowances=ExtendedHoursAllowances(entry_bps=Decimal("10"), exit_bps=Decimal("20")),
)


def _at(hour: int, minute: int = 0, second: int = 0, *, day: date = _WEDNESDAY) -> int:
    return to_ms_utc(datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=_ET))


def _live_touch(bid: float = 100.00, ask: float = 100.05) -> RecoveryPricing:
    """The sealed policy and a fresh IBKR touch at whatever instant is asked."""

    def quote(symbol: str, now_ms: int) -> TopOfBookQuote:
        return TopOfBookQuote(
            symbol=symbol, bid=bid, ask=ask, source="ibkr.market_data.status", observed_at_ms=now_ms
        )

    return RecoveryPricing(policy_source=lambda: _POLICY, quote_source=quote)


@pytest.fixture
def repo(tmp_path: Path) -> Iterator[ClerkSqliteRepository]:
    """A Clerk whose clock starts at 15:59 ET on an ordinary Wednesday."""
    r = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID,
        artifacts_root=tmp_path,
        clock=_clock_at(_at(15, 59)),
        lease_ttl_ms=300_000,
    )
    r.register_strategy_instance(strategy_instance_id=SID, symbol="SPY", config_hash="h1")
    submit_start_run(r, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id=RUN_ID)
    yield r
    r.close()


def _accept_program_exit(
    repo: ClerkSqliteRepository,
    entry_ref: str,
    *,
    shape: LegShape | None = None,
    valid_until_ms: int | None = None,
) -> str:
    accepted = accept_exit(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="program-exit-1",
        lifecycle_run_id=RUN_ID,
        entry_order_ref=entry_ref,
        reducing_shape=shape,
        reducing_valid_until_ms=valid_until_ms,
    )
    assert accepted.effect_operation_id is not None
    return accepted.effect_operation_id


def _acked() -> _FakeTrade:
    return _FakeTrade(submit_result=_broker_order("placeholder", side="sell", status="accepted"))


def _exit_not_flat(repo: ClerkSqliteRepository) -> dict | None:
    return repo.active_uncertainty(
        scope="CUSTODY_SUBJECT", reason_code=EXIT_NOT_FLAT_REASON_CODE, strategy_instance_id=SID
    )


async def test_a_program_exit_first_driven_after_the_close_is_never_a_queued_market_order(
    repo: ClerkSqliteRepository,
) -> None:
    """Codex R1, verbatim: accepted at 15:59 in the regular session, driven at 16:01.

    With nothing that can price an after-hours limit (the degraded pricing
    seam), nothing is sent at all — and the operator is told, on the bot page
    and in the lane's attention bell, that the position is still open.
    """
    entry_ref = await _make_entry(repo, status="filled", filled_quantity=10)
    effect_operation_id = _accept_program_exit(repo, entry_ref)
    _walk_clock_to(repo, _at(16, 1))
    trade = _acked()

    await resolve_exit(repo, effect_operation_id=effect_operation_id, trade=trade)

    assert trade.submit_calls == [], "a market DAY reduction was submitted after the close"
    assert repo.position(SID, "SPY") == 10
    effect = repo.effect_operation(effect_operation_id)
    assert effect is not None and effect.state == "failed"
    # Released, not queued: the entry is free for a priced reduction now.
    assert repo.active_exit_for_order(entry_ref) is None
    episode = _exit_not_flat(repo)
    assert episode is not None
    assert episode["headline"] == (
        "An exit could not be sent after its session ended; the position is still open"
    )
    assert "10 SPY is still held" in episode["explanation"]
    # Why no after-hours limit replaced it rides in the explanation.
    assert "No trading session is open now" in episode["explanation"]


@pytest.mark.parametrize(
    ("decided", "sent", "day"),
    [
        pytest.param((15, 59), (16, 0, 30), _WEDNESDAY, id="regular-close"),
        pytest.param((12, 59), (13, 0, 30), _EARLY_CLOSE_DAY, id="early-close-from-the-calendar"),
    ],
)
async def test_a_program_exit_delayed_past_the_close_goes_out_as_an_after_hours_limit(
    tmp_path: Path,
    decided: tuple[int, int],
    sent: tuple[int, int, int],
    day: date,
) -> None:
    """Delayed by the entry's cancellation: decided in the session, created after it.

    The first pass cancels the still-working entry and defers; the sweep's
    next pass proves it terminal after the close and creates the reduction.
    That reduction is the after-hours limit the owner decided on — the live
    bid less the sealed exit allowance, extended hours, DAY — never the
    decision's market leg. The close is the canonical calendar's, so 13:00 on
    an early-close day is the boundary exactly as 16:00 is on any other.
    """
    repo = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID,
        artifacts_root=tmp_path,
        clock=_clock_at(_at(*decided, day=day)),
        lease_ttl_ms=300_000,
    )
    try:
        repo.register_strategy_instance(strategy_instance_id=SID, symbol="SPY", config_hash="h1")
        submit_start_run(repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id=RUN_ID)
        entry_ref = await _make_entry(repo, status="partially_filled", filled_quantity=6)
        effect_operation_id = _accept_program_exit(repo, entry_ref)
        still_working = _FakeTrade(
            lookup_results=[_broker_order(entry_ref, status="partially_filled", filled_quantity=6)]
        )
        deferred = await resolve_exit(
            repo, effect_operation_id=effect_operation_id, trade=still_working, pricing=_live_touch()
        )
        assert deferred.reducing_order_ref is None and still_working.submit_calls == []

        _walk_clock_to(repo, _at(*sent, day=day))
        cancelled = _broker_order(
            entry_ref, status="canceled", filled_quantity=6, filled_avg_price=100.0
        )
        sweep = _FakeTrade(
            lookup_results=[cancelled, cancelled],
            submit_result=_broker_order("placeholder", side="sell", status="accepted"),
        )
        await resolve_exit(
            repo, effect_operation_id=effect_operation_id, trade=sweep, pricing=_live_touch()
        )

        ((leg, _client_order_id),) = sweep.submit_calls
        assert (leg.side, leg.quantity, leg.order_type, leg.time_in_force, leg.extended_hours) == (
            OrderSide.SELL,
            6,
            OrderType.LIMIT,
            TimeInForce.DAY,
            True,
        )
        assert leg.limit_price == 99.80  # floor_tick(100.00 × (1 − 20 / 10⁴))
        effect = repo.effect_operation(effect_operation_id)
        assert effect is not None and effect.state not in ("failed", "rejected")
        assert _exit_not_flat(repo) is None
    finally:
        repo.close()


async def test_a_market_leg_whose_submit_was_lost_before_the_close_is_not_resent_after_it(
    repo: ClerkSqliteRepository,
) -> None:
    """An outage: created and sent at 15:59:50, the answer lost; resumed after 16:00.

    The order already has its client identity, so it is replayed exactly or
    not at all — never re-priced under the same identity. Past the close it
    is not replayed: the EXIT folds releasably for the operator, and the
    watchdog's re-drive prices a fresh after-hours limit.
    """
    entry_ref = await _make_entry(repo, status="filled", filled_quantity=10)
    _walk_clock_to(repo, _at(15, 59, 50))
    effect_operation_id = _accept_program_exit(repo, entry_ref)
    lost = _FakeTrade(submit_error=BrokerUnavailable("timeout"))
    first = await resolve_exit(
        repo, effect_operation_id=effect_operation_id, trade=lost, pricing=_live_touch()
    )
    ((sent_leg, _),) = lost.submit_calls
    assert (sent_leg.order_type, sent_leg.extended_hours) == (OrderType.MARKET, False)
    assert first.reducing_order_ref is not None

    _walk_clock_to(repo, _at(16, 0, 40))  # past the 30 s submit-absence grace
    resumed = _FakeTrade(lookup_results=[None])
    await resolve_exit(
        repo, effect_operation_id=effect_operation_id, trade=resumed, pricing=_live_touch()
    )

    assert resumed.submit_calls == []
    effect = repo.effect_operation(effect_operation_id)
    assert effect is not None and effect.state == "failed"
    assert repo.active_exit_for_order(entry_ref) is None
    assert _exit_not_flat(repo) is not None


async def test_a_program_after_hours_limit_is_never_sent_past_the_session_it_was_priced_for(
    repo: ClerkSqliteRepository,
) -> None:
    """A POST decision's limit, deferred past 20:00: no session is open to take it.

    Alpaca would hold a DAY order sent after the after-hours close for the
    next day. Nothing is sent; the operator is told, and the watchdog prices
    a fresh limit once the next session opens.
    """
    _walk_clock_to(repo, _at(19, 59))
    entry_ref = await _make_entry(repo, status="filled", filled_quantity=10)
    effect_operation_id = _accept_program_exit(
        repo,
        entry_ref,
        shape=LegShape(
            order_type=OrderType.LIMIT,
            time_in_force=TimeInForce.DAY,
            limit_price=99.80,
            extended_hours=True,
            side=OrderSide.SELL,
        ),
        valid_until_ms=_at(20, 0),
    )
    _walk_clock_to(repo, _at(20, 0, 30))
    trade = _acked()

    await resolve_exit(repo, effect_operation_id=effect_operation_id, trade=trade, pricing=_live_touch())

    assert trade.submit_calls == []
    episode = _exit_not_flat(repo)
    assert episode is not None
    assert "limit could not go out" in episode["explanation"]


async def test_a_pre_market_limit_deferred_into_the_regular_session_goes_out_at_market(
    repo: ClerkSqliteRepository,
) -> None:
    """A price is never carried into another session: at 09:30 the leg is the market DAY one."""
    repo_clock_day = date(2026, 9, 3)
    _walk_clock_to(repo, _at(9, 29, day=repo_clock_day))
    entry_ref = await _make_entry(repo, status="filled", filled_quantity=10)
    effect_operation_id = _accept_program_exit(
        repo,
        entry_ref,
        shape=LegShape(
            order_type=OrderType.LIMIT,
            time_in_force=TimeInForce.DAY,
            limit_price=99.80,
            extended_hours=True,
            side=OrderSide.SELL,
        ),
        valid_until_ms=_at(9, 30, day=repo_clock_day),
    )
    _walk_clock_to(repo, _at(9, 30, 20, day=repo_clock_day))
    trade = _acked()

    await resolve_exit(
        repo, effect_operation_id=effect_operation_id, trade=trade, pricing=UNPRICEABLE_RECOVERY
    )

    ((leg, _),) = trade.submit_calls
    assert (leg.order_type, leg.limit_price, leg.extended_hours) == (OrderType.MARKET, None, False)


async def test_an_exit_sent_inside_the_regular_session_is_unchanged(
    repo: ClerkSqliteRepository,
) -> None:
    """#2440 AC: exits sent during regular hours keep the market DAY leg."""
    entry_ref = await _make_entry(repo, status="filled", filled_quantity=10)
    effect_operation_id = _accept_program_exit(repo, entry_ref)
    _walk_clock_to(repo, _at(15, 59, 30))
    trade = _acked()

    await resolve_exit(repo, effect_operation_id=effect_operation_id, trade=trade, pricing=_live_touch())

    ((leg, _),) = trade.submit_calls
    assert (leg.order_type, leg.time_in_force, leg.limit_price, leg.extended_hours, leg.quantity) == (
        OrderType.MARKET,
        TimeInForce.DAY,
        None,
        False,
        10,
    )


@pytest.mark.parametrize("recovery", [False, True], ids=["program-exit", "recovery-exit"])
async def test_program_and_recovery_exits_follow_the_same_send_time_rule(
    repo: ClerkSqliteRepository, recovery: bool
) -> None:
    """One decision point, not two copies: the same instants give the same leg.

    Before #2440 the send-time session guard applied only to recovery EXITs,
    and a recovery EXIT past the close folded and waited for the watchdog.
    Both now go out at once as the after-hours limit.
    """
    entry_ref = await _make_entry(repo, status="filled", filled_quantity=10)
    if recovery:
        accepted = accept_recovery_exit(
            repo,
            account_id=ACCOUNT_ID,
            strategy_instance_id=SID,
            decision_id="recovery-flatten-samerule01",
            entry_order_ref=entry_ref,
        )
        assert accepted.effect_operation_id is not None
        effect_operation_id = accepted.effect_operation_id
    else:
        effect_operation_id = _accept_program_exit(repo, entry_ref)
    _walk_clock_to(repo, _at(16, 1))
    trade = _acked()

    await resolve_exit(repo, effect_operation_id=effect_operation_id, trade=trade, pricing=_live_touch())

    ((leg, _),) = trade.submit_calls
    assert (leg.order_type, leg.limit_price, leg.extended_hours) == (OrderType.LIMIT, 99.8, True)
