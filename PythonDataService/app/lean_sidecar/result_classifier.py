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
ErrorCategory = Literal[
    "analysis_failed",
    "failed_data_requests",
    "runtime_error",
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
        """No errors at all in the LEAN log."""
        return self.total == 0

    @property
    def is_reconciliation_grade(self) -> bool:
        """No errors in any category that affects reconciliation.

        Compatibility runs may tolerate ``failed_data_requests`` (e.g.,
        the trusted sample doesn't stage interest-rate or daily
        benchmark data) but reconciliation cannot — any of those
        categories failing means the run's numbers cannot be compared
        to Engine Lab.
        """
        return self.is_clean


def _categorize(line: str) -> ErrorCategory:
    """Bucket a single LEAN error line.

    Rules are matched in priority order; the most specific category
    wins. New rules are deliberately additive — never change an
    existing rule silently because a downstream caller's branching
    depends on the category name being stable.
    """
    low = line.lower()
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
    current_category: ErrorCategory | None = None
    current_buffer: list[str] = []

    def flush() -> None:
        if current_category is None or not current_buffer:
            return
        joined = "\n".join(current_buffer).rstrip()
        by_category.setdefault(current_category, []).append(joined)

    for raw in log_text.splitlines():
        line = raw.rstrip()
        if _ERROR_PREFIX.search(line):
            flush()
            current_category = _categorize(line)
            current_buffer = [line]
            continue
        # Continuation lines (stack-trace indent, etc.) belong to the
        # most recent error block.
        if current_category is not None and line.startswith(("   ", "\t", "  ")):
            current_buffer.append(line)
            continue
        # A non-indented, non-error line terminates the current block.
        if current_category is not None:
            flush()
            current_category = None
            current_buffer = []
    flush()

    return ClassifiedErrors(by_category=by_category)


def classify_workspace(log_path: Path) -> ClassifiedErrors:
    """Convenience wrapper: classify the LEAN log on disk.

    Returns an empty result (not an error) if the log is missing; the
    launcher distinguishes "no log" from "no errors" by checking the
    log path's existence itself.
    """
    if not log_path.exists():
        return ClassifiedErrors()
    return classify_lean_log(log_path.read_text(encoding="utf-8", errors="replace"))
