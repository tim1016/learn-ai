"""Smoke coverage for the #1801 read-latency bench.

The bench is a measurement tool, not app logic — what this pins is that it
keeps *running* against the real read path (a route rename, registry-surface
change, or facade-activation change must fail here, not silently rot the
bench), and that its #1776 purity fence stays wired.
"""

from __future__ import annotations

import json
import os

import pytest

from scripts.bench_panel_read_latency import main


@pytest.fixture(autouse=True)
def _restore_live_runs_root():
    """The bench points the settings-backed artifacts root at its fixture;
    put both the env var and the cached settings back so sibling tests never
    see the bench's temp root."""
    from app.broker.ibkr.config import reset_settings_for_testing

    before = os.environ.get("IBKR_LIVE_RUNS_ROOT")
    yield
    if before is None:
        os.environ.pop("IBKR_LIVE_RUNS_ROOT", None)
    else:
        os.environ["IBKR_LIVE_RUNS_ROOT"] = before
    reset_settings_for_testing()


def test_bench_runs_at_small_scale_and_reads_stay_pure(capsys) -> None:
    exit_code = main(
        [
            "--rows",
            "4",
            "--requests",
            "2",
            "--rounds",
            "1",
            "--concurrency",
            "3",
            "--json",
        ]
    )

    assert exit_code == 0
    document = json.loads(capsys.readouterr().out)
    assert isinstance(document, list) and len(document) == 1  # one valid JSON doc
    payload = document[0]
    assert payload["rows"] == 4
    # Every surface produced samples — both panel lifecycle strata included.
    for key in (
        "catalog_sequential",
        "panel_sequential_stopped",
        "panel_sequential_running",
        "panel_concurrent",
        "catalog_concurrent",
    ):
        assert payload[key]["n"] > 0
        assert payload[key]["p50_ms"] >= 0
    # ...and the #1776 purity fence held: no broker contact during reads.
    assert payload["broker_calls_during_bench"] == 0
