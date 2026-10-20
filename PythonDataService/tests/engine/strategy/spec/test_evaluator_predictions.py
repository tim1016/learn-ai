"""Per-ref lookup dispatch in SpecAlgorithm._on_consolidated_bar.

These tests exercise the evaluator's prediction-row selection branches:
- exact_bar_close (default) reads the row at bar.end_time_ms.
- next_after_bar_close reads the smallest-key row with ts > bar.end_time_ms.
- PredictionLookupError raises if the resolved row is missing OR lacks the
  declared field. This is the runtime backstop — the coverage check is the
  intended first-line guard, but evaluator behavior must fail loudly rather
  than silently produce False conditions.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.engine.data.trade_bar import TradeBar
from app.engine.execution.portfolio import Portfolio
from app.engine.strategy.base import StrategyContext
from app.engine.strategy.spec.evaluator import SpecAlgorithm
from app.engine.strategy.spec.schema import StrategySpec
from app.research.ml.artifact import (
    ChunkRef,
    DeterministicRuleGenerator,
    PredictionSetManifest,
)
from app.research.ml.loader import PredictionLookupError, PredictionSet

NY = ZoneInfo("America/New_York")


def _make_pset(rows: list[tuple[int, dict[str, float]]]) -> PredictionSet:
    """Build a PredictionSet directly from (ts_ms, row_dict) pairs."""
    manifest = PredictionSetManifest(
        schema_version="1.0",
        prediction_set_id="test",
        symbol="AAPL",
        resolution_minutes=1440,
        field_names=["prediction"],
        warmup_policy="neutral_zero_until_feature_ready",
        generator=DeterministicRuleGenerator(kind="deterministic_rule", rule_id="x", rule_version="1.0"),
        chunks=[
            ChunkRef(
                trained_through_ms=min(r[0] for r in rows) - 1,
                start_ms=min(r[0] for r in rows),
                end_ms=max(r[0] for r in rows),
                row_count=len(rows),
                rows_hash="0" * 64,
            )
        ],
        prediction_set_hash="0" * 64,
    )
    index = {ts: dict(row) for ts, row in rows}
    return PredictionSet(manifest=manifest, index=index)


def _spec_with_lookup(lookup: str) -> StrategySpec:
    """Single-symbol AAPL spec using the given lookup mode."""
    return StrategySpec.model_validate(
        {
            "schema_version": "1.0",
            "name": f"test lookup={lookup}",
            "symbols": ["AAPL"],
            "resolution": {"period_minutes": 1440},
            "indicators": [],
            "predictions": [
                {
                    "id": "p",
                    "prediction_set_id": "test",
                    "field": "prediction",
                    "lookup": lookup,
                },
            ],
            "entry": {
                "logic": "AND",
                "conditions": [
                    {
                        "kind": "PredictionComparison",
                        "prediction": "p",
                        "op": ">",
                        "value": 0.0,
                    }
                ],
                "size": {"kind": "SetHoldings", "fraction": 1.0},
                "pyramiding": 1,
            },
            "position": {"kind": "EQUITY_LONG"},
            "survival": [],
            "exit": {
                "logic": "OR",
                "conditions": [
                    {
                        "kind": "PredictionComparison",
                        "prediction": "p",
                        "op": "<=",
                        "value": 0.0,
                    }
                ],
            },
        }
    )


def _make_bar(end_time: datetime) -> TradeBar:
    """Minimal TradeBar with .end_time set.

    Price values don't matter for these tests (PredictionComparison reads
    the prediction value, not bar price). 1-minute span before end_time
    keeps semantics sensible.
    """
    return TradeBar(
        symbol="AAPL",
        time=end_time - timedelta(minutes=1),
        end_time=end_time,
        open=Decimal("100"),
        high=Decimal("100"),
        low=Decimal("100"),
        close=Decimal("100"),
        volume=10_000,
    )


def _ts_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _run_one_bar(algo: SpecAlgorithm, bar_end_time: datetime) -> None:
    """Initialize the algorithm enough to feed one consolidated bar.

    Constructs a minimal StrategyContext that satisfies the evaluator's
    reads, then invokes _on_consolidated_bar directly, bypassing the
    engine's full event loop.

    ``ctx.current_time_ms`` must be set before calling _on_consolidated_bar so
    that any set_holdings / liquidate call (triggered by entry/exit) passes
    the ``assert self.current_time is not None`` guard in StrategyContext.
    """
    portfolio = Portfolio(initial_cash=Decimal("100000"))
    # Seed reference price so set_holdings can compute share delta.
    portfolio.reference_price["AAPL"] = Decimal("100")
    ctx = StrategyContext(portfolio=portfolio)
    algo.ctx = ctx
    algo.initialize()
    ctx.current_time_ms = _ts_ms(bar_end_time)
    algo._on_consolidated_bar(_make_bar(bar_end_time))


@pytest.mark.parametrize(
    "lookup,bar_end_prediction,next_prediction",
    [
        # With lookup='next_after_bar_close', the evaluator reads the row whose
        # timestamp is strictly greater than the bar's end_time_ms.
        # Setup: the bar's own row has 'prediction' = -1.0 (negative), the next
        # row has +1.0. Spec entry fires when prediction > 0.0. If the evaluator
        # wrongly used exact-match, entry would NOT fire. With
        # next_after_bar_close, the evaluator reads +1.0 at the next row and
        # entry fires.
        pytest.param("next_after_bar_close", -1.0, 1.0, id="next_row_at_decision_time_under_next_after_lookup"),
        # With lookup='exact_bar_close', the evaluator reads the row keyed at
        # bar.end_time_ms.
        # Same data layout but the exact-row value is what gets read. Here the
        # bar's own row has +1.0, so entry fires under exact_bar_close (and would
        # NOT under next_after because the next row's value is -1.0).
        pytest.param("exact_bar_close", 1.0, -1.0, id="exact_row_under_exact_bar_close_lookup"),
    ],
)
def test_evaluator_consumes_the_row_its_lookup_selects(
    lookup: str, bar_end_prediction: float, next_prediction: float
) -> None:
    bar_end = datetime(2026, 2, 9, 16, 0, tzinfo=NY)
    next_bar_end = datetime(2026, 2, 10, 16, 0, tzinfo=NY)

    pset = _make_pset(
        [
            (_ts_ms(bar_end), {"prediction": bar_end_prediction}),
            (_ts_ms(next_bar_end), {"prediction": next_prediction}),
        ]
    )
    algo = SpecAlgorithm(_spec_with_lookup(lookup), prediction_set=pset)
    _run_one_bar(algo, bar_end)

    assert algo._in_position is True, f"entry condition should have fired under {lookup} (selected-row prediction +1.0 > 0)"


@pytest.mark.parametrize(
    "lookup,rows,match",
    [
        # Runtime backstop: if coverage somehow let a bar through that has no
        # successor prediction, the evaluator must raise PredictionLookupError
        # rather than silently produce a False PredictionComparison.
        # Only row; no successor.
        pytest.param(
            "next_after_bar_close",
            (("bar_end", "prediction", 1.0),),
            r"next_after_bar_close.*no row strictly after",
            id="next_after_row_absent",
        ),
        # Runtime backstop for the exact-match branch: missing row at the bar's
        # own ts raises with a descriptive message rather than silently producing
        # False. Row at ts+1, not ts.
        pytest.param(
            "exact_bar_close",
            (("bar_end_plus_1ms", "prediction", 1.0),),
            r"exact_bar_close.*no row at ts_ms",
            id="exact_bar_close_row_absent",
        ),
        # Runtime backstop: row exists but lacks the declared field. Catches a
        # coverage bypass on the field-presence check.
        # next_after_bar_close finds row at next_bar_end, but it lacks 'prediction'.
        pytest.param(
            "next_after_bar_close",
            (("bar_end", "prediction", 0.0), ("next_bar_end", "other_field", 0.0)),
            r"missing declared field 'prediction'",
            id="resolved_row_missing_declared_field",
        ),
    ],
)
def test_evaluator_raises_when_the_resolved_row_or_field_is_absent(
    lookup: str, rows: tuple[tuple[str, str, float], ...], match: str
) -> None:
    bar_end = datetime(2026, 2, 9, 16, 0, tzinfo=NY)
    next_bar_end = datetime(2026, 2, 10, 16, 0, tzinfo=NY)
    ts_by_name = {
        "bar_end": _ts_ms(bar_end),
        "bar_end_plus_1ms": _ts_ms(bar_end) + 1,
        "next_bar_end": _ts_ms(next_bar_end),
    }
    pset = _make_pset([(ts_by_name[at], {field: value}) for at, field, value in rows])
    algo = SpecAlgorithm(_spec_with_lookup(lookup), prediction_set=pset)

    with pytest.raises(PredictionLookupError, match=match):
        _run_one_bar(algo, bar_end)
