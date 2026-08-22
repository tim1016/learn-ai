"""Golden-trace qualification test for the registry-backed RSI Signal Program.

Mirrors ``tests/engine/strategy/test_ema_signal_program.py::
test_validated_ema_settings_corpus_has_a_pinned_trace_root`` — see that
test's docstring for why this is the runtime admission gate (PRD S11.4):
a program edit that changes behavior without also updating the registry's
sealed ``golden_trace_root`` must fail here, not slip through as a
"qualified" receipt.
"""

from __future__ import annotations

import csv
import json
from decimal import Decimal
from pathlib import Path

from app.engine.data.trade_bar import TradeBar
from app.engine.engine import BacktestEngine
from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.engine.strategy.signal_program import trace_corpus_root, trace_root
from app.services.spec_strategy_runner import InMemoryDataReader


def test_validated_rsi_mean_reversion_settings_corpus_has_a_pinned_trace_root() -> None:
    fixture = Path(__file__).resolve().parents[2] / "fixtures/golden/rsi-mean-reversion-signal/v1/trace-corpus.json"
    corpus = json.loads(fixture.read_text(encoding="utf-8"))

    assert trace_corpus_root(corpus["entries"]) == corpus["trace_root"]
    assert len(corpus["entries"]) == 10
    cells_root = fixture.parents[2] / "cross-engine-studies/cells"
    registration = _STRATEGY_REGISTRY["rsi_mean_reversion"]
    contract = registration.signal_program_contract
    assert contract is not None
    assert corpus["trace_root"] == contract.golden_trace_root

    for entry in corpus["entries"]:
        cell = cells_root / entry["cell"]
        minute_bars: list[TradeBar] = []
        with (cell / "lean" / "observations.csv").open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                end_ms = int(row["ms_utc"])
                minute_bars.append(
                    TradeBar(
                        symbol=entry["settings"]["symbol"],
                        start_ms=end_ms - 60_000,
                        end_ms=end_ms,
                        open=Decimal(row["open"]),
                        high=Decimal(row["high"]),
                        low=Decimal(row["low"]),
                        close=Decimal(row["close"]),
                        volume=int(Decimal(row["volume"])),
                    )
                )
        strategy = registration.build(registration.param_schema(**entry["settings"]))
        BacktestEngine(InMemoryDataReader(minute_bars)).run(strategy)
        assert strategy.signal_program is not None
        assert len(strategy.signal_program.session.traces) == entry["trace_count"]
        assert trace_root(strategy.signal_program.session.traces) == entry["trace_root"]
