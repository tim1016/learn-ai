"""A Dry Run's recovery EXIT is priced like its strategy EXITs: from its own retained bar (H33, #2563).

A recovery EXIT (the operator's safe flatten, a sweep re-drive) has no
strategy decision bar. The shadow world binds the newest bar the instance
retained in the send session; the ``sim:`` world now does the same, so its
simulated fill is folded as simulated execution -- never as cumulative broker
recovery, which leaves the Dry Run's money unprovable.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from app.broker.alpaca.clerk.synthetic_broker import SyntheticBroker
from app.broker.contract.models import BrokerOrderLeg
from app.marketdata.feed import MarketDataBar
from app.services.session_authority import et_minute_of_day_ms
from app.services.source_bar_ledger import SourceBarLedger
from tests.broker.alpaca.clerk.sqlite.conftest import NOON, _TestClock

ACCOUNT = "sim:ema-1"
NEXT_SESSION_NOON = et_minute_of_day_ms(date(2026, 9, 9), 12 * 60)


def _retain(ledger: SourceBarLedger, *, end_ms: int, close: str) -> None:
    price = Decimal(close)
    ledger.append(MarketDataBar(
        symbol="SPY", start_ms=end_ms - 60_000, end_ms=end_ms, open=price, high=price, low=price, close=price,
        volume=1, fetched_at_ms=end_ms, feed_id="ibkr", session_phase="RTH",
    ), run_id="run-a")


async def test_a_recovery_exit_fills_at_the_newest_bar_retained_in_its_session(tmp_path: Path) -> None:
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id=ACCOUNT)
    _retain(ledger, end_ms=NOON - 60_000, close="599.00")
    _retain(ledger, end_ms=NOON, close="600.00")
    broker = SyntheticBroker(account_id=ACCOUNT, source_bars=ledger, clock=_TestClock(NOON + 30 * 60_000))

    assert broker.bind_latest_recovery_bar("bot:ema:recovery", symbol="SPY") is True
    order = await broker.submit(BrokerOrderLeg(symbol="SPY", side="sell", quantity=1), client_order_id="bot:ema:recovery")

    assert (order.status, order.filled_avg_price, order.filled_at_ms) == ("filled", 600.0, NOON)
    assert [event.execution_id for event in order.events] == ["sim-execution:bot:ema:recovery"]
    # The whole execution happened inside ``submit``: it is the evidence.
    assert broker.submission_response_is_authoritative_evidence is True


async def test_a_recovery_exit_never_prices_from_a_previous_session(tmp_path: Path) -> None:
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id=ACCOUNT)
    _retain(ledger, end_ms=NOON, close="600.00")
    broker = SyntheticBroker(account_id=ACCOUNT, source_bars=ledger, clock=_TestClock(NEXT_SESSION_NOON))

    assert broker.bind_latest_recovery_bar("bot:ema:recovery", symbol="SPY") is False


async def test_a_recovery_exit_without_any_retained_price_is_refused(tmp_path: Path) -> None:
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id=ACCOUNT)
    broker = SyntheticBroker(account_id=ACCOUNT, source_bars=ledger, clock=_TestClock(NOON))

    assert broker.bind_latest_recovery_bar("bot:ema:recovery", symbol="SPY") is False
