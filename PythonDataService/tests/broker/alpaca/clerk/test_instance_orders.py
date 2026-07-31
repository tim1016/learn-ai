"""S3 (#1261): bot-namespace order path + per-instance exposure projection.

Acceptance criteria pinned here:
- AC1: a bot submits through the clerk with a `learn-ai/{sid}/v1` ref; the fill
  is journaled, attributed to that instance, and the projection reflects it.
- AC2: two concurrent instances on one account each see ONLY their own
  exposure in hydration and in the per-bot timeline (07-27 wave-one defect).
- AC3: a flatten whose projection lacks the targeted exposure is refused
  BEFORE any broker call; a valid flatten closes only that instance's exposure.
- AC4: re-hydration comes exclusively from the journal projection, never the
  broker account-net map (behavioral + code-path guard).
- AC5: an unattributable broker order raises the existing account hold.
- AC6: the projection fold is idempotent under duplicate and out-of-order
  delivery (dedup by execution identity, not journal position).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from app.broker.alpaca.clerk import journal as journal_module
from app.broker.alpaca.clerk.clerk import AlpacaClerk
from app.broker.alpaca.clerk.exposure import (
    FlattenRefusedError,
    project_instance_exposure,
    project_instance_timeline,
    strategy_instance_id_for_namespace,
)
from app.broker.alpaca.clerk.models import ClerkEntryKind
from app.broker.contract.errors import (
    BrokerOrderRejected,
    BrokerSubmissionHeld,
    BrokerUnavailable,
)
from app.broker.contract.models import (
    BrokerAccountSnapshot,
    BrokerOrder,
    BrokerOrderEvent,
    BrokerOrderLeg,
)
from app.engine.live.order_identity import build_bot_order_namespace
from app.schemas.action_plan import ActionPlan, CloseLegExit, StockEntryLeg, StockInstrument

_SID_A = "alpaca-bot-a"
_SID_B = "alpaca-bot-b"
_T0 = 1_700_000_000_000


def _account(account_id: str = "PA-TEST") -> BrokerAccountSnapshot:
    return BrokerAccountSnapshot(
        broker="alpaca",
        account_id=account_id,
        account_status="ACTIVE",
        currency="USD",
        cash=1000.0,
        equity=1000.0,
        buying_power=2000.0,
        portfolio_value=1000.0,
        long_market_value=0.0,
        short_market_value=0.0,
        pattern_day_trader=False,
        trading_blocked=False,
        account_blocked=False,
        created_at_ms=1_600_000_000_000,
        observed_at_ms=_T0,
    )


def _accepted_order(client_order_id: str, *, symbol: str = "SPY", side: str = "buy") -> BrokerOrder:
    return BrokerOrder(
        broker="alpaca",
        order_id=f"broker-{abs(hash(client_order_id)) % 10_000_000}",
        client_order_id=client_order_id,
        symbol=symbol,
        asset_class="us_equity",
        side=side,
        order_type="market",
        time_in_force="day",
        quantity=1.0,
        filled_quantity=0.0,
        limit_price=None,
        stop_price=None,
        filled_avg_price=None,
        status="accepted",
        submitted_at_ms=_T0,
        created_at_ms=_T0,
        updated_at_ms=_T0,
        filled_at_ms=None,
        canceled_at_ms=None,
        expired_at_ms=None,
        events=[],
        observed_at_ms=_T0,
    )


class _FakeBroker:
    """Read+trade double: acks every submit, records calls, serves positions."""

    broker_id = "alpaca"

    def __init__(self) -> None:
        self.submit_calls: list[tuple[BrokerOrderLeg, str]] = []
        self.cancel_calls: list[str] = []
        self.submit_error: Exception | None = None
        self.cancel_error: Exception | None = None
        self.lookup_result: BrokerOrder | Exception | None = _accepted_order("unused")
        # A pre-existing account-net position the hydration path must NEVER
        # adopt (AC4 / 07-27 wave-one defect).
        self.account_net_positions: list[dict[str, Any]] = [
            {"symbol": "SPY", "quantity": 999.0}
        ]
        self.list_positions_calls = 0

    async def get_account(self) -> BrokerAccountSnapshot:
        return _account()

    async def submit(self, leg: BrokerOrderLeg, *, client_order_id: str) -> BrokerOrder:
        self.submit_calls.append((leg, client_order_id))
        if self.submit_error is not None:
            raise self.submit_error
        return _accepted_order(client_order_id, symbol=leg.symbol, side=leg.side.value)

    async def list_positions(self) -> list[dict[str, Any]]:
        self.list_positions_calls += 1
        return self.account_net_positions

    async def cancel(self, order_id: str) -> None:
        self.cancel_calls.append(order_id)
        if self.cancel_error is not None:
            raise self.cancel_error
        return None

    async def get_order_by_client_order_id(self, client_order_id: str) -> BrokerOrder | None:
        if isinstance(self.lookup_result, Exception):
            raise self.lookup_result
        if self.lookup_result is None:
            return None
        return _accepted_order(client_order_id)


@pytest.fixture(autouse=True)
def _clerk_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the clerk journal at a tmp dir for every test."""
    monkeypatch.setenv("ALPACA_CLERK_DIR", str(tmp_path))
    journal_module.reset_clerk_settings_for_testing()
    yield tmp_path
    journal_module.reset_clerk_settings_for_testing()


def _fill_event(quantity: float = 1.0, *, event_type: str = "fill") -> BrokerOrderEvent:
    return BrokerOrderEvent(
        event_type=event_type, occurred_at_ms=_T0 + 1_000, price=400.0, quantity=quantity
    )


def _effect_plan(symbol: str = "SPY", *, position: str = "long") -> ActionPlan:
    return ActionPlan(
        on_enter=[
            StockEntryLeg(
                leg_id="primary",
                instrument=StockInstrument(kind="stock", underlying=symbol),
                position=position,
                qty_ratio=1,
            )
        ],
        on_exit=[CloseLegExit(kind="close_leg", entry_leg_id="primary")],
    )


async def _submit_and_fill(
    clerk: AlpacaClerk,
    sid: str,
    *,
    symbol: str = "SPY",
    side: str = "buy",
    quantity: float = 1.0,
    exec_id: str | None = None,
) -> str:
    """Submit one leg under the bot namespace and journal its fill. Returns order_ref."""
    result = await clerk.submit_for_instance(
        strategy_instance_id=sid,
        legs=[BrokerOrderLeg(symbol=symbol, side=side, quantity=quantity)],
    )
    leg_result = result.results[0]
    assert leg_result.status == "acked"
    order_ref = leg_result.order_ref
    await clerk.record_lifecycle_event(
        client_order_id=order_ref,
        event=_fill_event(quantity),
        event_key=f"exec:{exec_id or (order_ref + '-x1')}",
        order=_accepted_order(order_ref, symbol=symbol, side=side),
    )
    return order_ref


async def _owned(clerk: AlpacaClerk, sid: str):
    """Hydration composition: journal snapshot -> pure per-instance fold."""
    return project_instance_exposure(
        await clerk.read_journal_entries(), namespace=build_bot_order_namespace(sid)
    )


async def _timeline(clerk: AlpacaClerk, sid: str):
    """P12 composition: journal snapshot -> namespace-filtered timeline."""
    return project_instance_timeline(await clerk.read_journal_entries(), sid)


# ── AC1: bot-namespace submit → attributed fill → projection ──────────


async def test_bot_namespace_submit_fill_attribution_and_projection() -> None:
    broker = _FakeBroker()
    clerk = AlpacaClerk(read=broker, trade=broker)

    order_ref = await _submit_and_fill(clerk, _SID_A, quantity=1.0)

    # The wire client_order_id carries the bot-namespace ref.
    assert order_ref.startswith(f"learn-ai/{_SID_A}/v1:")
    _leg, wire_client_order_id = broker.submit_calls[0]
    assert wire_client_order_id == order_ref

    # The fill was attributed (ORDER_EVENT, owned) — not unexplained.
    entries = clerk._journal.read_entries()  # type: ignore[union-attr]
    kinds = [entry.kind for entry in entries]
    assert ClerkEntryKind.ORDER_EVENT in kinds
    assert ClerkEntryKind.UNEXPLAINED_ORDER not in kinds

    exposure = await _owned(clerk, _SID_A)
    assert len(exposure) == 1
    assert exposure[0].symbol == "SPY"
    assert exposure[0].quantity == 1.0
    assert exposure[0].strategy_instance_id == _SID_A
    assert exposure[0].namespace == build_bot_order_namespace(_SID_A)


# ── AC2: two concurrent instances see only their own exposure (07-27 pin) ──


async def test_two_instances_on_one_account_see_only_their_own_exposure() -> None:
    broker = _FakeBroker()
    clerk = AlpacaClerk(read=broker, trade=broker)

    await _submit_and_fill(clerk, _SID_A, symbol="SPY", quantity=2.0)
    await _submit_and_fill(clerk, _SID_B, symbol="QQQ", quantity=3.0)

    exposure_a = await _owned(clerk, _SID_A)
    exposure_b = await _owned(clerk, _SID_B)

    assert [(e.symbol, e.quantity) for e in exposure_a] == [("SPY", 2.0)]
    assert [(e.symbol, e.quantity) for e in exposure_b] == [("QQQ", 3.0)]

    timeline_a = await _timeline(clerk, _SID_A)
    timeline_b = await _timeline(clerk, _SID_B)
    ns_a = build_bot_order_namespace(_SID_A)
    ns_b = build_bot_order_namespace(_SID_B)
    assert timeline_a and all(e.order_ref.startswith(f"{ns_a}:") for e in timeline_a)
    assert timeline_b and all(e.order_ref.startswith(f"{ns_b}:") for e in timeline_b)


async def test_same_symbol_two_instances_split_correctly() -> None:
    """Both bots hold the SAME symbol: each projection carries only its share."""
    broker = _FakeBroker()
    clerk = AlpacaClerk(read=broker, trade=broker)

    await _submit_and_fill(clerk, _SID_A, symbol="SPY", quantity=2.0)
    await _submit_and_fill(clerk, _SID_B, symbol="SPY", quantity=5.0)

    exposure_a = await _owned(clerk, _SID_A)
    exposure_b = await _owned(clerk, _SID_B)

    assert [(e.symbol, e.quantity) for e in exposure_a] == [("SPY", 2.0)]
    assert [(e.symbol, e.quantity) for e in exposure_b] == [("SPY", 5.0)]


# ── AC3: flatten verified against the projection before any broker call ──


async def test_flatten_without_owned_exposure_is_refused_before_broker_call() -> None:
    broker = _FakeBroker()
    clerk = AlpacaClerk(read=broker, trade=broker)

    # B holds SPY; A holds nothing. A's flatten must be refused pre-broker.
    await _submit_and_fill(clerk, _SID_B, symbol="SPY", quantity=1.0)
    calls_before = len(broker.submit_calls)

    with pytest.raises(FlattenRefusedError):
        await clerk.flatten_instance(strategy_instance_id=_SID_A, symbol="SPY", quantity=1.0)

    assert len(broker.submit_calls) == calls_before  # no broker call happened


async def test_flatten_exceeding_owned_quantity_is_refused() -> None:
    broker = _FakeBroker()
    clerk = AlpacaClerk(read=broker, trade=broker)
    await _submit_and_fill(clerk, _SID_A, symbol="SPY", quantity=2.0)
    calls_before = len(broker.submit_calls)

    with pytest.raises(FlattenRefusedError):
        await clerk.flatten_instance(strategy_instance_id=_SID_A, symbol="SPY", quantity=3.0)

    assert len(broker.submit_calls) == calls_before


async def test_valid_flatten_closes_only_this_instances_exposure() -> None:
    broker = _FakeBroker()
    clerk = AlpacaClerk(read=broker, trade=broker)
    await _submit_and_fill(clerk, _SID_A, symbol="SPY", quantity=2.0)
    await _submit_and_fill(clerk, _SID_B, symbol="SPY", quantity=5.0)

    result = await clerk.flatten_instance(
        strategy_instance_id=_SID_A, symbol="SPY", quantity=2.0
    )

    leg_result = result.results[0]
    assert leg_result.status == "acked"
    flatten_leg, flatten_ref = broker.submit_calls[-1]
    assert flatten_leg.side.value == "sell"  # closes a long
    assert flatten_leg.quantity == 2.0
    assert flatten_ref.startswith(f"learn-ai/{_SID_A}/v1:")

    # Journal the flatten's fill; A goes flat, B untouched.
    await clerk.record_lifecycle_event(
        client_order_id=flatten_ref,
        event=_fill_event(2.0),
        event_key="exec:flatten-a-1",
        order=_accepted_order(flatten_ref, symbol="SPY", side="sell"),
    )
    assert await _owned(clerk, _SID_A) == ()
    exposure_b = await _owned(clerk, _SID_B)
    assert [(e.symbol, e.quantity) for e in exposure_b] == [("SPY", 5.0)]


# ── AC4: hydration source is the journal projection, never account-net ──


async def test_hydration_never_reads_broker_account_net_positions() -> None:
    broker = _FakeBroker()  # account-net says 999 SPY
    clerk = AlpacaClerk(read=broker, trade=broker)

    exposure = await _owned(clerk, _SID_A)

    assert exposure == ()  # journal-owned only: nothing journaled, nothing owned
    assert broker.list_positions_calls == 0  # the code path never consulted it


def test_exposure_module_never_touches_broker_read_surface() -> None:
    """Code-path guard: the projection module cannot reach the account-net map."""
    import app.broker.alpaca.clerk.exposure as exposure_mod

    source = Path(exposure_mod.__file__).read_text(encoding="utf-8")
    for banned in ("list_positions", "BrokerReadPort", "get_account"):
        assert banned not in source, f"exposure projection references {banned!r}"


async def test_restart_rehydrates_exactly_the_journal_owned_exposure() -> None:
    broker = _FakeBroker()
    clerk = AlpacaClerk(read=broker, trade=broker)
    await _submit_and_fill(clerk, _SID_A, symbol="SPY", quantity=2.0)

    # A "restarted bot" is a fresh clerk over the same durable journal.
    restarted = AlpacaClerk(read=broker, trade=broker)
    exposure = await _owned(restarted, _SID_A)

    assert [(e.symbol, e.quantity) for e in exposure] == [("SPY", 2.0)]


# ── AC5: unattributable order raises the existing account hold ──────────


async def test_foreign_bot_namespace_order_raises_hold_and_blocks_submits() -> None:
    broker = _FakeBroker()
    clerk = AlpacaClerk(read=broker, trade=broker)
    await _submit_and_fill(clerk, _SID_A)  # establish the account + a known namespace

    kind = await clerk.record_lifecycle_event(
        client_order_id="learn-ai/never-minted-bot/v1:AAAAAAAAAAAAAAAAAAAAAA",
        event=_fill_event(1.0),
        event_key="exec:foreign-1",
        order=None,
    )

    assert kind is ClerkEntryKind.UNEXPLAINED_ORDER
    with pytest.raises(BrokerSubmissionHeld):
        await clerk.submit_for_instance(
            strategy_instance_id=_SID_A,
            legs=[BrokerOrderLeg(symbol="SPY", side="buy", quantity=1)],
        )


# ── AC6: fold idempotent under duplicate + out-of-order delivery ────────


async def test_projection_fold_is_idempotent_under_duplicate_and_out_of_order_delivery() -> None:
    broker = _FakeBroker()
    clerk = AlpacaClerk(read=broker, trade=broker)
    ref_a = await _submit_and_fill(clerk, _SID_A, symbol="SPY", quantity=2.0, exec_id="e-1")

    # Duplicate delivery of the SAME execution (same event_key).
    await clerk.record_lifecycle_event(
        client_order_id=ref_a,
        event=_fill_event(2.0),
        event_key="exec:e-1",
        order=_accepted_order(ref_a),
    )
    exposure = await _owned(clerk, _SID_A)
    assert [(e.symbol, e.quantity) for e in exposure] == [("SPY", 2.0)]

    # Out-of-order: shuffle the journal entries; the fold must not care.
    entries = clerk._journal.read_entries()  # type: ignore[union-attr]
    ns = build_bot_order_namespace(_SID_A)
    reordered = list(reversed(entries))
    forward = project_instance_exposure(entries, namespace=ns)
    backward = project_instance_exposure(reordered, namespace=ns)
    assert forward == backward
    assert [(e.symbol, e.quantity) for e in forward] == [("SPY", 2.0)]


def test_fills_without_execution_identity_do_not_accumulate() -> None:
    """Mirrors the canonical non-empty-exec_id gate (no identity → no dedup → skip)."""
    assert project_instance_exposure([]) == ()


# ── namespace parsing (ADR 0008 — exact structure, never a prefix) ─────


@pytest.mark.parametrize(
    ("namespace", "expected"),
    [
        ("learn-ai/bot-1/v1", "bot-1"),
        ("learn-ai/bot-1/v10", None),  # version mismatch must not prefix-match
        ("learn-ai//v1", None),
        ("manual/inkant/v1", None),
        ("learn-ai/bot-1/v1/extra", None),
    ],
)
def test_strategy_instance_id_for_namespace_exact_structure(
    namespace: str, expected: str | None
) -> None:
    assert strategy_instance_id_for_namespace(namespace) == expected


# ── P12: timeline is journal-derived, no sidecar ledger ────────────────


async def test_timeline_is_a_pure_journal_projection(tmp_path: Path) -> None:
    broker = _FakeBroker()
    clerk = AlpacaClerk(read=broker, trade=broker)
    await _submit_and_fill(clerk, _SID_A)

    timeline = await _timeline(clerk, _SID_A)
    kinds = [entry.kind for entry in timeline]
    assert ClerkEntryKind.INTENT_RECORDED in kinds
    assert ClerkEntryKind.SUBMIT_ACKED in kinds
    assert ClerkEntryKind.ORDER_EVENT in kinds

    # No per-run sidecar ledger was created anywhere under the clerk root:
    # the only durable artifacts are the account inbox + journal JSONL pair.
    files = sorted(p.name for p in tmp_path.rglob("*") if p.is_file())
    assert files == ["order_inbox.jsonl", "order_journal.jsonl"]
    assert project_instance_timeline([], _SID_A) == ()


# ── S7: durable Clerk-owned ENTER / EXIT operations ────────────────────


async def test_effect_enter_derives_side_and_replay_never_duplicates_broker_work() -> None:
    from app.broker.alpaca.clerk.models import EffectOperationState, EffectPurpose

    broker = _FakeBroker()
    clerk = AlpacaClerk(read=broker, trade=broker)
    plan = _effect_plan()

    first = await clerk.execute_for_instance(
        strategy_instance_id=_SID_A,
        run_id="run-1",
        decision_id="bar-1700000000000-enter",
        purpose=EffectPurpose.ENTER,
        action_plan=plan,
        quantity=2,
    )
    replay = await clerk.execute_for_instance(
        strategy_instance_id=_SID_A,
        run_id="run-2",
        decision_id="bar-1700000000000-enter",
        purpose=EffectPurpose.ENTER,
        action_plan=plan,
        quantity=2,
    )

    assert first.state is EffectOperationState.SUBMITTED
    assert replay == first
    assert [(leg.side.value, leg.quantity) for leg, _ref in broker.submit_calls] == [("buy", 2.0)]
    entries = await clerk.read_journal_entries()
    assert [entry.kind for entry in entries if entry.effect_receipt] == [
        ClerkEntryKind.EFFECT_ACCEPTED,
        ClerkEntryKind.EFFECT_RECEIPT,
    ]
    assert all(
        entry.effect_operation_id == "bar-1700000000000-enter"
        for entry in entries
        if entry.kind is ClerkEntryKind.INTENT_RECORDED
    )


async def test_stop_custody_cancels_only_a_clerk_owned_working_enter() -> None:
    """STOP cancels an ENTER child without inventing an exposure claim."""
    from app.broker.alpaca.clerk.models import EffectPurpose

    broker = _FakeBroker()
    clerk = AlpacaClerk(read=broker, trade=broker)
    await clerk.execute_for_instance(
        strategy_instance_id=_SID_A,
        run_id="run-1",
        decision_id="bar-1700000030000-enter",
        purpose=EffectPurpose.ENTER,
        action_plan=_effect_plan(),
        quantity=2,
    )

    cancelled = await clerk.cancel_working_entries_for_instance(_SID_A)

    assert len(cancelled) == 1
    assert cancelled[0].status == "acked"
    assert broker.cancel_calls == [cancelled[0].order_id]
    entries = await clerk.read_journal_entries()
    assert ClerkEntryKind.CANCEL_ACKED in [entry.kind for entry in entries]


async def test_effect_enter_rejection_is_a_backend_authored_terminal_receipt() -> None:
    from app.broker.alpaca.clerk.models import EffectOperationState, EffectPurpose

    broker = _FakeBroker()
    broker.submit_error = BrokerOrderRejected(
        "The broker rejected the entry.", broker="alpaca", detail="Buying power is insufficient."
    )
    clerk = AlpacaClerk(read=broker, trade=broker)

    receipt = await clerk.execute_for_instance(
        strategy_instance_id=_SID_A,
        run_id="run-1",
        decision_id="bar-1700000060000-enter",
        purpose=EffectPurpose.ENTER,
        action_plan=_effect_plan(),
        quantity=2,
    )

    assert receipt.state is EffectOperationState.REJECTED
    assert receipt.next_step == "Buying power is insufficient."
    assert await _owned(clerk, _SID_A) == ()
    projected = await clerk.read_effect_receipts_for_instance(_SID_A)
    assert projected == (receipt,)


async def test_effect_enter_uncertainty_stays_instance_scoped_and_never_retries() -> None:
    from app.broker.alpaca.clerk.models import EffectOperationState, EffectPurpose

    broker = _FakeBroker()
    broker.submit_error = BrokerUnavailable(
        "The broker did not confirm the entry.", broker="alpaca", detail="Timed out."
    )
    broker.lookup_result = None
    clerk = AlpacaClerk(read=broker, trade=broker)
    plan = _effect_plan()

    first = await clerk.execute_for_instance(
        strategy_instance_id=_SID_A,
        run_id="run-1",
        decision_id="bar-1700000120000-enter",
        purpose=EffectPurpose.ENTER,
        action_plan=plan,
        quantity=2,
    )
    replay = await clerk.execute_for_instance(
        strategy_instance_id=_SID_A,
        run_id="run-2",
        decision_id="bar-1700000120000-enter",
        purpose=EffectPurpose.ENTER,
        action_plan=plan,
        quantity=2,
    )

    assert first.state is EffectOperationState.UNCERTAIN
    assert replay == first
    assert len(broker.submit_calls) == 1


async def test_effect_acceptance_survives_caller_cancellation() -> None:
    from app.broker.alpaca.clerk.models import EffectOperationState, EffectPurpose

    class _GatedBroker(_FakeBroker):
        def __init__(self) -> None:
            super().__init__()
            self.submit_started = asyncio.Event()
            self.release_submit = asyncio.Event()
            self.submit_finished = asyncio.Event()

        async def submit(self, leg: BrokerOrderLeg, *, client_order_id: str) -> BrokerOrder:
            self.submit_started.set()
            await self.release_submit.wait()
            try:
                return await super().submit(leg, client_order_id=client_order_id)
            finally:
                self.submit_finished.set()

    broker = _GatedBroker()
    clerk = AlpacaClerk(read=broker, trade=broker)
    task = asyncio.create_task(
        clerk.execute_for_instance(
            strategy_instance_id=_SID_A,
            run_id="run-1",
            decision_id="bar-1700000180000-enter",
            purpose=EffectPurpose.ENTER,
            action_plan=_effect_plan(),
            quantity=2,
        )
    )
    await broker.submit_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    broker.release_submit.set()
    await asyncio.wait_for(broker.submit_finished.wait(), timeout=1)
    async with asyncio.timeout(1):
        while True:
            receipts = await clerk.read_effect_receipts_for_instance(_SID_A)
            if receipts[-1].state is EffectOperationState.SUBMITTED:
                break
            await asyncio.sleep(0)

    assert receipts[-1].state is EffectOperationState.SUBMITTED
    assert len(broker.submit_calls) == 1


async def test_effect_exit_cancels_working_entry_before_reducing_final_exposure() -> None:
    from app.broker.alpaca.clerk.models import EffectOperationState, EffectPurpose

    broker = _FakeBroker()
    clerk = AlpacaClerk(read=broker, trade=broker)
    plan = _effect_plan()
    await clerk.submit_for_instance(
        strategy_instance_id=_SID_A,
        legs=[BrokerOrderLeg(symbol="SPY", side="buy", quantity=2)],
    )

    receipt = await clerk.execute_for_instance(
        strategy_instance_id=_SID_A,
        run_id="run-1",
        decision_id="bar-1700000180000-exit",
        purpose=EffectPurpose.EXIT,
        action_plan=plan,
        quantity=2,
    )

    assert receipt.state is EffectOperationState.EXIT_PENDING
    assert len(broker.submit_calls) == 1  # no reducing order before cancel settles
    assert len(broker.cancel_calls) == 1
    entries = await clerk.read_journal_entries()
    assert [entry.kind for entry in entries if entry.broker_order_id] == [
        ClerkEntryKind.CANCEL_RECORDED,
        ClerkEntryKind.CANCEL_ACKED,
    ]


async def test_effect_exit_cancel_failure_is_unprovable_not_pending() -> None:
    from app.broker.alpaca.clerk.models import EffectOperationState, EffectPurpose

    broker = _FakeBroker()
    clerk = AlpacaClerk(read=broker, trade=broker)
    await clerk.submit_for_instance(
        strategy_instance_id=_SID_A,
        legs=[BrokerOrderLeg(symbol="SPY", side="buy", quantity=2)],
    )
    broker.cancel_error = BrokerUnavailable(
        "The cancellation outcome is unknown.", broker="alpaca", detail="Timed out."
    )

    receipt = await clerk.execute_for_instance(
        strategy_instance_id=_SID_A,
        run_id="run-1",
        decision_id="bar-1700000210000-exit",
        purpose=EffectPurpose.EXIT,
        action_plan=_effect_plan(),
        quantity=2,
    )

    assert receipt.state is EffectOperationState.UNPROVABLE
    assert len(broker.submit_calls) == 1


async def test_effect_exit_uncertainty_replay_never_submits_a_second_reduction() -> None:
    from app.broker.alpaca.clerk.models import EffectOperationState, EffectPurpose

    broker = _FakeBroker()
    clerk = AlpacaClerk(read=broker, trade=broker)
    await _submit_and_fill(clerk, _SID_A, quantity=2.0)
    broker.submit_error = BrokerUnavailable(
        "The broker did not confirm the exit.", broker="alpaca", detail="Timed out."
    )
    broker.lookup_result = None
    plan = _effect_plan()

    first = await clerk.execute_for_instance(
        strategy_instance_id=_SID_A,
        run_id="run-1",
        decision_id="bar-1700000230000-exit",
        purpose=EffectPurpose.EXIT,
        action_plan=plan,
        quantity=2,
    )
    replay = await clerk.execute_for_instance(
        strategy_instance_id=_SID_A,
        run_id="run-2",
        decision_id="bar-1700000230000-exit",
        purpose=EffectPurpose.EXIT,
        action_plan=plan,
        quantity=2,
    )

    assert first.state is EffectOperationState.UNCERTAIN
    assert replay == first
    assert len(broker.submit_calls) == 2  # entry + one uncertain reduction


async def test_effect_exit_reduces_exact_attributed_quantity_then_proves_flat() -> None:
    from app.broker.alpaca.clerk.models import EffectOperationState, EffectPurpose

    broker = _FakeBroker()
    clerk = AlpacaClerk(read=broker, trade=broker)
    plan = _effect_plan()
    await _submit_and_fill(clerk, _SID_A, quantity=2.0)

    pending = await clerk.execute_for_instance(
        strategy_instance_id=_SID_A,
        run_id="run-1",
        decision_id="bar-1700000240000-exit",
        purpose=EffectPurpose.EXIT,
        action_plan=plan,
        quantity=2,
    )
    exit_leg, exit_ref = broker.submit_calls[-1]
    assert pending.state is EffectOperationState.EXIT_PENDING
    assert (exit_leg.side.value, exit_leg.quantity) == ("sell", 2.0)
    await clerk.record_lifecycle_event(
        client_order_id=exit_ref,
        event=_fill_event(2.0),
        event_key="exec:effect-exit-fill",
        order=_accepted_order(exit_ref, side="sell"),
    )

    completed = await clerk.execute_for_instance(
        strategy_instance_id=_SID_A,
        run_id="run-2",
        decision_id="bar-1700000240000-exit",
        purpose=EffectPurpose.EXIT,
        action_plan=plan,
        quantity=2,
    )
    assert completed.state is EffectOperationState.FLAT


async def test_effect_exit_recovers_partial_fill_after_clerk_restart() -> None:
    from app.broker.alpaca.clerk.models import EffectOperationState, EffectPurpose

    broker = _FakeBroker()
    clerk = AlpacaClerk(read=broker, trade=broker)
    plan = _effect_plan()
    entered = await clerk.execute_for_instance(
        strategy_instance_id=_SID_A,
        run_id="run-1",
        decision_id="bar-1700000300000-enter",
        purpose=EffectPurpose.ENTER,
        action_plan=plan,
        quantity=2,
    )
    entry_ref = entered.child_order_refs[0]
    await clerk.record_lifecycle_event(
        client_order_id=entry_ref,
        event=_fill_event(1.0, event_type="partial_fill"),
        event_key="exec:effect-partial-entry",
        order=_accepted_order(entry_ref, side="buy"),
    )
    pending = await clerk.execute_for_instance(
        strategy_instance_id=_SID_A,
        run_id="run-1",
        decision_id="bar-1700000360000-exit",
        purpose=EffectPurpose.EXIT,
        action_plan=plan,
        quantity=2,
    )
    assert pending.state is EffectOperationState.EXIT_PENDING

    # A process restart loses in-memory tasks, not the durable effect.  Its
    # replay reads cancellation and partial-fill facts from the same journal.
    restarted = AlpacaClerk(read=broker, trade=broker)
    await restarted.record_lifecycle_event(
        client_order_id=entry_ref,
        event=_fill_event(0, event_type="canceled"),
        event_key="event:effect-entry-canceled",
        order=_accepted_order(entry_ref, side="buy"),
    )
    resumed = await restarted.execute_for_instance(
        strategy_instance_id=_SID_A,
        run_id="run-2",
        decision_id="bar-1700000360000-exit",
        purpose=EffectPurpose.EXIT,
        action_plan=plan,
        quantity=2,
    )

    exit_leg, exit_ref = broker.submit_calls[-1]
    assert resumed.state is EffectOperationState.EXIT_PENDING
    assert (exit_leg.side.value, exit_leg.quantity) == ("sell", 1.0)
    await restarted.record_lifecycle_event(
        client_order_id=exit_ref,
        event=_fill_event(1.0),
        event_key="exec:effect-partial-exit",
        order=_accepted_order(exit_ref, side="sell"),
    )
    completed = await restarted.execute_for_instance(
        strategy_instance_id=_SID_A,
        run_id="run-3",
        decision_id="bar-1700000360000-exit",
        purpose=EffectPurpose.EXIT,
        action_plan=plan,
        quantity=2,
    )
    assert completed.state is EffectOperationState.FLAT
