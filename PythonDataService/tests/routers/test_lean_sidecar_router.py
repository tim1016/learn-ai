"""Unit tests for the lean_sidecar FastAPI router request model.

Canonical P2.5 window (Mon 2025-01-06 → Fri 2025-01-10, 5 trading days):
  start_ms_utc = 09:30 ET of 2025-01-06 = 14:30 UTC = 1_736_173_800_000
  end_ms_utc   = 09:30 ET of 2025-01-13 = 14:30 UTC = 1_736_778_600_000
These values are known-good against the _validate_window validator.
"""

from __future__ import annotations

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


def test_trusted_run_request_model_legacy_accepts_partial_payload_with_pr_a_defaults() -> None:
    """PR B (2026-05-20, P1 review): the legacy top-level shape preserves
    PR A's one-deprecation-cycle compatibility guarantee by defaulting
    missing legacy fields (``data_source``/``bar_minutes``/``session``/
    ``adjustment``) instead of 422-ing. The deployed Lean Lab UI sends
    only ``run_id``/``symbol``/window/cash/template — without this
    defaulting, every UI submit would have 422'd until the client
    shipped a new payload.

    ``symbol`` is the only field with no sensible default and still
    must be present on the legacy shape (covered by a sibling test
    in ``tests/lean_sidecar/test_router_lean_sidecar.py``).
    """
    from app.routers.lean_sidecar import TrustedRunRequestModel

    model = TrustedRunRequestModel(
        run_id="test-def",
        symbol="SPY",
        start_ms_utc=_GOOD_START_MS,
        end_ms_utc=_GOOD_END_MS,
        starting_cash=100_000.0,
        # No data_source, bar_minutes, session, adjustment, or data_policy.
    )
    assert model.data_policy is not None
    assert model.data_policy.source == "synthetic"
    assert model.data_policy.session == "regular"
    assert model.data_policy.strategy_bars.multiplier == 15
    assert model.data_policy.adjusted is False  # legacy adjustment="raw" default


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
