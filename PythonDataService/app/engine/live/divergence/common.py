"""Shared types for the PRD-B divergence harness (Layer A + Layer B).

Kept layer-agnostic so the ``ReportBundler`` and both layers' classifiers
depend on one definition of severity rather than coupling Layer B to
Layer A.
"""

from __future__ import annotations

from enum import StrEnum


class Severity(StrEnum):
    """Whether a divergence counts toward the report's pass/fail gate."""

    GATING = "gating"
    NON_GATING = "non_gating"
