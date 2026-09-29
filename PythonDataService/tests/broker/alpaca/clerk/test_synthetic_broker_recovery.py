"""A stopped Dry Run's recovery EXIT fills at the live IBKR quote read when it is sent (#2563).

A recovery EXIT (the operator's safe flatten, a sweep re-drive) has no
strategy decision bar. Priced from the Dry Run's own newest bar, a crash at
15:59 left its shares unsellable forever the next day (review A3), and a sale
inside the session could fill at an hours-old price. It now fills at the bid
(a sale) or the ask (a purchase) of the quote IBKR shows right now, retained
first as its own evidence bar, so the fill is folded as simulated execution
with the price it came from.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from app.broker.alpaca.clerk.sqlite.broker_port_guard import GuardedBrokerTradePort
from app.broker.alpaca.clerk.sqlite.intake_fence import ReentrantAsyncLock
from app.broker.alpaca.clerk.synthetic_broker import SyntheticBroker
from app.broker.contract.models import BrokerOrderLeg
from app.marketdata.feed import MarketDataBar
from app.schemas.market_liveness import TopOfBookQuote
from app.services.session_authority import et_minute_of_day_ms
from app.services.source_bar_ledger import RECOVERY_QUOTE_PROVIDER, RUN_END_CLOSE_PROVIDER, SourceBarLedger
from tests.broker.alpaca.clerk.sqlite.conftest import NOON, _TestClock

ACCOUNT = "sim:ema-1"
NEXT_SESSION_NOON = et_minute_of_day_ms(date(2026, 9, 9), 12 * 60)
OVERNIGHT = et_minute_of_day_ms(date(2026, 9, 9), 2 * 60)


def _retain(ledger: SourceBarLedger, *, end_ms: int, close: str) -> None:
    price = Decimal(close)
    ledger.append(MarketDataBar(
        symbol="SPY", start_ms=end_ms - 60_000, end_ms=end_ms, open=price, high=price, low=price, close=price,
        volume=1, fetched_at_ms=end_ms, feed_id="ibkr", session_phase="RTH",
    ), run_id="run-a")


def _quote(*, observed_at_ms: int, bid: float = 601.25, ask: float = 601.30) -> TopOfBookQuote:
    return TopOfBookQuote(symbol="SPY", bid=bid, ask=ask, source="ibkr.market_data.status", observed_at_ms=observed_at_ms)


def _broker(ledger: SourceBarLedger, *, now_ms: int, quote: TopOfBookQuote | None) -> SyntheticBroker:
    return SyntheticBroker(account_id=ACCOUNT, source_bars=ledger, clock=_TestClock(now_ms),
                           quote_source=lambda _symbol, _now: quote)


async def test_a_recovery_sale_fills_at_the_live_bid_retained_as_its_own_evidence(tmp_path: Path) -> None:
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id=ACCOUNT)
    # The bot's last bar was hours ago; the sale never fills at it.
    _retain(ledger, end_ms=NOON, close="600.00")
    now = NOON + 3 * 60 * 60_000
    broker = _broker(ledger, now_ms=now, quote=_quote(observed_at_ms=now - 2_000))

    assert broker.recovery_price_available("SPY") is True
    assert broker.bind_latest_recovery_bar("bot:ema:recovery", symbol="SPY", side="sell") is True
    order = await broker.submit(BrokerOrderLeg(symbol="SPY", side="sell", quantity=1), client_order_id="bot:ema:recovery")

    assert (order.status, order.filled_avg_price, order.filled_at_ms) == ("filled", 601.25, now - 2_000)
    assert [event.execution_id for event in order.events] == ["sim-execution:bot:ema:recovery"]
    evidence = ledger.latest_for_symbol("SPY", provider=RECOVERY_QUOTE_PROVIDER)
    assert evidence is not None and (evidence.close, evidence.end_ms, evidence.run_id) == (Decimal("601.25"), now - 2_000, None)
    # The whole execution happened inside ``submit``: it is the evidence.
    assert broker.submission_response_is_authoritative_evidence is True


async def test_a_recovery_purchase_fills_at_the_live_ask(tmp_path: Path) -> None:
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id=ACCOUNT)
    broker = _broker(ledger, now_ms=NOON, quote=_quote(observed_at_ms=NOON - 1_000))

    assert broker.bind_latest_recovery_bar("bot:ema:cover", symbol="SPY", side="buy") is True
    order = await broker.submit(BrokerOrderLeg(symbol="SPY", side="buy", quantity=1), client_order_id="bot:ema:cover")

    assert (order.status, order.filled_avg_price) == ("filled", 601.30)


async def test_a_dry_run_stopped_yesterday_sells_at_todays_quote(tmp_path: Path) -> None:
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id=ACCOUNT)
    _retain(ledger, end_ms=NOON, close="600.00")
    broker = _broker(ledger, now_ms=NEXT_SESSION_NOON, quote=_quote(observed_at_ms=NEXT_SESSION_NOON - 500, bid=598.10))

    assert broker.bind_latest_recovery_bar("bot:ema:recovery", symbol="SPY", side="sell") is True
    order = await broker.submit(BrokerOrderLeg(symbol="SPY", side="sell", quantity=1), client_order_id="bot:ema:recovery")

    assert (order.status, order.filled_avg_price) == ("filled", 598.10)


async def test_no_live_quote_leaves_the_recovery_unpriced_and_retains_nothing(tmp_path: Path) -> None:
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id=ACCOUNT)
    _retain(ledger, end_ms=NOON, close="600.00")
    broker = _broker(ledger, now_ms=NOON + 60_000, quote=None)

    assert broker.recovery_price_available("SPY") is False
    assert broker.bind_latest_recovery_bar("bot:ema:recovery", symbol="SPY", side="sell") is False
    assert ledger.latest_for_symbol("SPY", provider=RECOVERY_QUOTE_PROVIDER) is None


async def test_a_quote_never_prices_a_recovery_while_no_session_is_open(tmp_path: Path) -> None:
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id=ACCOUNT)
    broker = _broker(ledger, now_ms=OVERNIGHT, quote=_quote(observed_at_ms=OVERNIGHT - 1_000))

    assert broker.recovery_price_available("SPY") is False
    assert broker.bind_latest_recovery_bar("bot:ema:recovery", symbol="SPY", side="sell") is False


async def test_a_run_end_close_fills_at_the_last_bar_its_run_saw_stamped_when_it_sells(tmp_path: Path) -> None:
    """Owner decision 2026-09-29: an ended Dry Run's close needs no live quote
    or open session; the price is the run's last bar, the instant is now."""
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id=ACCOUNT)
    _retain(ledger, end_ms=NOON - 60_000, close="599.00")
    _retain(ledger, end_ms=NOON, close="600.00")
    # A recovery quote is fill evidence, never a price the run saw.
    ledger.retain_recovery_quote(MarketDataBar(
        symbol="SPY", start_ms=NOON + 59_999, end_ms=NOON + 60_000, open=Decimal("601.25"), high=Decimal("601.25"),
        low=Decimal("601.25"), close=Decimal("601.25"), volume=0, fetched_at_ms=NOON + 60_000,
        feed_id=RECOVERY_QUOTE_PROVIDER,
    ))
    broker = _broker(ledger, now_ms=OVERNIGHT, quote=None)

    assert broker.bind_run_end_close_bar("bot:ema:close", symbol="SPY") is True
    order = await broker.submit(BrokerOrderLeg(symbol="SPY", side="sell", quantity=1), client_order_id="bot:ema:close")

    assert (order.status, order.filled_avg_price, order.filled_at_ms) == ("filled", 600.0, OVERNIGHT)
    evidence = ledger.latest_for_symbol("SPY", provider=RUN_END_CLOSE_PROVIDER)
    assert evidence is not None and (evidence.close, evidence.end_ms, evidence.run_id) == (Decimal("600.00"), OVERNIGHT, None)


async def test_a_run_end_close_with_no_bar_its_run_saw_is_left_unsent(tmp_path: Path) -> None:
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id=ACCOUNT)
    broker = _broker(ledger, now_ms=NOON, quote=_quote(observed_at_ms=NOON - 1_000))

    assert broker.bind_run_end_close_bar("bot:ema:close", symbol="SPY") is False
    assert ledger.latest_for_symbol("SPY", provider=RUN_END_CLOSE_PROVIDER) is None


def test_a_port_that_is_not_a_simulation_refuses_a_run_end_close() -> None:
    """A run-end close restates a price the run already saw: only a simulation
    can fill at one, so the guard never lets it reach a real broker."""

    class _VendorPort:
        broker_id = "alpaca"

    guarded = GuardedBrokerTradePort(_VendorPort(), intake=ReentrantAsyncLock())  # type: ignore[arg-type]

    assert guarded.bind_run_end_close_bar("bot:ema:close", symbol="SPY") is False
