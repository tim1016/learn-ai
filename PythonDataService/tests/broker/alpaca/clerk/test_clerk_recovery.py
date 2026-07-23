"""Clerk-service seam tests for S5 crash safety (Alpaca phase 2, #1196).

A fake broker (read + trade ports) is wired to a REAL ``OrderJournal`` on a tmp
dir. These assert the uncertain-submit resolution and the startup replay contract
at the clerk-service seam — on-disk journal entries + returned results, never
internals:

- Uncertain resolution FOUND / ABSENT / lookup-also-uncertain.
- Startup replay (crash between fsync and submit) resolves to acked.
- Idempotency: resolving twice writes exactly one terminal entry.

A fixed clock (mirroring the S4 consumer tests) makes journaled timestamps
deterministic.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from app.broker.alpaca.clerk import journal as journal_module
from app.broker.alpaca.clerk.clerk import UNCERTAIN_SUBMIT_GRACE_MS, AlpacaClerk
from app.broker.alpaca.clerk.models import ClerkEntryKind
from app.broker.contract.errors import BrokerRequestInvalid, BrokerUnavailable
from app.broker.contract.models import (
    BrokerAccountSnapshot,
    BrokerOrder,
    BrokerOrderLeg,
    BrokerOrderRequest,
)

_FIXED_MS = 1_700_000_000_000


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
        observed_at_ms=_FIXED_MS,
    )


def _accepted_order(client_order_id: str, *, symbol: str = "SPY") -> BrokerOrder:
    return BrokerOrder(
        broker="alpaca",
        order_id="broker-order-1",
        client_order_id=client_order_id,
        symbol=symbol,
        asset_class="us_equity",
        side="buy",
        order_type="market",
        time_in_force="day",
        quantity=1.0,
        filled_quantity=0.0,
        limit_price=None,
        stop_price=None,
        filled_avg_price=None,
        status="accepted",
        submitted_at_ms=_FIXED_MS,
        created_at_ms=_FIXED_MS,
        updated_at_ms=_FIXED_MS,
        filled_at_ms=None,
        canceled_at_ms=None,
        expired_at_ms=None,
        events=[],
        observed_at_ms=_FIXED_MS,
    )


class _FakeBroker:
    """A read+trade port double for the S5 uncertain / lookup paths.

    ``submit`` raises ``submit_error`` when set (the uncertain trigger is a
    ``BrokerUnavailable``). ``get_order_by_client_order_id`` returns a lookup
    result driven by ``lookup_result`` / ``lookup_error`` and records the ids it
    was asked to resolve so tests can assert the resolution path.
    """

    broker_id = "alpaca"

    def __init__(
        self,
        *,
        account: BrokerAccountSnapshot | None = None,
        submit_error: Exception | None = None,
        lookup_result: BrokerOrder | None = None,
        lookup_error: Exception | None = None,
        lookup_absent: bool = False,
    ) -> None:
        self._account = account or _account()
        self._submit_error = submit_error
        self._lookup_result = lookup_result
        self._lookup_error = lookup_error
        self._lookup_absent = lookup_absent
        self.submit_calls: list[tuple[BrokerOrderLeg, str]] = []
        self.lookup_calls: list[str] = []

    async def get_account(self) -> BrokerAccountSnapshot:
        return self._account

    async def submit(
        self, leg: BrokerOrderLeg, *, client_order_id: str
    ) -> BrokerOrder:
        self.submit_calls.append((leg, client_order_id))
        if self._submit_error is not None:
            raise self._submit_error
        return _accepted_order(client_order_id, symbol=leg.symbol)

    async def cancel(self, order_id: str) -> None:  # pragma: no cover - unused
        return None

    async def get_order_by_client_order_id(
        self, client_order_id: str
    ) -> BrokerOrder | None:
        self.lookup_calls.append(client_order_id)
        if self._lookup_error is not None:
            raise self._lookup_error
        if self._lookup_absent:
            return None
        if self._lookup_result is not None:
            return self._lookup_result
        return _accepted_order(client_order_id)


@pytest.fixture(autouse=True)
def _clerk_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the clerk journal at a tmp dir for every test."""
    monkeypatch.setenv("ALPACA_CLERK_DIR", str(tmp_path))
    journal_module.reset_clerk_settings_for_testing()
    yield tmp_path
    journal_module.reset_clerk_settings_for_testing()


def _request(operator: str = "inkant", **leg: Any) -> BrokerOrderRequest:
    base: dict[str, Any] = {"symbol": "SPY", "side": "buy", "quantity": 1}
    base.update(leg)
    return BrokerOrderRequest(operator=operator, legs=[BrokerOrderLeg(**base)])


def _clerk(broker: _FakeBroker) -> AlpacaClerk:
    return AlpacaClerk(read=broker, trade=broker, clock=lambda: _FIXED_MS)


def _kinds(clerk: AlpacaClerk) -> list[str]:
    return [entry.kind for entry in clerk._journal.read_entries()]  # type: ignore[union-attr]


# ── Uncertain resolution: FOUND ──────────────────────────────────────────────


async def test_uncertain_submit_resolves_to_acked_when_order_is_found() -> None:
    # submit raises BrokerUnavailable (response may have been lost), but the
    # by-client-id lookup finds the order → intent_recorded → submit_uncertain →
    # submit_acked; the order is reconciled.
    broker = _FakeBroker(
        submit_error=BrokerUnavailable(
            "Alpaca timed out.", broker="alpaca", detail="timeout"
        )
    )
    clerk = _clerk(broker)

    result = await clerk.submit(_request())

    leg_result = result.results[0]
    assert leg_result.status == "acked"
    assert leg_result.order is not None
    assert leg_result.order.order_id == "broker-order-1"
    # The lookup was asked for the minted client_order_id (== order_ref).
    assert broker.lookup_calls == [leg_result.order_ref]

    assert _kinds(clerk) == [
        ClerkEntryKind.INTENT_RECORDED,
        ClerkEntryKind.SUBMIT_UNCERTAIN,
        ClerkEntryKind.SUBMIT_ACKED,
    ]


# ── Uncertain resolution: initial absence grace ──────────────────────────────


async def test_uncertain_submit_keeps_initial_absence_uncertain_until_recovery() -> None:
    # A 404 immediately after a lost submit response is not terminal: Alpaca's
    # worker may still persist the order. The write path records uncertainty and
    # stops; a later recovery pass may make the terminal absence decision.
    broker = _FakeBroker(
        submit_error=BrokerUnavailable(
            "Alpaca returned a server error.", broker="alpaca", detail="HTTP 503"
        ),
        lookup_absent=True,
    )
    clock = {"now": _FIXED_MS}
    clerk = AlpacaClerk(read=broker, trade=broker, clock=lambda: clock["now"])

    result = await clerk.submit(_request())

    leg_result = result.results[0]
    assert leg_result.status == "uncertain"
    assert leg_result.error is not None
    assert leg_result.order is None

    assert _kinds(clerk) == [
        ClerkEntryKind.INTENT_RECORDED,
        ClerkEntryKind.SUBMIT_UNCERTAIN,
    ]

    # A restart during the bounded grace period must still leave the lost POST
    # uncertain rather than turning a potentially in-flight order into failed.
    await clerk.recover()

    assert _kinds(clerk) == [
        ClerkEntryKind.INTENT_RECORDED,
        ClerkEntryKind.SUBMIT_UNCERTAIN,
    ]

    clock["now"] += UNCERTAIN_SUBMIT_GRACE_MS
    await clerk.recover()

    assert _kinds(clerk) == [
        ClerkEntryKind.INTENT_RECORDED,
        ClerkEntryKind.SUBMIT_UNCERTAIN,
        ClerkEntryKind.SUBMIT_FAILED,
    ]


# ── Lookup-also-uncertain, then finished later ───────────────────────────────


async def test_uncertain_submit_and_uncertain_lookup_stays_uncertain() -> None:
    # submit raises BrokerUnavailable AND the lookup itself raises
    # BrokerUnavailable → the intent stays at submit_uncertain, NO terminal write.
    broker = _FakeBroker(
        submit_error=BrokerUnavailable(
            "Alpaca timed out.", broker="alpaca", detail="timeout"
        ),
        lookup_error=BrokerUnavailable(
            "Alpaca still unreachable.", broker="alpaca", detail="timeout"
        ),
    )
    clerk = _clerk(broker)

    result = await clerk.submit(_request())

    leg_result = result.results[0]
    assert leg_result.status == "uncertain"
    assert _kinds(clerk) == [
        ClerkEntryKind.INTENT_RECORDED,
        ClerkEntryKind.SUBMIT_UNCERTAIN,
    ]


async def test_uncertain_submit_stays_uncertain_for_any_lookup_broker_error() -> None:
    # A failed resolving GET cannot prove that the preceding POST did not land,
    # even if the GET was rejected rather than unavailable.
    broker = _FakeBroker(
        submit_error=BrokerUnavailable(
            "Alpaca timed out.", broker="alpaca", detail="timeout"
        ),
        lookup_error=BrokerRequestInvalid(
            "Alpaca rejected the lookup.", broker="alpaca", detail="HTTP 422"
        ),
    )
    clerk = _clerk(broker)

    result = await clerk.submit(_request())

    assert result.results[0].status == "uncertain"
    assert _kinds(clerk) == [
        ClerkEntryKind.INTENT_RECORDED,
        ClerkEntryKind.SUBMIT_UNCERTAIN,
    ]


async def test_uncertain_leg_stops_later_legs_before_any_broker_submit() -> None:
    broker = _FakeBroker(
        submit_error=BrokerUnavailable(
            "Alpaca timed out.", broker="alpaca", detail="timeout"
        ),
        lookup_error=BrokerUnavailable(
            "Alpaca still unreachable.", broker="alpaca", detail="timeout"
        ),
    )
    clerk = _clerk(broker)
    request = BrokerOrderRequest(
        operator="inkant",
        legs=[
            BrokerOrderLeg(symbol="SPY", side="buy", quantity=1),
            BrokerOrderLeg(symbol="QQQ", side="buy", quantity=1),
        ],
    )

    result = await clerk.submit(request)

    assert [leg.status for leg in result.results] == ["uncertain"]
    assert [leg.symbol for leg, _ in broker.submit_calls] == ["SPY"]


async def test_uncertain_submit_stays_uncertain_when_lookup_returns_mismatched_order() -> None:
    # Boundary validation: the by-client-id lookup returns an order whose
    # client_order_id is NOT the one we queried (a vendor integrity violation).
    # The clerk must NOT fabricate a terminal from corrupt data — it stays at
    # submit_uncertain for a later replay to re-resolve.
    broker = _FakeBroker(
        submit_error=BrokerUnavailable(
            "Alpaca timed out.", broker="alpaca", detail="timeout"
        ),
        lookup_result=_accepted_order("manual/inkant/v1:someone-elses-orderxx"),
    )
    clerk = _clerk(broker)

    result = await clerk.submit(_request())

    assert result.results[0].status == "uncertain"
    assert _kinds(clerk) == [
        ClerkEntryKind.INTENT_RECORDED,
        ClerkEntryKind.SUBMIT_UNCERTAIN,
    ]


async def test_later_resolve_finishes_a_stranded_uncertain_intent() -> None:
    # After a submit left uncertain (lookup unreachable), a later recover() with
    # the lookup now returning the order finishes it to submit_acked.
    broker = _FakeBroker(
        submit_error=BrokerUnavailable(
            "Alpaca timed out.", broker="alpaca", detail="timeout"
        ),
        lookup_error=BrokerUnavailable(
            "Alpaca still unreachable.", broker="alpaca", detail="timeout"
        ),
    )
    clerk = _clerk(broker)
    result = await clerk.submit(_request())
    order_ref = result.results[0].order_ref
    assert _kinds(clerk) == [
        ClerkEntryKind.INTENT_RECORDED,
        ClerkEntryKind.SUBMIT_UNCERTAIN,
    ]

    # The broker is reachable now and the order actually landed.
    broker._lookup_error = None
    broker._lookup_result = _accepted_order(order_ref)

    await clerk.recover()

    assert _kinds(clerk) == [
        ClerkEntryKind.INTENT_RECORDED,
        ClerkEntryKind.SUBMIT_UNCERTAIN,
        ClerkEntryKind.SUBMIT_ACKED,
    ]


# ── Startup replay / crash between fsync and submit (headline) ────────────────


async def test_startup_replay_resolves_intent_left_uncertain_by_crash() -> None:
    # Simulate a crash between the fsync'd intent and the broker response: submit
    # raises AND the lookup is unreachable, so the first clerk leaves the intent
    # at submit_uncertain. A NEW clerk on the SAME journal dir then recovers with
    # a reachable lookup returning the order → the intent reconciles to
    # submit_acked. This is THE headline crash-safety acceptance test.
    crashed_broker = _FakeBroker(
        submit_error=BrokerUnavailable(
            "Alpaca timed out.", broker="alpaca", detail="timeout"
        ),
        lookup_error=BrokerUnavailable(
            "Alpaca still unreachable.", broker="alpaca", detail="timeout"
        ),
    )
    crashed_clerk = _clerk(crashed_broker)
    result = await crashed_clerk.submit(_request())
    order_ref = result.results[0].order_ref

    # A fresh process boots on the same account journal — the order DID land.
    recovered_broker = _FakeBroker(lookup_result=_accepted_order(order_ref))
    recovered_clerk = _clerk(recovered_broker)

    await recovered_clerk.recover()

    assert recovered_broker.lookup_calls == [order_ref]
    assert _kinds(recovered_clerk) == [
        ClerkEntryKind.INTENT_RECORDED,
        ClerkEntryKind.SUBMIT_UNCERTAIN,
        ClerkEntryKind.SUBMIT_ACKED,
    ]
    acked = recovered_clerk._journal.read_entries()[-1]  # type: ignore[union-attr]
    assert acked.order is not None
    assert acked.order.order_id == "broker-order-1"


async def test_startup_replay_resolves_intent_recorded_with_no_uncertain() -> None:
    # A crash BEFORE even the submit_uncertain line (right after the intent's
    # fsync) leaves only intent_recorded. Recovery must still resolve it.
    from app.broker.alpaca.clerk.clerk import _LegIdentity
    from app.broker.alpaca.clerk.models import OrderJournalEntry
    from app.engine.live.order_identity import (
        build_manual_order_namespace,
        build_order_ref,
        mint_intent_id,
    )

    # Seed a lone intent_recorded line by hand (simulating the crash window).
    warm_broker = _FakeBroker(lookup_absent=True)
    clerk = _clerk(warm_broker)
    account_id, journal = await clerk._ensure_journal()  # type: ignore[attr-defined]
    intent_id = mint_intent_id()
    order_ref = build_order_ref(build_manual_order_namespace("inkant"), intent_id)
    identity = _LegIdentity(
        account_id,
        "inkant",
        intent_id,
        order_ref,
        BrokerOrderLeg(symbol="SPY", side="buy", quantity=1),
        lambda: _FIXED_MS,
    )
    journal.append(identity.entry(ClerkEntryKind.INTENT_RECORDED))
    assert _kinds(clerk) == [ClerkEntryKind.INTENT_RECORDED]
    assert isinstance(journal.read_entries()[0], OrderJournalEntry)

    # The order was NOT at the broker → resolves to submit_failed.
    await clerk.recover()

    assert warm_broker.lookup_calls == [order_ref]
    assert _kinds(clerk) == [
        ClerkEntryKind.INTENT_RECORDED,
        ClerkEntryKind.SUBMIT_FAILED,
    ]


async def test_recover_is_noop_on_a_fresh_install_with_no_journal() -> None:
    # A brand-new account (journal resolves but is empty) recovers nothing and
    # never calls the lookup.
    broker = _FakeBroker()
    clerk = _clerk(broker)

    await clerk.recover()

    assert broker.lookup_calls == []
    assert _kinds(clerk) == []


async def test_startup_recovery_failure_keeps_submit_clerk_disabled() -> None:
    """A failed recovery must fail closed before lifespan installs the clerk."""
    from app.main import _recover_alpaca_clerk_or_fail_closed

    class _RecoveryFailure:
        async def recover(self) -> None:
            raise RuntimeError("journal unavailable")

    assert not await _recover_alpaca_clerk_or_fail_closed(_RecoveryFailure())


async def test_startup_recovery_timeout_keeps_submit_clerk_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Startup proceeds fail-closed when replay exceeds its overall budget."""
    from app import main as app_main

    recovery_cancelled = asyncio.Event()

    class _RecoveryNeverCompletes:
        async def recover(self) -> None:
            try:
                await asyncio.Event().wait()
            finally:
                recovery_cancelled.set()

    monkeypatch.setattr(app_main, "_ALPACA_RECOVERY_TIMEOUT_S", 0.01)

    assert not await app_main._recover_alpaca_clerk_or_fail_closed(
        _RecoveryNeverCompletes()
    )
    assert recovery_cancelled.is_set()


# ── Idempotency ──────────────────────────────────────────────────────────────


async def test_recover_twice_writes_exactly_one_terminal_entry() -> None:
    # Running recover() twice must not double-write a terminal entry: the second
    # pass sees the terminal line and is a NO-OP.
    broker = _FakeBroker(
        submit_error=BrokerUnavailable(
            "Alpaca timed out.", broker="alpaca", detail="timeout"
        ),
        lookup_error=BrokerUnavailable(
            "unreachable", broker="alpaca", detail="timeout"
        ),
    )
    clerk = _clerk(broker)
    result = await clerk.submit(_request())
    order_ref = result.results[0].order_ref

    # Broker reachable now; the order landed.
    broker._lookup_error = None
    broker._lookup_result = _accepted_order(order_ref)

    await clerk.recover()
    kinds_after_first = _kinds(clerk)
    await clerk.recover()
    kinds_after_second = _kinds(clerk)

    assert kinds_after_first == kinds_after_second
    assert kinds_after_second.count(ClerkEntryKind.SUBMIT_ACKED) == 1
    assert kinds_after_second == [
        ClerkEntryKind.INTENT_RECORDED,
        ClerkEntryKind.SUBMIT_UNCERTAIN,
        ClerkEntryKind.SUBMIT_ACKED,
    ]


async def test_already_terminal_intent_is_noop_and_returns_existing_outcome() -> None:
    # A plain acked submit is already terminal; a subsequent recover() must not
    # re-resolve it (no new lookup, no new terminal line).
    broker = _FakeBroker()
    clerk = _clerk(broker)
    await clerk.submit(_request())
    assert _kinds(clerk) == [
        ClerkEntryKind.INTENT_RECORDED,
        ClerkEntryKind.SUBMIT_ACKED,
    ]

    await clerk.recover()

    assert broker.lookup_calls == []
    assert _kinds(clerk) == [
        ClerkEntryKind.INTENT_RECORDED,
        ClerkEntryKind.SUBMIT_ACKED,
    ]
