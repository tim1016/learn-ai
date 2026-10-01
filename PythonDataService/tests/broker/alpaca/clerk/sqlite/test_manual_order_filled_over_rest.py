"""A manual order leg filled where only REST saw it ends on its exact executions (#2686).

A manual leg ends ``MANUAL_ORDER_FILLED`` only when exact executions cover
it. When the ``trade_updates`` frame carrying a fill was missed, the sweep's
exact lookup saw ``filled`` and folded only the REST cumulative: the leg stayed
``in_progress`` for ever and every bot's entry was refused
``MANUAL_ORDER_OUTSTANDING``. These tests pin that the sweep now reads the
leg's executions from Alpaca's account activity and ends it, on the three
routes the issue names, crediting every execution exactly once whichever
route -- the stream or the account activity -- records it first.

The sweep runs through the runtime's composition: both broker ports are
guarded by the one intake fence the pass holds, and the account activity is
read through the real ``AlpacaBroker`` walk and adapter from raw ``FILL``
rows shaped like Alpaca's, whose ids embed the stream's execution id.
"""

from __future__ import annotations

import itertools
import logging
import uuid
from collections.abc import Callable

import pytest

from app.broker.alpaca.adapter import et_date_to_ms
from app.broker.alpaca.broker import _ACTIVITY_MAX_PAGES, AlpacaBroker
from app.broker.alpaca.clerk.recovery_reduction import UNPRICEABLE_RECOVERY
from app.broker.alpaca.clerk.sqlite import manual_order_executions
from app.broker.alpaca.clerk.sqlite.activity_executions import ACTIVITY_OVER_ORDER_QUANTITY_CONFLICT_COPY
from app.broker.alpaca.clerk.sqlite.broker_port_guard import guard_broker_ports
from app.broker.alpaca.clerk.sqlite.facts import ExecutionSliceFilledFacts
from app.broker.alpaca.clerk.sqlite.intake_fence import ReentrantAsyncLock
from app.broker.alpaca.clerk.sqlite.manual_order_executions import (
    ACTIVITY_READS_PER_WALK,
    BEYOND_REACH_REWALK_INTERVAL_MS,
)
from app.broker.alpaca.clerk.sqlite.reconcile import AccountReconciliationResult, reconcile_account
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    EXECUTION_COVERAGE_CONFLICT_REASON_CODE,
    RECONCILIATION_INCOMPLETE_REASON_CODE,
)
from app.broker.alpaca.clerk.trade_evidence import SqliteTradeUpdateEvidenceSink
from app.broker.alpaca.trade_updates import _opt_ms_to_rfc3339
from app.broker.contract.errors import BrokerUnavailable
from app.broker.contract.models import BrokerActivityEvidence, BrokerOrder, BrokerOrderEvent
from app.utils.session_anchors import et_date_at_ms
from tests.broker.alpaca.clerk.sqlite.conftest import _walk_clock_to
from tests.broker.alpaca.clerk.sqlite.test_manual_order_replaced_at_alpaca import (
    _B,
    _C,
    _account_hold_active,
    _another_bots_entry,
    _buy_limit,
    _manual_endings,
    _replace_at_alpaca,
    _replacement_of,
    _unexplained_hold_active,
    _Website,
)
from tests.broker.alpaca.clerk.sqlite.test_reconcile import (
    _FakeRead,
    _NoReconciler,
    _position,
    _register_second_spy_lane,
    clocked_repo,  # noqa: F401 -- pytest fixture, used by name
)

_EXEC_1 = str(uuid.UUID(int=0xE1))
_EXEC_2 = str(uuid.UUID(int=0xE2))
_EXEC_A = str(uuid.UUID(int=0xEA))
_EXEC_B = str(uuid.UUID(int=0xEB))
_EXEC_C = str(uuid.UUID(int=0xEC))
_FOREIGN_EXEC = str(uuid.UUID(int=0xEF))
_FOREIGN_ORDER = str(uuid.UUID(int=0xF0))
_MISBRIDGED_EXEC = str(uuid.UUID(int=0xBAD))
_FRAME_NUMBERS = itertools.count(1)


class _ActivityFeed:
    """Alpaca's account-activity endpoint: raw rows, newest first, paged after the last row's id."""

    def __init__(self) -> None:
        self.rows: list[tuple[int, dict[str, str]]] = []
        self.unavailable = False
        self.pages_read = 0

    def fill(
        self, *, execution_id: str, order_id: str, quantity: float, price: float, at_ms: int,
        activity_execution_id: str | None = None, symbol: str = "SPY",
    ) -> None:
        """Alpaca posts one execution of ``order_id``; its id embeds the stream's execution id.

        ``activity_execution_id`` is the execution part the activity id
        carries instead, when it does not match the stream's.
        """
        digits = "".join(character for character in _opt_ms_to_rfc3339(at_ms) or "" if character.isdigit())
        self.rows.append((at_ms, {
            "id": f"{digits}::{activity_execution_id or execution_id}", "activity_type": "FILL",
            "transaction_time": _opt_ms_to_rfc3339(at_ms) or "", "type": "fill",
            "qty": str(quantity), "price": str(price), "symbol": symbol, "side": "buy",
            "leaves_qty": "0", "order_id": order_id, "cum_qty": str(quantity), "order_status": "filled",
        }))

    def dividend(self, *, number: int, on_ms: int) -> None:
        """Alpaca posts a dividend: a non-trade row, dated by its ET day alone."""
        day = et_date_at_ms(on_ms).isoformat()
        self.rows.append((et_date_to_ms(day), {
            "id": f"{day.replace('-', '')}000000000::{uuid.UUID(int=0xD0_000 + number)}", "activity_type": "DIV",
            "date": day, "net_amount": "1.25", "symbol": "QQQ", "qty": "1", "per_share_amount": "1.25",
        }))

    async def list_activities(
        self, *, limit: int, page_token: str | None = None, activity_type: str | None = None
    ) -> list[dict[str, str]]:
        if self.unavailable:
            raise BrokerUnavailable("GET /v2/account/activities timed out", broker="alpaca")
        self.pages_read += 1
        rows = [
            row for _at_ms, row in sorted(self.rows, key=lambda item: (item[0], item[1]["id"]), reverse=True)
            if activity_type is None or row["activity_type"] == activity_type
        ]
        if page_token is not None:
            rows = rows[[row["id"] for row in rows].index(page_token) + 1:]
        return rows[:limit]


class _AlpacaAccount(_FakeRead):
    """Alpaca's open orders and positions, and its account activity through the real walk and adapter."""

    def __init__(self, *, feed: _ActivityFeed, spy_held: float) -> None:
        super().__init__(orders=[], positions=[_position("SPY", quantity=spy_held)] if spy_held else [])
        self._activity = AlpacaBroker(client=feed)  # type: ignore[arg-type]

    async def read_activity_evidence(
        self, *, page_token: str | None = None, after_ms: int | None = None, activity_type: str | None = None
    ) -> BrokerActivityEvidence:
        return await self._activity.read_activity_evidence(
            page_token=page_token, after_ms=after_ms, activity_type=activity_type
        )


class _PostsWhenRead(_Website):
    """Alpaca's site, posting an order's execution to the account activity only once the order is read."""

    def __init__(self, *, repo: ClerkSqliteRepository, posts: dict[str, Callable[[], None]]) -> None:
        super().__init__(repo=repo)
        self._posts = posts

    async def get_order_by_broker_order_id(self, order_id: str) -> BrokerOrder | None:
        post = self._posts.pop(order_id, None)
        if post is not None:
            post()
        return await super().get_order_by_broker_order_id(order_id)


async def _sweep(
    repo: ClerkSqliteRepository, website: _Website, feed: _ActivityFeed, *, spy_held: float
) -> AccountReconciliationResult:
    """One reconciliation pass, both ports guarded by the intake fence it holds, as the runtime runs it."""
    intake = ReentrantAsyncLock()
    read, trade = guard_broker_ports(read=_AlpacaAccount(feed=feed, spy_held=spy_held), trade=website, intake=intake)
    return await reconcile_account(repo, read=read, trade=trade, intake=intake, pricing=UNPRICEABLE_RECOVERY)


def _filled(order: BrokerOrder, repo: ClerkSqliteRepository, *, filled_quantity: float, avg: float) -> BrokerOrder:
    now = repo.clock()
    return order.model_copy(update={
        "status": "filled", "filled_quantity": filled_quantity, "filled_avg_price": avg,
        "filled_at_ms": now, "updated_at_ms": now, "observed_at_ms": now,
    })


async def _fill_frame(
    repo: ClerkSqliteRepository, order: BrokerOrder, *, execution_id: str, quantity: float, price: float
) -> str:
    """One ``trade_updates`` fill frame for ``order``, folded by the Clerk's sink."""
    sink = SqliteTradeUpdateEvidenceSink(repo=repo, intake=ReentrantAsyncLock(), reconciler=_NoReconciler())
    return await sink.record_lifecycle_event(
        client_order_id=order.client_order_id,
        event=BrokerOrderEvent(
            event_type="fill", occurred_at_ms=order.updated_at_ms or repo.clock(),
            price=price, quantity=quantity, execution_id=execution_id,
        ),
        event_key=f"fill:{execution_id}:{next(_FRAME_NUMBERS)}",
        order=order,
        recovery_source=None,
        recovery_window_limit=None,
    )


def _credited(repo: ClerkSqliteRepository, order_ref: str) -> list[tuple[str | None, str, float, float]]:
    """Every effective fill of the leg: its execution id, evidence source, shares and price."""
    return sorted(
        (fill["execution_id"], fill["evidence_source"], fill["qty"], fill["price"])
        for fill in repo.fills_for_order(order_ref)
    )


def _exact_credited(repo: ClerkSqliteRepository, order_ref: str) -> list[tuple[str, str, float, float]]:
    """Every effective exact execution of the leg: its id, evidence source, shares and price."""
    superseded = {fill["superseded_execution_ref"] for fill in repo.fills_for_order(order_ref)}
    return sorted(
        (fill["execution_id"], fill["evidence_source"], fill["qty"], fill["price"])
        for fill in repo.fills_for_order(order_ref)
        if fill["execution_id"] is not None and fill["execution_id"] not in superseded
    )


def _executed_on(repo: ClerkSqliteRepository, order_ref: str) -> dict[str, str]:
    """Each recorded execution id and the broker order its exact slice names."""
    return {
        ExecutionSliceFilledFacts.from_facts_json(transition["facts_json"]).execution_id: transition["broker_order_id"]
        for transition in repo.transitions_for_order(order_ref)
        if transition["transition_kind"] == "EXECUTION_SLICE_FILLED"
    }


def _coverage_conflict_episodes(repo: ClerkSqliteRepository) -> list[str]:
    """Every coverage-conflict episode ever raised, oldest first: ``active`` or ``resolved``."""
    rows = repo._conn.execute(
        "SELECT resolved_at_ms FROM uncertainties WHERE reason_code = ? ORDER BY observed_at_ms, uncertainty_id",
        (EXECUTION_COVERAGE_CONFLICT_REASON_CODE,),
    ).fetchall()
    return ["active" if row["resolved_at_ms"] is None else "resolved" for row in rows]


def _coverage_conflict_active(repo: ClerkSqliteRepository) -> bool:
    row = repo._conn.execute(
        "SELECT COUNT(*) AS n FROM uncertainties WHERE reason_code = ? AND resolved_at_ms IS NULL",
        (EXECUTION_COVERAGE_CONFLICT_REASON_CODE,),
    ).fetchone()
    return int(row["n"]) > 0


# ── Route 1: an unreplaced manual order whose fill frames were missed ─────────


async def test_a_manual_order_filled_only_over_rest_ends_on_its_executions_from_account_activity(
    clocked_repo,  # noqa: F811
) -> None:
    """The stream missed both fill frames; the sweep's lookup sees ``filled``.

    The sweep reads the order's two executions from account activity -- and
    nothing of another order's execution in the same symbol -- and the leg
    ends filled, once. When the stream later redelivers both frames, nothing
    is credited again.
    """
    repo, _clock = clocked_repo
    website = _Website(repo=repo)
    manual = await _buy_limit(repo, website)
    order_ref, effect_id = manual.leg.order_ref, manual.leg.effect_operation_id
    sid_b, _run_b = _register_second_spy_lane(repo)
    original = website.orders[order_ref]
    filled = _filled(original, repo, filled_quantity=5, avg=99.87)
    website.orders[order_ref] = filled
    feed = _ActivityFeed()
    now = repo.clock()
    feed.fill(execution_id=_EXEC_1, order_id=original.order_id, quantity=2, price=99.90, at_ms=now)
    feed.fill(execution_id=_FOREIGN_EXEC, order_id=_FOREIGN_ORDER, quantity=7, price=99.88, at_ms=now + 1)
    feed.fill(execution_id=_EXEC_2, order_id=original.order_id, quantity=3, price=99.85, at_ms=now + 2)

    result = await _sweep(repo, website, feed, spy_held=5.0)

    assert result.verdict == "clean"
    effect = repo.effect_operation(effect_id)
    assert effect is not None and effect.state == "succeeded", "a fill only REST saw must end the leg"
    assert [t["transition_kind"] for t in _manual_endings(repo, order_ref)] == ["MANUAL_ORDER_FILLED"]
    assert _credited(repo, order_ref) == [
        (_EXEC_1, "activity_recovery", 2.0, 99.90),
        (_EXEC_2, "activity_recovery", 3.0, 99.85),
    ]
    assert _another_bots_entry(repo, sid_b) == (True, None)

    for execution_id, quantity, price in ((_EXEC_1, 2.0, 99.90), (_EXEC_2, 3.0, 99.85)):
        assert await _fill_frame(
            repo, filled, execution_id=execution_id, quantity=quantity, price=price
        ) == "order_event"

    assert _credited(repo, order_ref) == [
        (_EXEC_1, "activity_recovery", 2.0, 99.90),
        (_EXEC_2, "activity_recovery", 3.0, 99.85),
    ]
    assert repo.attributed_positions_by_symbol() == pytest.approx({"SPY": 5.0}, abs=1e-9, rel=0)
    assert len(_manual_endings(repo, order_ref)) == 1
    assert not _coverage_conflict_active(repo)


async def test_an_execution_the_stream_recorded_is_not_credited_again_from_account_activity(
    clocked_repo,  # noqa: F811
) -> None:
    """The stream delivered the first execution and missed the second.

    The sweep's activity read returns both: the first is the stream's own
    execution by identity and is not credited again; the second ends the leg.
    """
    repo, _clock = clocked_repo
    website = _Website(repo=repo)
    manual = await _buy_limit(repo, website)
    order_ref, effect_id = manual.leg.order_ref, manual.leg.effect_operation_id
    original = website.orders[order_ref]
    now = repo.clock()
    partial = original.model_copy(update={
        "status": "partially_filled", "filled_quantity": 2, "filled_avg_price": 99.90,
        "updated_at_ms": now, "observed_at_ms": now,
    })
    await _fill_frame(repo, partial, execution_id=_EXEC_1, quantity=2, price=99.90)
    website.orders[order_ref] = _filled(original, repo, filled_quantity=5, avg=99.87)
    feed = _ActivityFeed()
    feed.fill(execution_id=_EXEC_1, order_id=original.order_id, quantity=2, price=99.90, at_ms=now)
    feed.fill(execution_id=_EXEC_2, order_id=original.order_id, quantity=3, price=99.85, at_ms=now + 1)

    await _sweep(repo, website, feed, spy_held=5.0)

    effect = repo.effect_operation(effect_id)
    assert effect is not None and effect.state == "succeeded"
    assert _credited(repo, order_ref) == [
        (_EXEC_1, "websocket", 2.0, 99.90),
        (_EXEC_2, "activity_recovery", 3.0, 99.85),
    ]
    assert repo.attributed_positions_by_symbol() == pytest.approx({"SPY": 5.0}, abs=1e-9, rel=0)
    assert not _coverage_conflict_active(repo)


# ── Route 2: the second replacement fills before the Clerk sees either link ───


async def test_a_second_replacement_that_fills_before_the_clerk_sees_its_chain_ends_the_leg(
    clocked_repo,  # noqa: F811
) -> None:
    """A replaced by B, B by C, and C fills 5 before the Clerk knows either link.

    C's fill frame is consumed as a foreign order's. The sweep follows the
    chain to C, reads C's execution from account activity and ends the leg
    on it, crediting C's execution once under C's own broker order. A
    redelivery of C's frame once the chain is known credits nothing again.
    """
    repo, _clock = clocked_repo
    website = _Website(repo=repo)
    manual = await _buy_limit(repo, website)
    order_ref, effect_id = manual.leg.order_ref, manual.leg.effect_operation_id
    sid_b, _run_b = _register_second_spy_lane(repo)
    _replaced_a, replacement_b = _replace_at_alpaca(repo, website, order_ref)
    replaced_b = replacement_b.model_copy(update={"status": "replaced", "replaced_by": _C})
    website.replacements[_B] = replaced_b
    filled_c = _filled(_replacement_of(replaced_b, repo, replacement_id=_C), repo, filled_quantity=5, avg=99.90)
    website.replacements[_C] = filled_c
    assert await _fill_frame(repo, filled_c, execution_id=_EXEC_C, quantity=5, price=99.90) in {
        "unexplained_order", "unfoldable_order",
    }
    feed = _ActivityFeed()
    feed.fill(execution_id=_EXEC_C, order_id=_C, quantity=5, price=99.90, at_ms=repo.clock())

    await _sweep(repo, website, feed, spy_held=5.0)

    assert repo.order(order_ref).broker_order_id == _C
    effect = repo.effect_operation(effect_id)
    assert effect is not None and effect.state == "succeeded", "the chain's filled head must end the leg"
    assert [t["transition_kind"] for t in _manual_endings(repo, order_ref)] == ["MANUAL_ORDER_FILLED"]
    assert _credited(repo, order_ref) == [(_EXEC_C, "activity_recovery", 5.0, 99.90)]
    assert _executed_on(repo, order_ref) == {_EXEC_C: _C}

    result = await _sweep(repo, website, feed, spy_held=5.0)

    assert result.verdict == "clean"
    assert not _unexplained_hold_active(repo)
    assert _another_bots_entry(repo, sid_b) == (True, None)

    assert await _fill_frame(repo, filled_c, execution_id=_EXEC_C, quantity=5, price=99.90) == "order_event"

    assert _credited(repo, order_ref) == [(_EXEC_C, "activity_recovery", 5.0, 99.90)]
    assert repo.attributed_positions_by_symbol() == pytest.approx({"SPY": 5.0}, abs=1e-9, rel=0)


# ── Route 3: a replacement seen only over REST credits the whole chain ───────


@pytest.mark.parametrize("a_seen_on_stream", [True, False], ids=["a_on_stream", "a_over_rest_only"])
@pytest.mark.parametrize("carried", [True, False], ids=["filled_qty_carried", "filled_qty_own"])
async def test_a_replacement_filled_only_over_rest_credits_the_whole_chain_once(
    clocked_repo, carried: bool, a_seen_on_stream: bool,  # noqa: F811
) -> None:
    """A fills 2 of 5; the owner's edit books B, which fills the other 3 where only REST sees it.

    Whether or not Alpaca carries A's 2 shares into B's ``filled_qty``, the
    leg is credited A's and B's executions exactly once -- 5 shares, the
    broker's position -- and ends filled. Before #2686 an uncarried count
    under-credited the chain to 3 and fenced the symbol as drift.
    """
    repo, _clock = clocked_repo
    website = _Website(repo=repo)
    manual = await _buy_limit(repo, website)
    order_ref, effect_id = manual.leg.order_ref, manual.leg.effect_operation_id
    now = repo.clock()
    a_partial = website.orders[order_ref].model_copy(update={
        "status": "partially_filled", "filled_quantity": 2, "filled_avg_price": 99.90,
        "updated_at_ms": now, "observed_at_ms": now,
    })
    website.orders[order_ref] = a_partial
    if a_seen_on_stream:
        await _fill_frame(repo, a_partial, execution_id=_EXEC_A, quantity=2, price=99.90)
    _replaced_a, replacement_b = _replace_at_alpaca(repo, website, order_ref)
    website.replacements[_B] = _filled(
        replacement_b, repo, filled_quantity=5 if carried else 3, avg=99.84 if carried else 99.80
    )
    feed = _ActivityFeed()
    feed.fill(execution_id=_EXEC_A, order_id=a_partial.order_id, quantity=2, price=99.90, at_ms=now)
    feed.fill(execution_id=_EXEC_B, order_id=_B, quantity=3, price=99.80, at_ms=now + 1)

    result = await _sweep(repo, website, feed, spy_held=5.0)

    assert result.verdict == "clean", "the chain must be credited the broker's 5 shares"
    assert repo.attributed_positions_by_symbol() == pytest.approx({"SPY": 5.0}, abs=1e-9, rel=0)
    effect = repo.effect_operation(effect_id)
    assert effect is not None and effect.state == "succeeded"
    assert [t["transition_kind"] for t in _manual_endings(repo, order_ref)] == ["MANUAL_ORDER_FILLED"]
    assert _credited(repo, order_ref) == [
        (_EXEC_A, "websocket" if a_seen_on_stream else "activity_recovery", 2.0, 99.90),
        (_EXEC_B, "activity_recovery", 3.0, 99.80),
    ]
    assert _executed_on(repo, order_ref) == {_EXEC_A: a_partial.order_id, _EXEC_B: _B}
    assert not _coverage_conflict_active(repo)


# ── A leg whose executions are not readable yet stays on the worklist ────────


@pytest.mark.parametrize("first_read", ["unavailable", "not_posted_yet"])
async def test_a_filled_leg_whose_executions_cannot_be_read_yet_is_retried_until_they_can(
    clocked_repo, first_read: str,  # noqa: F811
) -> None:
    """The first pass cannot read the execution: the activity read fails, or Alpaca has not posted it.

    The pass still reaches its verdict and holds nothing for it; the REST
    cumulative counts the shares and the leg stays outstanding. A filled leg
    that has not ended stays on the sweep's worklist, so the next pass reads
    the execution and ends it, replacing the cumulative with the exact one.
    """
    repo, clock = clocked_repo
    website = _Website(repo=repo)
    manual = await _buy_limit(repo, website)
    order_ref, effect_id = manual.leg.order_ref, manual.leg.effect_operation_id
    sid_b, _run_b = _register_second_spy_lane(repo)
    original = website.orders[order_ref]
    website.orders[order_ref] = _filled(original, repo, filled_quantity=5, avg=99.90)
    feed = _ActivityFeed()
    feed.unavailable = first_read == "unavailable"

    first = await _sweep(repo, website, feed, spy_held=5.0)

    assert first.verdict == "clean"
    assert not _account_hold_active(repo, RECONCILIATION_INCOMPLETE_REASON_CODE)
    assert repo.effect_operation(effect_id).state == "in_progress"
    assert repo.attributed_positions_by_symbol() == pytest.approx({"SPY": 5.0}, abs=1e-9, rel=0)
    assert _another_bots_entry(repo, sid_b) == (False, "MANUAL_ORDER_OUTSTANDING")
    assert effect_id in {item.effect_operation_id for item in repo.reconcilable_effect_operations()}

    feed.unavailable = False
    feed.fill(execution_id=_EXEC_1, order_id=original.order_id, quantity=5, price=99.90, at_ms=repo.clock())
    clock.value += 15_000
    await _sweep(repo, website, feed, spy_held=5.0)

    effect = repo.effect_operation(effect_id)
    assert effect is not None and effect.state == "succeeded"
    assert [t["transition_kind"] for t in _manual_endings(repo, order_ref)] == ["MANUAL_ORDER_FILLED"]
    assert _credited(repo, order_ref) == [(_EXEC_1, "activity_recovery", 5.0, 99.90)]
    assert repo.attributed_positions_by_symbol() == pytest.approx({"SPY": 5.0}, abs=1e-9, rel=0)
    assert not _coverage_conflict_active(repo)
    assert effect_id not in {item.effect_operation_id for item in repo.reconcilable_effect_operations()}
    assert _another_bots_entry(repo, sid_b) == (True, None)


# ── Executions recovered over several passes ─────────────────────────────────


async def test_executions_found_over_several_passes_end_the_leg_only_once_they_cover_it(
    clocked_repo,  # noqa: F811
) -> None:
    """Two executions, 2 and 3 shares: the first pass cannot read the activity, the second finds only one.

    The first pass folds the REST cumulative for all 5 shares. The second
    meets the 2-share execution: one execution cannot stand for a 5-share
    cumulative, so it is quarantined behind a coverage conflict, which the
    broker's final total then clears -- the cumulative already counts those
    shares. The leg stays outstanding on 2 of 5. The third finds both:
    together they replace the cumulative, and only then does the leg end --
    with the 5 shares credited once at every step.
    """
    repo, clock = clocked_repo
    website = _Website(repo=repo)
    manual = await _buy_limit(repo, website)
    order_ref, effect_id = manual.leg.order_ref, manual.leg.effect_operation_id
    original = website.orders[order_ref]
    website.orders[order_ref] = _filled(original, repo, filled_quantity=5, avg=99.84)
    feed = _ActivityFeed()
    feed.unavailable = True
    now = repo.clock()

    await _sweep(repo, website, feed, spy_held=5.0)

    assert repo.effect_operation(effect_id).state == "in_progress"
    assert _exact_credited(repo, order_ref) == []
    assert not _coverage_conflict_active(repo)
    assert repo.attributed_positions_by_symbol() == pytest.approx({"SPY": 5.0}, abs=1e-9, rel=0)

    feed.unavailable = False
    feed.fill(execution_id=_EXEC_1, order_id=original.order_id, quantity=2, price=99.90, at_ms=now)
    clock.value += 15_000
    await _sweep(repo, website, feed, spy_held=5.0)

    assert repo.effect_operation(effect_id).state == "in_progress", "2 of 5 shares cannot end the leg"
    assert _coverage_conflict_episodes(repo) == ["resolved"], "raised for the lone execution, cleared by the total"
    assert _exact_credited(repo, order_ref) == []
    assert repo.effective_fill_totals_for_order(order_ref)[0] == pytest.approx(5.0, abs=1e-9, rel=0)
    assert repo.attributed_positions_by_symbol() == pytest.approx({"SPY": 5.0}, abs=1e-9, rel=0)

    feed.fill(execution_id=_EXEC_2, order_id=original.order_id, quantity=3, price=99.80, at_ms=now + 1)
    clock.value += 15_000
    await _sweep(repo, website, feed, spy_held=5.0)

    effect = repo.effect_operation(effect_id)
    assert effect is not None and effect.state == "succeeded"
    assert [t["transition_kind"] for t in _manual_endings(repo, order_ref)] == ["MANUAL_ORDER_FILLED"]
    assert _coverage_conflict_episodes(repo) == ["resolved"]
    assert _exact_credited(repo, order_ref) == [
        (_EXEC_1, "activity_recovery", 2.0, 99.90),
        (_EXEC_2, "activity_recovery", 3.0, 99.80),
    ]
    assert repo.effective_fill_totals_for_order(order_ref)[0] == pytest.approx(5.0, abs=1e-9, rel=0)
    assert repo.attributed_positions_by_symbol() == pytest.approx({"SPY": 5.0}, abs=1e-9, rel=0)


async def test_a_chain_head_filled_after_the_first_activity_read_ends_the_leg_in_the_same_pass(
    clocked_repo,  # noqa: F811
) -> None:
    """A fills 2 of 5 and is replaced by B; Alpaca posts B's 3-share execution only after the pass read A's.

    The pass reads the account activity for A and records A's execution,
    then reads B ``filled`` and still uncovered by that read. It reads the
    activity once more and ends the leg in this pass, not the next.
    """
    repo, _clock = clocked_repo
    feed = _ActivityFeed()
    now = repo.clock()
    website = _PostsWhenRead(
        repo=repo,
        posts={_B: lambda: feed.fill(execution_id=_EXEC_B, order_id=_B, quantity=3, price=99.80, at_ms=now + 1)},
    )
    manual = await _buy_limit(repo, website)
    order_ref, effect_id = manual.leg.order_ref, manual.leg.effect_operation_id
    a_partial = website.orders[order_ref].model_copy(update={
        "status": "partially_filled", "filled_quantity": 2, "filled_avg_price": 99.90,
        "updated_at_ms": now, "observed_at_ms": now,
    })
    website.orders[order_ref] = a_partial
    _replaced_a, replacement_b = _replace_at_alpaca(repo, website, order_ref)
    website.replacements[_B] = _filled(replacement_b, repo, filled_quantity=3, avg=99.80)
    feed.fill(execution_id=_EXEC_A, order_id=a_partial.order_id, quantity=2, price=99.90, at_ms=now)

    await _sweep(repo, website, feed, spy_held=5.0)

    effect = repo.effect_operation(effect_id)
    assert effect is not None and effect.state == "succeeded", "the head's late execution must end the leg this pass"
    assert _exact_credited(repo, order_ref) == [
        (_EXEC_A, "activity_recovery", 2.0, 99.90),
        (_EXEC_B, "activity_recovery", 3.0, 99.80),
    ]
    assert repo.attributed_positions_by_symbol() == pytest.approx({"SPY": 5.0}, abs=1e-9, rel=0)


async def test_a_former_members_answer_never_judges_the_chain_against_its_own_smaller_quantity(
    clocked_repo,  # noqa: F811
) -> None:
    """A asks for 5 and fills 2; the owner's edit books B for 10, which fills the other 8.

    The first pass cannot read the activity but records the A-to-B link. On
    the next, Alpaca still answers for A under our client id. A's answer is a
    former member's: it records nothing, so the chain's 10 shares are never
    judged against A's 5 -- that would raise a false coverage conflict and
    block trading. B's answer records the chain against B's 10 and ends the
    leg, crediting each execution once.
    """
    repo, clock = clocked_repo
    website = _Website(repo=repo)
    manual = await _buy_limit(repo, website)
    order_ref, effect_id = manual.leg.order_ref, manual.leg.effect_operation_id
    now = repo.clock()
    a_partial = website.orders[order_ref].model_copy(update={
        "status": "partially_filled", "filled_quantity": 2, "filled_avg_price": 99.90,
        "updated_at_ms": now, "observed_at_ms": now,
    })
    website.orders[order_ref] = a_partial
    _replaced_a, replacement_b = _replace_at_alpaca(repo, website, order_ref, quantity=10)
    feed = _ActivityFeed()
    feed.unavailable = True

    await _sweep(repo, website, feed, spy_held=2.0)

    assert repo.order(order_ref).broker_order_id == _B, "the first pass must record the replacement link"
    assert repo.effect_operation(effect_id).state == "in_progress"

    feed.unavailable = False
    feed.fill(execution_id=_EXEC_A, order_id=a_partial.order_id, quantity=2, price=99.90, at_ms=now)
    feed.fill(execution_id=_EXEC_B, order_id=_B, quantity=8, price=99.80, at_ms=now + 1)
    website.replacements[_B] = _filled(replacement_b, repo, filled_quantity=8, avg=99.80)
    clock.value += 15_000
    result = await _sweep(repo, website, feed, spy_held=10.0)

    assert _coverage_conflict_episodes(repo) == [], "a former member's quantity must never fence the chain"
    effect = repo.effect_operation(effect_id)
    assert effect is not None and effect.state == "succeeded"
    assert [t["transition_kind"] for t in _manual_endings(repo, order_ref)] == ["MANUAL_ORDER_FILLED"]
    assert _exact_credited(repo, order_ref) == [
        (_EXEC_A, "activity_recovery", 2.0, 99.90),
        (_EXEC_B, "activity_recovery", 8.0, 99.80),
    ]
    assert repo.effective_fill_totals_for_order(order_ref)[0] == pytest.approx(10.0, abs=1e-9, rel=0)
    assert repo.attributed_positions_by_symbol() == pytest.approx({"SPY": 10.0}, abs=1e-9, rel=0)
    assert result.verdict == "clean"


# ── A wrong activity-to-execution id bridge fails closed ─────────────────────


async def test_an_activity_naming_a_recorded_execution_under_another_id_raises_a_conflict_not_double_credit(
    clocked_repo,  # noqa: F811
) -> None:
    """The stream recorded the 2-share execution; its activity's id carries a different execution id.

    Read by id, that activity is a second execution of the same 2 shares:
    with the 3 shares the stream missed, the leg's executions would total 7
    on a 5-share order. No order executes more than it asked for, so the
    sweep records neither activity and raises the order's coverage conflict,
    once however many passes read them; the REST cumulative counts the
    missing shares and the leg stays outstanding.
    """
    repo, clock = clocked_repo
    website = _Website(repo=repo)
    manual = await _buy_limit(repo, website)
    order_ref, effect_id = manual.leg.order_ref, manual.leg.effect_operation_id
    original = website.orders[order_ref]
    now = repo.clock()
    partial = original.model_copy(update={
        "status": "partially_filled", "filled_quantity": 2, "filled_avg_price": 99.90,
        "updated_at_ms": now, "observed_at_ms": now,
    })
    await _fill_frame(repo, partial, execution_id=_EXEC_1, quantity=2, price=99.90)
    website.orders[order_ref] = _filled(original, repo, filled_quantity=5, avg=99.84)
    feed = _ActivityFeed()
    feed.fill(
        execution_id=_EXEC_1, activity_execution_id=_MISBRIDGED_EXEC,
        order_id=original.order_id, quantity=2, price=99.90, at_ms=now,
    )
    feed.fill(execution_id=_EXEC_2, order_id=original.order_id, quantity=3, price=99.80, at_ms=now + 1)

    await _sweep(repo, website, feed, spy_held=5.0)
    clock.value += 15_000
    await _sweep(repo, website, feed, spy_held=5.0)

    assert _coverage_conflict_episodes(repo) == ["active"]
    conflict = repo._conn.execute(
        "SELECT headline FROM uncertainties WHERE reason_code = ? AND resolved_at_ms IS NULL",
        (EXECUTION_COVERAGE_CONFLICT_REASON_CODE,),
    ).fetchone()
    assert conflict["headline"] == ACTIVITY_OVER_ORDER_QUANTITY_CONFLICT_COPY.headline, (
        "the recovery's quantity guard raised it, so its copy sends the operator to Alpaca"
    )
    assert _exact_credited(repo, order_ref) == [(_EXEC_1, "websocket", 2.0, 99.90)]
    assert repo.effective_fill_totals_for_order(order_ref)[0] == pytest.approx(5.0, abs=1e-9, rel=0)
    assert repo.attributed_positions_by_symbol() == pytest.approx({"SPY": 5.0}, abs=1e-9, rel=0)
    assert repo.effect_operation(effect_id).state == "in_progress"


# ── How far one pass reads the account activity ───────────────────────────────


async def test_the_activity_walk_stops_once_it_proves_the_window_start(
    clocked_repo,  # noqa: F811
) -> None:
    """The account's activity before the leg's order is long; the walk does not read through it.

    A page whose oldest row falls on a date before the order's, then one
    more full page, prove nothing of the leg's lies further back: the walk
    reads those two pages, not the three the old bounded walk always read.
    """
    repo, _clock = clocked_repo
    website = _Website(repo=repo)
    manual = await _buy_limit(repo, website)
    order_ref, effect_id = manual.leg.order_ref, manual.leg.effect_operation_id
    original = website.orders[order_ref]
    website.orders[order_ref] = _filled(original, repo, filled_quantity=5, avg=99.90)
    assert original.created_at_ms is not None
    feed = _ActivityFeed()
    feed.fill(execution_id=_EXEC_1, order_id=original.order_id, quantity=5, price=99.90, at_ms=repo.clock())
    the_day_before = original.created_at_ms - 86_400_000
    for number in range(400):
        feed.fill(
            execution_id=str(uuid.UUID(int=0x10_000 + number)), order_id=_FOREIGN_ORDER,
            quantity=1, price=400.0, at_ms=the_day_before - number, symbol="QQQ",
        )

    await _sweep(repo, website, feed, spy_held=5.0)

    assert repo.effect_operation(effect_id).state == "succeeded"
    assert feed.pages_read == 2


async def test_the_activity_walk_reads_fills_only_so_other_activity_never_pushes_an_execution_out_of_reach(
    clocked_repo,  # noqa: F811
) -> None:
    """More newer dividend rows than one walk reads lie above the leg's only execution.

    The walk asks Alpaca for the account's FILL activity alone, so none of
    them takes a read's room: the first page holds the execution, and the
    leg ends on it.
    """
    repo, _clock = clocked_repo
    website = _Website(repo=repo)
    manual = await _buy_limit(repo, website)
    order_ref, effect_id = manual.leg.order_ref, manual.leg.effect_operation_id
    original = website.orders[order_ref]
    website.orders[order_ref] = _filled(original, repo, filled_quantity=5, avg=99.90)
    feed = _ActivityFeed()
    now = repo.clock()
    feed.fill(execution_id=_EXEC_1, order_id=original.order_id, quantity=5, price=99.90, at_ms=now)
    for number in range(ACTIVITY_READS_PER_WALK * _ACTIVITY_MAX_PAGES * 100):
        feed.dividend(number=number, on_ms=now + 86_400_000)

    await _sweep(repo, website, feed, spy_held=5.0)

    assert repo.effect_operation(effect_id).state == "succeeded"
    assert _credited(repo, order_ref) == [(_EXEC_1, "activity_recovery", 5.0, 99.90)]
    assert feed.pages_read == 1


@pytest.mark.parametrize("walked_again_once", ["its_answer_changes", "the_interval_passes"])
async def test_a_leg_whose_execution_lies_beyond_one_walk_is_not_walked_again_for_the_same_answer(
    clocked_repo, caplog: pytest.LogCaptureFixture, walked_again_once: str,  # noqa: F811
) -> None:
    """More newer fills than one walk reads push the leg's only execution out of reach.

    The walk stops short of the window's start, and the pass says so, naming
    the order. Every walk starts from the newest row again, so the next pass
    would spend the same reads for the same answer: it reads nothing and says
    the walk is deferred. The leg is walked again once its head's answer
    changes, or once the re-walk interval has passed, and stays outstanding
    on its REST cumulative throughout.
    """
    repo, clock = clocked_repo
    website = _Website(repo=repo)
    manual = await _buy_limit(repo, website)
    order_ref, effect_id = manual.leg.order_ref, manual.leg.effect_operation_id
    original = website.orders[order_ref]
    now = repo.clock()
    website.orders[order_ref] = original.model_copy(update={
        "status": "partially_filled", "filled_quantity": 3, "filled_avg_price": 99.90,
        "updated_at_ms": now, "observed_at_ms": now,
    })
    feed = _ActivityFeed()
    feed.fill(execution_id=_EXEC_1, order_id=original.order_id, quantity=3, price=99.90, at_ms=now)
    pages_one_walk_reads = ACTIVITY_READS_PER_WALK * _ACTIVITY_MAX_PAGES
    for number in range(pages_one_walk_reads * 100):
        feed.fill(
            execution_id=str(uuid.UUID(int=0x10_000 + number)), order_id=_FOREIGN_ORDER,
            quantity=1, price=400.0, at_ms=now + 1 + number, symbol="QQQ",
        )

    def _logged(action: str) -> list[tuple[str, str]]:
        return [
            (record.order_ref, record.broker_order_id)
            for record in caplog.records
            if getattr(record, "action", None) == action
        ]

    with caplog.at_level(logging.INFO, logger=manual_order_executions.__name__):
        await _sweep(repo, website, feed, spy_held=3.0)

        assert feed.pages_read == pages_one_walk_reads
        assert _logged("manual_order_executions_beyond_reach") == [(order_ref, original.order_id)]

        clock.value += 15_000
        await _sweep(repo, website, feed, spy_held=3.0)

        assert feed.pages_read == pages_one_walk_reads, "the next pass must not walk the same answer again"
        assert _logged("manual_order_executions_walk_deferred") == [(order_ref, original.order_id)]
        assert _logged("manual_order_executions_beyond_reach") == [(order_ref, original.order_id)]

        held = 3.0
        if walked_again_once == "its_answer_changes":
            website.orders[order_ref] = _filled(original, repo, filled_quantity=5, avg=99.88)
            held = 5.0
            clock.value += 15_000
        else:
            _walk_clock_to(repo, clock.value + BEYOND_REACH_REWALK_INTERVAL_MS)
        await _sweep(repo, website, feed, spy_held=held)

    assert feed.pages_read == 2 * pages_one_walk_reads
    assert _logged("manual_order_executions_beyond_reach") == [(order_ref, original.order_id)] * 2
    assert repo.effect_operation(effect_id).state == "in_progress"
    assert _exact_credited(repo, order_ref) == []
