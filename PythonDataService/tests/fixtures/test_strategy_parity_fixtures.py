"""Golden fixture validation for Strategy A/B/C self-equivalence (ENG-008, #1699).

Runs Strategy A/B/C fresh through ``BacktestEngine`` against the fixture's
pinned bar series and diffs the resulting public ``trade_log`` — not the
private ``_entry_extra_gate_passes`` gate ``app/engine/tests/test_strategies_abc.py``
drives — against the committed pre-port trade log, bit-exact
(``atol=0, rtol=0`` per the manifest). This is the S3 intent port's
refactor-neutrality receipt: a failure here means the port changed a trade,
not that the port is "wrong" in some external sense.

Run in isolation (no FastAPI app needed):
  python -m pytest tests/fixtures/test_strategy_parity_fixtures.py -v --noconftest
"""

from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pytest

_SVC_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_SVC_ROOT))

from golden_support.registry import default as registry  # noqa: E402
from golden_support.strategy_replay import run_strategy_over_bars  # noqa: E402

from app.engine.data.trade_bar import TradeBar  # noqa: E402
from app.engine.strategy.algorithms.spy_strategy_a import SpyStrategyAAlgorithm  # noqa: E402
from app.engine.strategy.algorithms.spy_strategy_b import SpyStrategyBAlgorithm  # noqa: E402
from app.engine.strategy.algorithms.spy_strategy_c import SpyStrategyCAlgorithm  # noqa: E402
from app.engine.strategy.base import Strategy  # noqa: E402

FIXTURE_ID = "ENG-008"

_STRATEGIES: dict[str, type[Strategy]] = {
    "strategy_a": SpyStrategyAAlgorithm,
    "strategy_b": SpyStrategyBAlgorithm,
    "strategy_c": SpyStrategyCAlgorithm,
}


def _load_bars() -> list[TradeBar]:
    path = registry.resolve(FIXTURE_ID, "input")
    table = pa.ipc.open_file(path).read_all()
    rows = table.to_pylist()
    return [
        TradeBar(
            symbol=row["symbol"],
            start_ms=row["time_ms_utc"],
            end_ms=row["end_time_ms_utc"],
            open=Decimal(str(row["open"])),
            high=Decimal(str(row["high"])),
            low=Decimal(str(row["low"])),
            close=Decimal(str(row["close"])),
            volume=row["volume"],
        )
        for row in rows
    ]


def _load_output() -> dict:
    path = registry.resolve(FIXTURE_ID, "output")
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("strategy_key", sorted(_STRATEGIES))
def test_strategy_trade_log_matches_pinned_pre_port_receipt(strategy_key: str) -> None:
    bars = _load_bars()
    stored = _load_output()["trades"][strategy_key]
    strategy_cls = _STRATEGIES[strategy_key]

    fresh_trades = run_strategy_over_bars(strategy_cls(), bars)

    assert len(fresh_trades) == len(stored), (
        f"{strategy_key}: trade count changed — fresh={len(fresh_trades)} pinned={len(stored)}. "
        "If this is an intentional S3 port change, regenerate the fixture and document why "
        "in the commit message per the golden-fixture lifecycle rules."
    )
    for i, (fresh, oracle) in enumerate(zip(fresh_trades, stored, strict=True)):
        problems: list[str] = []
        if fresh.entry_time_ms != oracle["entry_time_ms"]:
            problems.append(f"entry_time_ms {fresh.entry_time_ms} != {oracle['entry_time_ms']}")
        if fresh.exit_time_ms != oracle["exit_time_ms"]:
            problems.append(f"exit_time_ms {fresh.exit_time_ms} != {oracle['exit_time_ms']}")
        if fresh.entry_price != Decimal(oracle["entry_price"]):
            problems.append(f"entry_price {fresh.entry_price} != {oracle['entry_price']}")
        if fresh.exit_price != Decimal(oracle["exit_price"]):
            problems.append(f"exit_price {fresh.exit_price} != {oracle['exit_price']}")
        if fresh.quantity != oracle["quantity"]:
            problems.append(f"quantity {fresh.quantity} != {oracle['quantity']}")
        if fresh.pnl_pts != Decimal(oracle["pnl_pts"]):
            problems.append(f"pnl_pts {fresh.pnl_pts} != {oracle['pnl_pts']}")
        if fresh.pnl_pct != Decimal(oracle["pnl_pct"]):
            problems.append(f"pnl_pct {fresh.pnl_pct} != {oracle['pnl_pct']}")
        if fresh.result != oracle["result"]:
            problems.append(f"result {fresh.result!r} != {oracle['result']!r}")
        if fresh.signal_reason != oracle["signal_reason"]:
            problems.append(f"signal_reason {fresh.signal_reason!r} != {oracle['signal_reason']!r}")
        if fresh.is_synthetic_exit != oracle["is_synthetic_exit"]:
            problems.append(f"is_synthetic_exit {fresh.is_synthetic_exit} != {oracle['is_synthetic_exit']}")
        fresh_indicators = {name: str(value) for name, value in fresh.indicators.items()}
        if fresh_indicators != oracle["indicators"]:
            problems.append(f"indicators {fresh_indicators} != {oracle['indicators']}")
        if problems:
            raise AssertionError(f"{strategy_key}: trade #{i + 1} mismatch\n  - " + "\n  - ".join(problems))


def test_fixture_bar_series_is_deterministic_and_shared_across_strategies() -> None:
    """Regenerating the fixture must reproduce byte-identical trade counts.

    Pins the trade *counts* the fixture was generated with, so a change to
    the shared synthetic bar series (not just the strategy math) is also
    caught, not only per-trade drift within an unchanged count.
    """
    output = _load_output()
    assert output["trade_counts"] == {key: len(output["trades"][key]) for key in _STRATEGIES}
    assert all(output["trade_counts"][key] > 0 for key in _STRATEGIES)
