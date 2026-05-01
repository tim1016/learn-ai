"""Verify feature/signal runners emit the right phases and friendly logs.

These tests don't go through Redis or the Jobs framework — they exercise
the runners directly with mock callbacks, so a regression in the SSE
vocabulary fails fast without needing the container stack.
"""

from __future__ import annotations

import math
import random

import pytest

from app.research.runner import run_feature_research
from app.research.signal.config import SignalConfig
from app.research.signal.engine import run_signal_engine


def _synthetic_bars(n: int = 1500, seed: int = 1) -> list[dict]:
    """Generate enough bars for the runners to exercise every phase."""
    rng = random.Random(seed)
    bars: list[dict] = []
    price = 100.0
    ts = 1_700_000_000_000
    for i in range(n):
        ret = rng.gauss(0.0, 0.001)
        price = price * math.exp(ret)
        high = price * (1 + abs(rng.gauss(0, 0.0005)))
        low = price * (1 - abs(rng.gauss(0, 0.0005)))
        open_ = price * (1 + rng.gauss(0, 0.0002))
        bars.append(
            {
                "timestamp": ts + i * 60_000,
                "open": float(open_),
                "high": float(high),
                "low": float(low),
                "close": float(price),
                "volume": 1_000_000.0 + rng.random() * 100_000,
            }
        )
    return bars


class _Capture:
    """Accumulate phase ids, log lines, and progress tuples."""

    def __init__(self) -> None:
        self.phases: list[str] = []
        self.logs: list[tuple[str, str]] = []
        self.progress: list[tuple[int, int, str, str | None]] = []

    def on_phase(self, phase: str) -> None:
        self.phases.append(phase)

    def on_log(self, msg: str, level: str = "info") -> None:
        self.logs.append((level, msg))

    def on_progress(self, current: int, total: int, unit: str = "x", message: str | None = None) -> None:
        self.progress.append((current, total, unit, message))


class TestFeatureRunnerCallbacks:
    def test_emits_expected_phase_sequence(self) -> None:
        bars = _synthetic_bars()
        cap = _Capture()
        report = run_feature_research(
            ticker="TEST",
            feature_name="rsi_14",
            bars=bars,
            start_date="2024-01-01",
            end_date="2024-01-05",
            on_phase=cap.on_phase,
            on_log=cap.on_log,
            on_progress=cap.on_progress,
        )
        # The runner should set ``error`` to None on a healthy run.
        assert report.error is None
        # Phase sequence must include each documented stage in order.
        # The runner skips stationarity-side messages occasionally based
        # on series length, but the phase event itself is always emitted.
        expected_subsequence = [
            "compute_target",
            "compute_feature",
            "compute_ic",
            "stationarity",
            "quantile",
            "robustness",
            "validate",
        ]
        # Scan for the subsequence in cap.phases preserving order.
        idx = 0
        for phase in cap.phases:
            if idx < len(expected_subsequence) and phase == expected_subsequence[idx]:
                idx += 1
        assert idx == len(expected_subsequence), (
            f"missing phases — saw: {cap.phases}, expected: {expected_subsequence}"
        )

    def test_logs_include_friendly_messages(self) -> None:
        bars = _synthetic_bars()
        cap = _Capture()
        run_feature_research(
            ticker="TEST",
            feature_name="momentum_5m",
            bars=bars,
            start_date="2024-01-01",
            end_date="2024-01-05",
            on_log=cap.on_log,
        )
        joined = "\n".join(msg for _, msg in cap.logs).lower()
        assert "information coefficient" in joined
        assert "stationarity" in joined or "stationary" in joined
        assert "validation" in joined or "verdict" in joined

    def test_cancel_callback_propagates(self) -> None:
        """When the cancel_check raises, the runner should re-raise it."""

        class FakeJobCancelled(Exception):
            """Same name pattern run_feature_research sniffs by."""

        # Rename so type(e).__name__ matches the framework's expectation.
        FakeJobCancelled.__name__ = "JobCancelled"

        def cancel() -> bool:
            raise FakeJobCancelled("user requested")

        bars = _synthetic_bars(n=600)
        with pytest.raises(FakeJobCancelled):
            run_feature_research(
                ticker="TEST",
                feature_name="rsi_14",
                bars=bars,
                start_date="2024-01-01",
                end_date="2024-01-05",
                cancel_check=cancel,
            )


class TestSignalEngineCallbacks:
    def test_emits_expected_phase_sequence(self) -> None:
        bars = _synthetic_bars(n=2500)
        cap = _Capture()
        report = run_signal_engine(
            ticker="TEST",
            feature_name="rsi_14",
            bars=bars,
            start_date="2024-01-01",
            end_date="2024-01-10",
            config=SignalConfig(feature_name="rsi_14", regime_gate_enabled=False),
            on_phase=cap.on_phase,
            on_log=cap.on_log,
            on_progress=cap.on_progress,
        )
        # error may be set for synthetic data — that's fine; we only
        # care that the phase sequence got far enough to be useful.
        expected = [
            "compute_feature",
            "diagnostics",
            "regime_coverage",
            "backtest_grid",
        ]
        idx = 0
        for phase in cap.phases:
            if idx < len(expected) and phase == expected[idx]:
                idx += 1
        assert idx == len(expected), (
            f"signal engine missed phases — saw: {cap.phases}; report.error: {report.error}"
        )
