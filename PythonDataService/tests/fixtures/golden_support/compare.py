"""Explicit-tolerance comparator for golden fixture validation.

The rule from numerical-rigor.md:
  "np.allclose(a, b) without explicit tolerances is a bug."

Every call to this module must supply both atol and rtol. The caller also
provides a tolerance_note explaining why those values are appropriate — this
note is stored in the manifest and surfaces in CI output so future reviewers
can judge whether a breach is a real problem or a precision artifact.

Usage:
    result = assert_close(
        actual=our_output,
        expected=reference_output,
        atol=1e-10,
        rtol=0.0,
        tolerance_note="py_vollib cross-library floor; 1e-12 excluded (CI/dev divergence)",
        label="BS-001 call price",
    )
    if not result.passed:
        raise AssertionError(result.summary())
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional, Sequence

import numpy as np


@dataclass(frozen=True)
class CompareResult:
    """Result of a single fixture comparison."""

    label: str
    passed: bool
    atol: float
    rtol: float
    tolerance_note: str
    n_compared: int
    n_failed: int
    max_abs_error: Optional[float]
    max_rel_error: Optional[float]
    first_failure_idx: Optional[int]
    first_actual: Optional[float]
    first_expected: Optional[float]

    def summary(self) -> str:
        if self.passed:
            return (
                f"PASS [{self.label}] "
                f"n={self.n_compared}, atol={self.atol}, rtol={self.rtol}"
            )
        return (
            f"FAIL [{self.label}] "
            f"{self.n_failed}/{self.n_compared} values outside tolerance "
            f"(atol={self.atol}, rtol={self.rtol}). "
            f"max_abs_err={self.max_abs_error:.3e}, "
            f"max_rel_err={self.max_rel_error:.3e}. "
            f"First failure at idx={self.first_failure_idx}: "
            f"actual={self.first_actual}, expected={self.first_expected}. "
            f"Note: {self.tolerance_note}"
        )


def assert_close(
    actual: "np.ndarray | Sequence[float] | float",
    expected: "np.ndarray | Sequence[float] | float",
    *,
    atol: float,
    rtol: float,
    tolerance_note: str,
    label: str = "",
) -> CompareResult:
    """Compare actual vs expected with explicit tolerances.

    Parameters
    ----------
    actual, expected:
        Scalar or array-like. Both are converted to float64 numpy arrays.
    atol:
        Absolute tolerance. Must be provided explicitly — no default.
    rtol:
        Relative tolerance. Must be provided explicitly — no default.
    tolerance_note:
        Required human-readable explanation of why this tolerance applies.
        Stored in CompareResult.tolerance_note.
    label:
        Short description for error messages and CI output.

    Returns
    -------
    CompareResult
        Always returns — never raises. Caller decides whether to assert.
    """
    if not tolerance_note.strip():
        raise ValueError(
            "tolerance_note is required and must explain why these tolerances apply. "
            "It is stored in the manifest and surfaces in CI output."
        )

    a = np.asarray(actual, dtype=np.float64).ravel()
    e = np.asarray(expected, dtype=np.float64).ravel()

    if a.shape != e.shape:
        raise ValueError(
            f"Shape mismatch: actual={a.shape}, expected={e.shape} for [{label}]"
        )

    n = len(a)
    if n == 0:
        return CompareResult(
            label=label,
            passed=True,
            atol=atol,
            rtol=rtol,
            tolerance_note=tolerance_note,
            n_compared=0,
            n_failed=0,
            max_abs_error=None,
            max_rel_error=None,
            first_failure_idx=None,
            first_actual=None,
            first_expected=None,
        )

    abs_errors = np.abs(a - e)
    # |actual - expected| <= atol + rtol * |expected|
    allowed = atol + rtol * np.abs(e)
    failed_mask = abs_errors > allowed

    n_failed = int(np.sum(failed_mask))
    passed = n_failed == 0

    max_abs_error = float(np.max(abs_errors)) if n > 0 else None
    with np.errstate(divide="ignore", invalid="ignore"):
        rel_errors = np.where(np.abs(e) > 0, abs_errors / np.abs(e), abs_errors)
    max_rel_error = float(np.max(rel_errors)) if n > 0 else None

    first_idx: Optional[int] = None
    first_actual_val: Optional[float] = None
    first_expected_val: Optional[float] = None
    if n_failed > 0:
        first_idx = int(np.argmax(failed_mask))
        first_actual_val = float(a[first_idx])
        first_expected_val = float(e[first_idx])

    return CompareResult(
        label=label,
        passed=passed,
        atol=atol,
        rtol=rtol,
        tolerance_note=tolerance_note,
        n_compared=n,
        n_failed=n_failed,
        max_abs_error=max_abs_error,
        max_rel_error=max_rel_error,
        first_failure_idx=first_idx,
        first_actual=first_actual_val,
        first_expected=first_expected_val,
    )


def assert_close_scalar(
    actual: float,
    expected: float,
    *,
    atol: float,
    rtol: float,
    tolerance_note: str,
    label: str = "",
) -> CompareResult:
    """Compare two scalar floats. Thin wrapper around assert_close."""
    return assert_close(
        [actual],
        [expected],
        atol=atol,
        rtol=rtol,
        tolerance_note=tolerance_note,
        label=label,
    )
