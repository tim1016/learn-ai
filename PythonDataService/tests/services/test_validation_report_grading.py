"""The CSV comparison report grades only sound evidence (#2461).

A comparison is graded only when its rows align one-to-one on a shared,
unique time column and enough of the aligned rows carry comparable
values. Duplicate time keys invalidate the comparison outright; pairing
rows by position is explicitly unverified; thin coverage withholds the
grade and names the minimum it failed.
"""

from __future__ import annotations

from app.services.validation_service import generate_validation_report


def _csv(text: str) -> bytes:
    return text.strip().lstrip("\n").encode()


def _our_csv(rows: int, *, value: float = 100.0) -> bytes:
    lines = ["unix_ts,close,ema10"]
    lines += [f"{t},101.0,{value}" for t in range(rows)]
    return _csv("\n".join(lines))


def _tv_csv(rows: int, *, value: float = 100.0, missing_from: int = 0) -> bytes:
    lines = ["unix_ts,close,ema10"]
    for t in range(rows):
        cell = "" if t < missing_from else str(value)
        lines.append(f"{t},101.0,{cell}")
    return _csv("\n".join(lines))


def test_thin_coverage_withholds_the_grade_and_names_the_minimum() -> None:
    """1,000 aligned rows where TradingView is missing 999 values cannot grade.

    On master the single surviving pair is 100% exact, so the report
    says Excellent about a comparison that compared one row.
    """
    report = generate_validation_report(
        _our_csv(1_000), _tv_csv(1_000, missing_from=999), "SPY"
    )

    assert "Excellent" not in report
    assert "insufficient comparable evidence" in report
    assert "80% minimum" in report
    assert "Overall grade" in report  # the withheld grade is explained, not absent


def test_duplicate_time_keys_invalidate_the_comparison_with_counts() -> None:
    """A repeated time value in either file means rows cannot pair one-to-one.

    On master the duplicate keys inflate the merged row count past both
    file sizes: the match rate reads 102.0% and the unmatched counts go
    negative. After the fix there is no rate to read — the comparison is
    invalid and the duplicates are counted.
    """
    our_lines = ["unix_ts,close,ema10"]
    our_lines += [f"{t},101.0,100.0" for t in range(99)]
    our_lines.append("1,101.0,100.0")  # unix_ts=1 twice on our side
    tv_lines = ["unix_ts,close,ema10"]
    tv_lines += [f"{t},101.0,100.0" for t in range(100)]
    tv_lines.append("1,101.0,100.0")  # unix_ts=1 twice on their side

    report = generate_validation_report(_csv("\n".join(our_lines)), _csv("\n".join(tv_lines)), "SPY")

    assert "Comparison invalid" in report
    assert "| pandas-ta (ours) | 100 | 1 |" in report
    assert "| TradingView | 101 | 1 |" in report
    assert "Overall grade" not in report
    assert "102" not in report  # no inflated match rate exists to show


def test_positional_pairing_is_labelled_unverified_and_gets_no_grade() -> None:
    """Without a shared time column the numbers are a diagnostic, not a verdict."""
    ours = _csv("close,ema10\n101.0,100.0\n102.0,100.5\n103.0,101.0")
    tv = _csv("close,ema10\n101.0,100.0\n102.0,100.5\n103.0,101.0")

    report = generate_validation_report(ours, tv, "SPY")

    assert "unverified" in report
    assert "paired by position" in report
    assert "Excellent" not in report
    assert "Significant divergence" not in report


def test_per_field_table_shows_missing_counts_on_each_side_and_coverage() -> None:
    report = generate_validation_report(
        _our_csv(1_000), _tv_csv(1_000, missing_from=999), "SPY"
    )

    assert "Missing (ours)" in report
    assert "Missing (TradingView)" in report
    assert "Missing (both)" in report
    assert "Coverage" in report
    assert "| ema10 | 1,000 | 1 | 0.1% | 0 | 999 | 0 |" in report


def test_identical_fully_aligned_files_still_grade_excellent() -> None:
    report = generate_validation_report(_our_csv(50), _tv_csv(50), "SPY")

    assert "Excellent" in report


def test_aligned_files_with_different_values_still_grade_significant_divergence() -> None:
    report = generate_validation_report(_our_csv(50), _tv_csv(50, value=200.0), "SPY")

    assert "Significant divergence" in report


def test_match_rate_never_exceeds_100_and_unmatched_never_negative() -> None:
    """A sound overlap smaller than either file reports bounded numbers."""
    tv = _csv("unix_ts,close,ema10\n" + "\n".join(f"{t},101.0,100.0" for t in range(60)))
    report = generate_validation_report(_our_csv(100), tv, "SPY")

    assert "Match rate | 60.0%" in report
    assert "Unmatched pandas-ta rows | 40" in report
    assert "Unmatched TradingView rows | 0" in report
