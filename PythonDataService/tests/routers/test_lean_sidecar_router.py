"""Unit tests for the lean_sidecar FastAPI router request model.

Canonical P2.5 window (Mon 2025-01-06 → Fri 2025-01-10, 5 trading days):
  start_ms_utc = 09:30 ET of 2025-01-06 = 14:30 UTC = 1_736_173_800_000
  end_ms_utc   = 09:30 ET of 2025-01-13 = 14:30 UTC = 1_736_778_600_000
These values are known-good against the _validate_window validator.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

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


@pytest.mark.asyncio
async def test_post_trusted_run_returns_502_when_metadata_staging_fails(monkeypatch) -> None:
    """Regression: when the LEAN launcher is unreachable (or the
    pinned image isn't pulled locally), ``stage_lean_metadata_from_image``
    raises ``MetadataStagingError`` from inside the orchestrator. Prior
    to the fix this escaped through the global ``Exception`` handler as
    a 500, which Starlette returned without re-running CORS headers —
    the browser then surfaced it as a misleading CORS error. The router
    must catch the staging error and surface it as a 502 with a stable
    ``reason`` label so the frontend can branch.
    """
    from app.lean_sidecar.staging import MetadataStagingError
    from app.main import app
    from app.routers import lean_sidecar as router_module

    async def _raise_staging_error(_request):
        raise MetadataStagingError(
            "metadata extraction via launcher failed: "
            "launcher at http://127.0.0.1:8090/extract-metadata unreachable: "
            "[Errno 111] Connection refused"
        )

    monkeypatch.setattr(router_module, "run_trusted_sample", _raise_staging_error)

    payload = {
        "run_id": "test-staging-502",
        "symbol": "SPY",
        "start_ms_utc": _GOOD_START_MS,
        "end_ms_utc": _GOOD_END_MS,
        "starting_cash": 100_000.0,
        "data_source": "polygon",
        "bar_minutes": 15,
        "session": "regular",
        "adjustment": "raw",
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/lean-sidecar/trusted-runs",
            json=payload,
            headers={"Origin": "http://localhost:4200"},
        )

    assert response.status_code == 502, response.text
    body = response.json()
    assert body["detail"]["reason"] == "metadata_staging_failed"
    assert "launcher" in body["detail"]["message"].lower()
    # CORS headers must still be attached — the whole point of the fix
    # is that the browser doesn't see "no Access-Control-Allow-Origin".
    assert response.headers.get("access-control-allow-origin") == "http://localhost:4200"
