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

# The SPY-zip miss + ``BacktestingResultHandler.SendFinalResult`` cascade
# below is the *benign* benchmark-unavailable pattern, NOT a real
# data-request failure. Engine Lab doesn't stage LEAN's default benchmark
# daily zip, so any run that doesn't call ``SetBenchmark`` trips it; the
# strategy itself produced trades and STATISTICS::. These two log shapes
# now route to the ``benchmark_unavailable`` bucket and ``is_clean=True``.
BENIGN_SPY_BENCHMARK_MISS_LOG = """\
20260517 17:12:59.263 ERROR:: SubscriptionDataSourceReader.InvalidSource(): File not found: /lean-run/data/equity/usa/daily/spy.zip
"""

BENIGN_EQUITY_CURVE_CASCADE_LOG = """\
20260517 17:12:59.355 ERROR:: BacktestingResultHandler.SendFinalResult(): Error running backtest analysis System.InvalidOperationException: Sequence contains no elements
   at System.Linq.ThrowHelper.ThrowNoElementsException()
   at QuantConnect.Lean.Engine.Results.BacktestingResultHandler.ReadEquityCurve()
"""

# A real failed data request — different symbol, no equity-curve cascade.
# This must still gate so the classifier doesn't silently swallow genuine
# missing-data failures alongside the benign benchmark cascade.
REAL_FAILED_DATA_REQUEST_LOG = """\
20260517 17:12:59.263 ERROR:: SubscriptionDataSourceReader.InvalidSource(): File not found: /lean-run/data/equity/usa/daily/qqq.zip
"""

RUNTIME_ERROR_LOG = """\
20260517 17:10:18.448 ERROR:: Algorithm.Initialize() Error: Unable to locate symbol properties file
"""

# Trade-bar-only capture: LEAN subscribes to quote bars automatically; our
# captures only include trade bars. A missing _quote.zip is benign (non-gating).
TRADE_ONLY_QUOTE_MISS_LOG = """\
20260522 04:07:59.306 ERROR:: SubscriptionDataSourceReader.InvalidSource(): File not found: /lean-run/data/equity/usa/minute/spy/20251103_quote.zip
"""

# A zero-reference-price dividend error is NOT benign: it kills the data
# subscription worker and truncates the backtest. It must gate (runtime_error).
DIVIDEND_ZERO_REF_PRICE_LOG = """\
20260522 04:07:59.351 ERROR:: <>c__DisplayClass1_0.<CreateAndScheduleWorker>b__0(): Subscription worker task exception SPY,#0,SPY,Minute,QuoteBar,Quote,Raw,OpenInterest. System.InvalidOperationException: Zero reference price for SPY dividend at 12/22/2025 12:00:00 AM
   at QuantConnect.Lean.Engine.DataFeeds.Enumerators.DividendEventProvider.GetEvents(NewTradableDateEventArgs eventArgs)+MoveNext()
"""

MIXED_LOG = ANALYSIS_FAILED_LOG + REAL_FAILED_DATA_REQUEST_LOG + RUNTIME_ERROR_LOG

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
        """A ``ResultsAnalyzer`` NullReferenceException still routes to
        ``analysis_failed`` and gates — only the specific
        ``ReadEquityCurve`` + ``Sequence contains no elements`` cascade
        gets absorbed by ``benchmark_unavailable``."""
        result = classify_lean_log(ANALYSIS_FAILED_LOG)
        assert "analysis_failed" in result.by_category
        assert "benchmark_unavailable" not in result.by_category
        assert not result.is_clean
        # The classifier captures the stack-trace continuation lines.
        block = result.by_category["analysis_failed"][0]
        assert "ResultsAnalyzer" in block or "ParameterCountAnalysis" in block

    @pytest.mark.parametrize(
        ("log", "category", "absent_category", "is_clean"),
        [
            # A real ``daily/<symbol>.zip`` miss (not SPY) still gates.
            #
            # Regression coverage for the narrow benign-pattern rule — the
            # benchmark-unavailable bucket must NOT swallow genuine
            # data-request failures for non-benchmark symbols.
            pytest.param(
                REAL_FAILED_DATA_REQUEST_LOG,
                "failed_data_requests",
                "benchmark_unavailable",
                False,
                id="real_failed_data_request_still_gates",
            ),
            # SPY-zip miss alone routes to ``benchmark_unavailable``.
            pytest.param(
                BENIGN_SPY_BENCHMARK_MISS_LOG,
                "benchmark_unavailable",
                "failed_data_requests",
                True,
                id="spy_benchmark_zip_miss_is_benign",
            ),
            # Missing _quote.zip files (trade-bar-only captures) are non-gating.
            pytest.param(
                TRADE_ONLY_QUOTE_MISS_LOG,
                "trade_only_capture",
                "failed_data_requests",
                True,
                id="quote_zip_miss_routes_to_trade_only_capture",
            ),
            # A zero-reference-price dividend error gates: it truncates the backtest.
            #
            # Regression coverage — an earlier revision classified this as the
            # non-gating ``trade_only_capture``, masking a SPY parity run that
            # executed only ~35 of 123 trading days.
            pytest.param(
                DIVIDEND_ZERO_REF_PRICE_LOG,
                "runtime_error",
                "trade_only_capture",
                False,
                id="zero_reference_price_dividend_error_gates",
            ),
        ],
    )
    def test_single_pattern_log_routes_to_its_category(
        self, log: str, category: str, absent_category: str, is_clean: bool
    ) -> None:
        result = classify_lean_log(log)
        assert category in result.by_category, f"log routes to {category}"
        assert absent_category not in result.by_category, f"log does not route to {absent_category}"
        assert result.is_clean == is_clean, "is_clean follows whether the category gates"
        # Reconciliation still disqualifies — alpha/beta are zero
        # without the benchmark.
        assert not result.is_reconciliation_grade, "any error, gating or not, disqualifies reconciliation"

    @pytest.mark.parametrize(
        ("log", "expected_counts", "is_clean"),
        [
            # SPY-zip miss + equity-curve cascade both route to
            # ``benchmark_unavailable`` and the run is reported clean.
            pytest.param(
                BENIGN_SPY_BENCHMARK_MISS_LOG + BENIGN_EQUITY_CURVE_CASCADE_LOG,
                (("benchmark_unavailable", 2),),
                True,
                id="spy_benchmark_cascade_only_is_clean",
            ),
            # Quote-zip misses stay non-gating; a co-occurring zero-reference-price
            # dividend error still gates the whole run.
            pytest.param(
                TRADE_ONLY_QUOTE_MISS_LOG * 34 + DIVIDEND_ZERO_REF_PRICE_LOG * 3,
                (("trade_only_capture", 34), ("runtime_error", 3)),
                False,
                id="quote_miss_clean_but_dividend_error_gates",
            ),
            pytest.param(
                MIXED_LOG,
                (("analysis_failed", 1), ("failed_data_requests", 1), ("runtime_error", 1)),
                False,
                id="mixed_log_separates_categories",
            ),
        ],
    )
    def test_multi_error_log_separates_categories(
        self, log: str, expected_counts: tuple[tuple[str, int], ...], is_clean: bool
    ) -> None:
        result = classify_lean_log(log)
        counts = {category: len(blocks) for category, blocks in result.by_category.items()}
        assert counts == dict(expected_counts), "categories and their error-block counts"
        assert result.total == sum(n for _, n in expected_counts), "total counts every error block"
        assert result.is_clean == is_clean, "is_clean follows whether any gating category is present"
        assert not result.is_reconciliation_grade, "any error, gating or not, disqualifies reconciliation"

    def test_runtime_error_category(self) -> None:
        result = classify_lean_log(RUNTIME_ERROR_LOG)
        assert "runtime_error" in result.by_category
        assert not result.is_clean

    def test_unknown_error_bucketed_as_other(self) -> None:
        log = "20260517 12:00:00.000 ERROR:: something we have not seen before\n"
        result = classify_lean_log(log)
        assert "other" in result.by_category

    def test_classify_workspace_missing_log_is_not_clean(self, tmp_path: Path) -> None:
        """Review-fix (P2.6): a missing log is itself a non-clean
        diagnostic. Previously this returned an empty ClassifiedErrors
        and the launcher computed ``is_clean=True`` (exit_code 0 + no
        recorded errors), masking the case where LEAN crashed before
        flushing any output. Now ``classify_workspace`` returns a
        diagnostic in the ``other`` bucket so ``is_clean`` flips to
        False."""
        result = classify_workspace(tmp_path / "log.txt")
        assert not result.is_clean
        assert result.total == 1
        assert "other" in result.by_category
        assert "log.txt not present" in result.by_category["other"][0]

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
