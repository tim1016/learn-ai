"""A bot's end sale is not sent while the Clerk has stopped exiting the bot by itself (#2854).

``EXIT_STUCK`` means the Clerk's own re-drives of a bot's exit are exhausted:
the position is the operator's to sell or write off, and the watchdog holds on
it. The sale at the bot's owner-set end did not: with the episode open, a due
SELL end accepted and sent a new sale. It now waits -- the end stays pending,
and the bell and the bot's page say why -- until the operator's sale or
write-off leaves the bot flat, and then completes with nothing sold. With no
such episode the end sells exactly as before.

Real code on a real temp SQLite Clerk, pass after pass at the sweep's cadence.
Only Alpaca is faked: what the account holds, and each order it is sent. Every
end is placed from the canonical calendar's session for the day.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.broker.alpaca.clerk.recovery_reduction import UNPRICEABLE_RECOVERY
from app.broker.alpaca.clerk.sqlite import scheduled_end
from app.broker.alpaca.clerk.sqlite.commands import submit_start_run
from app.broker.alpaca.clerk.sqlite.enter import submit_enter
from app.broker.alpaca.clerk.sqlite.exit import accept_exit
from app.broker.alpaca.clerk.sqlite.exit_recovery import DEFAULT_RECOVERY_INTERVAL_MS
from app.broker.alpaca.clerk.sqlite.exit_resolution import (
    SCHEDULED_END_DECISION_PREFIX,
    resolve_exit,
)
from app.broker.alpaca.clerk.sqlite.intake_fence import ReentrantAsyncLock
from app.broker.alpaca.clerk.sqlite.manual_orders import submit_manual_order
from app.broker.alpaca.clerk.sqlite.order_evidence import fold_order_evidence
from app.broker.alpaca.clerk.sqlite.reconcile import AccountReconciliationResult, reconcile_account
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.residue_discharge import discharge_attributed_residue
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.alpaca.clerk.sqlite.uncertainty import raise_uncertainty
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    EXIT_NOT_FLAT_REASON_CODE,
    EXIT_STUCK_REASON_CODE,
    ExitStuckCause,
)
from app.broker.alpaca.clerk.trade_evidence import SqliteTradeUpdateEvidenceSink
from app.broker.contract.errors import BrokerError
from app.broker.contract.models import (
    BrokerOrder,
    BrokerOrderEvent,
    BrokerOrderLeg,
    BrokerPosition,
    OrderSide,
    OrderType,
    TimeInForce,
)
from app.lean_sidecar.trading_calendar import session_window_for_date
from app.schemas.bot_end import BotEndView, RecordedEnd
from app.schemas.broker_v2_panel import LaneAttentionItem
from app.services.bot_end import BOT_END_LEAD_MS, bot_end_view, default_bot_end
from app.services.broker_v2_panel import lane_summary, panel_data_source
from tests._helpers.exit_terms import DEPLOY_EXIT_TERMS
from tests.broker.alpaca.clerk.sqlite import test_safe_flatten_execution as safe_flatten
from tests.broker.alpaca.clerk.sqlite import test_scheduled_end as _ends
from tests.broker.alpaca.clerk.sqlite.conftest import (
    _AssertingNoReconciler,
    _broker_leg,
    _broker_order_fixture,
    _broker_position_fixture,
    _clock_at,
    _FakeReadPort,
    _FakeTradePort,
    _make_held_position,
    _walk_clock_to,
)
from tests.broker.alpaca.clerk.sqlite.test_flatten_send_cover import _context_of
from tests.broker.alpaca.clerk.sqlite.test_manual_orders import LEG_ID, OPERATOR_ID, TICKET_ID
from tests.broker.alpaca.clerk.sqlite.test_manual_orders import FakeTrade as _OwnersOrders
from tests.broker.alpaca.clerk.sqlite.test_scheduled_end import (
    _WEDNESDAY,
    ACCOUNT_ID,
    RUN_ID,
    SID,
    ScheduledEnd,
    _end_sales,
    _hold_ten,
    _Market,
    _Schedule,
)
from tests.broker.alpaca.clerk.sqlite.test_two_bots_one_symbol import _wash_trade_rejection

# The scheduled-end suite's fixtures: a bot with an active run on a real Clerk, and its end schedule.
repo = _ends.repo
schedule = _ends.schedule

PASS_MS = DEFAULT_RECOVERY_INTERVAL_MS
TEN_MINUTES_OF_PASSES = 600_000 // PASS_MS
_SESSION = session_window_for_date(_WEDNESDAY)
# Two hours into Wednesday's regular session, by the calendar: a sale then has
# the whole afternoon to go out in.
_END_MS = _SESSION.open_ms_utc + 2 * 3_600_000
_THE_SALE = (OrderSide.SELL, 10, OrderType.MARKET, TimeInForce.DAY, False)

type _Sent = tuple[OrderSide, float, OrderType, TimeInForce, bool]


class _Alpaca(_FakeReadPort):
    """The Alpaca account as every pass reads it: what it holds in SPY, and the orders open there."""

    def __init__(self, *, holds: float) -> None:
        super().__init__()
        self.holds = holds

    async def list_positions(self) -> list[BrokerPosition]:
        if not self.holds:
            return []
        return [_broker_position_fixture("SPY", quantity=self.holds, side="long" if self.holds > 0 else "short")]


class _AlpacaOrders(_Market):
    """Alpaca's orders, looked up exactly: the filled entries, each order sent, and no order it refused.

    ``_Market`` reads every order it was not sent as a filled entry. A sale Alpaca refused never
    reached its book, and reads absent here as it does there. An order of the owner's own is read
    from the owner's book.
    """

    def __init__(self, *, owner: _OwnersOrders | None = None) -> None:
        super().__init__()
        self.refuses: BrokerError | None = None
        self.never_took: set[str] = set()
        self._owner = owner

    async def submit(self, leg: BrokerOrderLeg, *, client_order_id: str) -> BrokerOrder:
        self._submit_error = self.refuses
        if self.refuses is not None:
            self.never_took.add(client_order_id)
        return await super().submit(leg, client_order_id=client_order_id)

    async def get_order_by_client_order_id(self, client_order_id: str) -> BrokerOrder | None:
        if self._owner is not None and client_order_id in self._owner.orders:
            self.lookup_calls.append(client_order_id)
            return self._owner.orders[client_order_id]
        if client_order_id in self.never_took:
            self.lookup_calls.append(client_order_id)
            return None
        return await super().get_order_by_client_order_id(client_order_id)

    async def fill(self, repo: ClerkSqliteRepository, client_order_id: str) -> None:
        """Alpaca fills one sale: it reads filled, and its own execution arrives on trade_updates."""
        filled = self.sent[client_order_id].model_copy(update={
            "status": "filled", "filled_quantity": 10.0, "filled_avg_price": 101.0,
        })
        self.sent[client_order_id] = filled
        execution_id = f"sale:{client_order_id}"
        sink = SqliteTradeUpdateEvidenceSink(repo=repo, intake=ReentrantAsyncLock(), reconciler=_AssertingNoReconciler())
        await sink.record_lifecycle_event(
            client_order_id=client_order_id,
            event=BrokerOrderEvent(
                event_type="fill", occurred_at_ms=repo.clock(), price=101.0, quantity=10, execution_id=execution_id,
            ),
            event_key=f"execution:{execution_id}",
            order=filled,
            recovery_source=None,
            recovery_window_limit=None,
        )


def _a_running_bot(artifacts_root: Path, **registration: Any) -> ClerkSqliteRepository:
    """The suite's bot on a Clerk of the test's own, half an hour into Wednesday's session."""
    own = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID, artifacts_root=artifacts_root, clock=_clock_at(_SESSION.open_ms_utc + 1_800_000),
        lease_ttl_ms=300_000,
    )
    own.register_strategy_instance(strategy_instance_id=SID, symbol="SPY", config_hash="h1", **registration)
    submit_start_run(own, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id=RUN_ID)
    return own


def _the_end(strategy_instance_id: str = SID) -> ScheduledEnd:
    return ScheduledEnd(strategy_instance_id=strategy_instance_id, end=_ends._end("SELL", end_at_ms=_END_MS).end)


async def _passes(
    repo: ClerkSqliteRepository, alpaca: _Alpaca, market: _FakeTradePort, count: int,
) -> list[AccountReconciliationResult]:
    """``count`` reconciliation passes, one sweep interval apart."""
    results = []
    for _ in range(count):
        _walk_clock_to(repo, repo.clock() + PASS_MS)
        results.append(await reconcile_account(
            repo, read=alpaca, trade=market, trigger="AUTOMATIC",
            intake=ReentrantAsyncLock(), pricing=UNPRICEABLE_RECOVERY,
        ))
    return results


def _open_reasons(repo: ClerkSqliteRepository) -> set[str]:
    return {episode["reason_code"] for episode in repo.active_uncertainties()}


def _sent(market: _FakeTradePort) -> list[_Sent]:
    return [
        (leg.side, leg.quantity, leg.order_type, leg.time_in_force, leg.extended_hours)
        for leg in market.submitted_legs
    ]


def _all_end_sales(repo: ClerkSqliteRepository) -> list[str]:
    """Every bot's EXIT decided as its end's sale."""
    rows = repo._conn.execute("SELECT effect_operation_id FROM effect_operations WHERE kind = 'EXIT'").fetchall()
    return [row[0] for row in rows if SCHEDULED_END_DECISION_PREFIX in row[0]]


async def _the_clerk_stops_exiting_the_bot(
    repo: ClerkSqliteRepository, alpaca: _Alpaca, market: _AlpacaOrders,
) -> list[_Sent]:
    """Alpaca refuses the bot's own exit and each of the watchdog's re-drives, so the watchdog stops.

    The bot keeps its 10 SPY under ``EXIT_STUCK``, and Alpaca holds what the records say
    throughout. Returns the refused orders; Alpaca refuses nothing after them.
    """
    entry_ref = await _hold_ten(repo)
    market.refuses = _wash_trade_rejection()
    own_exit = accept_exit(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, decision_id="own-exit",
        lifecycle_run_id=RUN_ID, entry_order_ref=entry_ref,
    )
    assert own_exit.effect_operation_id is not None
    await resolve_exit(
        repo, effect_operation_id=own_exit.effect_operation_id, pricing=UNPRICEABLE_RECOVERY, read=None, trade=market,
    )
    for _ in range(2 * TEN_MINUTES_OF_PASSES):
        if EXIT_STUCK_REASON_CODE in _open_reasons(repo):
            break
        await _passes(repo, alpaca, market, 1)
    assert _open_reasons(repo) == {EXIT_NOT_FLAT_REASON_CODE, EXIT_STUCK_REASON_CODE}
    refused = _sent(market)
    assert refused == [_THE_SALE] * 4, "the bot's exit and the watchdog's three re-drives"
    market.refuses = None
    return refused


def _the_end_comes(repo: ClerkSqliteRepository, schedule: _Schedule) -> None:
    schedule.ends[SID] = _the_end()
    _walk_clock_to(repo, _END_MS)


async def _end_lines(repo: ClerkSqliteRepository, monkeypatch: pytest.MonkeyPatch) -> list[LaneAttentionItem]:
    """What the bell and Home say of the bots' ends."""
    monkeypatch.setattr(lane_summary, "get_active_clerk_runtime", lambda: SimpleNamespace(
        sqlite_repository=repo, clerk=None, startup_failure=None, selected_account_authority_kind="real_paper",
    ))
    return [
        item for item in (await lane_summary.lane_attention_read()).items
        if item.condition_id.startswith("end-sale-waits:")
    ]


def _end_on_the_bots_page(
    repo: ClerkSqliteRepository, end: ScheduledEnd | None = None, *, carried_out_at_ms: int | None = None
) -> BotEndView:
    """The bot's end as its page shows it: the runner's words, or the Clerk's while the end waits for the operator.

    The suite's bot and end, unless ``end`` names another's. ``carried_out_at_ms``
    is the runner's record of when the Clerk carried the end out, as it reads
    once the end's sale was accepted.
    """
    end = end or _the_end()
    words = bot_end_view(
        RecordedEnd(
            end_at_ms=end.end.end_at_ms, end_action=end.end.end_action, carried_out_at_ms=carried_out_at_ms
        ),
        now_ms=repo.clock(), dry_run=False, running=False, use_rth=True,
    )
    return panel_data_source.end_with_waiting_sale(words, repo, end.strategy_instance_id)


_SALE_LEFT_TO_THE_OPERATOR = (
    "The Clerk stopped exiting this bot's position by itself, so the end's sale of 10 SPY was not sent. "
    "Sell the position or write it off from the bot's page; the end then completes with no sale."
)


async def _the_end_is_told_as_waiting_for_the_operator(
    repo: ClerkSqliteRepository, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bell's line and the bot's page while the long bot's end waits for the operator."""
    [line] = await _end_lines(repo, monkeypatch)
    assert (line.condition_id, line.reason_code) == (f"end-sale-waits:{SID}", "SCHEDULED_END_WAITS_FOR_OPERATOR")
    assert (line.kind, line.severity, line.symbol, line.action.destination) == ("exit", "warning", "SPY", "bot")
    assert (line.strategy_instance_id, line.headline) == (
        SID, f"{SID} reached its end. {_SALE_LEFT_TO_THE_OPERATOR}",
    )
    page = _end_on_the_bots_page(repo)
    assert (page.status, page.notice, page.waits_for_operator) == ("ending", _SALE_LEFT_TO_THE_OPERATOR, True)
    assert page.headline == "End reached Wed Sep 30, 11:30 ET · no order sent", "the headline still says the end sells"
    assert page.explanation == (
        "Its end has come and the bot is stopped. "
        "At its end the Clerk sent no order for the position the bot holds."
    ), "the page still says the Clerk sells the shares on its next pass"
    assert page.edit_refusal == "This bot's end has come; it completes once the bot holds no position.", (
        "the page still says the Clerk is carrying the end out"
    )


# ── the Clerk has stopped exiting the bot by itself ──────────────────────────


async def test_the_end_sale_waits_while_the_clerk_has_stopped_exiting_the_bot_by_itself(
    repo: ClerkSqliteRepository, schedule: _Schedule, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ``EXIT_STUCK`` open and Alpaca holding exactly what the records say, a due SELL end
    used to accept and send a new sale of its own. It now sends nothing, pass after pass: the end
    stops the bot and stays pending, and the bell and the bot's page say why."""
    alpaca, market = _Alpaca(holds=10.0), _AlpacaOrders()
    refused = await _the_clerk_stops_exiting_the_bot(repo, alpaca, market)
    _the_end_comes(repo, schedule)

    await _passes(repo, alpaca, market, TEN_MINUTES_OF_PASSES)

    assert _sent(market) == refused, "the end sent a sale of its own for a position handed to the operator"
    assert _end_sales(repo) == []
    assert repo.active_run(SID) is None, "the end still stops the bot"
    assert schedule.carried_out == [] and schedule.ends[SID] == _the_end(), "the end is no longer pending"
    assert repo.position(SID, "SPY") == 10
    await _the_end_is_told_as_waiting_for_the_operator(repo, monkeypatch)
    waited = [row for row in repo.custody_transitions() if row["transition_kind"] == "EXIT_MARKET_HOLD"]
    assert waited == [], "the wait is read from the records, never written pass after pass"


async def test_writing_the_position_off_completes_the_end_with_no_sale(
    repo: ClerkSqliteRepository, schedule: _Schedule, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The operator sells the shares by hand at Alpaca and writes the bot's record off. The bot
    holds nothing, so its end is carried out as a flat bot's is: nothing sold."""
    alpaca, market = _Alpaca(holds=10.0), _AlpacaOrders()
    refused = await _the_clerk_stops_exiting_the_bot(repo, alpaca, market)
    _the_end_comes(repo, schedule)
    await _passes(repo, alpaca, market, 4)
    assert schedule.carried_out == [] and _sent(market) == refused

    alpaca.holds = 0.0
    await _passes(repo, alpaca, market, 1)
    receipt = await discharge_attributed_residue(
        repo, read=alpaca, intake=ReentrantAsyncLock(), strategy_instance_id=SID, symbol="SPY",
        operator_reason="Sold by hand at Alpaca.",
    )
    [after] = await _passes(repo, alpaca, market, 1)

    assert receipt.discharged_qty == 10 and repo.position(SID, "SPY") == 0
    assert after.verdict == "clean" and _open_reasons(repo) == set()
    assert [end for end, _at_ms in schedule.carried_out] == [_the_end()]
    assert _end_sales(repo) == [] and _sent(market) == refused
    assert await _end_lines(repo, monkeypatch) == []
    page = _end_on_the_bots_page(repo)
    assert (page.notice, page.waits_for_operator) == (None, False)


async def test_the_operators_sale_completes_the_end_with_no_sale_of_its_own(
    tmp_path: Path, schedule: _Schedule, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The operator flattens the stuck position from the bot's page, through the Clerk's own
    facade. While that sale works the end still waits for the operator: the bell and the page
    used to drop the wait and say the Clerk sells the shares on its next pass. The sale leaves
    the bot flat, and its end is carried out with no sale of its own."""
    repo = _a_running_bot(tmp_path, exit_terms=DEPLOY_EXIT_TERMS)
    try:
        alpaca, market = _Alpaca(holds=10.0), _AlpacaOrders()
        refused = await _the_clerk_stops_exiting_the_bot(repo, alpaca, market)
        _the_end_comes(repo, schedule)
        await _passes(repo, alpaca, market, 4)
        assert schedule.carried_out == [] and _sent(market) == refused

        _walk_clock_to(repo, repo.clock() + PASS_MS)
        facade = SqliteAlpacaClerkFacade(account_mode="paper", repo=repo, read=alpaca, trade=market)
        assert (await facade.reconcile_account(trigger="OPERATOR_RECONCILE_NOW")).verdict == "clean"
        flatten = await safe_flatten._execute(facade, _context_of(repo, SID), confirmed_limit=None)
        assert flatten.applied is True and _sent(market) == [*refused, _THE_SALE]
        await _passes(repo, alpaca, market, 1)
        assert schedule.carried_out == [] and _end_sales(repo) == [], "the end waits for the operator's sale to finish"
        assert repo.active_exit_for_strategy(SID) is not None, "the operator's sale is no longer working"
        await _the_end_is_told_as_waiting_for_the_operator(repo, monkeypatch)
        [operators_sale] = market.sent
        await market.fill(repo, operators_sale)
        alpaca.holds = 0.0
        [after] = await _passes(repo, alpaca, market, 1)

        assert repo.position(SID, "SPY") == 0
        assert after.verdict == "clean" and _open_reasons(repo) == set()
        assert [end for end, _at_ms in schedule.carried_out] == [_the_end()]
        assert _end_sales(repo) == [] and _sent(market) == [*refused, _THE_SALE]
        assert await _end_lines(repo, monkeypatch) == []
    finally:
        repo.close()


async def _short_ten(repo: ClerkSqliteRepository) -> None:
    """The bot is short 10 SPY: a filled sell entry with its exact execution slice."""
    submission = await submit_enter(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, decision_id="enter-short",
        lifecycle_run_id=RUN_ID, leg=_broker_leg(side="sell"), trade=_FakeTradePort(),
    )
    assert submission.order_ref is not None
    filled = _broker_order_fixture(
        submission.order_ref, status="filled", side="sell", quantity=10.0, filled_quantity=10.0, filled_avg_price=100.0,
    )
    fold_order_evidence(repo, effect_operation_id=submission.effect_operation_id, order=filled)
    sink = SqliteTradeUpdateEvidenceSink(repo=repo, intake=ReentrantAsyncLock(), reconciler=_AssertingNoReconciler())
    await sink.record_lifecycle_event(
        client_order_id=submission.order_ref,
        event=BrokerOrderEvent(
            event_type="fill", occurred_at_ms=1_700_000_000_600, price=100, quantity=10, execution_id="exec-short",
        ),
        event_key="execution:exec-short",
        order=filled,
        recovery_source=None,
        recovery_window_limit=None,
    )


def _exit_stuck_is_open(
    repo: ClerkSqliteRepository, *, attributed_qty: float, attested_absent_order_ref: str | None = None
) -> None:
    """The watchdog's escalation as it stands in the records, for a bot whose refusals are not replayed here."""
    raise_uncertainty(
        repo, strategy_instance_id=SID, reason_code=EXIT_STUCK_REASON_CODE,
        headline="A stuck EXIT exhausted automatic re-drives",
        explanation=f"{attributed_qty:g} SPY remains attributed after 3 automatic EXIT re-drives.",
        operator_impact="New exposure stays on hold for this strategy and automatic re-drives stopped.",
        next_step="Run Reconcile now, then execute the presented safe flatten.",
        cause_facts=ExitStuckCause(
            symbol="SPY", attributed_qty=attributed_qty, redrive_count=3, first_observed_at_ms=repo.clock(),
            attested_absent_order_ref=attested_absent_order_ref,
        ).to_mapping(),
        severity="error",
    )


async def test_a_short_bots_end_waits_too_and_is_told_as_a_buy_back(
    repo: ClerkSqliteRepository, schedule: _Schedule, monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _short_ten(repo)
    assert repo.position(SID, "SPY") == -10
    _exit_stuck_is_open(repo, attributed_qty=-10.0)
    alpaca, market = _Alpaca(holds=-10.0), _Market()
    _the_end_comes(repo, schedule)

    await _passes(repo, alpaca, market, 4)

    assert _sent(market) == [] and _end_sales(repo) == []
    assert schedule.carried_out == []
    told = (
        "The Clerk stopped exiting this bot's position by itself, so the end's buy-back of 10 SPY was not sent. "
        "Buy the position back or write it off from the bot's page; the end then completes with no buy-back."
    )
    [line] = await _end_lines(repo, monkeypatch)
    assert line.headline == f"{SID} reached its end. {told}"
    assert _end_on_the_bots_page(repo).notice == told


# ── an end recorded carried out keeps its words unless its own sale was settled away ──


async def test_an_end_recorded_carried_out_keeps_its_words_while_the_bot_is_stuck_for_another_reason(
    repo: ClerkSqliteRepository, schedule: _Schedule,
) -> None:
    """The end's sale is working and the end is recorded carried out; the bot is stuck over an
    order that is not the end's own sale -- here its own entry, wrongly named. The card's words
    stand: only the end's own sale settled as not sent rewrites them (#2854)."""
    entry_ref = await _hold_ten(repo)
    _the_end_comes(repo, schedule)
    alpaca, market = _Alpaca(holds=10.0), _Market()
    await _passes(repo, alpaca, market, 1)

    [(carried, carried_at_ms)] = schedule.carried_out
    assert carried == _the_end() and repo.position(SID, "SPY") == 10
    _exit_stuck_is_open(repo, attributed_qty=10.0, attested_absent_order_ref=entry_ref)

    assert scheduled_end.attested_absent_end_sale(repo, SID) is None, "an entry is not the end's sale"
    page = _end_on_the_bots_page(repo, carried_out_at_ms=carried_at_ms)
    assert page.status == "ended" and page.waits_for_operator is False
    assert page.headline.endswith("· sale put in") and page.notice is None


# ── with no such episode, the end sells as it always has ─────────────────────


async def _another_bot_holds_ten(repo: ClerkSqliteRepository, sid: str) -> None:
    repo.register_strategy_instance(strategy_instance_id=sid, symbol="SPY", config_hash=f"h-{sid}")
    submit_start_run(repo, account_id=ACCOUNT_ID, strategy_instance_id=sid, lifecycle_run_id=f"run-{sid}")
    await _make_held_position(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=sid, run_id=f"run-{sid}",
        decision_id=f"enter-{sid}", execution_id=f"exec-{sid}",
    )


@pytest.mark.parametrize("bots", [2, 4, 8])
async def test_bots_in_one_symbol_ending_together_at_the_default_end_all_sell_in_the_first_pass(
    repo: ClerkSqliteRepository, schedule: _Schedule, bots: int,
) -> None:
    """Every bot's sale goes out on the first pass after their common end -- the default end,
    one lead before the session's close -- so none is left holding overnight."""
    sids = [SID, *(f"end-bot-{n}" for n in range(2, bots + 1))]
    await _hold_ten(repo)
    for sid in sids[1:]:
        await _another_bot_holds_ten(repo, sid)
    default_end = default_bot_end(repo.clock())
    assert default_end.end_at_ms == _SESSION.close_ms_utc - BOT_END_LEAD_MS
    assert default_end.end_action == "SELL"
    for sid in sids:
        schedule.ends[sid] = ScheduledEnd(strategy_instance_id=sid, end=default_end)
    alpaca, market = _Alpaca(holds=10.0 * bots), _Market()
    _walk_clock_to(repo, default_end.end_at_ms)

    await _passes(repo, alpaca, market, 1)

    assert _sent(market) == [_THE_SALE] * bots
    assert len(_all_end_sales(repo)) == bots
    assert sorted(end.strategy_instance_id for end, _at_ms in schedule.carried_out) == sorted(sids)
    assert all(repo.active_run(sid) is None for sid in sids)

    await _passes(repo, alpaca, market, 1)
    assert _sent(market) == [_THE_SALE] * bots, "an end sold twice while its order worked"


@pytest.mark.parametrize(
    "resting",
    [
        pytest.param(("QQQ", 10.0), id="in-another-symbol"),
        pytest.param(("SPY", 99.90), id="in-the-same-symbol"),
    ],
)
async def test_the_owners_resting_order_does_not_hold_an_end_sale(
    repo: ClerkSqliteRepository, schedule: _Schedule, resting: tuple[str, float],
) -> None:
    """The owner's own buy limit rests at Alpaca, unfilled. The bot's end sale goes out on the
    first pass after its end all the same, and the owner's order goes on resting."""
    symbol, limit_price = resting
    await _hold_ten(repo)
    owner = _OwnersOrders(repo=repo)
    buy_limit = BrokerOrderLeg(
        symbol=symbol, side="buy", quantity=1, order_type="limit", limit_price=limit_price, time_in_force="gtc",
    )
    manual = await submit_manual_order(
        repo, account_id=ACCOUNT_ID, operator_id=OPERATOR_ID, ticket_id=TICKET_ID, leg_id=LEG_ID,
        leg=buy_limit, trade=owner,
    )
    assert manual.leg.order_ref is not None
    alpaca, market = _Alpaca(holds=10.0), _AlpacaOrders(owner=owner)
    alpaca._orders = [owner.orders[manual.leg.order_ref]]
    await _passes(repo, alpaca, market, 1)
    assert repo.has_nonterminal_manual_order()
    _the_end_comes(repo, schedule)

    await _passes(repo, alpaca, market, 1)

    assert _sent(market) == [_THE_SALE]
    assert [end for end, _at_ms in schedule.carried_out] == [_the_end()]
    assert len(_end_sales(repo)) == 1
    assert repo.has_nonterminal_manual_order(), "the owner's order was ended"


# ── a Dry Run's end is its simulation's ──────────────────────────────────────


@pytest.mark.parametrize("repo", [f"sim:{SID}"], indirect=True)
async def test_a_dry_runs_end_is_carried_out_as_before_whatever_its_exit_episodes(
    repo: ClerkSqliteRepository, schedule: _Schedule, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Dry Run's shares are its run-end close's to sell, never this step's: its end stops the
    bot and is recorded carried out with no end sale, and no end line asks for the operator."""
    await _make_held_position(repo, account_id=repo.account_id, strategy_instance_id=SID, run_id=RUN_ID)
    _exit_stuck_is_open(repo, attributed_qty=10.0)
    _the_end_comes(repo, schedule)
    _walk_clock_to(repo, repo.clock() + PASS_MS)
    intake, market = ReentrantAsyncLock(), _Market()

    due = await scheduled_end.fence_bots_at_their_end(repo, intake=intake)
    await scheduled_end.finish_bots_at_their_end(
        repo, due, trade=market, intake=intake, pricing=UNPRICEABLE_RECOVERY,
    )

    assert _sent(market) == [] and _end_sales(repo) == []
    assert repo.active_run(SID) is None
    assert [end for end, _at_ms in schedule.carried_out] == [_the_end()]
    schedule.ends[SID] = _the_end()  # as if the carried-out record had not landed
    assert await _end_lines(repo, monkeypatch) == []
