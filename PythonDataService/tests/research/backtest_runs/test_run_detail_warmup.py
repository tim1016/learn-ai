"""A run report says where the run's warmup began, so its strategy view can draw it (#2639 D13)."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date

import pytest

from app.research.backtest_runs.records import persisted_execution_configuration
from app.schemas.backtest_runs import BacktestRunDetailResponse
from app.utils.session_anchors import et_midnight_ms
from tests.research.backtest_runs.test_parity import _run


def test_the_run_report_gives_the_warmup_start_as_the_same_anchor_as_the_window() -> None:
    configuration = persisted_execution_configuration(
        compatibility_profile=None, warmup_from_date="2026-03-02", slippage_per_share=0.0
    )
    run = replace(_run(1, "engine"), execution_config_json=json.dumps(configuration))

    wire = BacktestRunDetailResponse.model_validate(run).model_dump(by_alias=True)

    assert wire["warmupFromDate"] == et_midnight_ms(date(2026, 3, 2))


@pytest.mark.parametrize(
    "stored",
    [
        None,
        json.dumps(persisted_execution_configuration(compatibility_profile=None, warmup_from_date=None, slippage_per_share=0.0)),
    ],
    ids=["no-configuration", "no-warmup"],
)
def test_a_run_that_read_no_history_before_its_window_has_no_warmup_start(stored: str | None) -> None:
    run = replace(_run(1, "engine"), execution_config_json=stored)

    assert BacktestRunDetailResponse.model_validate(run).model_dump(by_alias=True)["warmupFromDate"] is None
