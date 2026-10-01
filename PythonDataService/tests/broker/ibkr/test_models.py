"""Tests for the wire models in app.broker.ibkr.models — sentinel coercion
and round-trip serialisation."""

from __future__ import annotations

import math
import sys

from app.broker.ibkr.models import (
    _coerce_optional_float,
)


def test_coerce_optional_float_preserves_real_values() -> None:
    assert _coerce_optional_float(0.0) == 0.0
    assert _coerce_optional_float(-1.0) == -1.0  # not iv-specific
    assert _coerce_optional_float(0.42) == 0.42


def test_coerce_optional_float_treats_nan_as_none() -> None:
    assert _coerce_optional_float(math.nan) is None
    assert _coerce_optional_float(None) is None


def test_coerce_optional_float_treats_ibkr_unset_double_as_none() -> None:
    assert _coerce_optional_float(sys.float_info.max) is None
