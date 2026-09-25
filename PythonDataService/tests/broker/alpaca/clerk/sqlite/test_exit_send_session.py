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

import json
import threading
from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.broker.alpaca.clerk.program_leg import LegShape, ProgramLeg, ProgramLegPolicy
from app.broker.alpaca.clerk.recovery_reduction import (
    UNPRICEABLE_RECOVERY,
    RecoveryPricing,
    next_redrive_at_ms,
    price_automatic_recovery_reduction,
)
from app.broker.alpaca.clerk.sqlite.commands import submit_start_run
from app.broker.alpaca.clerk.sqlite.exit import accept_exit, accept_recovery_exit
from app.broker.alpaca.clerk.sqlite.exit_resolution import (
    EXIT_REDRIVE_DECISION_PREFIX,
    RECOVERY_FLATTEN_DECISION_PREFIX,
    priced_reduction_reference_price,
    resolve_exit,
)
from app.broker.alpaca.clerk.sqlite.facts import ExitReducingOrderCreatedFacts
from app.broker.alpaca.clerk.sqlite.off_loop import to_thread
from app.broker.alpaca.clerk.sqlite.projections import SqliteClerkProjectionReader
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty import (
    EXIT_NOT_FLAT_REASON_CODE,
    ORDER_OUTCOME_UNKNOWN_REASON_CODE,
)
from app.broker.alpaca.marketable_limit import ExtendedHoursAllowances
from app.broker.contract.capabilities import ExtendedHoursWindow
from app.broker.contract.errors import BrokerOrderRejected, BrokerUnavailable
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
        program_leg=None if shape is None else ProgramLeg(shape, valid_until_ms=valid_until_ms),
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
    """Codex R1's scenario: accepted at 15:59 in the regular session, driven at 16:01.

    With nothing that can price an after-hours limit (the degraded pricing
    seam), nothing is sent at all — and the operator is told, on the bot page
    and in the lane's attention bell, that the position is still open, and
    when the watchdog tries again. On master this test fails only with a
    ``TypeError`` (``resolve_exit`` took no ``pricing``); it pins the fold,
    not the regression. ``test_runtime_program_leg.py::
    test_a_regular_hours_exit_decided_on_the_last_bar_goes_out_as_an_after_hours_limit``
    is the one that fails on master for the behavioural reason — a MARKET/DAY
    order submitted after the close.
    """
    entry_ref = await _make_entry(repo, status="filled", filled_quantity=10)
    effect_operation_id = _accept_program_exit(repo, entry_ref)
    _walk_clock_to(repo, _at(16, 1))
    trade = _acked()

    await resolve_exit(repo, effect_operation_id=effect_operation_id, trade=trade, pricing=UNPRICEABLE_RECOVERY)

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
    # Why no after-hours limit replaced it rides in the explanation — and it is
    # the truth: after-hours IS open at 16:01; this Clerk cannot price it.
    assert "cannot price an extended-hours limit automatically" in episode["explanation"]
    assert "No trading session would be open" not in episode["explanation"]
    # The degraded seam prices nothing outside the regular session, so the
    # watchdog's next try is the first send that lands in Thursday's open —
    # the guard band before it (a time value, not prose).
    assert json.loads(episode["facts_json"])["next_attempt_at_ms"] == _at(
        9, 29, 55, day=date(2026, 9, 3)
    )


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


@pytest.mark.parametrize(
    ("decided_at", "sent_at", "recorded_limit", "reads"),
    [
        pytest.param((15, 59), (16, 1), False, 1, id="a-market-leg-past-the-close-is-re-priced"),
        pytest.param((15, 59), (15, 59, 30), False, 0, id="inside-the-regular-session-nothing-is-read"),
        pytest.param((17, 13), (17, 14), True, 0, id="a-sendable-after-hours-limit-reads-nothing"),
    ],
)
async def test_the_live_touch_is_read_on_the_event_loop_and_only_to_re_price(
    repo: ClerkSqliteRepository,
    decided_at: tuple[int, ...],
    sent_at: tuple[int, ...],
    recorded_limit: bool,
    reads: int,
) -> None:
    """#2440 review: the quote source is never called off the event loop, nor without need.

    The production source registers IBKR demand in the market-liveness store,
    whose symbol map the IBKR status loop iterates and replaces on the event
    loop. The sweep, restart recovery and the watchdog run the EXIT machine's
    repository steps on a worker thread (``off_loop=to_thread``), so the touch
    is read on the loop between two of them — and only for a leg that must be
    re-priced: a leg that goes out as recorded registers no demand at all.
    """
    loop_thread = threading.get_ident()
    read_on: list[int] = []

    def quote(symbol: str, now_ms: int) -> TopOfBookQuote:
        read_on.append(threading.get_ident())
        return TopOfBookQuote(
            symbol=symbol, bid=100.00, ask=100.05, source="ibkr.market_data.status", observed_at_ms=now_ms
        )

    _walk_clock_to(repo, _at(*decided_at))
    entry_ref = await _make_entry(repo, status="filled", filled_quantity=10)
    effect_operation_id = _accept_program_exit(
        repo,
        entry_ref,
        shape=(
            LegShape(
                order_type=OrderType.LIMIT,
                time_in_force=TimeInForce.DAY,
                limit_price=99.80,
                extended_hours=True,
                side=OrderSide.SELL,
            )
            if recorded_limit
            else None
        ),
        valid_until_ms=_at(20, 0) if recorded_limit else None,
    )
    _walk_clock_to(repo, _at(*sent_at))
    trade = _acked()

    await resolve_exit(
        repo,
        effect_operation_id=effect_operation_id,
        trade=trade,
        pricing=RecoveryPricing(policy_source=lambda: _POLICY, quote_source=quote),
        off_loop=to_thread,
    )

    assert len(trade.submit_calls) == 1
    assert len(read_on) == reads
    assert all(thread == loop_thread for thread in read_on), "the live touch was read off the event loop"


async def test_a_leg_the_clerk_prices_at_send_records_who_priced_it_and_its_reference_quote(
    repo: ClerkSqliteRepository,
) -> None:
    """#2440 review (#2229's invariant): a live-money limit the Clerk priced from an IBKR
    quote is recorded as the Clerk's, with the quote it was priced against.

    Without it a re-priced leg's fills had no slippage reference, and later
    copy could not tell the Clerk's price from an operator's confirmation.
    """
    entry_ref = await _make_entry(repo, status="filled", filled_quantity=10)
    effect_operation_id = _accept_program_exit(repo, entry_ref)
    _walk_clock_to(repo, _at(16, 1))
    trade = _acked()

    resolved = await resolve_exit(
        repo, effect_operation_id=effect_operation_id, trade=trade, pricing=_live_touch()
    )

    assert resolved.reducing_order_ref is not None
    transition = repo.first_order_transition(
        order_ref=resolved.reducing_order_ref, transition_kind="EXIT_REDUCING_ORDER_CREATED"
    )
    assert transition is not None
    created = ExitReducingOrderCreatedFacts.from_facts_json(transition["facts_json"])
    assert (created.order_type, created.limit_price, created.extended_hours, created.valid_until_ms) == (
        "limit",
        99.8,
        True,
        _at(20, 0),
    )
    assert (
        created.priced_by,
        created.reference_bid,
        created.reference_ask,
        created.reference_quote_observed_at_ms,
    ) == ("clerk", 100.00, 100.05, _at(16, 1))
    # A fill of it is measured from the bid it was priced against.
    assert priced_reduction_reference_price(repo, resolved.reducing_order_ref) == 100.00


async def test_a_clerk_priced_leg_that_expires_on_resubmit_never_claims_a_confirmation(
    repo: ClerkSqliteRepository,
) -> None:
    """#2440 review: the fold copy is chosen from who priced the order being judged.

    An operator's market flatten, driven at 19:59:50, is re-priced by the Clerk
    as an after-hours limit good until 20:00; the submit is lost. Resumed after
    the close, the order is never replayed — and the operator is not told that
    a price they never saw was "confirmed". This copy is what blocked the
    #2230 review.
    """
    entry_ref = await _make_entry(repo, status="filled", filled_quantity=10)
    accepted = accept_recovery_exit(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="recovery-flatten-repriced01",
        entry_order_ref=entry_ref,
    )
    assert accepted.effect_operation_id is not None
    _walk_clock_to(repo, _at(19, 59, 50))
    lost = _FakeTrade(submit_error=BrokerUnavailable("timeout"))
    first = await resolve_exit(
        repo, effect_operation_id=accepted.effect_operation_id, trade=lost, pricing=_live_touch()
    )
    ((sent, _),) = lost.submit_calls
    assert (sent.order_type, sent.limit_price, sent.extended_hours) == (OrderType.LIMIT, 99.8, True)
    assert first.reducing_order_ref is not None

    _walk_clock_to(repo, _at(20, 0, 40))  # past the 30 s submit-absence grace and the POST close
    resumed = _FakeTrade(lookup_results=[None])
    await resolve_exit(
        repo, effect_operation_id=accepted.effect_operation_id, trade=resumed, pricing=_live_touch()
    )

    assert resumed.submit_calls == []
    episode = _exit_not_flat(repo)
    assert episode is not None
    assert episode["explanation"].startswith("The Clerk-priced limit for SPY was not sent")
    assert "confirmed" not in episode["explanation"]
    (not_flat,) = [
        row
        for row in repo.transitions_for_order(first.reducing_order_ref)
        if row["transition_kind"] == "EXIT_NOT_FLAT"
    ]
    assert "confirmed" not in json.loads(not_flat["facts_json"])["reason"]


# ── #2440 review batch 2 ──────────────────────────────────────────────────────


def _raised_episode_writes(repo: ClerkSqliteRepository) -> int:
    return repo._conn.execute(
        "SELECT COUNT(*) FROM custody_transitions WHERE transition_kind IN "
        "('UNCERTAINTY_RAISED', 'UNCERTAINTY_REFRESHED')"
    ).fetchone()[0]


def _next_attempt_at_ms(episode: dict) -> int | None:
    return json.loads(episode["facts_json"]).get("next_attempt_at_ms")


def _open_unknown_outcome(repo: ClerkSqliteRepository) -> dict | None:
    return repo.active_uncertainty(
        scope="CUSTODY_SUBJECT", reason_code=ORDER_OUTCOME_UNKNOWN_REASON_CODE, strategy_instance_id=SID
    )


async def test_a_market_leg_judged_within_the_guard_band_of_the_close_goes_out_as_an_after_hours_limit(
    repo: ClerkSqliteRepository,
) -> None:
    """15:59:57 passes a bare ``now < 16:00`` check, but the order can reach Alpaca after 16:00.

    Judged at the instant it may arrive — now plus ``EXIT_SEND_GUARD_BAND_MS``
    — the market leg is already past the close, so it is re-priced for
    after-hours exactly as a leg driven at 16:00:30 is, never sent as a
    MARKET/DAY order Alpaca could queue for the next open.
    """
    entry_ref = await _make_entry(repo, status="filled", filled_quantity=10)
    effect_operation_id = _accept_program_exit(repo, entry_ref)
    _walk_clock_to(repo, _at(15, 59, 57))
    trade = _acked()

    await resolve_exit(repo, effect_operation_id=effect_operation_id, trade=trade, pricing=_live_touch())

    ((sent, _),) = trade.submit_calls
    assert (sent.order_type, sent.extended_hours) == (OrderType.LIMIT, True)
    assert sent.limit_price == pytest.approx(99.80)


async def test_a_limit_judged_within_the_guard_band_of_its_bound_is_not_sent(
    repo: ClerkSqliteRepository,
) -> None:
    """A POST limit bound by 20:00, driven at 19:59:57: it could reach Alpaca after the close.

    Nothing is sent; the operator is told, with the watchdog's next attempt —
    the first send that lands in the next morning's 04:00 pre-market, the
    guard band before it — as a time value.
    """
    _walk_clock_to(repo, _at(19, 50))
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
    _walk_clock_to(repo, _at(19, 59, 57))
    trade = _acked()

    await resolve_exit(repo, effect_operation_id=effect_operation_id, trade=trade, pricing=_live_touch())

    assert trade.submit_calls == []
    episode = _exit_not_flat(repo)
    assert episode is not None
    assert "No trading session would be open" in episode["explanation"]
    assert _next_attempt_at_ms(episode) == _at(3, 59, 55, day=date(2026, 9, 3))


def test_the_next_redrive_is_judged_where_its_send_would_land() -> None:
    """#2440 review (X m3): a not-before of 19:59:57 used to come back as 19:59:57.

    The watchdog refuses ``NO_SESSION_OPEN`` at that instant — its send would
    land after 20:00 — so the notice read "overdue since 19:59:57" all night.
    Judged at the send's arrival, the next try is the first instant whose
    send lands in the pre-market: 03:59:55.
    """
    thursday = date(2026, 9, 3)

    assert next_redrive_at_ms(not_before_ms=_at(19, 59, 57), policy=_POLICY) == _at(
        3, 59, 55, day=thursday
    )
    assert next_redrive_at_ms(not_before_ms=_at(3, 59, 55, day=thursday), policy=_POLICY) == _at(
        3, 59, 55, day=thursday
    )
    assert next_redrive_at_ms(not_before_ms=_at(19, 59, 54), policy=_POLICY) == _at(19, 59, 54)


def _projected_notice(repo: ClerkSqliteRepository) -> tuple[int | None, bool]:
    """What the bot page shows for SID's ``EXIT_NOT_FLAT``: eligibility and exit working."""
    reader = SqliteClerkProjectionReader.from_repository(repo, pricing=_live_touch())
    try:
        snapshot = reader.bot_snapshot(SID)
    finally:
        reader.close()
    assert snapshot is not None
    [episode] = [item for item in snapshot.uncertainties if item.reason_code == EXIT_NOT_FLAT_REASON_CODE]
    guidance = snapshot.guidance
    shown = (episode.next_attempt_at_ms, episode.exit_working)
    assert (guidance.next_attempt_at_ms, guidance.exit_working) == shown
    return shown


@pytest.mark.parametrize(
    "decision_id",
    [f"{EXIT_REDRIVE_DECISION_PREFIX}0123456789ab-1", f"{RECOVERY_FLATTEN_DECISION_PREFIX}operator01"],
    ids=["watchdog-redrive", "operator-priced-flatten"],
)
async def test_a_working_exit_replaces_retry_eligibility_until_it_ends(
    repo: ClerkSqliteRepository, decision_id: str
) -> None:
    """#2440 review (major): a sell resting in pre-market read "overdue since 04:00" until 20:00.

    The ``EXIT_NOT_FLAT`` episode stays open until flat, and nothing rewrites
    its recorded time when the watchdog accepts a re-drive — or while the
    operator's own priced flatten works. The watchdog then skips the
    strategy, so the time is not a promise: the notice says an exit is
    working instead, never that the automatic sell failed. Once the exit ends
    unfilled, the watchdog's next try is shown again.
    """
    thursday, friday = date(2026, 9, 3), date(2026, 9, 4)
    _walk_clock_to(repo, _at(19, 50))
    entry_ref = await _make_entry(repo, status="filled", filled_quantity=10)
    program_exit = _accept_program_exit(
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
    _walk_clock_to(repo, _at(19, 59, 57))
    await resolve_exit(repo, effect_operation_id=program_exit, trade=_acked(), pricing=_live_touch())
    assert _projected_notice(repo) == (_at(3, 59, 55, day=thursday), False)

    _walk_clock_to(repo, _at(4, 0, 15, day=thursday))
    priced = price_automatic_recovery_reduction(
        side=OrderSide.SELL,
        symbol="SPY",
        quantity=10,
        now_ms=repo.clock(),
        policy=_POLICY,
        quote=TopOfBookQuote(
            symbol="SPY", bid=100.00, ask=100.05, source="ibkr.market_data.status",
            observed_at_ms=repo.clock(),
        ),
    )
    working = accept_recovery_exit(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id=decision_id,
        entry_order_ref=entry_ref,
        confirmed_shape=priced,
    )
    assert working.effect_operation_id is not None
    sent = await resolve_exit(
        repo, effect_operation_id=working.effect_operation_id, trade=_acked(), pricing=_live_touch()
    )
    assert sent.reducing_order_ref is not None
    for read_at_ms in (_at(4, 0, 30, day=thursday), _at(9, 35, day=thursday)):
        _walk_clock_to(repo, read_at_ms)
        assert _projected_notice(repo) == (None, True)

    _walk_clock_to(repo, _at(20, 0, 10, day=thursday))
    expired = _FakeTrade(
        lookup_results=[
            _broker_order(sent.reducing_order_ref, side="sell", status="expired", filled_quantity=0.0)
        ]
    )
    await resolve_exit(
        repo, effect_operation_id=working.effect_operation_id, trade=expired, pricing=_live_touch()
    )
    assert repo.position(SID, "SPY") == 10
    assert _projected_notice(repo) == (_at(3, 59, 55, day=friday), False)


async def test_an_exit_delayed_past_a_half_days_after_hours_close_is_not_sent(
    tmp_path: Path,
) -> None:
    """2026-11-27: the regular close is 13:00 and after-hours ends at 17:00, not 20:00.

    An EXIT decided at 12:59 and first driven at 17:30 is inside the declared
    04:00-20:00 window but past the calendar's after-hours close, so no limit
    is priced for a session Alpaca no longer runs; the operator is told the
    watchdog tries at Monday's 04:00 pre-market.
    """
    repo = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID,
        artifacts_root=tmp_path,
        clock=_clock_at(_at(12, 59, day=_EARLY_CLOSE_DAY)),
        lease_ttl_ms=300_000,
    )
    try:
        repo.register_strategy_instance(strategy_instance_id=SID, symbol="SPY", config_hash="h1")
        submit_start_run(repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id=RUN_ID)
        entry_ref = await _make_entry(repo, status="filled", filled_quantity=10)
        effect_operation_id = _accept_program_exit(repo, entry_ref)
        _walk_clock_to(repo, _at(17, 30, day=_EARLY_CLOSE_DAY))
        trade = _acked()

        await resolve_exit(
            repo, effect_operation_id=effect_operation_id, trade=trade, pricing=_live_touch()
        )

        assert trade.submit_calls == [], "a limit was sent after the half-day's after-hours close"
        episode = _exit_not_flat(repo)
        assert episode is not None
        assert "No trading session would be open" in episode["explanation"]
        assert _next_attempt_at_ms(episode) == _at(3, 59, 55, day=date(2026, 11, 30))
    finally:
        repo.close()


async def test_a_half_days_after_hours_limit_is_bounded_by_the_calendar_close(
    tmp_path: Path,
) -> None:
    """Re-priced at 13:00:30 on 2026-11-27, the limit carries 17:00 as its bound, not 20:00."""
    repo = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID,
        artifacts_root=tmp_path,
        clock=_clock_at(_at(12, 59, day=_EARLY_CLOSE_DAY)),
        lease_ttl_ms=300_000,
    )
    try:
        repo.register_strategy_instance(strategy_instance_id=SID, symbol="SPY", config_hash="h1")
        submit_start_run(repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id=RUN_ID)
        entry_ref = await _make_entry(repo, status="filled", filled_quantity=10)
        effect_operation_id = _accept_program_exit(repo, entry_ref)
        _walk_clock_to(repo, _at(13, 0, 30, day=_EARLY_CLOSE_DAY))

        resolved = await resolve_exit(
            repo, effect_operation_id=effect_operation_id, trade=_acked(), pricing=_live_touch()
        )

        assert resolved.reducing_order_ref is not None
        created = repo.first_order_transition(
            order_ref=resolved.reducing_order_ref, transition_kind="EXIT_REDUCING_ORDER_CREATED"
        )
        assert created is not None
        facts = ExitReducingOrderCreatedFacts.from_facts_json(created["facts_json"])
        assert facts.extended_hours
        assert facts.valid_until_ms == _at(17, 0, day=_EARLY_CLOSE_DAY)
    finally:
        repo.close()


@pytest.mark.parametrize(
    ("sent_at", "shape", "valid_until", "quiet_at", "alarm_at"),
    [
        pytest.param(
            (17, 13),
            LegShape(
                order_type=OrderType.LIMIT,
                time_in_force=TimeInForce.DAY,
                limit_price=99.80,
                extended_hours=True,
                side=OrderSide.SELL,
            ),
            (20, 0),
            (20, 4),
            (20, 6),
            id="after-hours-limit-past-its-close",
        ),
        pytest.param((15, 30), None, None, (16, 4), (16, 6), id="market-leg-past-the-regular-close"),
    ],
)
async def test_a_reducing_order_still_working_past_its_session_tells_the_operator_once(
    repo: ClerkSqliteRepository,
    sent_at: tuple[int, int],
    shape: LegShape | None,
    valid_until: tuple[int, int] | None,
    quiet_at: tuple[int, int],
    alarm_at: tuple[int, int],
) -> None:
    """Acceptance criterion 4 does not rest on Alpaca ending the order.

    The broker still reports the reducing order working five minutes after
    its session ended: the Clerk raises ``EXIT_NOT_FLAT`` once, keeps custody
    of the order (the EXIT is not failed, the entry not released — it may
    still execute), and a later pass does not raise it again.
    """
    _walk_clock_to(repo, _at(*sent_at))
    entry_ref = await _make_entry(repo, status="filled", filled_quantity=10)
    effect_operation_id = _accept_program_exit(
        repo,
        entry_ref,
        shape=shape,
        valid_until_ms=None if valid_until is None else _at(*valid_until),
    )
    sent = await resolve_exit(
        repo, effect_operation_id=effect_operation_id, trade=_acked(), pricing=_live_touch()
    )
    assert sent.reducing_order_ref is not None

    def still_working() -> _FakeTrade:
        return _FakeTrade(
            lookup_results=[_broker_order("placeholder", side="sell", status="accepted")]
        )

    _walk_clock_to(repo, _at(*quiet_at))
    await resolve_exit(repo, effect_operation_id=effect_operation_id, trade=still_working(), pricing=_live_touch())
    assert _exit_not_flat(repo) is None, "raised inside the grace the broker has to end the order"

    _walk_clock_to(repo, _at(*alarm_at))
    await resolve_exit(repo, effect_operation_id=effect_operation_id, trade=still_working(), pricing=_live_touch())
    episode = _exit_not_flat(repo)
    assert episode is not None
    assert episode["headline"] == (
        "An exit order is still working after its session ended; the position is still open"
    )
    effect = repo.effect_operation(effect_operation_id)
    assert effect is not None and effect.state == "in_progress"
    assert repo.active_exit_for_order(entry_ref) is not None
    writes = _raised_episode_writes(repo)

    _walk_clock_to(repo, _at(alarm_at[0], alarm_at[1] + 1))
    await resolve_exit(repo, effect_operation_id=effect_operation_id, trade=still_working(), pricing=_live_touch())
    assert _raised_episode_writes(repo) == writes, "the alarm was raised again"


@pytest.mark.parametrize(
    ("day", "sent_at", "quiet_at", "alarm_at"),
    [
        pytest.param(date(2026, 9, 3), (4, 0), ((9, 35), (20, 4)), (20, 6), id="ordinary-day"),
        pytest.param(_EARLY_CLOSE_DAY, (8, 0), ((9, 35), (17, 4)), (17, 6), id="half-day"),
    ],
)
async def test_a_pre_market_limit_working_into_the_regular_session_is_no_alarm_until_the_after_hours_close(
    tmp_path: Path,
    day: date,
    sent_at: tuple[int, int],
    quiet_at: tuple[tuple[int, int], ...],
    alarm_at: tuple[int, int],
) -> None:
    """#2440 review: Alpaca keeps a DAY extended-hours limit working until that day's after-hours close.

    The owner's own 04:00 re-drive is a pre-market limit whose send bound is
    09:30; judged against that bound, the still-working alarm fired at 09:35
    on every re-drive that had not filled yet. The alarm is bounded by the
    broker's expiry — the calendar's after-hours close of the day it was
    sent, 17:00 on a half-day — and raises exactly once past it.
    """
    repo = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID,
        artifacts_root=tmp_path,
        clock=_clock_at(_at(*sent_at, day=day)),
        lease_ttl_ms=300_000,
    )
    try:
        repo.register_strategy_instance(strategy_instance_id=SID, symbol="SPY", config_hash="h1")
        submit_start_run(repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id=RUN_ID)
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
            valid_until_ms=_at(9, 30, day=day),
        )
        sent = await resolve_exit(
            repo, effect_operation_id=effect_operation_id, trade=_acked(), pricing=_live_touch()
        )
        assert sent.reducing_order_ref is not None

        def still_working() -> _FakeTrade:
            return _FakeTrade(
                lookup_results=[_broker_order("placeholder", side="sell", status="accepted")]
            )

        for quiet in quiet_at:
            _walk_clock_to(repo, _at(*quiet, day=day))
            await resolve_exit(
                repo, effect_operation_id=effect_operation_id, trade=still_working(), pricing=_live_touch()
            )
            assert _exit_not_flat(repo) is None, f"alarmed at {quiet} while the broker still works the limit"

        _walk_clock_to(repo, _at(*alarm_at, day=day))
        await resolve_exit(
            repo, effect_operation_id=effect_operation_id, trade=still_working(), pricing=_live_touch()
        )
        assert _exit_not_flat(repo) is not None
        writes = _raised_episode_writes(repo)
        _walk_clock_to(repo, _at(alarm_at[0], alarm_at[1] + 1, day=day))
        await resolve_exit(
            repo, effect_operation_id=effect_operation_id, trade=still_working(), pricing=_live_touch()
        )
        assert _raised_episode_writes(repo) == writes, "the alarm was raised again"
    finally:
        repo.close()


async def test_a_lost_submit_refused_after_the_close_keeps_its_notice_and_next_attempt(
    repo: ClerkSqliteRepository,
) -> None:
    """#2440 review M1: the working-order alarm never overwrites the send-time rule's notice.

    A market exit's submit is lost at 15:59; the Clerk restarts and resumes
    at 16:10. The send-time rule refuses to replay the market leg after the
    close: the EXIT fails, the entry is released, and the operator is told
    the exit could not be sent and when the watchdog next tries. That order
    never reached Alpaca, so nothing may then call it "still working", tell
    the operator to cancel it at the broker, or drop the next attempt.
    """
    entry_ref = await _make_entry(repo, status="filled", filled_quantity=10)
    effect_operation_id = _accept_program_exit(repo, entry_ref)
    lost = _FakeTrade(submit_error=BrokerUnavailable("timeout"))
    await resolve_exit(repo, effect_operation_id=effect_operation_id, trade=lost, pricing=_live_touch())

    _walk_clock_to(repo, _at(16, 10))  # past the regular close by more than the 5 min alarm grace
    resumed = _FakeTrade(lookup_results=[None])
    await resolve_exit(repo, effect_operation_id=effect_operation_id, trade=resumed, pricing=_live_touch())

    assert resumed.submit_calls == []
    effect = repo.effect_operation(effect_operation_id)
    assert effect is not None and effect.state == "failed"
    assert repo.active_exit_for_order(entry_ref) is None
    episode = _exit_not_flat(repo)
    assert episode is not None
    assert episode["headline"] == (
        "An exit could not be sent after its session ended; the position is still open"
    )
    assert "still working" not in episode["explanation"]
    assert "cancel it there" not in episode["next_step"]
    # The watchdog's next try: its 120 s re-drive age, inside after-hours.
    assert _next_attempt_at_ms(episode) == _at(16, 12)
    assert _open_unknown_outcome(repo) is None


async def test_a_market_exit_re_sent_the_next_morning_is_dated_from_the_send_that_reached_the_broker(
    repo: ClerkSqliteRepository,
) -> None:
    """#2440 review: the still-working alarm dates a leg from its latest send, not its first.

    A market exit's submit is lost at 15:59 and the broker stays unobservable
    across the close. The next morning the exact lookup proves the order never
    arrived, and the Clerk legitimately sends it again inside the regular
    session. That order's session is Thursday's; dated from Wednesday's lost
    send, it was "still working after its session ended" at 09:45 and the
    operator was told to cancel a live regular-session exit at the broker.
    """
    thursday = date(2026, 9, 3)
    entry_ref = await _make_entry(repo, status="filled", filled_quantity=10)
    effect_operation_id = _accept_program_exit(repo, entry_ref)
    lost = _FakeTrade(submit_error=BrokerUnavailable("timeout"))
    await resolve_exit(repo, effect_operation_id=effect_operation_id, trade=lost, pricing=_live_touch())

    _walk_clock_to(repo, _at(16, 10))
    unobservable = _FakeTrade(lookup_error=BrokerUnavailable("down"))
    await resolve_exit(repo, effect_operation_id=effect_operation_id, trade=unobservable, pricing=_live_touch())

    _walk_clock_to(repo, _at(9, 35, day=thursday))
    resent = _FakeTrade(
        lookup_results=[None],
        submit_result=_broker_order("placeholder", side="sell", status="accepted"),
    )
    await resolve_exit(repo, effect_operation_id=effect_operation_id, trade=resent, pricing=_live_touch())
    assert len(resent.submit_calls) == 1, "the order proven absent was not sent again"

    _walk_clock_to(repo, _at(9, 45, day=thursday))
    still_working = _FakeTrade(
        lookup_results=[_broker_order("placeholder", side="sell", status="accepted")]
    )
    await resolve_exit(repo, effect_operation_id=effect_operation_id, trade=still_working, pricing=_live_touch())

    assert _exit_not_flat(repo) is None, "a regular-session exit was alarmed against yesterday's close"
    assert repo.active_exit_for_order(entry_ref) is not None


async def test_a_failed_lookup_past_the_session_raises_no_working_order_alarm(
    repo: ClerkSqliteRepository,
) -> None:
    """#2440 review M1: a lost lookup says nothing about the order, so it cannot be "still working".

    An after-hours limit sent at 17:13, bound by 20:00; at 20:06 the exact
    lookup fails. The order's outcome is unknown — that episode is what the
    operator sees — and no ``EXIT_NOT_FLAT`` claims it is working at the broker.
    """
    _walk_clock_to(repo, _at(17, 13))
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
    sent = await resolve_exit(
        repo, effect_operation_id=effect_operation_id, trade=_acked(), pricing=_live_touch()
    )
    assert sent.reducing_order_ref is not None

    _walk_clock_to(repo, _at(20, 6))
    unobservable = _FakeTrade(lookup_error=BrokerUnavailable("down"))
    await resolve_exit(
        repo, effect_operation_id=effect_operation_id, trade=unobservable, pricing=_live_touch()
    )

    assert _exit_not_flat(repo) is None, "a failed lookup raised the still-working alarm"
    assert _open_unknown_outcome(repo) is not None
    assert repo.active_exit_for_order(entry_ref) is not None


async def test_a_reducing_order_the_broker_refuses_tells_the_operator(
    repo: ClerkSqliteRepository,
) -> None:
    """A synchronous 4xx on the reducing submit: nothing reached the book, the position is open.

    It used to fold a bare ``ORDER_SUBMIT_FAILED`` — no ``EXIT_NOT_FLAT``, no
    bell — so the operator learned nothing while the exposure stayed. It is
    the same releasable fold now, with the notice and the watchdog's next try.
    """
    entry_ref = await _make_entry(repo, status="filled", filled_quantity=10)
    effect_operation_id = _accept_program_exit(repo, entry_ref)
    refused = _FakeTrade(submit_error=BrokerOrderRejected("insufficient qty available for order"))

    await resolve_exit(repo, effect_operation_id=effect_operation_id, trade=refused, pricing=_live_touch())

    assert len(refused.submit_calls) == 1
    effect = repo.effect_operation(effect_operation_id)
    assert effect is not None and effect.state == "failed"
    assert repo.active_exit_for_order(entry_ref) is None
    episode = _exit_not_flat(repo)
    assert episode is not None
    assert episode["headline"] == "The broker refused this exit's order; the position is still open"
    assert "10 SPY is still held" in episode["explanation"]
    # Inside the regular session the watchdog's next try is its re-drive age away.
    assert _next_attempt_at_ms(episode) == _at(15, 59) + 120_000
    failed = repo.first_effect_transition(
        effect_operation_id=effect_operation_id, transition_kind="EXIT_NOT_FLAT"
    )
    assert failed is not None and failed["summary_code"] == "ORDER_SUBMIT_FAILED"
    assert "insufficient qty available for order" in failed["facts_json"]


async def test_a_completed_exit_that_left_exposure_says_when_the_watchdog_tries_again(
    repo: ClerkSqliteRepository,
) -> None:
    """#2440 review: every fold the watchdog re-drives carries the time it will.

    A regular-session market leg the broker ended unfilled folds as "a
    completed EXIT left attributed exposure". The watchdog re-drives that
    episode like every other ``EXIT_NOT_FLAT``, but this fold used to carry no
    time, so its notice could not say when. ``_fold_exit_not_flat`` computes
    it for every fold now.
    """
    entry_ref = await _make_entry(repo, status="filled", filled_quantity=10)
    effect_operation_id = _accept_program_exit(repo, entry_ref)
    canceled = _FakeTrade(submit_result=_broker_order("placeholder", side="sell", status="canceled"))

    await resolve_exit(repo, effect_operation_id=effect_operation_id, trade=canceled, pricing=_live_touch())

    episode = _exit_not_flat(repo)
    assert episode is not None
    assert episode["headline"] == "A completed EXIT left attributed exposure"
    assert _next_attempt_at_ms(episode) == _at(15, 59) + 120_000


@pytest.mark.parametrize(
    "resumed_at",
    [
        pytest.param((15, 59, 40), id="in-session-resubmit"),
        pytest.param((16, 0, 40), id="out-of-session-resubmit"),
    ],
)
async def test_a_lost_submit_is_never_resent_into_a_flat_position(
    repo: ClerkSqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
    resumed_at: tuple[int, int, int],
) -> None:
    """Created and sent at 15:59, the answer lost; by the resume the position is flat.

    Resending the recorded SELL 10 then would sell into a flat — or short —
    position. In the session and out of it alike, the EXIT proves
    attributed-flat instead — against the reducing order it would have
    resent, because the lost submit left that exact identity's outcome
    unknown, and only proof recorded against it closes the episode (#2440
    review). Cited against the entry, the unknown outcome stayed open with
    nothing left to resolve it, blocking new exposure and the lane's quiet
    drain.
    """
    entry_ref = await _make_entry(repo, status="filled", filled_quantity=10)
    effect_operation_id = _accept_program_exit(repo, entry_ref)
    lost = _FakeTrade(submit_error=BrokerUnavailable("timeout"))
    first = await resolve_exit(
        repo, effect_operation_id=effect_operation_id, trade=lost, pricing=_live_touch()
    )
    assert len(lost.submit_calls) == 1
    assert _open_unknown_outcome(repo) is not None

    # The attribution moved to flat while the order was lost (a correction,
    # an operator's own reduction) — the resume must read it, not the past.
    monkeypatch.setattr(repo, "position", lambda strategy_instance_id, symbol: 0.0)
    _walk_clock_to(repo, _at(*resumed_at))  # past the 30 s submit-absence grace
    resumed = _FakeTrade(lookup_results=[None])
    await resolve_exit(repo, effect_operation_id=effect_operation_id, trade=resumed, pricing=_live_touch())

    assert resumed.submit_calls == [], "a reduction was resent into a flat position"
    effect = repo.effect_operation(effect_operation_id)
    assert effect is not None and effect.state == "succeeded"
    flat = repo.first_effect_transition(
        effect_operation_id=effect_operation_id, transition_kind="EXIT_ATTRIBUTED_FLAT"
    )
    assert flat is not None and flat["order_ref"] == first.reducing_order_ref
    assert _open_unknown_outcome(repo) is None, "the lost submit's unknown outcome was stranded"


async def test_a_clerk_that_cannot_price_after_hours_says_so_on_a_recovery_exit(
    repo: ClerkSqliteRepository,
) -> None:
    """The recovery fold keeps why nothing was priced in the leg's place (#2440 review).

    An operator's market flatten accepted in the session and driven at 16:01
    by a Clerk with no extended-hours pricing: the episode says after-hours
    pricing is unavailable — not that no session is open, which at 16:01 is
    false — and carries the watchdog's next try.
    """
    entry_ref = await _make_entry(repo, status="filled", filled_quantity=10)
    accepted = accept_recovery_exit(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="recovery-flatten-unpriced01",
        entry_order_ref=entry_ref,
        confirmed_shape=None,
    )
    assert accepted.effect_operation_id is not None
    _walk_clock_to(repo, _at(16, 1))
    trade = _acked()

    await resolve_exit(
        repo, effect_operation_id=accepted.effect_operation_id, trade=trade, pricing=UNPRICEABLE_RECOVERY
    )

    assert trade.submit_calls == []
    episode = _exit_not_flat(repo)
    assert episode is not None
    assert "cannot price an extended-hours limit automatically" in episode["explanation"]
    assert "No trading session would be open" not in episode["explanation"]
    assert _next_attempt_at_ms(episode) == _at(9, 29, 55, day=date(2026, 9, 3))
