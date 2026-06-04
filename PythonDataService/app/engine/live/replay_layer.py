"""Layer B replay call site (PRD-B #8).

``replay_session`` re-runs a strategy over canonical bars through the SAME
``LiveEngine`` decision path the live run used, so the replayed decisions
are an apples-to-apples comparison with the live decisions (PRD-B story
12): same engine, same consolidation, ``live_paper`` semantics regardless
of the live run's actual mode. It is a thin call site — no new abstraction
over the engine.

The deterministic ``ReplaySimBroker`` fills market orders at the next
bar's open (the engine's ``NEXT_BAR_OPEN`` model), so the replayed
strategy's position evolution — and therefore its position-dependent
decisions — matches the backtest. It is a production replay component (a
forerunner of PRD-C's ``IBrokerAdapter`` / ``ShadowFillSimulator``),
distinct from the test-only ``FakeBroker``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import pandas as pd

from app.broker.ibkr.models import (
    IbkrAccountSummary,
    IbkrOrderAck,
    IbkrOrderSpec,
    IbkrPositionsSnapshot,
)
from app.engine.data.trade_bar import TradeBar
from app.engine.execution.order import Direction, OrderEvent
from app.engine.live.artifacts import CORE_DECISION_COLUMNS, DECISION_COLUMNS, DecisionRow
from app.engine.live.config import LiveConfig
from app.engine.live.divergence.bar_series_joiner import CanonicalBar, join_bar_series
from app.engine.live.divergence.replay_divergence import (
    ReplayTolerances,
    classify_replay_divergences,
    classify_trade_graph_drift,
)
from app.engine.live.divergence.report_bundler import (
    BundlePaths,
    ReportMetadata,
    write_report_bundle,
)
from app.engine.live.live_engine import LiveEngine
from app.engine.strategy.base import Strategy


@dataclass
class _Pending:
    order_id: int
    spec: IbkrOrderSpec


class ReplaySimBroker:
    """Deterministic in-memory broker: fills market orders at the next bar open.

    Implements the engine's ``ReplayBrokerAdapter`` protocol (``advance_bar``
    + ``drain_order_events``) plus the base ``BrokerAdapter`` surface. Fees
    use the modelled IBKR per-order floor so the replay's portfolio matches
    the deterministic backtest fill model.
    """

    def __init__(self, *, initial_cash: Decimal = Decimal("100000")) -> None:
        self.cash = initial_cash
        self.positions: dict[str, int] = {}
        self.orders: list[IbkrOrderSpec] = []
        self._pending: list[_Pending] = []
        self._events: list[OrderEvent] = []

    async def fetch_account_summary(self) -> IbkrAccountSummary:
        return IbkrAccountSummary(
            account_id="REPLAY",
            is_paper=True,
            cash_balance=float(self.cash),
            net_liquidation=float(self.cash),
            fetched_at_ms=1,
        )

    async def fetch_positions(self) -> IbkrPositionsSnapshot:
        return IbkrPositionsSnapshot(
            account_id="REPLAY", is_paper=True, positions=[], fetched_at_ms=1
        )

    async def place_order(
        self, spec: IbkrOrderSpec, *, perm_id_wait_s: float = 0.0
    ) -> IbkrOrderAck:
        # perm_id_wait_s accepted for BrokerAdapter parity; replay assigns
        # deterministic ids synchronously, so there is nothing to wait for.
        order_id = self._resolve_order_id(spec)
        self.orders.append(spec)
        self._pending.append(_Pending(order_id=order_id, spec=spec))
        return IbkrOrderAck(
            account_id="REPLAY",
            is_paper=True,
            order_id=order_id,
            client_id=1,
            con_id=1,
            symbol=spec.symbol,
            action=spec.action,
            quantity=spec.quantity,
            order_type=spec.order_type,
            status="PendingSubmit",
            placed_at_ms=1,
        )

    async def advance_bar(self, bar: TradeBar) -> None:
        pending = list(self._pending)
        self._pending.clear()
        for item in pending:
            signed_qty = (
                int(item.spec.quantity)
                if item.spec.action == "BUY"
                else -int(item.spec.quantity)
            )
            event = OrderEvent(
                order_id=item.order_id,
                symbol=item.spec.symbol,
                time=bar.time,
                fill_price=bar.open,
                fill_quantity=signed_qty,
                direction=Direction.LONG if signed_qty > 0 else Direction.SHORT,
                fee=Decimal("1.00"),
                tag="SetHoldings" if signed_qty > 0 else "Liquidate",
            )
            self.positions[event.symbol] = self.positions.get(event.symbol, 0) + signed_qty
            self.cash -= Decimal(signed_qty) * bar.open + event.fee
            self._events.append(event)

    def drain_order_events(self) -> list[OrderEvent]:
        events = list(self._events)
        self._events.clear()
        return events

    async def cancel_open_orders(self) -> list[int]:
        cancelled = [item.order_id for item in self._pending]
        self._pending.clear()
        return cancelled

    def _resolve_order_id(self, spec: IbkrOrderSpec) -> int:
        if spec.client_order_id and spec.client_order_id.startswith("live-"):
            return int(spec.client_order_id.removeprefix("live-"))
        return len(self.orders) + 1


async def _iter_bars(bars: Iterable[TradeBar]) -> AsyncIterator[TradeBar]:
    for bar in bars:
        yield bar


async def replay_session(
    strategy: Strategy,
    bars: Sequence[TradeBar],
    *,
    output_dir: Path,
    decision_columns: tuple[str, ...] = DECISION_COLUMNS,
    run_mode: str = "live_paper",
    bar_source: str = "ibkr_paper_delayed",
    config: LiveConfig | None = None,
) -> pd.DataFrame:
    """Replay ``strategy`` over ``bars`` and return the decisions.parquet-shaped
    output. ``run_mode`` defaults to ``live_paper`` (story 12: replay uses
    live_paper semantics regardless of the live run's mode)."""
    engine = LiveEngine(
        None,
        # force_flat_at=None so the replay compares apples-to-apples with the
        # backtest decision path rather than the live force-flat barrier.
        config or LiveConfig(force_flat_at=None),
        broker=ReplaySimBroker(),
        output_dir=output_dir,
        run_mode=run_mode,
        bar_source=bar_source,
        decision_columns=decision_columns,
    )
    await engine.run(strategy, _iter_bars(bars))

    decisions_path = output_dir / "decisions.parquet"
    if not decisions_path.exists():
        return pd.DataFrame(columns=list(decision_columns))
    return pd.read_parquet(decisions_path)


def _decision_rows_by_bar(decisions: pd.DataFrame) -> dict[int, DecisionRow]:
    """Reconstruct ``DecisionRow``s (keyed by bar_close_ms) from a
    decisions.parquet-shaped frame, folding non-core columns into
    ``indicator_values``."""
    indicator_cols = [c for c in decisions.columns if c not in CORE_DECISION_COLUMNS]
    rows: dict[int, DecisionRow] = {}
    for record in decisions.to_dict("records"):
        bar_close_ms = int(record["bar_close_ms"])
        rows[bar_close_ms] = DecisionRow(
            bar_close_ms=bar_close_ms,
            signal=str(record["signal"]),
            intended_price=float(record["intended_price"]),
            bar_source=str(record.get("bar_source", "")),
            bar_open=_opt(record.get("bar_open")),
            bar_high=_opt(record.get("bar_high")),
            bar_low=_opt(record.get("bar_low")),
            bar_close=_opt(record.get("bar_close")),
            bar_volume=_opt(record.get("bar_volume")),
            indicator_values={c: record[c] for c in indicator_cols},
        )
    return rows


def _opt(value: object) -> float | None:
    return None if value is None or pd.isna(value) else float(value)


async def run_layer_b(
    *,
    live_decisions: pd.DataFrame,
    strategy: Strategy,
    canonical_minute_bars: Sequence[TradeBar],
    canonical_decision_bars: Sequence[CanonicalBar],
    reports_dir: Path,
    work_dir: Path,
    metadata: ReportMetadata,
    tolerances: ReplayTolerances | None = None,
) -> BundlePaths:
    """Run the Layer B ``ReplayDivergence`` pipeline for one trading day.

    Replays ``strategy`` over canonical minute bars, joins the live decision
    bars against the canonical decision-cadence bars, classifies per-bar and
    end-of-day trade-graph divergence, and writes the ``day-N.replay`` bundle.
    A thin orchestrator over already-tested pure functions plus the replay
    call site — no new divergence logic here.
    """
    tolerances = tolerances or ReplayTolerances()

    # Replay uses live_paper semantics regardless of the live run's actual
    # mode, so the replayed decisions are apples-to-apples (story 12).
    replayed_df = await replay_session(
        strategy, canonical_minute_bars, output_dir=work_dir, run_mode="live_paper"
    )

    live_by_bar = _decision_rows_by_bar(live_decisions)
    replayed_by_bar = _decision_rows_by_bar(replayed_df)

    joined_bars = join_bar_series(list(live_by_bar.values()), canonical_decision_bars)

    divergences = []
    for joined in joined_bars:
        replayed = replayed_by_bar.get(joined.bar_close_ms)
        divergences.extend(classify_replay_divergences(joined, replayed, tolerances))
    divergences.extend(
        classify_trade_graph_drift(
            list(live_by_bar.values()), list(replayed_by_bar.values()), tolerances
        )
    )

    return write_report_bundle(divergences, metadata=metadata, reports_dir=reports_dir)
