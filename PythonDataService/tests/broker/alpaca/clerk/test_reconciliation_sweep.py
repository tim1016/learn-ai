"""Reconciliation-sweep loop tests (Alpaca phase 2, S6, #1197).

The loop is a thin scheduler over :meth:`AlpacaClerk.reconcile_once`. An injected
``sleep`` seam and a ``max_passes`` budget drive a bounded, timer-free loop so a
test runs an exact number of passes deterministically with no real timer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.broker.alpaca.clerk import journal as journal_module
from app.broker.alpaca.clerk.clerk import AlpacaClerk
from app.broker.alpaca.clerk.models import ClerkEntryKind
from app.broker.alpaca.clerk.sweep import ReconciliationSweep
from app.broker.contract.models import BrokerAccountSnapshot, BrokerOrder, BrokerPosition

_FIXED_MS = 1_700_000_000_000


def _account() -> BrokerAccountSnapshot:
    return BrokerAccountSnapshot(
        broker="alpaca",
        account_id="PA-SWEEP",
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


class _FakeBroker:
    broker_id = "alpaca"

    def __init__(self, orders: list[BrokerOrder] | None = None) -> None:
        self._orders = orders or []

    async def get_account(self) -> BrokerAccountSnapshot:
        return _account()

    async def list_orders(self, **_: Any) -> list[BrokerOrder]:
        return list(self._orders)

    async def list_positions(self) -> list[BrokerPosition]:
        return []


@pytest.fixture(autouse=True)
def _clerk_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ALPACA_CLERK_DIR", str(tmp_path))
    journal_module.reset_clerk_settings_for_testing()
    yield tmp_path
    journal_module.reset_clerk_settings_for_testing()


async def test_sweep_runs_a_bounded_number_of_passes() -> None:
    broker = _FakeBroker()
    clerk = AlpacaClerk(read=broker, trade=broker, clock=lambda: _FIXED_MS)

    sleeps: list[float] = []

    async def _no_wait(seconds: float) -> None:
        sleeps.append(seconds)

    sweep = ReconciliationSweep(
        clerk=clerk, interval_s=15.0, sleep=_no_wait, max_passes=3
    )
    await sweep.run()

    entries = clerk._journal.read_entries()  # type: ignore[union-attr]
    verdicts = [e.verdict for e in entries if e.kind is ClerkEntryKind.RECONCILIATION]
    assert verdicts == ["clean", "clean", "clean"]
    # Slept between passes but not after the last (budget-bounded exit).
    assert sleeps == [15.0, 15.0]


async def test_sweep_pass_error_does_not_kill_the_loop() -> None:
    # A pass that raises unexpectedly is surfaced and the loop continues.
    class _ExplodingClerk(AlpacaClerk):
        def __init__(self) -> None:
            broker = _FakeBroker()
            super().__init__(read=broker, trade=broker, clock=lambda: _FIXED_MS)
            self.calls = 0

        async def reconcile_once(self):  # type: ignore[override]
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("boom")
            return "clean"

    clerk = _ExplodingClerk()

    async def _no_wait(_: float) -> None:
        return None

    sweep = ReconciliationSweep(
        clerk=clerk, interval_s=1.0, sleep=_no_wait, max_passes=2
    )
    await sweep.run()

    # Both passes ran despite the first raising.
    assert clerk.calls == 2


async def test_start_then_stop_is_clean() -> None:
    broker = _FakeBroker()
    clerk = AlpacaClerk(read=broker, trade=broker, clock=lambda: _FIXED_MS)

    async def _no_wait(_: float) -> None:
        return None

    # No max_passes → runs until stopped.
    sweep = ReconciliationSweep(clerk=clerk, interval_s=0.0, sleep=_no_wait)
    sweep.start()
    await sweep.stop()

    # Idempotent stop.
    await sweep.stop()
