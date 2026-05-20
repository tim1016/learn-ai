"""LEAN vs engine parity on Polygon-sourced bars (the receipt test).

Runs the LEAN sidecar in Polygon-source mode against a recorded
fixture, then runs the in-process engine over the same staged LEAN
zips via LeanMinuteDataReader. Asserts per-bar indicator state
equivalence (state.csv == DecisionSnapshot stream) and trade-by-trade
equivalence.

Skipped without ``LEAN_LAUNCHER_URL`` because LEAN must be reachable
to produce state.csv. The fixture itself does not require
``POLYGON_API_KEY`` -- the RecordedPolygonFixtureProvider replays
bars.json.

Different oracle from tests/integration/parity/test_ema_crossover_lean_vs_spec.py
(PR #296, LEAN-vs-spec on the LEAN data dump). This test is LEAN-vs-
hand-coded-engine on a Polygon fixture, validating that both engines
ingest the same Polygon bars and produce equal state.
"""

from __future__ import annotations

import csv
import json
import os
import uuid
from datetime import date
from decimal import Decimal

import pytest

from app.engine.data.lean_format import LeanMinuteDataReader
from app.engine.data.trade_bar import TradeBar
from app.engine.execution.fill_model import FillModel
from app.engine.execution.order import Direction, FillMode, OrderEvent
from app.engine.strategy.algorithms.spy_ema_crossover import SpyEmaCrossoverAlgorithm
from app.engine.strategy.base import DecisionSnapshot
from tests._helpers.parity_fixture import PARITY_FIXTURE_NAME, parity_fixture_dir


class _RecordingAlgorithm(SpyEmaCrossoverAlgorithm):
    """Subclass that records per-bar decision snapshots AND per-trade entry quantities.

    Overrides ``_on_fifteen_minute_bar`` so the recording wrapper runs
    after the parent handler (which sets ``last_decision_snapshot``)
    and appends each snapshot to ``decision_rows``. Using a subclass
    rather than monkey-patching the bound method means the consolidator
    captures the override at ``initialize()`` time -- the only path that
    actually works, because the consolidator holds a direct reference to
    the handler registered at subscription time.

    Entry quantities are captured directly from OrderEvent.fill_quantity
    on the LONG fill — the same shares LEAN reports. Computing
    floor(starting_cash / entry_price) is wrong for trades 2+ because
    LEAN's set_holdings(1.0) sizes against current portfolio_value
    (cash + position value), which drifts with realized PnL.
    """

    def __init__(self, symbol: str = "SPY", *, entry_quantities: list[Decimal] | None = None) -> None:
        super().__init__(symbol=symbol)
        self.decision_rows: list[dict] = []
        self._entry_quantities: list[Decimal] = entry_quantities if entry_quantities is not None else []

    def _on_fifteen_minute_bar(self, bar: TradeBar) -> None:
        super()._on_fifteen_minute_bar(bar)
        snap: DecisionSnapshot | None = self.last_decision_snapshot
        if snap is None:
            return
        self.decision_rows.append(
            {
                "ts_ms_utc": snap.bar_close_ms,
                "close": float(snap.intended_price),
                "ema_fast": float(snap.ema5),
                "ema_slow": float(snap.ema10),
                "rsi": float(snap.rsi),
                "cross_state": ("above" if snap.ema5 > snap.ema10 else "below" if snap.ema5 < snap.ema10 else "equal"),
                "signal": snap.signal,
            }
        )

    def on_order_event(self, event: OrderEvent) -> None:
        super().on_order_event(event)
        # Capture LONG fills (entries). LEAN's fill_quantity is the
        # number of shares the engine actually filled, which reflects
        # set_holdings(1.0) sizing against current portfolio_value —
        # not the fixed floor(starting_cash / entry_price) formula
        # that breaks for trades 2+ once PnL has moved portfolio_value.
        if event.direction == Direction.LONG:
            self._entry_quantities.append(Decimal(event.fill_quantity))


def _lean_trades_from_normalized(
    normalized,
    pair_order_events,
) -> list[dict]:
    """Pair LEAN's normalized order events into round-trip trades.

    NormalizedOrderEvent objects are Pydantic models; pair_order_events
    expects Sequence[dict]. Convert via model_dump() so the dict-style
    access inside pair_order_events (e.get("status"), fill["direction"])
    works correctly.
    """
    raw_dicts = [ev.model_dump() for ev in normalized.order_events]
    paired, open_lot = pair_order_events(raw_dicts)
    assert open_lot is None, "LEAN ended with an unmatched open lot; check OnEndOfAlgorithm"
    return [
        {
            "entry_ms_utc": t.entry_ms_utc,
            "exit_ms_utc": t.exit_ms_utc,
            "quantity": Decimal(str(t.quantity)),
            "entry_price": Decimal(str(t.entry_price)),
            "exit_price": Decimal(str(t.exit_price)),
        }
        for t in paired
    ]


def _engine_trades_from_strategy(algo: _RecordingAlgorithm) -> list[dict]:
    """Translate the engine's LoggedTrade stream + recorded entry quantities into parity-helper shape.

    Entry quantity comes from OrderEvent.fill_quantity (captured in
    _RecordingAlgorithm.on_order_event), NOT from floor(starting_cash /
    entry_price) — the latter is wrong for trades 2+ when realized PnL
    has moved portfolio_value away from starting_cash.
    """
    entry_quantities = algo._entry_quantities
    assert len(algo.trade_log) == len(entry_quantities), (
        f"trade_log length {len(algo.trade_log)} != entry quantities recorded "
        f"{len(entry_quantities)} — fill-event recorder lost an entry"
    )
    return [
        {
            "entry_ms_utc": int(t.entry_time.timestamp() * 1000),
            "exit_ms_utc": int(t.exit_time.timestamp() * 1000),
            "quantity": qty,
            "entry_price": Decimal(str(t.entry_price)),
            "exit_price": Decimal(str(t.exit_price)),
        }
        for t, qty in zip(algo.trade_log, entry_quantities, strict=True)
    ]


@pytest.mark.skipif(
    not os.environ.get("LEAN_LAUNCHER_URL"),
    reason="LEAN_LAUNCHER_URL unset; integration test requires the sidecar launcher",
)
@pytest.mark.asyncio
async def test_lean_and_engine_agree_on_polygon_fixture(monkeypatch) -> None:
    from app.engine.engine import BacktestEngine
    from app.lean_sidecar import polygon_canonical
    from app.lean_sidecar.trading_calendar import next_trading_day, session_open_ms_utc
    from app.routers.lean_sidecar import TrustedRunRequestModel
    from app.services.lean_sidecar_persistence import pair_order_events
    from app.services.lean_sidecar_service import TrustedRunRequest, run_trusted_sample
    from tests._helpers.parity import assert_state_traces_match, assert_trade_equivalence

    fixture_dir = parity_fixture_dir()
    assert fixture_dir.name == PARITY_FIXTURE_NAME
    meta = json.loads((fixture_dir / "metadata.json").read_text())
    symbol = meta["symbol"]
    from_date = date.fromisoformat(meta["from_date"])
    to_date = date.fromisoformat(meta["to_date"])

    # Skip — not fail — when observed_trade_count hasn't been filled in
    # yet. Same semantic as a missing fixture: the operator (Task 10 of
    # the parity plan) has captured the bars but hasn't yet recorded
    # the parity-receipt metadata. The fixture cannot serve as a
    # ground-truth receipt without it, but it is also not the engine's
    # fault, so the test should not block CI.
    observed_count = meta.get("observed_trade_count")
    if not observed_count or observed_count < 1:
        pytest.skip(
            f"fixture {fixture_dir.name} has observed_trade_count={observed_count!r}; "
            "rerun the parity test once after capture to populate metadata.json"
        )

    # Inject the fixture provider for the LEAN run.
    fixture_provider = polygon_canonical.RecordedPolygonFixtureProvider(fixture_dir)
    monkeypatch.setattr(
        polygon_canonical,
        "get_default_provider",
        lambda: fixture_provider,
    )

    # Run LEAN.
    run_id = f"parity-{uuid.uuid4().hex[:8]}"
    # PR B: TrustedRunRequest carries a single ``data_policy`` block.
    from app.lean_sidecar.data_policy import BarsSpec, DataPolicy

    data_policy = DataPolicy(
        source="polygon",
        symbol=symbol,
        adjusted=False,  # adjustment="raw" -> adjusted=False
        session="regular",
        input_bars=BarsSpec(timespan="minute", multiplier=1),
        strategy_bars=BarsSpec(timespan="minute", multiplier=15),
        timestamp_policy="bar_close_ms_utc",
        timezone="America/New_York",
        provider_kind="fixture",
        fixture_id=fixture_dir.name,
        fixture_sha256=None,
    )
    # P1-WINDOW: route the window construction through TrustedRunRequestModel
    # so the router's P2.5 session-open exclusive-end contract validates
    # (rejects same-day end, advances past weekends/holidays). next_trading_day
    # gives the right exclusive end for Friday to_date plus MLK Monday.
    exclusive_end = next_trading_day(to_date)
    model = TrustedRunRequestModel(
        run_id=run_id,
        symbol=symbol,
        start_ms_utc=session_open_ms_utc(from_date),
        end_ms_utc=session_open_ms_utc(exclusive_end),
        starting_cash=100_000.0,
        template="ema_crossover",
        data_source="polygon",
        bar_minutes=15,
        session="regular",
        adjustment="raw",
    )
    request = TrustedRunRequest(
        run_id=model.run_id,
        start_ms_utc=model.start_ms_utc,
        end_ms_utc=model.end_ms_utc,
        starting_cash=model.starting_cash,
        algorithm_source=model.algorithm_source,
        template=model.template,
        data_policy=data_policy,
    )
    result = await run_trusted_sample(request)
    assert result.exit_code == 0, f"LEAN exited non-zero: {result.log_tail}"

    # Parse LEAN state.csv.
    state_csv = result.workspace_root / "output" / "storage" / "state.csv"
    assert state_csv.exists(), f"LEAN did not emit state.csv at {state_csv}"
    lean_rows: list[dict] = []
    with state_csv.open() as f:
        for r in csv.DictReader(f):
            lean_rows.append(
                {
                    "ts_ms_utc": int(r["ts_ms_utc"]),
                    "close": float(r["close"]),
                    "ema_fast": float(r["ema_fast"]),
                    "ema_slow": float(r["ema_slow"]),
                    "rsi": float(r["rsi"]),
                    "cross_state": r["cross_state"],
                    "signal": r["signal"],
                }
            )

    # Run the in-process engine over the SAME staged LEAN zips.
    entry_quantities: list[Decimal] = []
    algo = _RecordingAlgorithm(symbol=symbol, entry_quantities=entry_quantities)

    # Pin the engine's window and cash to match the LEAN run.
    orig_init = algo.initialize

    def pinned_init() -> None:
        orig_init()
        algo.set_start_date(from_date.year, from_date.month, from_date.day)
        algo.set_end_date(to_date.year, to_date.month, to_date.day)
        algo.set_cash(100_000.0)

    algo.initialize = pinned_init  # type: ignore[method-assign]

    reader = LeanMinuteDataReader(result.workspace_root / "data")
    engine = BacktestEngine(
        data_source=reader,
        fill_model=FillModel(
            mode=FillMode.SIGNAL_BAR_CLOSE,
            commission_per_order=Decimal("0"),
        ),
    )
    engine.run(algo)

    # Assert state-trace parity (indicator + decision state).
    assert_state_traces_match(lean_rows, algo.decision_rows, atol=1e-9, rtol=0.0)

    # Assert trade equivalence.
    lean_trades = _lean_trades_from_normalized(result.normalized, pair_order_events)
    engine_trades = _engine_trades_from_strategy(algo)  # uses algo._entry_quantities
    assert_trade_equivalence(
        lean_trades,
        engine_trades,
        fill_price_atol=Decimal("0.01"),
    )
