"""The universe block: every requested member is accounted for exactly once (#3524, #3527; PRD #3490 T18).

The job tests prove the block end to end; these pin the accounting rules themselves, including the two that no job
reaches: a row that is not valid but names no reason, and the shape of a degraded member.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.research.study_universe import MemberExclusion, members_summary, universe_of_rows, universe_record


def inputs(*, rate_days_fetch_failed: int = 0) -> dict[str, Any]:
    return {"risk_free_rate": {"days_fetch_failed": rate_days_fetch_failed}, "degraded": bool(rate_days_fetch_failed)}


def valid(ticker: str, option_inputs: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"ticker": ticker, "validity": "valid", "exclusion": None, "option_inputs": option_inputs or inputs()}


def excluded(ticker: str, reason: str = "no bars") -> dict[str, Any]:
    exclusion = MemberExclusion(ticker, "stock_bars", "data_missing", reason, retryable=True).as_dict()
    return {"ticker": ticker, "validity": "invalid_data", "exclusion": exclusion, "option_inputs": None, "error": reason}


def test_every_requested_member_is_in_exactly_one_of_evaluated_or_excluded_and_the_denominator_is_the_evaluated_count():
    rows = [valid("SPY"), excluded("IWM"), valid("QQQ")]

    block = universe_of_rows(["SPY", "IWM", "QQQ"], rows)

    assert block["evaluated"] == ["SPY", "QQQ"] and [e["ticker"] for e in block["excluded"]] == ["IWM"]
    assert sorted(block["evaluated"] + [e["ticker"] for e in block["excluded"]]) == sorted(block["requested"])
    assert (block["requested_count"], block["evaluated_count"], block["excluded_count"], block["denominator"]) == (3, 2, 1, 2)
    assert block["complete"] is False


def test_a_member_in_the_statistics_is_degraded_for_a_fred_outage_and_the_universe_is_then_not_complete():
    """FRED is the one live input left; every option input the lake supplies either arrives whole or excludes the member."""
    rows = [valid("SPY"), valid("QQQ", inputs(rate_days_fetch_failed=5)), valid("IWM", inputs(rate_days_fetch_failed=2))]

    block = universe_of_rows(["SPY", "QQQ", "IWM"], rows)

    assert block["evaluated"] == ["SPY", "QQQ", "IWM"] and block["excluded"] == []
    assert block["degraded"] == [
        {"ticker": "QQQ", "rate_days_fetch_failed": 5},
        {"ticker": "IWM", "rate_days_fetch_failed": 2},
    ]
    assert block["complete"] is False


def test_a_universe_in_which_every_member_is_evaluated_and_none_degraded_is_complete():
    assert universe_of_rows(["SPY", "QQQ"], [valid("SPY"), valid("QQQ")])["complete"] is True


def test_a_member_that_is_not_valid_but_names_no_reason_is_a_bug_not_a_silent_omission():
    row = {"ticker": "IWM", "validity": "error", "exclusion": None, "option_inputs": None}

    with pytest.raises(RuntimeError, match="IWM is error but carries no exclusion"):
        universe_of_rows(["IWM"], [row])


def test_a_listing_names_each_member_but_reports_the_total_beside_a_truncated_one():
    many = [{"ticker": f"T{i}", "reason": "r"} for i in range(60)]

    block = universe_record([f"T{i}" for i in range(60)], evaluated=[], excluded=many, degraded=[])

    assert len(block["excluded"]) == 50 and block["excluded_count"] == 60


def test_a_summary_counts_the_members_it_does_not_name():
    five = [(f"T{i}", "r") for i in range(5)]

    assert members_summary(five).endswith("And 2 more.") and "T3" not in members_summary(five)
    assert "more" not in members_summary(five[:3])
