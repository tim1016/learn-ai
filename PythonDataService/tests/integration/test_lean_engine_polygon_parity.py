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
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.engine.data.lean_format import LeanMinuteDataReader
from app.engine.data.trade_bar import TradeBar
from app.engine.execution.fill_model import FillModel
from app.engine.execution.order import Direction, FillMode, OrderEvent
from app.engine.strategy.algorithms.spy_ema_crossover import SpyEmaCrossoverAlgorithm
from app.engine.strategy.base import DecisionSnapshot

REPO_ROOT = Path(__file__).resolve().parents[2]  # PythonDataService/
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "polygon_capture"


def _pick_fixture() -> Path:
    """Find the single committed parity fixture.

    Currently the parity test pins to one window; if multiple fixtures
    exist, fail loudly so the test is unambiguous.
    """
    if not FIXTURE_ROOT.exists():
        pytest.skip(f"no fixture directory at {FIXTURE_ROOT} -- run scripts/regenerate_polygon_fixture.py")
    candidates = sorted(d for d in FIXTURE_ROOT.iterdir() if d.is_dir() and (d / "metadata.json").exists())
    if not candidates:
        pytest.skip(f"no Polygon fixture committed under {FIXTURE_ROOT}")
    if len(candidates) > 1:
        names = ", ".join(c.name for c in candidates)
        raise RuntimeError(f"parity test expects exactly one fixture; found {len(candidates)}: {names}")
    return candidates[0]


def _ms_at_session_open(d: date) -> int:
    """09:30 ET of date d, expressed as int64 ms UTC."""
    et = ZoneInfo("America/New_York")
    dt = datetime(d.year, d.month, d.day, 9, 30, tzinfo=et)
    return int(dt.astimezone(UTC).timestamp() * 1000)


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
    from app.services.lean_sidecar_persistence import pair_order_events
    from app.services.lean_sidecar_service import TrustedRunRequest, run_trusted_sample
    from tests._helpers.parity import assert_state_traces_match, assert_trade_equivalence

    fixture_dir = _pick_fixture()
    meta = json.loads((fixture_dir / "metadata.json").read_text())
    symbol = meta["symbol"]
    from_date = date.fromisoformat(meta["from_date"])
    to_date = date.fromisoformat(meta["to_date"])

    assert meta.get("observed_trade_count", 0) and meta["observed_trade_count"] >= 1, (
        f"fixture {fixture_dir.name} has observed_trade_count="
        f"{meta.get('observed_trade_count')!r}; cannot serve as parity receipt"
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
    request = TrustedRunRequest(
        run_id=run_id,
        symbol=symbol,
        # end_ms_utc is session-open of the day AFTER to_date (half-open
        # per the P2.5 contract).
        start_ms_utc=_ms_at_session_open(from_date),
        end_ms_utc=_ms_at_session_open(to_date + timedelta(days=1)),
        starting_cash=100_000.0,
        template="ema_crossover",
        data_source="polygon",
        bar_minutes=15,
        session="regular",
        adjustment="raw",
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
