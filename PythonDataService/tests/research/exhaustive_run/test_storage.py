"""Persistence and traversal tests for Exhaustive Run artifacts."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.research.exhaustive_run.errors import ExhaustiveRunNotFoundError
from app.research.exhaustive_run.result import ExhaustiveRunConfig, ExhaustiveRunResult
from app.research.exhaustive_run.storage import (
    load_exhaustive_run,
    load_latest_for_walk_forward,
    save_exhaustive_run,
)


def _artifact(identifier: str, source: str, created_at_ms: int) -> tuple[ExhaustiveRunConfig, ExhaustiveRunResult]:
    config = ExhaustiveRunConfig(
        exhaustive_run_id=identifier,
        source_walk_forward_id=source,
        source_protocol_id="spy-ema-normalized-gap",
        source_protocol_version="1.0",
        start_ms=1,
        recent_window_start_ms=2,
        end_ms=3,
        initial_cash=100_000.0,
        fill_mode="next_bar_open",
        commission_per_order=0.0,
        slippage_per_share=0.0,
        random_seed=0,
        data_root_revision="revision",
        created_at_ms=created_at_ms,
    )
    result = ExhaustiveRunResult(
        exhaustive_run_id=identifier,
        source_walk_forward_id=source,
        selections=[],
        candidates=[],
        status="failed",
        failure_reason="fixture",
        created_at_ms=created_at_ms,
        completed_at_ms=created_at_ms + 1,
    )
    return config, result


def test_storage_round_trips_and_loads_latest_by_source(tmp_path: Path) -> None:
    source = "b" * 32
    older = _artifact("a" * 32, source, 10)
    newer = _artifact("c" * 32, source, 20)
    save_exhaustive_run(*older, root=tmp_path)
    save_exhaustive_run(*newer, root=tmp_path)

    assert load_exhaustive_run("a" * 32, root=tmp_path) == older
    assert load_latest_for_walk_forward(source, root=tmp_path) == newer


def test_storage_rejects_path_traversal_and_missing_source(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="artifact_id"):
        load_exhaustive_run("../escape", root=tmp_path)
    with pytest.raises(ExhaustiveRunNotFoundError):
        load_latest_for_walk_forward("d" * 32, root=tmp_path)
