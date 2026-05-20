"""Unit tests for the lean_sidecar FastAPI router request model.

Canonical P2.5 window (Mon 2025-01-06 → Fri 2025-01-10, 5 trading days):
  start_ms_utc = 09:30 ET of 2025-01-06 = 14:30 UTC = 1_736_173_800_000
  end_ms_utc   = 09:30 ET of 2025-01-13 = 14:30 UTC = 1_736_778_600_000
These values are known-good against the _validate_window validator.
"""

from __future__ import annotations

import pytest

_GOOD_START_MS = 1_736_173_800_000
_GOOD_END_MS = 1_736_778_600_000


def test_trusted_run_request_model_accepts_new_fields() -> None:
    from app.routers.lean_sidecar import TrustedRunRequestModel

    payload = {
        "run_id": "test-payload",
        "symbol": "SPY",
        "start_ms_utc": _GOOD_START_MS,
        "end_ms_utc": _GOOD_END_MS,
        "starting_cash": 100_000.0,
        "data_source": "polygon",
        "bar_minutes": 15,
        "session": "regular",
        "adjustment": "raw",
    }

    model = TrustedRunRequestModel(**payload)
    assert model.data_source == "polygon"
    assert model.bar_minutes == 15


def test_trusted_run_request_model_legacy_requires_all_legacy_fields() -> None:
    """PR B (2026-05-19): the legacy top-level shape now requires all of
    ``data_source`` / ``bar_minutes`` / ``session`` so the router can
    distinguish "caller picked the legacy shape" from "caller forgot to
    send a ``data_policy`` block". PR A's per-field Literal defaults
    silently masked that ambiguity.
    """
    from pydantic import ValidationError

    from app.routers.lean_sidecar import TrustedRunRequestModel

    with pytest.raises(ValidationError):
        TrustedRunRequestModel(
            run_id="test-def",
            symbol="SPY",
            start_ms_utc=_GOOD_START_MS,
            end_ms_utc=_GOOD_END_MS,
            starting_cash=100_000.0,
        )


def test_trusted_run_request_model_accepts_non_15_bar_minutes() -> None:
    """PR B replaces PR A's ``bar_minutes: Literal[15]`` pin with a
    free integer; template-internal source code asserts the value at
    LEAN runtime. The router only enforces ``ge=1``.
    """
    from app.routers.lean_sidecar import TrustedRunRequestModel

    model = TrustedRunRequestModel(
        run_id="test-non15-bm",
        symbol="SPY",
        start_ms_utc=_GOOD_START_MS,
        end_ms_utc=_GOOD_END_MS,
        starting_cash=100_000.0,
        data_source="synthetic",
        bar_minutes=30,
        session="regular",
        adjustment="raw",
    )
    assert model.data_policy is not None
    assert model.data_policy.strategy_bars.multiplier == 30
