"""Unit tests for LEAN log classification.

These tests assert the contract the launcher promises to callers:
``ClassifiedErrors`` categories are stable strings; ``is_clean`` only
returns True for a genuinely empty log.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.lean_sidecar.result_classifier import (
    ClassifiedErrors,
    classify_lean_log,
    classify_workspace,
)

# Representative log shapes harvested from real Phase 1b LEAN runs so
# the classifier is exercised against shapes we've actually seen, not
# only synthetic ones.
ANALYSIS_FAILED_LOG = """\
20260517 17:12:55.956 TRACE:: Engine.Main(): Started 5:12 PM
20260517 17:12:59.355 ERROR:: BacktestingResultHandler.SendFinalResult(): Error running backtest analysis System.NullReferenceException: Object reference not set to an instance of an object.
   at QuantConnect.Lean.Engine.Results.Analysis.Analyses.ParameterCountAnalysis.Run
   at QuantConnect.Lean.Engine.Results.Analysis.ResultsAnalyzer.Run
20260517 17:12:59.358 TRACE:: Engine.Main(): Analysis Complete.
"""

FAILED_DATA_REQUEST_LOG = """\
20260517 17:12:59.263 ERROR:: SubscriptionDataSourceReader.InvalidSource(): File not found: /lean-run/data/equity/usa/daily/spy.zip
"""

RUNTIME_ERROR_LOG = """\
20260517 17:10:18.448 ERROR:: Algorithm.Initialize() Error: Unable to locate symbol properties file
"""

MIXED_LOG = ANALYSIS_FAILED_LOG + FAILED_DATA_REQUEST_LOG + RUNTIME_ERROR_LOG

CLEAN_LOG = """\
20260517 17:12:54.511 TRACE:: Using /lean-run/project/config.json as configuration file
20260517 17:12:54.575 TRACE:: Composer(): Loading Assemblies
20260517 17:12:55.956 TRACE:: Engine.Main(): LEAN ALGORITHMIC TRADING ENGINE v2.5.0.0
20260517 17:12:59.358 TRACE:: Engine.Main(): Analysis Complete.
"""


class TestClassifyLeanLog:
    def test_clean_log_returns_clean(self) -> None:
        result = classify_lean_log(CLEAN_LOG)
        assert result.total == 0
        assert result.is_clean
        assert result.is_reconciliation_grade
        assert result.by_category == {}

    def test_analysis_failed_category(self) -> None:
        result = classify_lean_log(ANALYSIS_FAILED_LOG)
        assert "analysis_failed" in result.by_category
        assert not result.is_clean
        # The classifier captures the stack-trace continuation lines.
        block = result.by_category["analysis_failed"][0]
        assert "ResultsAnalyzer" in block or "ParameterCountAnalysis" in block

    def test_failed_data_requests_category(self) -> None:
        result = classify_lean_log(FAILED_DATA_REQUEST_LOG)
        assert "failed_data_requests" in result.by_category
        assert not result.is_clean

    def test_runtime_error_category(self) -> None:
        result = classify_lean_log(RUNTIME_ERROR_LOG)
        assert "runtime_error" in result.by_category
        assert not result.is_clean

    def test_mixed_log_separates_categories(self) -> None:
        result = classify_lean_log(MIXED_LOG)
        assert set(result.by_category.keys()) == {
            "analysis_failed",
            "failed_data_requests",
            "runtime_error",
        }
        assert result.total == 3
        assert not result.is_clean

    def test_unknown_error_bucketed_as_other(self) -> None:
        log = "20260517 12:00:00.000 ERROR:: something we have not seen before\n"
        result = classify_lean_log(log)
        assert "other" in result.by_category

    def test_classify_workspace_missing_log_is_clean(self, tmp_path: Path) -> None:
        # A run that died before LEAN wrote its log still gets an
        # empty ClassifiedErrors — the launcher distinguishes
        # "no log" from "no errors" itself.
        result = classify_workspace(tmp_path / "log.txt")
        assert result.is_clean
        assert result.total == 0

    def test_classify_workspace_reads_real_file(self, tmp_path: Path) -> None:
        log_path = tmp_path / "log.txt"
        log_path.write_text(MIXED_LOG, encoding="utf-8")
        result = classify_workspace(log_path)
        assert result.total == 3


class TestClassifiedErrorsContract:
    def test_categories_are_sorted_for_stable_serialization(self) -> None:
        errors = ClassifiedErrors(
            by_category={
                "runtime_error": ["x"],
                "analysis_failed": ["y"],
            }
        )
        assert errors.categories == ["analysis_failed", "runtime_error"]

    @pytest.mark.parametrize(
        "log",
        [CLEAN_LOG, ""],
    )
    def test_empty_inputs_are_clean(self, log: str) -> None:
        assert classify_lean_log(log).is_clean
