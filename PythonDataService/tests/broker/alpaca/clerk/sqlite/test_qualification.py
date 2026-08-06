"""Bounded tests for the SQLite Clerk qualification evidence runner."""

from __future__ import annotations

from app.broker.alpaca.clerk.sqlite.qualification import (
    PROFILE_SCALES,
    qualification_markdown,
    run_performance_profile,
)


def test_full_profile_pins_required_scale_points() -> None:
    assert PROFILE_SCALES["full"] == (
        (1, 10_000),
        (10, 100_000),
        (100, 1_000_000),
    )


def test_bounded_profile_records_pragmas_sizes_latencies_and_index_plans() -> None:
    report = run_performance_profile("smoke", scales=((1, 100),))

    assert report["broker_dependency"] == "NONE"
    assert report["performance_budget"]["status"] == "PASSED"
    assert report["performance_budget"]["violations"] == []
    scale = report["scales"][0]
    assert (scale["bot_count"], scale["transition_count"]) == (1, 100)
    assert scale["pragmas"]["journal_mode"] == "wal"
    assert scale["pragmas"]["synchronous"] == 2
    assert scale["sizes_bytes"]["database"] > 0
    assert scale["sizes_bytes"]["mirror"] > 0
    assert scale["latencies"]["account_snapshot"]["sample_count"] == 12
    assert any(
        "ix_custody_transitions_strategy_sequence" in detail
        for detail in scale["query_plans"]["bot_timeline"]
    )
    markdown = qualification_markdown(report)
    assert "| 1 | 100 |" in markdown
    assert "offline, batched, hash-chained fixture builder" in markdown
    assert "Performance budget: `PASSED`" in markdown
