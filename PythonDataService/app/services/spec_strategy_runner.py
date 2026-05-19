"""Run a declarative spec strategy through BacktestEngine and capture trades.

Sibling to ``lean_sidecar_service.run_trusted_sample`` but for the in-process
engine path. Loads a ``StrategySpec`` from a fixture path, runs it through
``BacktestEngine`` against caller-provided ``TradeBar`` data, captures
``OrderEvent``s as they fire to build a quantity-aware
``list[EngineTrade]``, and optionally persists the run through
``engine_persistence``.

The trade-capture path is independent of the strategy's own ``trade_log``:
``trade_log`` is the public, dataclass-based observable that all strategies
populate; ``EngineTrade`` is the persist-layer shape that needs the share
count which ``trade_log`` doesn't carry. We compute it here from
``OrderEvent.fill_quantity``.

This module is engine-data-source-agnostic — it consumes an in-memory list
of ``TradeBar``. Callers (the parity test, integration tests, ad-hoc
notebooks) are responsible for constructing the bar list however they
prefer (Polygon, LEAN data dump, synthetic).
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.engine.data.trade_bar import TradeBar
from app.engine.engine import BacktestEngine
from app.engine.execution.fill_model import FillModel
from app.engine.execution.order import Direction, FillMode, OrderEvent
from app.engine.strategy.spec import SpecAlgorithm, load_spec_from_path
from app.services.engine_persistence import EngineTrade, persist_engine_run

__all__ = [
    "InMemoryDataReader",
    "SpecRunResult",
    "pair_engine_fills",
    "run_spec_against_bars",
    "run_spec_against_bars_and_persist",
]


@dataclass
class InMemoryDataReader:
    """Tiny ``BacktestEngine``-compatible reader over a pre-built bar list.

    The engine calls ``iter_bars(symbol, start, end)`` and consumes a stream
    of ``TradeBar``s. Filtering by symbol and date range here mirrors how
    real data sources behave, so a strategy subscribed to the wrong ticker
    silently receives no bars (rather than silently consuming the wrong ones).
    """

    bars: list[TradeBar]

    def iter_bars(self, symbol: str, start: date, end: date) -> Iterator[TradeBar]:
        target = symbol.upper()
        for bar in self.bars:
            if bar.symbol.upper() != target:
                continue
            if start <= bar.time.date() <= end:
                yield bar


@dataclass
class SpecRunResult:
    """The output of running a spec against bars.

    ``total_fees`` is summed from the ``fee`` field of every captured
    ``OrderEvent`` — non-zero only when the ``FillModel`` charges
    commissions. The default commission is zero for parity tests.

    ``strategy_execution_id`` is populated by
    ``run_spec_against_bars_and_persist`` when persistence succeeds.
    """

    trades: list[EngineTrade]
    total_fees: Decimal = Decimal("0")
    strategy_execution_id: int | None = None
    captured_events: list[OrderEvent] = field(default_factory=list)


def pair_engine_fills(events: Sequence[OrderEvent]) -> list[EngineTrade]:
    """Pair order-fill events into round-trip EngineTrade objects.

    Long-only strategies in scope (ema_crossover, sma_crossover, rsi_mean_rev):
    a LONG fill opens an entry; the next SHORT or FLAT fill closes it. This
    mirrors how ``SpecAlgorithm.on_order_event`` treats SHORT/FLAT identically
    as exits — the engine's force-flat path uses ``Direction.SHORT`` (opposite
    sign of entry) for session-close and bracket exits, while ``Direction.FLAT``
    would be used by a strategy that explicitly liquidates to zero.

    Raises:
      NotImplementedError: A second LONG fill arrives before an intervening
        exit. The in-scope parity strategies don't pyramid.
      ValueError: An exit fill arrives without an open LONG, or the event
        stream ends with an unmatched LONG (open position). The engine's
        ``on_force_flat`` session-close hook should preclude the latter.
    """
    open_entry: OrderEvent | None = None
    trade_number = 0
    trades: list[EngineTrade] = []

    for event in events:
        if event.direction == Direction.LONG:
            if open_entry is not None:
                raise NotImplementedError(
                    f"Pyramiding not supported: second LONG fill at {event.time} "
                    f"before exit of prior entry at {open_entry.time}"
                )
            open_entry = event
            continue

        # SHORT or FLAT → exit of the open LONG.
        if open_entry is None:
            raise ValueError(f"Unmatched {event.direction.name} fill at {event.time}: no open entry")
        trade_number += 1
        # The exit event's ``fill_quantity`` is signed-negative (the engine's
        # convention for closing a long). The trade's positive share count is
        # the entry's quantity.
        entry_qty = Decimal(open_entry.fill_quantity)
        gross_pnl = (event.fill_price - open_entry.fill_price) * entry_qty
        net_pnl = gross_pnl - (open_entry.fee + event.fee)
        is_synthetic = event.tag == "ForceFlat"
        trades.append(
            EngineTrade(
                trade_number=trade_number,
                entry_ms_utc=int(open_entry.time.timestamp() * 1000),
                exit_ms_utc=int(event.time.timestamp() * 1000),
                entry_price=open_entry.fill_price,
                exit_price=event.fill_price,
                quantity=entry_qty,
                pnl=net_pnl,
                signal_reason=event.tag or "",
                is_synthetic_exit=is_synthetic,
            )
        )
        open_entry = None

    if open_entry is not None:
        raise ValueError(
            f"Event stream ended with an open LONG at {open_entry.time}; "
            f"expected engine.on_force_flat to close all positions"
        )

    return trades


class _RecordingSpecAlgorithm(SpecAlgorithm):
    """SpecAlgorithm subclass that records every fill into a shared list.

    The captured ``OrderEvent``s carry ``fill_quantity`` which the strategy's
    own ``trade_log`` (a ``list[LoggedTrade]``) does not preserve. We delegate
    to ``super().on_order_event`` first so the strategy's own bookkeeping
    runs identically to the un-instrumented case — then append for our use.
    """

    def __init__(
        self,
        spec: Any,
        *,
        fill_events: list[OrderEvent],
    ) -> None:
        super().__init__(spec)
        self._captured_events = fill_events

    def on_order_event(self, event: OrderEvent) -> None:
        super().on_order_event(event)
        self._captured_events.append(event)


def run_spec_against_bars(
    *,
    spec_path: Path,
    symbol: str,
    bars: list[TradeBar],
    start_date: tuple[int, int, int],
    end_date: tuple[int, int, int],
    starting_cash: Decimal | None = None,
    commission_per_order: Decimal = Decimal("0"),
    fill_mode: FillMode = FillMode.SIGNAL_BAR_CLOSE,
) -> SpecRunResult:
    """Load a spec, run it through BacktestEngine against ``bars``, capture trades.

    ``start_date`` / ``end_date`` are ``(year, month, day)`` tuples passed to
    the strategy's ``set_start_date`` / ``set_end_date`` after the spec's
    ``initialize`` has set its own defaults. The bars list is consumed by an
    ``InMemoryDataReader``.

    ``starting_cash`` overrides the spec's own ``set_cash`` call so that order
    sizing and PnL computation use the same cash value that the persist layer
    records. When ``None``, the spec's default (typically 100 000) is used.
    """
    spec = load_spec_from_path(spec_path)
    captured: list[OrderEvent] = []
    strategy = _RecordingSpecAlgorithm(spec, fill_events=captured)
    strategy._symbol_name = symbol  # type: ignore[attr-defined]  # match symbol of provided bars

    orig_init = strategy.initialize

    def _patched_init() -> None:
        orig_init()
        strategy.set_start_date(*start_date)
        strategy.set_end_date(*end_date)
        if starting_cash is not None:
            strategy.set_cash(float(starting_cash))

    strategy.initialize = _patched_init  # type: ignore[method-assign]

    reader = InMemoryDataReader(bars=bars)
    engine = BacktestEngine(
        data_source=reader,
        fill_model=FillModel(mode=fill_mode, commission_per_order=commission_per_order),
    )
    engine.run(strategy)

    trades = pair_engine_fills(captured)
    total_fees = sum((e.fee for e in captured), start=Decimal("0"))

    return SpecRunResult(
        trades=trades,
        total_fees=total_fees,
        captured_events=captured,
    )


async def run_spec_against_bars_and_persist(
    *,
    spec_path: Path,
    symbol: str,
    bars: list[TradeBar],
    start_date: tuple[int, int, int],
    end_date: tuple[int, int, int],
    starting_cash: Decimal,
    backend_url: str,
    strategy_name: str | None = None,
    commission_per_order: Decimal = Decimal("0"),
    fill_mode: FillMode = FillMode.SIGNAL_BAR_CLOSE,
    extra_statistics: dict[str, Any] | None = None,
) -> SpecRunResult:
    """End-to-end: run the spec against bars AND persist via the .NET backend.

    Returns the same ``SpecRunResult`` but with ``strategy_execution_id``
    populated. If persistence fails (HTTP/network), the id stays ``None`` —
    the in-memory trades remain authoritative and the caller can retry.

    ``strategy_name`` defaults to the spec's ``name`` if not provided, so
    LEAN and the spec can be aligned on the same name for guardrail
    matching in ``compareBacktestRuns``.
    """
    spec = load_spec_from_path(spec_path)
    resolved_name = strategy_name or spec.name

    # Re-run by calling the sync version; it doesn't need to re-load the
    # spec since load_spec_from_path is cheap and stateless.
    result = run_spec_against_bars(
        spec_path=spec_path,
        symbol=symbol,
        bars=bars,
        start_date=start_date,
        end_date=end_date,
        starting_cash=starting_cash,
        commission_per_order=commission_per_order,
        fill_mode=fill_mode,
    )

    start_ms = _date_tuple_to_ms_utc(start_date)
    end_ms = _date_tuple_to_ms_utc(end_date)

    persisted_id = await persist_engine_run(
        base_url=backend_url,
        strategy_name=resolved_name,
        symbol=symbol,
        starting_cash=starting_cash,
        start_date_ms=start_ms,
        end_date_ms=end_ms,
        trades=result.trades,
        total_fees=result.total_fees,
        extra_statistics=extra_statistics,
    )

    return SpecRunResult(
        trades=result.trades,
        total_fees=result.total_fees,
        strategy_execution_id=persisted_id,
        captured_events=result.captured_events,
    )


def _date_tuple_to_ms_utc(date_tuple: tuple[int, int, int]) -> int:
    """Convert ``(year, month, day)`` to int64 ms UTC at midnight."""
    from datetime import UTC, datetime

    return int(datetime(*date_tuple, tzinfo=UTC).timestamp() * 1000)
