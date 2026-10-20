"""Tests for the wire models in app.broker.ibkr.models — sentinel coercion
and round-trip serialisation."""

from __future__ import annotations

import math
import sys

from app.broker.ibkr.models import (
    _coerce_optional_float,
)


def test_coerce_optional_float_preserves_real_values_and_strips_nan_none_and_the_unset_double() -> None:
    assert _coerce_optional_float(0.0) == 0.0, "preserves real values: zero"
    assert _coerce_optional_float(-1.0) == -1.0, "preserves real values: -1.0 (not iv-specific)"
    assert _coerce_optional_float(0.42) == 0.42, "preserves real values: a positive value"
    assert _coerce_optional_float(math.nan) is None, "treats nan as none"
    assert _coerce_optional_float(None) is None, "treats none as none"
    assert _coerce_optional_float(sys.float_info.max) is None, "treats the ibkr unset double as none"
