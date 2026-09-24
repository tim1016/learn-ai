"""A terminal order stays reconcilable until its fills are proven complete (#2305).

A ``trade_updates`` fill slice can be lost: dropped on capture/map failure, or
never delivered because it executed between the boot reconcile and the first
``listen`` (#2316). When the order's next frame is terminal, ``broker_state``
goes terminal while only that frame's slice is recorded. The fix is keyed on
the ORDER, not on the lost frame: every broker acknowledgement durably records
the broker's own cumulative ``filled_quantity``, and a terminal order whose
effective fills fall short of it stays on the reconciliation worklist, so the
sweep's exact lookup folds the missing cumulative through the one shared
``fold_order_evidence`` path.

Real code: ``TradeUpdatesConsumer`` (first connect), ``SqliteTradeUpdateEvidenceSink``,
the SQLite folds and ``reconcile_account`` on a temp clerk repo. Faked: the
broker read/trade ports, the websocket frame source, the capture journal, the
clock. Probes adapted from the #2305 / #2316 / #2346 issue comments.
"""

from __future__ import annotations

import copy
import json
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import pytest

from app.broker.alpaca.clerk.sqlite import reads, schema
from app.broker.alpaca.clerk.sqlite.enter import submit_enter
from app.broker.alpaca.clerk.sqlite.exit import accept_exit, resolve_exit
from app.broker.alpaca.clerk.sqlite.facts import OrderSubmitAckedFacts
from app.broker.alpaca.clerk.sqlite.reconcile import reconcile_account
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import ReentrantAsyncLock
from app.broker.alpaca.clerk.sqlite.uncertainty import (
    Capability,
    ReductionIntent,
    decide_capability,
)
from app.broker.alpaca.clerk.trade_evidence import SqliteTradeUpdateEvidenceSink
from app.broker.alpaca.trade_updates import TradeUpdatesConsumer
from app.broker.capture.journal import CaptureJournal
from app.broker.contract.models import BrokerOrder, BrokerOrderEvent, BrokerOrderLeg
from app.broker.contract.ports import BrokerReadPort
from tests.broker.alpaca.clerk.sqlite.test_reconcile import (
    ACCOUNT_ID,
    WATCHDOG_RUN,
    WATCHDOG_SID,
    _broker_order,
    _FakeRead,
    _FakeTrade,
    _held_position,
    _leg,
    _position,
    clocked_repo,  # noqa: F401 -- pytest fixture
)
from tests.broker.alpaca.conftest import load_alpaca_fixture_file

QTY_ATOL = 1e-9


class _Trade(_FakeTrade):
    """Exact lookup answers whatever the broker currently holds for that ref."""

    def __init__(self) -> None:
        super().__init__()
        self.broker_state: dict[str, BrokerOrder] = {}
        self.legs: list[tuple[str, float]] = []

    async def get_order_by_client_order_id(self, client_order_id: str) -> BrokerOrder | None:
        self.lookup_calls.append(client_order_id)
        return self.broker_state.get(client_order_id)

    async def submit(self, leg: BrokerOrderLeg, *, client_order_id: str) -> BrokerOrder:
        self.legs.append((str(leg.side), leg.quantity))
        return await super().submit(leg, client_order_id=client_order_id)


class _RecordingReconciler:
    def __init__(self) -> None:
        self.calls = 0

    async def reconcile_account(self, *, trigger: str) -> None:
        del trigger
        self.calls += 1


class _Capture:
    """Capture journal double; frames at ``fail_at`` (0 = the auth ack) fail capture."""

    def __init__(self, *, fail_at: frozenset[int] = frozenset()) -> None:
        self._fail_at = fail_at
        self._seen = 0

    def record(self, **_kwargs: Any) -> bool:
        index = self._seen
        self._seen += 1
        return index not in self._fail_at


class _NoClosed:
    async def list_orders(self, **_kw: Any) -> list[BrokerOrder]:
        return []


def _frame(
    *,
    event: str,
    coid: str,
    broker_id: str,
    side: str,
    order_qty: float,
    filled_qty: float,
    status: str,
    qty: float | None = None,
    exec_id: str | None = None,
) -> str:
    frames = load_alpaca_fixture_file("trade_updates", "trade_updates.json")
    template = "fill" if event in {"fill", "partial_fill"} else event
    f = copy.deepcopy(
        next(
            x
            for x in frames
            if x.get("stream") == "trade_updates" and x["data"].get("event") == template
        )
    )
    d = f["data"]
    ts = "2023-11-15T15:00:05.000000Z"
    d.update({"event": event, "timestamp": ts, "at": ts})
    if exec_id is not None:
        d.update({"qty": str(qty), "price": "100.0", "execution_id": exec_id, "position_qty": "0"})
    o = d["order"]
    o.update(
        {
            "id": broker_id,
            "client_order_id": coid,
            "symbol": "SPY",
            "side": side,
            "qty": str(order_qty),
            "filled_qty": str(filled_qty),
            "filled_avg_price": "100.0" if filled_qty else None,
            "status": status,
            "position_intent": "buy_to_open" if side == "buy" else "sell_to_close",
            "created_at": "2023-11-15T15:00:00.000000Z",
            "updated_at": ts,
            "submitted_at": "2023-11-15T15:00:00.000000Z",
            "filled_at": ts if status == "filled" else None,
        }
    )
    return json.dumps(f)


async def _connect(
    repo: ClerkSqliteRepository, frames: list[str], *, capture: _Capture | None = None
) -> TradeUpdatesConsumer:
    """One first-connect cycle of the real consumer over ``frames``."""
    sink = SqliteTradeUpdateEvidenceSink(
        repo=repo, intake=ReentrantAsyncLock(), reconciler=_RecordingReconciler()
    )

    def source() -> AsyncIterator[bytes | str]:
        async def _gen() -> AsyncIterator[bytes | str]:
            yield '{"stream":"authorization","data":{"status":"authorized"}}'
            for fr in frames:
                yield fr

        return _gen()

    async def _no_backoff(_attempt: int) -> None:
        return None

    consumer = TradeUpdatesConsumer(
        evidence_sink=sink,
        read=cast(BrokerReadPort, _NoClosed()),
        frame_source=source,
        journal=cast(CaptureJournal, capture or _Capture()),
        backoff=_no_backoff,
        max_reconnects=0,
    )
    await consumer.run()
    return consumer


async def _open_enter(repo: ClerkSqliteRepository, trade: _Trade, *, decision_id: str) -> tuple[str, str, str]:
    """ENTER BUY 5, open with 0 filled at a boot reconcile."""
    sub = await submit_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=WATCHDOG_SID,
        decision_id=decision_id,
        lifecycle_run_id=WATCHDOG_RUN,
        leg=_leg(quantity=5),
        trade=trade,
    )
    ref = sub.order_ref
    assert ref is not None
    order = repo.order(ref)
    assert order is not None and order.broker_order_id is not None
    open0 = _broker_order(ref, order_id=order.broker_order_id, status="accepted", quantity=5.0)
    trade.broker_state[ref] = open0
    await reconcile_account(repo, read=_FakeRead(orders=[open0]), trade=trade)
    return ref, order.broker_order_id, sub.effect_operation_id


def _reconcilable(repo: ClerkSqliteRepository) -> list[str]:
    return [e.effect_operation_id for e in repo.reconcilable_effect_operations()]


def _broker_filled(ref: str, broker_id: str, *, qty: float = 5.0, status: str = "filled") -> BrokerOrder:
    return _broker_order(
        ref,
        order_id=broker_id,
        status=status,
        quantity=5.0,
        filled_quantity=qty,
        filled_avg_price=100.0,
    )


async def _sweep_until_settled(
    repo: ClerkSqliteRepository, clock: Any, trade: _Trade, *, broker_qty: float, passes: int = 3
) -> list[str]:
    verdicts = []
    positions = [_position("SPY", quantity=broker_qty)] if broker_qty else []
    for _ in range(passes):
        clock.advance(15_000)
        result = await reconcile_account(repo, read=_FakeRead(positions=positions), trade=trade)
        verdicts.append(result.verdict)
    return verdicts


def test_order_submit_acked_facts_omit_an_absent_cumulative() -> None:
    """Hash-chained schema evolution: an unfilled acknowledgement stays byte-identical ``{}``."""
    assert OrderSubmitAckedFacts().to_facts_json() == "{}"
    assert OrderSubmitAckedFacts(reported_filled_quantity=5.0).to_facts_json() == (
        '{"reported_filled_quantity":5.0}'
    )
    assert OrderSubmitAckedFacts.from_facts_json("{}") == OrderSubmitAckedFacts()


@pytest.mark.parametrize("terminal", ["fill", "canceled"])
async def test_dropped_partial_fill_then_terminal_frame_converges_after_sweep(
    clocked_repo: tuple[ClerkSqliteRepository, Any],  # noqa: F811
    terminal: str,
) -> None:
    """#2305: exec-A's ``partial_fill`` is dropped at capture; the terminal frame follows."""
    repo, clock = clocked_repo
    trade = _Trade()
    ref, bo, effect_id = await _open_enter(repo, trade, decision_id=f"lost-{terminal}")
    dropped = _frame(
        event="partial_fill", coid=ref, broker_id=bo, side="buy", order_qty=5,
        filled_qty=2, status="partially_filled", qty=2, exec_id="exec-A",
    )
    if terminal == "fill":
        final = _frame(
            event="fill", coid=ref, broker_id=bo, side="buy", order_qty=5,
            filled_qty=5, status="filled", qty=3, exec_id="exec-B",
        )
        broker_qty, recorded = 5.0, 3.0
    else:
        final = _frame(
            event="canceled", coid=ref, broker_id=bo, side="buy", order_qty=5,
            filled_qty=2, status="canceled",
        )
        broker_qty, recorded = 2.0, 0.0

    consumer = await _connect(repo, [dropped, final], capture=_Capture(fail_at=frozenset({1})))
    assert consumer.counters.capture_failures == 1
    assert repo.position(WATCHDOG_SID, "SPY") == pytest.approx(recorded, abs=QTY_ATOL, rel=0)
    assert (repo.order(ref).broker_state or "").lower() == ("filled" if terminal == "fill" else "canceled")
    # Keyed on the order: its recorded fills fall short of the broker's cumulative.
    assert effect_id in _reconcilable(repo)

    trade.broker_state[ref] = _broker_filled(
        ref, bo, qty=broker_qty, status="filled" if terminal == "fill" else "canceled"
    )
    trade.lookup_calls.clear()
    verdicts = await _sweep_until_settled(repo, clock, trade, broker_qty=broker_qty)

    assert repo.position(WATCHDOG_SID, "SPY") == pytest.approx(broker_qty, abs=QTY_ATOL, rel=0)
    assert verdicts[-1] == "clean"
    assert effect_id not in _reconcilable(repo)
    # One exact lookup closes the gap; the settled order is never re-polled.
    assert trade.lookup_calls == [ref]


async def test_fill_in_boot_window_converges_after_sweep(
    clocked_repo: tuple[ClerkSqliteRepository, Any],  # noqa: F811
) -> None:
    """#2316 G5: exec-A fills before ``listen``; the first connect carries only exec-B."""
    repo, clock = clocked_repo
    trade = _Trade()
    ref, bo, effect_id = await _open_enter(repo, trade, decision_id="boot-window")

    consumer = await _connect(
        repo,
        [
            _frame(
                event="fill", coid=ref, broker_id=bo, side="buy", order_qty=5,
                filled_qty=5, status="filled", qty=3, exec_id="exec-B",
            )
        ],
    )
    # No frame was dropped: the execution channel reads healthy throughout.
    assert consumer.evidence_health.healthy
    assert repo.position(WATCHDOG_SID, "SPY") == pytest.approx(3.0, abs=QTY_ATOL, rel=0)
    assert effect_id in _reconcilable(repo)

    trade.broker_state[ref] = _broker_filled(ref, bo)
    verdicts = await _sweep_until_settled(repo, clock, trade, broker_qty=5.0)

    assert repo.position(WATCHDOG_SID, "SPY") == pytest.approx(5.0, abs=QTY_ATOL, rel=0)
    assert verdicts == ["clean", "clean", "clean"]
    assert effect_id not in _reconcilable(repo)


async def test_terminal_order_with_complete_fills_is_not_repolled(
    clocked_repo: tuple[ClerkSqliteRepository, Any],  # noqa: F811
) -> None:
    """Negative: every slice arrived, so the filled order never re-enters the worklist."""
    repo, clock = clocked_repo
    trade = _Trade()
    ref, bo, effect_id = await _open_enter(repo, trade, decision_id="complete")
    await _connect(
        repo,
        [
            _frame(
                event="partial_fill", coid=ref, broker_id=bo, side="buy", order_qty=5,
                filled_qty=2, status="partially_filled", qty=2, exec_id="exec-A",
            ),
            _frame(
                event="fill", coid=ref, broker_id=bo, side="buy", order_qty=5,
                filled_qty=5, status="filled", qty=3, exec_id="exec-B",
            ),
        ],
    )
    assert repo.position(WATCHDOG_SID, "SPY") == pytest.approx(5.0, abs=QTY_ATOL, rel=0)
    assert effect_id not in _reconcilable(repo)

    trade.broker_state[ref] = _broker_filled(ref, bo)
    trade.lookup_calls.clear()
    verdicts = await _sweep_until_settled(repo, clock, trade, broker_qty=5.0)

    assert verdicts == ["clean", "clean", "clean"]
    assert trade.lookup_calls == []


async def test_sweep_cumulative_then_late_exact_slice_leaves_no_coverage_conflict(
    clocked_repo: tuple[ClerkSqliteRepository, Any],  # noqa: F811
) -> None:
    """#2346 guard: the sweep folds the missing cumulative, then exec-A arrives late.

    The late exact slice must replace the cumulative row one-for-one (the
    automatic set proof), never open a lasting ``EXECUTION_COVERAGE_CONFLICT``.
    """
    repo, clock = clocked_repo
    trade = _Trade()
    ref, bo, effect_id = await _open_enter(repo, trade, decision_id="late-slice")
    await _connect(
        repo,
        [
            _frame(
                event="fill", coid=ref, broker_id=bo, side="buy", order_qty=5,
                filled_qty=5, status="filled", qty=3, exec_id="exec-B",
            )
        ],
    )
    trade.broker_state[ref] = _broker_filled(ref, bo)
    await _sweep_until_settled(repo, clock, trade, broker_qty=5.0, passes=1)
    assert repo.position(WATCHDOG_SID, "SPY") == pytest.approx(5.0, abs=QTY_ATOL, rel=0)

    late_order = _broker_order(
        ref, order_id=bo, status="partially_filled", quantity=5.0,
        filled_quantity=2.0, filled_avg_price=100.0,
    )
    await SqliteTradeUpdateEvidenceSink(
        repo=repo, intake=ReentrantAsyncLock(), reconciler=_RecordingReconciler()
    ).record_lifecycle_event(
        client_order_id=ref,
        event=BrokerOrderEvent(
            event_type="partial_fill",
            occurred_at_ms=1_700_000_000_400,
            price=100.0,
            quantity=2.0,
            execution_id="exec-A",
        ),
        event_key="exec:exec-A",
        order=late_order,
        recovery_source=None,
        recovery_window_limit=None,
    )

    conflicts = [
        u
        for u in repo.active_uncertainties_for_admission(strategy_instance_id=WATCHDOG_SID)
        if u["reason_code"] == "EXECUTION_COVERAGE_CONFLICT"
    ]
    assert conflicts == []
    fills = sorted((f["execution_id"], f["qty"]) for f in repo.fills_for_order(ref))
    assert fills == [("exec-A", 2.0), ("exec-B", 3.0)]
    assert repo.position(WATCHDOG_SID, "SPY") == pytest.approx(5.0, abs=QTY_ATOL, rel=0)
    assert effect_id not in _reconcilable(repo)
    verdicts = await _sweep_until_settled(repo, clock, trade, broker_qty=5.0, passes=1)
    assert verdicts == ["clean"]


async def _exit_with_open_reducing(
    repo: ClerkSqliteRepository, trade: _Trade, *, decision_id: str
) -> tuple[str, str, str]:
    """Attributed +10, an EXIT whose SELL 10 is open with 0 filled at a boot reconcile."""
    entry_ref = await _held_position(repo)
    accepted = accept_exit(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=WATCHDOG_SID,
        decision_id=decision_id,
        lifecycle_run_id=WATCHDOG_RUN,
        entry_order_ref=entry_ref,
    )
    trade.broker_state[entry_ref] = _broker_order(
        entry_ref, status="filled", quantity=10.0, filled_quantity=10.0, filled_avg_price=100.0
    )
    resolved = await resolve_exit(repo, effect_operation_id=accepted.effect_operation_id, trade=trade)
    red = resolved.reducing_order_ref
    assert red is not None
    red_order = repo.order(red)
    assert red_order is not None and red_order.broker_order_id is not None
    bo = red_order.broker_order_id
    open0 = _broker_order(red, order_id=bo, side="sell", status="accepted", quantity=10.0)
    trade.broker_state[red] = open0
    await reconcile_account(
        repo, read=_FakeRead(orders=[open0], positions=[_position("SPY", quantity=10.0)]), trade=trade
    )
    return accepted.effect_operation_id, red, bo


def _sell_fill_frame(red: str, bo: str) -> str:
    """The first connect carries only exec-B-sell (6) on an order the broker reports filled 10."""
    return _frame(
        event="fill", coid=red, broker_id=bo, side="sell", order_qty=10,
        filled_qty=10, status="filled", qty=6, exec_id="exec-B-sell",
    )


def _transition_count(repo: ClerkSqliteRepository) -> int:
    return len(repo.custody_transitions())


async def test_exit_reducing_fill_in_boot_window_reaches_flat(
    clocked_repo: tuple[ClerkSqliteRepository, Any],  # noqa: F811
) -> None:
    """#2316 EXIT variant: the reducing order's exec-A (4) falls in the boot window.

    The EXIT refreshes a terminal reducing order whose fills are short of the
    broker's cumulative instead of failing it ``EXIT_NOT_FLAT``.
    """
    repo, clock = clocked_repo
    trade = _Trade()
    exit_id, red, bo = await _exit_with_open_reducing(repo, trade, decision_id="lost-exit")

    await _connect(repo, [_sell_fill_frame(red, bo)])
    assert repo.position(WATCHDOG_SID, "SPY") == pytest.approx(4.0, abs=QTY_ATOL, rel=0)
    trade.broker_state[red] = _broker_order(
        red, order_id=bo, side="sell", status="filled", quantity=10.0,
        filled_quantity=10.0, filled_avg_price=100.0,
    )
    submits_before = len(trade.legs)

    verdicts = await _sweep_until_settled(repo, clock, trade, broker_qty=0.0, passes=2)

    assert repo.position(WATCHDOG_SID, "SPY") == pytest.approx(0.0, abs=QTY_ATOL, rel=0)
    effect = repo.effect_operation(exit_id)
    assert effect is not None and effect.state == "succeeded"
    assert trade.legs[submits_before:] == []
    assert verdicts[-1] == "clean"


async def test_rest_reporting_less_than_the_websocket_settles_exit_not_flat_with_bounded_rows(
    clocked_repo: tuple[ClerkSqliteRepository, Any],  # noqa: F811
) -> None:
    """The frame said filled 10 over a 6 slice; the exact REST lookup says 6.

    The latest acknowledgement is the broker's current word: the lower REST
    report closes the gap, so the EXIT falls through to ``EXIT_NOT_FLAT``
    (master's outcome, which feeds the #2343 watchdog) instead of alternating
    ``unknown``/``in_progress`` and appending rows on every sweep.
    """
    repo, clock = clocked_repo
    trade = _Trade()
    exit_id, red, bo = await _exit_with_open_reducing(repo, trade, decision_id="rest-less")
    await _connect(repo, [_sell_fill_frame(red, bo)])
    trade.broker_state[red] = _broker_order(
        red, order_id=bo, side="sell", status="filled", quantity=10.0,
        filled_quantity=6.0, filled_avg_price=100.0,
    )
    positions = [_position("SPY", quantity=4.0)]

    counts = []
    for _ in range(20):
        # 1 s apart: 20 sweeps stay inside EXIT_NOT_FLAT's 120 s redrive age,
        # so the watchdog's re-drive does not enter the row count.
        clock.advance(1_000)
        await reconcile_account(repo, read=_FakeRead(positions=positions), trade=trade)
        counts.append(_transition_count(repo))

    effect = repo.effect_operation(exit_id)
    assert effect is not None and effect.state == "failed"
    assert repo.active_uncertainty(
        scope="CUSTODY_SUBJECT", reason_code="EXIT_NOT_FLAT", strategy_instance_id=WATCHDOG_SID
    ) is not None
    assert repo.position(WATCHDOG_SID, "SPY") == pytest.approx(4.0, abs=QTY_ATOL, rel=0)
    assert not repo.order_fills_short_of_broker_cumulative(red)
    # Settled after the first sweep: no hash-chained row is appended again.
    assert counts[1:] == [counts[0]] * 19


async def test_unknown_exit_after_a_filled_submit_response_refreshes_with_bounded_rows(
    clocked_repo: tuple[ClerkSqliteRepository, Any],  # noqa: F811
) -> None:
    """The submit response says filled 10 with no slice: the EXIT holds ``unknown``.

    The sweep's re-drive refreshes the terminal reducing order by exact lookup
    (it is short of the reported cumulative), folds the cumulative, proves the
    strategy flat and stops appending rows.
    """
    repo, clock = clocked_repo

    class _FilledOnSubmit(_Trade):
        async def submit(self, leg: BrokerOrderLeg, *, client_order_id: str) -> BrokerOrder:
            self.legs.append((str(leg.side), leg.quantity))
            return _broker_order(
                client_order_id, order_id=f"bo-{client_order_id}", side="sell", status="filled",
                quantity=10.0, filled_quantity=10.0, filled_avg_price=100.0,
            )

    trade = _FilledOnSubmit()
    entry_ref = await _held_position(repo)
    accepted = accept_exit(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=WATCHDOG_SID,
        decision_id="filled-submit",
        lifecycle_run_id=WATCHDOG_RUN,
        entry_order_ref=entry_ref,
    )
    trade.broker_state[entry_ref] = _broker_order(
        entry_ref, status="filled", quantity=10.0, filled_quantity=10.0, filled_avg_price=100.0
    )
    resolved = await resolve_exit(repo, effect_operation_id=accepted.effect_operation_id, trade=trade)
    red = resolved.reducing_order_ref
    assert red is not None
    effect = repo.effect_operation(accepted.effect_operation_id)
    assert effect is not None and effect.state == "unknown"
    trade.broker_state[red] = _broker_order(
        red, order_id=f"bo-{red}", side="sell", status="filled", quantity=10.0,
        filled_quantity=10.0, filled_avg_price=100.0,
    )

    counts = []
    for _ in range(20):
        clock.advance(1_000)
        await reconcile_account(repo, read=_FakeRead(positions=[]), trade=trade)
        counts.append(_transition_count(repo))

    effect = repo.effect_operation(accepted.effect_operation_id)
    assert effect is not None and effect.state == "succeeded"
    assert repo.position(WATCHDOG_SID, "SPY") == pytest.approx(0.0, abs=QTY_ATOL, rel=0)
    assert trade.legs == [("sell", 10.0)]
    assert counts[1:] == [counts[0]] * 19


async def test_two_lost_slices_then_one_late_clears_on_the_next_sweep(
    clocked_repo: tuple[ClerkSqliteRepository, Any],  # noqa: F811
) -> None:
    """#2346: a late exact after the order's final REST fold must not fence the bot for ever.

    exec-A (1) and exec-B (1) are both lost, the terminal exec-C (3) arrives
    with the order filled 5, and the sweep folds the missing cumulative 2, so
    the position is right. Then exec-A alone arrives late: ``{A=1}`` cannot
    prove ``{cumulative 2}``, so an ``EXECUTION_COVERAGE_CONFLICT`` opens while
    exec-B may still complete the set. exec-B never comes, and the order is
    terminal with complete fills, so no REST fold will revisit it: the next
    sweep's order-total proof (final broker cumulative 5 == effective fills,
    ``{A=1}`` inside cumulative 2) closes the episode without moving a fill.
    """
    repo, clock = clocked_repo
    trade = _Trade()
    ref, bo, _effect_id = await _open_enter(repo, trade, decision_id="double-loss")
    await _connect(
        repo,
        [
            _frame(
                event="fill", coid=ref, broker_id=bo, side="buy", order_qty=5,
                filled_qty=5, status="filled", qty=3, exec_id="exec-C",
            )
        ],
    )
    trade.broker_state[ref] = _broker_filled(ref, bo)
    await _sweep_until_settled(repo, clock, trade, broker_qty=5.0, passes=1)
    assert repo.position(WATCHDOG_SID, "SPY") == pytest.approx(5.0, abs=QTY_ATOL, rel=0)

    await SqliteTradeUpdateEvidenceSink(
        repo=repo, intake=ReentrantAsyncLock(), reconciler=_RecordingReconciler()
    ).record_lifecycle_event(
        client_order_id=ref,
        event=BrokerOrderEvent(
            event_type="partial_fill",
            occurred_at_ms=1_700_000_000_300,
            price=100.0,
            quantity=1.0,
            execution_id="exec-A",
        ),
        event_key="exec:exec-A",
        order=_broker_order(
            ref, order_id=bo, status="partially_filled", quantity=5.0,
            filled_quantity=1.0, filled_avg_price=100.0,
        ),
        recovery_source=None,
        recovery_window_limit=None,
    )

    def conflicts() -> list[dict]:
        return [
            u
            for u in repo.active_uncertainties_for_admission(strategy_instance_id=WATCHDOG_SID)
            if u["reason_code"] == "EXECUTION_COVERAGE_CONFLICT"
        ]

    assert len(conflicts()) == 1
    fills_before = repo.fills_for_order(ref)

    verdicts = await _sweep_until_settled(repo, clock, trade, broker_qty=5.0, passes=1)

    assert verdicts == ["clean"]
    assert conflicts() == []
    assert repo.fills_for_order(ref) == fills_before
    assert repo.position(WATCHDOG_SID, "SPY") == pytest.approx(5.0, abs=QTY_ATOL, rel=0)
    reduce = decide_capability(
        repo,
        capability=Capability.REDUCE,
        strategy_instance_id=WATCHDOG_SID,
        reduction_intent=ReductionIntent(symbol="SPY", side="SELL", quantity=5.0),
    )
    assert reduce.allowed


_FILL_INDEXES = frozenset({"ix_fills_order_ref", "ix_fills_superseded_execution_ref"})


def _index_names(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}


def test_v15_migration_adds_the_fill_indexes(tmp_path: Path) -> None:
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path)
    conn = repo._conn
    try:
        for index in sorted(_FILL_INDEXES):
            conn.execute(f"DROP INDEX {index}")
        conn.execute("UPDATE control_meta SET schema_version = 14 WHERE id = 1")
        conn.commit()

        schema.migrate_schema(conn, from_version=14)

        assert conn.execute("SELECT schema_version FROM control_meta").fetchone()[0] == schema.SCHEMA_VERSION
        assert _index_names(conn) >= _FILL_INDEXES
    finally:
        repo.close()


def test_reconcilable_worklist_read_probes_fills_through_its_indexes(
    clocked_repo: tuple[ClerkSqliteRepository, Any],  # noqa: F811
) -> None:
    """Perf guard: every historical ENTER stays nonterminal, so the worklist
    evaluates the fill-completeness predicate per order. It must probe
    ``fills`` through its v15 indexes, never scan it (quadratic in history)."""
    repo, _clock = clocked_repo
    conn = repo._conn
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        reads.reconcilable_effect_operations(conn)
    finally:
        conn.set_trace_callback(None)
    (statement,) = [sql for sql in statements if "FROM effect_operations e" in sql]

    plan = " | ".join(row["detail"] for row in conn.execute("EXPLAIN QUERY PLAN " + statement))

    assert "ix_fills_order_ref" in plan, plan
    assert "ix_fills_superseded_execution_ref" in plan, plan
    assert "SCAN f " not in f"{plan} " and "SCAN successor" not in plan, plan


