"""Clerk-service seam tests (Alpaca phase 2, S1).

A fake broker (implementing the read + trade ports) is wired to a REAL
``OrderJournal`` on a tmp dir. These assert the Clerk's contract, not any
vendor: intent is fsync'd BEFORE the broker call, intake is serialized,
acked/failed journaling, identity mapping, and fail-closed over the order_ref
cap.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.broker.alpaca.clerk import journal as journal_module
from app.broker.alpaca.clerk.clerk import AlpacaClerk
from app.broker.alpaca.clerk.models import (
    ClerkEntryKind,
    OrderLegError,
    OrderLegResult,
)
from app.broker.contract.errors import BrokerRequestInvalid, BrokerUnavailable
from app.broker.contract.models import (
    BrokerAccountSnapshot,
    BrokerOrder,
    BrokerOrderLeg,
    BrokerOrderRequest,
)
from app.engine.live.order_identity import (
    build_manual_order_namespace,
    parse_order_ref,
)


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
        observed_at_ms=1_700_000_000_000,
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
        submitted_at_ms=1_700_000_000_000,
        created_at_ms=1_700_000_000_000,
        updated_at_ms=1_700_000_000_000,
        filled_at_ms=None,
        canceled_at_ms=None,
        expired_at_ms=None,
        events=[],
        observed_at_ms=1_700_000_000_000,
    )


class _FakeBroker:
    """A read+trade port double whose ``submit`` inspects the live journal."""

    broker_id = "alpaca"

    def __init__(
        self,
        *,
        account: BrokerAccountSnapshot | None = None,
        error: Exception | None = None,
        lookup_result: BrokerOrder | None = None,
        lookup_error: Exception | None = None,
        lookup_absent: bool = False,
        on_submit: Any = None,
        cancel_error: Exception | None = None,
        on_cancel: Any = None,
    ) -> None:
        self._account = account or _account()
        self._error = error
        self._lookup_result = lookup_result
        self._lookup_error = lookup_error
        self._lookup_absent = lookup_absent
        self._on_submit = on_submit
        self._cancel_error = cancel_error
        self._on_cancel = on_cancel
        self.submit_calls: list[tuple[BrokerOrderLeg, str]] = []
        self.cancel_calls: list[str] = []
        self.lookup_calls: list[str] = []

    async def get_account(self) -> BrokerAccountSnapshot:
        return self._account

    async def submit(
        self, leg: BrokerOrderLeg, *, client_order_id: str
    ) -> BrokerOrder:
        self.submit_calls.append((leg, client_order_id))
        if self._on_submit is not None:
            await self._on_submit(leg, client_order_id)
        if self._error is not None:
            raise self._error
        return _accepted_order(client_order_id, symbol=leg.symbol)

    async def cancel(self, order_id: str) -> None:
        self.cancel_calls.append(order_id)
        if self._on_cancel is not None:
            await self._on_cancel(order_id)
        if self._cancel_error is not None:
            raise self._cancel_error
        return None

    async def get_order_by_client_order_id(
        self, client_order_id: str
    ) -> BrokerOrder | None:
        self.lookup_calls.append(client_order_id)
        if self._lookup_error is not None:
            raise self._lookup_error
        if self._lookup_absent:
            return None
        return self._lookup_result or _accepted_order(client_order_id)


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


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "acked"},
        {
            "status": "acked",
            "order": _accepted_order("manual/inkant/v1:abc"),
            "error": OrderLegError(message="contradiction"),
        },
        {
            "status": "failed",
            "order": _accepted_order("manual/inkant/v1:abc"),
        },
        {"status": "uncertain"},
    ],
)
def test_order_leg_result_rejects_status_payload_contradictions(
    payload: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        OrderLegResult(
            order_ref="manual/inkant/v1:abc",
            intent_id="abc",
            **payload,
        )


async def test_intent_is_fsynced_before_broker_submit() -> None:
    seen_kinds: list[str] = []

    async def _inspect(leg: BrokerOrderLeg, client_order_id: str) -> None:
        # By the time submit runs, the intent must already be in the journal.
        entries = clerk._journal.read_entries()  # type: ignore[union-attr]
        seen_kinds.extend(entry.kind for entry in entries)

    broker = _FakeBroker(on_submit=_inspect)
    clerk = AlpacaClerk(read=broker, trade=broker)

    await clerk.submit(_request())

    assert ClerkEntryKind.INTENT_RECORDED in seen_kinds


async def test_identity_is_client_order_id_equals_order_ref() -> None:
    broker = _FakeBroker()
    clerk = AlpacaClerk(read=broker, trade=broker)

    result = await clerk.submit(_request(operator="inkant"))

    leg_result = result.results[0]
    assert leg_result.status == "acked"
    order_ref = leg_result.order_ref
    # client_order_id passed to the broker == order_ref.
    _, submitted_coid = broker.submit_calls[0]
    assert submitted_coid == order_ref
    # order_ref == manual/{operator}/v1:{intent_id}.
    namespace, intent_id = parse_order_ref(order_ref)
    assert namespace == build_manual_order_namespace("inkant")
    assert intent_id == leg_result.intent_id
    assert order_ref == f"{namespace}:{intent_id}"
    # Alpaca echoes the client_order_id back on the accepted order.
    assert leg_result.order is not None
    assert leg_result.order.client_order_id == order_ref


async def test_submit_acked_is_journaled_with_order() -> None:
    broker = _FakeBroker()
    clerk = AlpacaClerk(read=broker, trade=broker)

    await clerk.submit(_request())

    entries = clerk._journal.read_entries()  # type: ignore[union-attr]
    kinds = [entry.kind for entry in entries]
    assert kinds == [ClerkEntryKind.INTENT_RECORDED, ClerkEntryKind.SUBMIT_ACKED]
    acked = entries[-1]
    assert acked.order is not None
    assert acked.order.status == "accepted"


async def test_submit_failed_is_journaled_and_returned() -> None:
    broker = _FakeBroker(
        error=BrokerRequestInvalid(
            "Alpaca rejected the order.", broker="alpaca", detail="HTTP 422"
        )
    )
    clerk = AlpacaClerk(read=broker, trade=broker)

    result = await clerk.submit(_request())

    leg_result = result.results[0]
    assert leg_result.status == "failed"
    assert leg_result.error is not None
    assert leg_result.error.message == "Alpaca rejected the order."
    assert leg_result.error.why == "HTTP 422"

    entries = clerk._journal.read_entries()  # type: ignore[union-attr]
    kinds = [entry.kind for entry in entries]
    assert kinds == [ClerkEntryKind.INTENT_RECORDED, ClerkEntryKind.SUBMIT_FAILED]
    assert entries[-1].error_message == "Alpaca rejected the order."


async def test_unavailable_submit_is_resolved_by_client_order_id() -> None:
    broker = _FakeBroker(
        error=BrokerUnavailable("Alpaca timed out.", broker="alpaca", detail="timeout")
    )
    clerk = AlpacaClerk(read=broker, trade=broker)

    result = await clerk.submit(_request())

    [leg_result] = result.results
    assert leg_result.status == "acked"
    assert broker.lookup_calls == [leg_result.order_ref]
    entries = clerk._journal.read_entries()  # type: ignore[union-attr]
    assert [entry.kind for entry in entries] == [
        ClerkEntryKind.INTENT_RECORDED,
        ClerkEntryKind.SUBMIT_UNCERTAIN,
        ClerkEntryKind.SUBMIT_ACKED,
    ]


async def test_unavailable_submit_stays_uncertain_when_lookup_is_unavailable() -> None:
    broker = _FakeBroker(
        error=BrokerUnavailable("Alpaca timed out.", broker="alpaca", detail="timeout"),
        lookup_error=BrokerUnavailable(
            "Alpaca is still unavailable.", broker="alpaca", detail="timeout"
        ),
    )
    clerk = AlpacaClerk(read=broker, trade=broker)

    result = await clerk.submit(_request())

    [leg_result] = result.results
    assert leg_result.status == "uncertain"
    assert leg_result.error is not None
    entries = clerk._journal.read_entries()  # type: ignore[union-attr]
    assert [entry.kind for entry in entries] == [
        ClerkEntryKind.INTENT_RECORDED,
        ClerkEntryKind.SUBMIT_UNCERTAIN,
    ]


async def test_unavailable_submit_keeps_initial_absent_lookup_uncertain() -> None:
    broker = _FakeBroker(
        error=BrokerUnavailable("Alpaca timed out.", broker="alpaca", detail="timeout"),
        lookup_absent=True,
    )
    clerk = AlpacaClerk(read=broker, trade=broker)

    result = await clerk.submit(_request())

    assert result.results[0].status == "uncertain"
    entries = clerk._journal.read_entries()  # type: ignore[union-attr]
    assert [entry.kind for entry in entries] == [
        ClerkEntryKind.INTENT_RECORDED,
        ClerkEntryKind.SUBMIT_UNCERTAIN,
    ]


async def test_order_ref_over_cap_fails_closed_without_broker_call() -> None:
    # An operator id long enough to overflow the order_ref cap must fail the leg
    # BEFORE any broker call, never truncate the order_ref.
    long_operator = "x" * 40
    broker = _FakeBroker()
    clerk = AlpacaClerk(read=broker, trade=broker)

    result = await clerk.submit(_request(operator=long_operator))

    leg_result = result.results[0]
    assert leg_result.status == "failed"
    assert leg_result.error is not None
    assert broker.submit_calls == []


async def test_bad_operator_fails_typed_without_journal_or_broker_call() -> None:
    # Defense in depth: even if a bad operator (a space is not a valid manual
    # namespace segment) bypasses the endpoint's 422 boundary and reaches the
    # clerk directly, the leg fails typed — with NO journal entry written and
    # NO broker submit call. The router boundary is the primary guard; this is
    # the last line so a bad value can never surface as a raw 500.
    broker = _FakeBroker()
    clerk = AlpacaClerk(read=broker, trade=broker)

    # ``operator`` now carries a boundary pattern, so a normal ``BrokerOrderRequest``
    # can't be built with a space — that's the endpoint guard. Bypass validation
    # with ``model_construct`` to prove the clerk *also* fails closed if a bad
    # value ever reaches it directly.
    bad_request = BrokerOrderRequest.model_construct(
        operator="bad operator",
        legs=[BrokerOrderLeg(symbol="SPY", side="buy", quantity=1)],
    )
    result = await clerk.submit(bad_request)

    leg_result = result.results[0]
    assert leg_result.status == "failed"
    assert leg_result.error is not None
    assert leg_result.order is None
    # No broker submit call.
    assert broker.submit_calls == []
    # No journal entry — not even an intent_recorded — for an un-buildable id.
    entries = clerk._journal.read_entries()  # type: ignore[union-attr]
    assert entries == []


async def test_intake_lock_serializes_concurrent_submits() -> None:
    # Two concurrent submits must not interleave: the second broker call must
    # not begin until the first has completed.
    order_log: list[str] = []
    release = asyncio.Event()

    async def _slow_first(leg: BrokerOrderLeg, client_order_id: str) -> None:
        order_log.append(f"enter:{leg.symbol}")
        if leg.symbol == "AAA":
            await release.wait()
        order_log.append(f"exit:{leg.symbol}")

    broker = _FakeBroker(on_submit=_slow_first)
    clerk = AlpacaClerk(read=broker, trade=broker)

    first = asyncio.create_task(clerk.submit(_request(symbol="AAA")))
    await asyncio.sleep(0.01)
    second = asyncio.create_task(clerk.submit(_request(symbol="BBB")))
    await asyncio.sleep(0.01)

    # While the first holds the lock, the second must not have entered submit.
    assert order_log == ["enter:AAA"]
    release.set()
    await asyncio.gather(first, second)

    assert order_log == ["enter:AAA", "exit:AAA", "enter:BBB", "exit:BBB"]


async def test_journal_path_is_account_scoped_and_separate() -> None:
    broker = _FakeBroker(account=_account("PA-9999"))
    clerk = AlpacaClerk(read=broker, trade=broker)

    await clerk.submit(_request())

    journal_dir = clerk._journal.account_dir  # type: ignore[union-attr]
    assert journal_dir.name == "PA-9999"
    assert journal_dir.parent.name == "alpaca"
    assert journal_dir.parent.parent.name == "accounts"


# ── S3 cancel path ──────────────────────────────────────────────────────────


async def test_cancel_recorded_is_journaled_before_broker_cancel() -> None:
    # "No journal → no cancel": the cancel_recorded entry must be in the ledger
    # by the time the broker's cancel runs.
    seen_kinds: list[str] = []

    async def _inspect(order_id: str) -> None:
        entries = clerk._journal.read_entries()  # type: ignore[union-attr]
        seen_kinds.extend(entry.kind for entry in entries)

    broker = _FakeBroker(on_cancel=_inspect)
    clerk = AlpacaClerk(read=broker, trade=broker)

    await clerk.cancel("broker-order-1")

    assert ClerkEntryKind.CANCEL_RECORDED in seen_kinds
    assert broker.cancel_calls == ["broker-order-1"]


async def test_cancel_of_owned_order_resolves_intent_and_acks() -> None:
    # A prior submit_acked binds the broker order_id to the minted order_ref.
    # The cancel must recover that identity (owned=True) and journal cancel_acked.
    broker = _FakeBroker()
    clerk = AlpacaClerk(read=broker, trade=broker)

    submit_result = await clerk.submit(_request())
    order_ref = submit_result.results[0].order_ref

    result = await clerk.cancel("broker-order-1")

    assert result.status == "acked"
    assert result.owned is True
    assert result.order_id == "broker-order-1"
    # The originating intent is recoverable from the cancel result.
    assert result.order_ref == order_ref

    entries = clerk._journal.read_entries()  # type: ignore[union-attr]
    cancel_entries = [
        entry
        for entry in entries
        if entry.kind
        in (ClerkEntryKind.CANCEL_RECORDED, ClerkEntryKind.CANCEL_ACKED)
    ]
    assert [entry.kind for entry in cancel_entries] == [
        ClerkEntryKind.CANCEL_RECORDED,
        ClerkEntryKind.CANCEL_ACKED,
    ]
    # The owning identity is copied onto both cancel lines — never re-derived.
    for entry in cancel_entries:
        assert entry.owned is True
        assert entry.order_ref == order_ref
        assert entry.broker_order_id == "broker-order-1"
        assert entry.leg is not None
        assert entry.leg.symbol == "SPY"


async def test_cancel_of_unowned_order_still_cancels_journaled_honestly() -> None:
    # A foreign order (never submitted through this Clerk) has no submit_acked
    # entry. Canceling it reduces exposure — the safe direction — so it succeeds,
    # but the ledger records owned=False with NO fabricated intent identity.
    broker = _FakeBroker()
    clerk = AlpacaClerk(read=broker, trade=broker)
    # Warm the journal (resolve account id) without an owning order.
    await clerk.submit(_request(symbol="AAA"))

    result = await clerk.cancel("foreign-order-999")

    assert result.status == "acked"
    assert result.owned is False
    assert result.order_ref is None
    assert broker.cancel_calls == ["foreign-order-999"]

    entries = clerk._journal.read_entries()  # type: ignore[union-attr]
    acked = next(
        entry for entry in entries if entry.kind is ClerkEntryKind.CANCEL_ACKED
    )
    assert acked.owned is False
    assert acked.broker_order_id == "foreign-order-999"
    # No fabricated attribution.
    assert acked.order_ref == ""
    assert acked.intent_id == ""
    assert acked.operator == ""
    assert acked.leg is None


async def test_cancel_of_non_cancelable_order_fails_typed_not_500() -> None:
    broker = _FakeBroker(
        cancel_error=BrokerRequestInvalid(
            "Alpaca rejected the request as invalid: order is not cancelable",
            broker="alpaca",
            detail="HTTP 422",
        )
    )
    clerk = AlpacaClerk(read=broker, trade=broker)
    await clerk.submit(_request())

    result = await clerk.cancel("broker-order-1")

    assert result.status == "failed"
    assert result.error is not None
    assert "not cancelable" in result.error.message
    assert result.error.why == "HTTP 422"

    entries = clerk._journal.read_entries()  # type: ignore[union-attr]
    failed = entries[-1]
    assert failed.kind is ClerkEntryKind.CANCEL_FAILED
    assert failed.error_message is not None
    assert "not cancelable" in failed.error_message


async def test_cancel_does_not_consult_the_submit_path() -> None:
    # Hold-independence guard (the S6 exposure hold lands on submit only).
    # Cancel must reach the broker via its OWN path — never through submit or its
    # per-leg gating. We assert the cancel neither invokes submit nor mints new
    # order identity: no submit call, and no INTENT_RECORDED entry appears from
    # the cancel. When S6 adds a hold to submit, this order-reducing path stays
    # reachable. (The full "cancel succeeds while a hold is active" test belongs
    # to S6, once the hold exists — we do not fabricate one here.)
    broker = _FakeBroker()
    clerk = AlpacaClerk(read=broker, trade=broker)

    # Cancel with a cold journal (no prior submit): if cancel were routed through
    # submit it would mint identity / call submit. It must do neither.
    await clerk.cancel("some-order")

    assert broker.submit_calls == []
    entries = clerk._journal.read_entries()  # type: ignore[union-attr]
    kinds = [entry.kind for entry in entries]
    assert ClerkEntryKind.INTENT_RECORDED not in kinds
    assert ClerkEntryKind.SUBMIT_ACKED not in kinds
    assert kinds == [ClerkEntryKind.CANCEL_RECORDED, ClerkEntryKind.CANCEL_ACKED]
