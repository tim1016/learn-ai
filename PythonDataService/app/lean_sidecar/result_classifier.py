"""Classify LEAN runtime errors from ``output/log.txt``.

LEAN's exit code is not a reliable cleanliness signal: a run that
crashes ``ResultsAnalyzer``, fails several ``SubscriptionDataSource``
reads, or hits a runtime exception in ``Algorithm.Initialize`` can
still exit 0. Treating exit-code-0 as "success" is the same class of
silent-success bug we hit in Phase 1b when the wrong algorithm ran
silently.

This module parses LEAN's structured log and produces a
:class:`ClassifiedErrors` summary the launcher attaches to every
``LaunchResponse``. Callers can then decide whether the run is
acceptable for compatibility (warnings allowed) or
reconciliation-grade (no analysis failures, no failed data requests).

Authority: ``docs/architecture/lean-sidecar-lab.md`` §"Phase 1b
progress" — *clean-run classification beyond exit_code*.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

# LEAN log lines start with ``YYYYMMDD HH:MM:SS.fff TRACE|DEBUG|ERROR::``;
# anything containing ``ERROR::`` is what we sweep up. We also catch
# ``Runtime Error:`` strings emitted from the Python.NET bridge.
_ERROR_PREFIX = re.compile(r"ERROR::|Runtime Error:")

# Categories worth distinguishing for "clean run" judgements. They are
# named so the API consumer can branch on them without parsing free
# text. Anything we can't bucket lands in ``other``.
#
# ``benchmark_unavailable`` is the one non-gating category: LEAN's default
# benchmark is SPY's daily zip, which Engine Lab does not stage. The
# resulting ``SubscriptionDataSourceReader.InvalidSource`` + the cascading
# ``BacktestingResultHandler.SendFinalResult`` "Sequence contains no
# elements" from ``ReadEquityCurve`` are both *post*-strategy housekeeping
# failures — the strategy itself produced trades and a STATISTICS:: block.
# The fix in commit 843172ab adds ``SetBenchmark`` to the seed template,
# but user-pasted scripts and pre-fix saved scripts still trip this; the
# classifier acknowledges the cascade as benign so ``is_clean`` flips
# back to True. Reconciliation runs still disqualify (alpha/beta zero).
ErrorCategory = Literal[
    "analysis_failed",
    "failed_data_requests",
    "runtime_error",
    "benchmark_unavailable",
    "other",
]


@dataclass(frozen=True, slots=True)
class ClassifiedErrors:
    """A summary of LEAN errors found in ``output/log.txt``.

    ``by_category`` keys are stable strings so callers can persist them
    in the manifest and grep against them later. Empty lists are kept
    out so a "clean" run serializes as ``{}``.
    """

    by_category: dict[ErrorCategory, list[str]] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(len(v) for v in self.by_category.values())

    @property
    def categories(self) -> list[ErrorCategory]:
        return sorted(self.by_category.keys())

    @property
    def is_clean(self) -> bool:
        """True when no gating-category errors are present.

        ``benchmark_unavailable`` is explicitly non-gating: the SPY-zip
        miss + equity-curve cascade are LEAN's post-strategy housekeeping
        failing after a successful run produced trades and statistics.
        Treating the cascade as gating mislabels working strategies as
        "failed" in the UI. Every other category (including ``other``)
        still gates ``is_clean``.
        """
        gating_total = sum(len(v) for k, v in self.by_category.items() if k != "benchmark_unavailable")
        return gating_total == 0

    @property
    def is_reconciliation_grade(self) -> bool:
        """No errors in any category — including the benign benchmark cascade.

        Reconciliation runs disqualify even on ``benchmark_unavailable``
        because LEAN computes alpha/beta against the missing benchmark and
        silently emits zeros. Comparing those zeros to a reconciled
        reference would corrupt the report.
        """
        return self.total == 0


def _categorize(block: str) -> ErrorCategory:
    """Bucket a complete LEAN error block (header line + indented continuation).

    Categorization runs on the full joined block rather than the header
    alone because the equity-curve-cascade signal lives in the stack
    trace's continuation lines (``ReadEquityCurve`` /
    ``Sequence contains no elements``), not in the
    ``BacktestingResultHandler.SendFinalResult`` header.

    Rules are matched in priority order; the most specific category
    wins. New rules are deliberately additive — never change an
    existing rule silently because a downstream caller's branching
    depends on the category name being stable.

    The two benign SPY-benchmark patterns are checked first so they
    take priority over the generic ``failed_data_requests`` /
    ``analysis_failed`` rules below:

    1. ``SubscriptionDataSourceReader.InvalidSource`` referencing
       ``daily/spy.zip`` — Engine Lab does not stage LEAN's default
       benchmark daily zip.
    2. ``BacktestingResultHandler.SendFinalResult`` whose stack trace
       includes ``ReadEquityCurve`` and ``Sequence contains no
       elements`` — the cascade triggered by (1) when the
       analyzer reads the missing benchmark series.

    Any other ``daily/<symbol>.zip`` miss (QQQ, IWM, etc.) still gates
    via ``failed_data_requests``; any other ``ResultsAnalyzer`` /
    ``SendFinalResult`` failure still gates via ``analysis_failed``.
    """
    low = block.lower()
    if "daily/spy.zip" in low and "subscriptiondatasourcereader.invalidsource" in low:
        return "benchmark_unavailable"
    if "sendfinalresult" in low and "readequitycurve" in low and "sequence contains no elements" in low:
        return "benchmark_unavailable"
    if "resultsanalyzer" in low or "sendfinalresult" in low or "equity curve" in low:
        return "analysis_failed"
    if "subscriptiondatasource" in low or "no data" in low or "file not found" in low:
        return "failed_data_requests"
    if "algorithm.initialize" in low or "runtime error" in low or "onerror" in low:
        return "runtime_error"
    return "other"


def classify_lean_log(log_text: str) -> ClassifiedErrors:
    """Parse the body of LEAN's ``log.txt`` into a classified summary.

    Strips lines until it sees an ``ERROR::`` / ``Runtime Error:``
    marker, then keeps the rest of the line through the next newline.
    Multiline tracebacks attach to the most recent error line via the
    indent test ``leading-whitespace``: this preserves enough context
    for the operator to recognize the failure but does not flood the
    response payload.
    """
    by_category: dict[ErrorCategory, list[str]] = {}
    in_block = False
    current_buffer: list[str] = []

    def flush() -> None:
        nonlocal in_block, current_buffer
        if not in_block or not current_buffer:
            return
        joined = "\n".join(current_buffer).rstrip()
        # Categorization deliberately runs on the full block so the
        # equity-curve-cascade signal in continuation lines reaches
        # ``_categorize`` (see its docstring).
        category = _categorize(joined)
        by_category.setdefault(category, []).append(joined)
        in_block = False
        current_buffer = []

    for raw in log_text.splitlines():
        line = raw.rstrip()
        if _ERROR_PREFIX.search(line):
            flush()
            in_block = True
            current_buffer = [line]
            continue
        # Continuation lines (stack-trace indent, etc.) belong to the
        # most recent error block.
        if in_block and line.startswith(("   ", "\t", "  ")):
            current_buffer.append(line)
            continue
        # A non-indented, non-error line terminates the current block.
        if in_block:
            flush()
    flush()

    return ClassifiedErrors(by_category=by_category)


def classify_workspace(log_path: Path) -> ClassifiedErrors:
    """Convenience wrapper: classify the LEAN log on disk.

    Treats a missing log as a non-clean diagnostic — surfaces it in the
    ``other`` bucket with a stable label so ``is_clean`` flips to False
    instead of silently returning success on a run that crashed before
    flushing any output. Previously an absent log produced an empty
    ``ClassifiedErrors`` (the launcher then computed ``is_clean=True``
    when exit_code was also 0); a runaway algorithm that exited 0 but
    wrote nothing would have passed for a clean run.
    """
    if not log_path.exists():
        return ClassifiedErrors(
            by_category={
                "other": [
                    f"LEAN log.txt not present at {log_path}; "
                    "the run did not produce a log file. Treat as "
                    "non-clean — LEAN crashed before flushing or the "
                    "output directory was misconfigured."
                ],
            }
        )
    return classify_lean_log(log_path.read_text(encoding="utf-8", errors="replace"))
