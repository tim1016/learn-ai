"""Real-fixture runtime parity tests against the captured QC export.

Activates when both:

1. ``tests/fixtures/golden/qc-precomputed-predictions/qc_export.json`` exists, AND
2. ``tests/research/ml/fixtures/qc_known_hashes.json`` carries the
   ``run_ledger_prediction_set_hash`` and ``result_hash`` keys.

Both conditions are met after this PR: the §B fixture lands and the
runtime hashes are pinned by capturing them from a one-shot run, then
asserting they reproduce on every subsequent CI invocation.

The runtime test:

* imports the captured ``qc_export.json`` into the v0.5 artifact format
  via ``import_qc_fixture`` (matching the §C-data parity test's
  provenance kwargs exactly, so the same ``prediction_set_hash`` lands);
* builds a synthetic SPY daily bar stream whose consolidator output
  fires at exactly the 243 QC prediction timestamps (one minute bar per
  trading day at NYSE-close anchor + a sentinel to flush the last day);
* constructs a minimal SPY-daily ``StrategySpec`` whose entry condition
  is a single ``PredictionComparison`` against the imported artifact;
* runs the spec via ``run_strategy_spec`` against the synthetic bars;
* asserts ``RunLedger.prediction_set_hash`` and ``RunLedger.result_hash``
  reproduce the pinned values stored in ``qc_known_hashes.json``.

The spec uses no indicators (so no warmup / price-history dependence)
and a 1-bar exit, so the only deterministic input to ``result_hash`` is
the (artifact, spec, bar stream, engine config) tuple.
"""

from __future__ import annotations

import json
from datetime import date as Date
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.engine.data.trade_bar import TradeBar
from app.engine.strategy.spec import StrategySpec
from app.engine.strategy.spec.tests._parity_helpers import FakeDataReader
from app.research.ml.generators.quantconnect_fixture import import_qc_fixture
from app.research.runs.runner import RunRequest, run_strategy_spec

_FIXTURE_DIR = (
    Path(__file__).resolve().parents[2]
    / "fixtures" / "golden" / "qc-precomputed-predictions"
)
_QC_EXPORT = _FIXTURE_DIR / "qc_export.json"
_KNOWN_HASHES_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "qc_known_hashes.json"
)
_SYMBOL = "SPY"
_PREDICTION_SET_ID = "qc_spy_precomputed_v001"
_NY = ZoneInfo("America/New_York")

_PROVENANCE = {
    "qc_tutorial_url": "https://www.quantconnect.com/docs/v2/writing-algorithms/importing-data/streaming-data/precomputed-ml-predictions",
    "qc_exported_at_ms": 1778443824165,
    "qc_calendar_window_start_ms": 1735851600000,
    "qc_calendar_window_end_ms": 1767214800000,
    "qc_dataset_id": "QuantConnect/USEquity-Daily",
    "qc_versions": {"sklearn": "1.6.1", "numpy": "1.26.4", "pandas": "2.3.3"},
}


def _runtime_hashes_pinned() -> bool:
    if not _QC_EXPORT.is_file() or not _KNOWN_HASHES_PATH.is_file():
        return False
    data = json.loads(_KNOWN_HASHES_PATH.read_text(encoding="utf-8"))
    return (
        "run_ledger_prediction_set_hash" in data
        and "result_hash" in data
    )


pytestmark = pytest.mark.skipif(
    not _runtime_hashes_pinned(),
    reason="QC runtime hashes not yet pinned — see §C runtime PR follow-up",
)


def _build_spy_daily_bars() -> list[TradeBar]:
    """One minute bar per QC prediction date + a sentinel to flush the last day.

    Each bar is anchored such that 1440-minute consolidation emits a daily
    bar with ``end_time = 16:00 America/New_York`` on the prediction date —
    matching the importer's anchor convention exactly.
    """
    raw = json.loads(_QC_EXPORT.read_text(encoding="utf-8"))
    dates: list[Date] = []
    for record in raw:
        if _SYMBOL in record["prediction_by_symbol"]:
            y, m, d = record["date"].split("-")
            dates.append(Date(int(y), int(m), int(d)))
    dates.sort()

    bars: list[TradeBar] = []
    flat_price = Decimal("400.00")
    for d in dates:
        bar_start = datetime(d.year, d.month, d.day, 9, 30, tzinfo=_NY)
        bar_end = datetime(d.year, d.month, d.day, 16, 0, tzinfo=_NY)
        bars.append(
            TradeBar(
                symbol=_SYMBOL,
                time=bar_start,
                end_time=bar_end,
                open=flat_price,
                high=flat_price,
                low=flat_price,
                close=flat_price,
                volume=100,
            )
        )

    sentinel_date = dates[-1] + timedelta(days=4)
    sentinel_start = datetime(
        sentinel_date.year, sentinel_date.month, sentinel_date.day, 9, 30, tzinfo=_NY
    )
    bars.append(
        TradeBar(
            symbol=_SYMBOL,
            time=sentinel_start,
            end_time=sentinel_start + timedelta(minutes=1),
            open=flat_price,
            high=flat_price,
            low=flat_price,
            close=flat_price,
            volume=100,
        )
    )
    return bars


@pytest.fixture
def qc_spy_data_factory():
    bars = _build_spy_daily_bars()

    def factory(symbol: str, start: Date, end: Date):
        return FakeDataReader(bars=bars)

    return factory


@pytest.fixture
def qc_artifacts_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the runner at a tmp-path-rooted artifact directory and import
    the QC fixture into it, so the runner's ``PredictionSet.load`` finds
    the same artifact the §C-data parity test pinned."""
    root = tmp_path / "artifacts" / "predictions"
    root.mkdir(parents=True)
    monkeypatch.setenv("LEARN_AI_PREDICTION_ARTIFACTS_ROOT", str(root))

    import_qc_fixture(
        qc_export_path=_QC_EXPORT,
        prediction_set_id=_PREDICTION_SET_ID,
        output_root=root,
        symbol=_SYMBOL,
        **_PROVENANCE,
    )
    return root


def _build_qc_spec() -> StrategySpec:
    return StrategySpec.model_validate(
        {
            "schema_version": "1.0",
            "name": "QC SPY Daily Prediction parity (no indicators)",
            "symbols": [_SYMBOL],
            "resolution": {"period_minutes": 1440},
            "indicators": [],
            "predictions": [
                {
                    "id": "qc_pred",
                    "prediction_set_id": _PREDICTION_SET_ID,
                    "field": "prediction",
                },
            ],
            "entry": {
                "logic": "AND",
                "conditions": [
                    {
                        "kind": "PredictionComparison",
                        "prediction": "qc_pred",
                        "op": ">",
                        "value": 0.0,
                    },
                ],
                "size": {"kind": "SetHoldings", "fraction": 1.0},
                "pyramiding": 1,
            },
            "position": {"kind": "EQUITY_LONG"},
            "survival": [],
            "exit": {
                "logic": "OR",
                "conditions": [{"kind": "BarsSinceEntry", "op": ">=", "value": 1}],
            },
        }
    )


def _run() -> tuple[str | None, str]:
    """Run the parity backtest in-memory; return (prediction_set_hash, result_hash).

    Implemented as a free function (not a pytest fixture) so the helper
    in test_qc_fixture_strategy_spec_run_ledger_hash_pinned can reuse it
    without forcing both runtime tests to share a single execution.
    """
    raise NotImplementedError(
        "tests build the run inline — see the test bodies below"
    )


def test_qc_fixture_strategy_spec_run_ledger_hash_pinned(
    qc_spy_data_factory, qc_artifacts_root: Path
) -> None:
    """RunLedger.prediction_set_hash threads through unchanged from the
    artifact's manifest; if the importer or artifact format ever drifts,
    this fails before result_hash even matters."""
    pinned = json.loads(_KNOWN_HASHES_PATH.read_text(encoding="utf-8"))
    expected_pred_hash = pinned["run_ledger_prediction_set_hash"]

    spec = _build_qc_spec()
    ledger, _ = run_strategy_spec(
        RunRequest(
            spec=spec,
            start_date=Date(2025, 1, 13),
            end_date=Date(2025, 12, 30),
        ),
        data_source_factory=qc_spy_data_factory,
        data_root_revision="qc-parity-fixture-v1",
    )

    assert ledger.status == "completed", ledger.failure_reason
    assert ledger.prediction_set_hash == expected_pred_hash, (
        f"RunLedger.prediction_set_hash drift: pinned={expected_pred_hash}, "
        f"got={ledger.prediction_set_hash}"
    )


def test_qc_fixture_strategy_spec_result_hash_pinned(
    qc_spy_data_factory, qc_artifacts_root: Path
) -> None:
    """result_hash is the SHA256 of the BacktestRunResult payload — pins
    the (artifact, spec, bars, engine config) tuple end-to-end. Drift here
    means *something* in the run pipeline changed semantics; investigate
    via the reconcile-backtest taxonomy before regenerating the pin."""
    pinned = json.loads(_KNOWN_HASHES_PATH.read_text(encoding="utf-8"))
    expected_result_hash = pinned["result_hash"]

    spec = _build_qc_spec()
    ledger, _ = run_strategy_spec(
        RunRequest(
            spec=spec,
            start_date=Date(2025, 1, 13),
            end_date=Date(2025, 12, 30),
        ),
        data_source_factory=qc_spy_data_factory,
        data_root_revision="qc-parity-fixture-v1",
    )

    assert ledger.status == "completed", ledger.failure_reason
    assert ledger.result_hash == expected_result_hash, (
        f"RunLedger.result_hash drift: pinned={expected_result_hash}, "
        f"got={ledger.result_hash}"
    )
