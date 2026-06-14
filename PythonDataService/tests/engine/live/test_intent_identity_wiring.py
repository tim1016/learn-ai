"""Phase 5A / VCR-0002 — intent identity foundation tests.

The PRD's tracer-bullet contract:
``intent_id ↔ order_ref ↔ attempted broker order``. An ``intent_id`` is
minted only after sizing resolution proves the engine will submit
(``delta != 0``); a skip never mints one. Every submitted order stamps a
deterministic ``orderRef`` and emits ordered WAL events.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.broker.ibkr.models import IbkrOrderAck, IbkrOrderSpec
from app.engine.execution.order_sizer import FixedShares, OrderSizer
from app.engine.live.intent_events import IntentEventType
from app.engine.live.intent_wal import IntentWal
from app.engine.live.live_portfolio import LivePortfolio
from app.engine.live.order_identity import (
    build_bot_order_namespace,
    parse_order_ref,
)
from tests.engine.live.fixtures.fake_broker import FakeBroker


def _bar_time() -> datetime:
    return datetime(2026, 5, 4, 14, 30, tzinfo=UTC)


def _portfolio_with_intent_wal(tmp_path: Path, *, namespace: str = "test-instance") -> LivePortfolio:
    broker = FakeBroker()
    wal = IntentWal(tmp_path / "intent_events.jsonl")
    portfolio = LivePortfolio(
        broker,
        intent_wal=wal,
        bot_order_namespace=build_bot_order_namespace(namespace),
    )
    portfolio.order_sizer = OrderSizer(FixedShares(value=10))
    portfolio.update_reference_price("SPY", Decimal("500"))
    return portfolio


def test_ibkr_order_spec_accepts_order_ref() -> None:
    """``IbkrOrderSpec`` gains an ``order_ref`` field so the deterministic
    namespace+intent_id token can ride to the broker."""
    spec = IbkrOrderSpec(
        symbol="SPY",
        sec_type="STK",
        action="BUY",
        quantity=10,
        order_type="MKT",
        time_in_force="DAY",
        confirm_paper=True,
        client_order_id="live-1",
        order_ref="learn-ai/test-instance/v1:AAAAAAAAAAAAAAAAAAAAAA",
    )
    assert spec.order_ref == "learn-ai/test-instance/v1:AAAAAAAAAAAAAAAAAAAAAA"


def test_set_holdings_with_zero_delta_does_not_mint_intent_id(tmp_path: Path) -> None:
    """A ``set_holdings`` call that resolves to ``target_qty == current_qty``
    is a no-op: nothing to submit, nothing to identify. The WAL stays empty,
    and the sizing card still records the resolution (the existing in-memory
    list is unchanged)."""
    portfolio = _portfolio_with_intent_wal(tmp_path)
    portfolio.get_position("SPY").quantity = 10  # already at FixedShares(10)

    order = portfolio.set_holdings("SPY", Decimal("1.0"), _bar_time())

    assert order is None
    assert portfolio.last_minted_intent_id() is None
    # WAL still empty — no PENDING_INTENT minted for a skip.
    wal_path = tmp_path / "intent_events.jsonl"
    assert not wal_path.exists() or wal_path.read_text() == ""
    # Sizing card audit row still appended (Phase 5A keeps the in-memory
    # list per PRD §5A; Phase 8 promotes to a WAL fold).
    assert len(portfolio.sizing_resolutions) == 1


def test_set_holdings_with_non_zero_delta_mints_intent_id(tmp_path: Path) -> None:
    """When ``delta != 0`` the engine is committing to a submit. ``intent_id``
    is minted now (not earlier) so a skip never reserves an identity."""
    portfolio = _portfolio_with_intent_wal(tmp_path)

    order = portfolio.set_holdings("SPY", Decimal("1.0"), _bar_time())

    assert order is not None
    intent_id = portfolio.last_minted_intent_id()
    assert intent_id is not None
    assert len(intent_id) == 22  # base64url uuid4 → 22 chars
    # The order carries the intent so ``submit_pending_orders`` can look it up.
    assert portfolio.intent_id_for_order(order.order_id) == intent_id


def test_submit_pending_orders_stamps_order_ref_on_spec(tmp_path: Path) -> None:
    """The broker sees ``IbkrOrderSpec.order_ref`` filled with the
    deterministic ``{namespace}:{intent_id}`` token. ``FakeBroker.orders``
    is the recorded boundary."""
    import asyncio

    portfolio = _portfolio_with_intent_wal(tmp_path)
    portfolio.set_holdings("SPY", Decimal("1.0"), _bar_time())
    intent_id = portfolio.last_minted_intent_id()
    assert intent_id is not None

    asyncio.run(portfolio.submit_pending_orders())

    assert len(portfolio.broker.orders) == 1
    spec = portfolio.broker.orders[0]
    assert spec.order_ref is not None
    namespace, parsed_intent = parse_order_ref(spec.order_ref)
    assert namespace == build_bot_order_namespace("test-instance")
    assert parsed_intent == intent_id


def test_submit_pending_orders_writes_pending_intent_before_submit(tmp_path: Path) -> None:
    """The WAL invariant: ``PENDING_INTENT`` must be appended BEFORE
    ``broker.place_order`` is called. The broker fixture verifies the WAL
    file already contains the event when the submit lands."""
    import asyncio

    portfolio = _portfolio_with_intent_wal(tmp_path)
    portfolio.set_holdings("SPY", Decimal("1.0"), _bar_time())
    intent_id = portfolio.last_minted_intent_id()

    wal_at_submit: list[str] = []
    original_place = portfolio.broker.place_order

    async def _capture(spec: IbkrOrderSpec, **kwargs: object) -> IbkrOrderAck:
        wal_at_submit.append((tmp_path / "intent_events.jsonl").read_text(encoding="utf-8"))
        return await original_place(spec, **kwargs)

    portfolio.broker.place_order = _capture  # type: ignore[assignment]
    asyncio.run(portfolio.submit_pending_orders())

    assert wal_at_submit, "broker should have been called"
    pending_line = wal_at_submit[0]
    assert IntentEventType.PENDING_INTENT.value in pending_line
    assert intent_id in pending_line


def test_submit_pending_orders_writes_submitted_after_success(tmp_path: Path) -> None:
    """On success, ``SUBMITTED`` is appended with the broker-side ``order_id``.
    The WAL pairs ``PENDING_INTENT → SUBMITTED`` with the same ``intent_id``."""
    import asyncio
    import json as _json

    portfolio = _portfolio_with_intent_wal(tmp_path)
    portfolio.set_holdings("SPY", Decimal("1.0"), _bar_time())

    asyncio.run(portfolio.submit_pending_orders())

    wal_text = (tmp_path / "intent_events.jsonl").read_text(encoding="utf-8")
    events = [_json.loads(line) for line in wal_text.splitlines() if line.strip()]
    assert [e["event_type"] for e in events] == [
        IntentEventType.PENDING_INTENT.value,
        IntentEventType.SUBMITTED.value,
    ]
    assert events[0]["intent_id"] == events[1]["intent_id"]
    assert events[1]["order_id"] is not None


def test_submit_pending_orders_writes_ack_failed_uncertain_on_exception(tmp_path: Path) -> None:
    """If ``broker.place_order`` raises, the submit path is uncertain — the
    order may or may not have landed at the broker. ``ACK_FAILED_UNCERTAIN``
    is the only honest event."""
    import asyncio
    import json as _json

    portfolio = _portfolio_with_intent_wal(tmp_path)
    portfolio.set_holdings("SPY", Decimal("1.0"), _bar_time())

    async def _boom(spec: IbkrOrderSpec, **kwargs: object) -> IbkrOrderAck:
        raise RuntimeError("network reset by peer")

    portfolio.broker.place_order = _boom  # type: ignore[assignment]
    with pytest.raises(RuntimeError, match="network reset"):
        asyncio.run(portfolio.submit_pending_orders())

    wal_text = (tmp_path / "intent_events.jsonl").read_text(encoding="utf-8")
    events = [_json.loads(line) for line in wal_text.splitlines() if line.strip()]
    types = [e["event_type"] for e in events]
    assert IntentEventType.PENDING_INTENT.value in types
    assert IntentEventType.ACK_FAILED_UNCERTAIN.value in types
    uncertain = next(e for e in events if e["event_type"] == IntentEventType.ACK_FAILED_UNCERTAIN.value)
    assert "network reset" in (uncertain.get("reason") or "")


def test_legacy_portfolio_without_wal_keeps_working(tmp_path: Path) -> None:
    """Existing callers that don't pass an ``IntentWal`` keep their pre-Phase-5A
    behaviour — no intent_id minting, no WAL writes."""
    import asyncio

    broker = FakeBroker()
    portfolio = LivePortfolio(broker)
    portfolio.order_sizer = OrderSizer(FixedShares(value=10))
    portfolio.update_reference_price("SPY", Decimal("500"))
    portfolio.set_holdings("SPY", Decimal("1.0"), _bar_time())

    assert portfolio.last_minted_intent_id() is None
    asyncio.run(portfolio.submit_pending_orders())  # No WAL file expected
    assert not (tmp_path / "intent_events.jsonl").exists()
