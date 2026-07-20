"""Direct One duration receipt for the default SPY EMA crossover signal.

The vendor receipt validates the observed QuantConnect/LEAN fill timing. It
cannot prove strict signal parity because it does not carry the source minute
bars or per-consolidated-bar indicator states; the cross-engine matrix covers
that shared-input Python ↔ LEAN proof separately.
"""

from __future__ import annotations

import ast
import csv
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from app.engine.data.trade_bar import TradeBar
from app.engine.execution.portfolio import Portfolio
from app.engine.strategy.algorithms.ema_crossover_signal import EmaCrossoverSignalAlgorithm
from app.engine.strategy.base import StrategyContext
from app.engine.strategy.signal_intent import SignalIntent, SignalIntentKind
from app.lean_sidecar.trusted_samples.ema_crossover_signal import EMA_CROSSOVER_SIGNAL_SOURCE

_REFERENCE_DIR = (
    Path(__file__).resolve().parents[4]
    / "references"
    / "qc-shadow"
    / "backtests"
    / "2025-01-01_to_2026-01-01"
)
_CLOSED_TRADES = _REFERENCE_DIR / "closed_trades.csv"
_ORDERS = _REFERENCE_DIR / "orders.csv"
_MINUTE_MS = 60_000


@dataclass
class _ReadyIndicator:
    current_value: Decimal
    is_ready: bool = True

    def update(self, _time: datetime, _value: Decimal) -> None:
        """Retain controlled indicator values for lifecycle testing."""


@dataclass
class _RecordingSignalIntentExecutor:
    intents: list[SignalIntent] = field(default_factory=list)

    def execute(self, _context: object, intent: SignalIntent) -> None:
        self.intents.append(intent)


def _closed_trades() -> list[dict[str, str]]:
    with _CLOSED_TRADES.open(newline="", encoding="utf-8") as source:
        reader = csv.DictReader(source)
        assert reader.fieldnames == [
            "entry_time_ms",
            "exit_time_ms",
            "entry_price",
            "exit_price",
            "quantity",
            "profit_loss",
            "total_fees",
            "reported_duration",
            "order_ids",
        ]
        return list(reader)


def _consolidated_bar(end_time: datetime) -> TradeBar:
    return TradeBar(
        symbol="SPY",
        time=end_time - timedelta(minutes=15),
        end_time=end_time,
        open=Decimal("500"),
        high=Decimal("500"),
        low=Decimal("500"),
        close=Decimal("500"),
        volume=100,
    )


def _lean_exit_bars() -> int:
    tree = ast.parse(EMA_CROSSOVER_SIGNAL_SOURCE)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "MyAlgorithm":
            for statement in node.body:
                if (
                    isinstance(statement, ast.Assign)
                    and len(statement.targets) == 1
                    and isinstance(statement.targets[0], ast.Name)
                    and statement.targets[0].id == "EXIT_BARS"
                    and isinstance(statement.value, ast.Constant)
                ):
                    return int(statement.value.value)
    raise AssertionError("LEAN EMA crossover template does not define EXIT_BARS")


def test_direct_one_order_receipt_is_complete_and_time_ordered() -> None:
    with _ORDERS.open(newline="", encoding="utf-8") as source:
        orders = list(csv.DictReader(source))

    assert len(orders) == 72
    assert {order["Symbol"] for order in orders} == {"SPY"}
    assert {order["Status"] for order in orders} == {"Filled"}
    assert Counter(int(order["Quantity"]) > 0 for order in orders) == {True: 36, False: 36}

    order_times = [datetime.fromisoformat(order["Time"]).astimezone(UTC) for order in orders]
    assert order_times == sorted(order_times)


def test_direct_one_receipt_preserves_the_five_bar_execution_profile() -> None:
    trades = _closed_trades()

    assert len(trades) == 36
    durations_minutes = [
        (int(trade["exit_time_ms"]) - int(trade["entry_time_ms"])) // _MINUTE_MS
        for trade in trades
    ]
    assert Counter(durations_minutes) == {75: 29, 74: 3, 1125: 3, 1126: 1}

    for trade, duration_minutes in zip(trades, durations_minutes, strict=True):
        assert int(trade["entry_time_ms"]) < int(trade["exit_time_ms"])
        assert trade["order_ids"].count(";") == 1
        hours, minutes, seconds = (int(part) for part in trade["reported_duration"].split(":"))
        assert duration_minutes == hours * 60 + minutes + seconds // 60


def test_python_and_lean_templates_exit_after_five_consolidated_bars() -> None:
    assert _lean_exit_bars() == 5

    strategy = EmaCrossoverSignalAlgorithm(symbol="SPY")
    context = StrategyContext(portfolio=Portfolio(initial_cash=Decimal("100000")))
    strategy.ctx = context
    strategy.initialize()
    strategy._ema5 = _ReadyIndicator(Decimal("500.50"))  # type: ignore[assignment]
    strategy._ema10 = _ReadyIndicator(Decimal("500.00"))  # type: ignore[assignment]
    strategy._rsi14 = _ReadyIndicator(Decimal("60"))  # type: ignore[assignment]
    strategy._prev_ema5_above_ema10 = False
    recorder = _RecordingSignalIntentExecutor()
    context.set_signal_intent_executor(recorder)

    entry_end_time = datetime(2026, 1, 6, 15, 0, tzinfo=UTC)
    for bar_number in range(6):
        bar = _consolidated_bar(entry_end_time + timedelta(minutes=15 * bar_number))
        context.current_time = bar.end_time
        strategy._on_fifteen_minute_bar(bar)
        if bar_number < 5:
            assert [intent.kind for intent in recorder.intents] == [SignalIntentKind.ENTER]

    assert [intent.kind for intent in recorder.intents] == [
        SignalIntentKind.ENTER,
        SignalIntentKind.EXIT,
    ]
    assert recorder.intents[1].bar_close_ms - recorder.intents[0].bar_close_ms == 75 * _MINUTE_MS
