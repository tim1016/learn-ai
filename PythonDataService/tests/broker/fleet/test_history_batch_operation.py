"""The coordinator's history-batch internal operation (issue #2204).

Covers the sixth ``/internal/fleet/*`` operation added in
``app.routers.internal_fleet``: authentication (token + header/body clerk-id
agreement), request validation, the one-hop widening walk, and that the route
never mounts outside the coordinator role.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.broker.fleet.errors import FleetAgentTokenRefused
from app.config import settings
from app.data_lake.polygon_fetcher import PolygonBar
from app.services.broker_v2_panel import chart_projection_service, history_batch_client
from app.services.broker_v2_panel.history_batch_client import RemoteHistoryBatchClient
from app.utils.error_handlers import install_fleet_control_error_handler
from app.utils.session_anchors import MAX_TIMESTAMP_MS

_CLERK_ID = "clrk_history00000000000000000aa"
_OTHER_CLERK_ID = "clrk_other0000000000000000000bb"
_TOKEN = "svct_" + "2" * 32
_NOW = 1_700_000_000_000
_PATH = "/internal/fleet/history/batch"
_SERVICE_ROOT = Path(__file__).resolve().parents[3]


def _build_app(tokens: dict[str, str]) -> FastAPI:
    """A minimal coordinator app mounting only the internal fleet router."""
    from app.routers import internal_fleet

    app = FastAPI()
    install_fleet_control_error_handler(app)
    app.state.fleet_agent_tokens_text = json.dumps(tokens)
    app.include_router(internal_fleet.router)
    return app


def _payload(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "clerk_id": _CLERK_ID,
        "symbol": "SPY",
        "timeframe": "1m",
        "required_bar_count": 300,
        "as_of_ms": _NOW,
    }
    body.update(overrides)
    return body


# ---- authentication ---------------------------------------------------------


async def test_correct_token_and_matching_header_body_clerk_id_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "POLYGON_API_KEY", "")
    app = _build_app({_CLERK_ID: _TOKEN})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            _PATH,
            json=_payload(),
            headers={"X-Fleet-Clerk-Id": _CLERK_ID, "X-Fleet-Agent-Token": _TOKEN},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["bars"] == []
    assert [notice["code"] for notice in body["overlay_notices"]] == ["polygon_api_key_missing"]
    assert body["effective_as_of_ms"] == _NOW


async def test_missing_agent_token_is_refused_flat() -> None:
    app = _build_app({_CLERK_ID: _TOKEN})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            _PATH, json=_payload(), headers={"X-Fleet-Clerk-Id": _CLERK_ID}
        )

    assert response.status_code == 403
    body = response.json()
    assert body["reason"] == FleetAgentTokenRefused.reason
    assert "detail" not in body


async def test_wrong_agent_token_is_refused() -> None:
    app = _build_app({_CLERK_ID: _TOKEN})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            _PATH,
            json=_payload(),
            headers={"X-Fleet-Clerk-Id": _CLERK_ID, "X-Fleet-Agent-Token": "wrong"},
        )

    assert response.status_code == 403
    assert response.json()["reason"] == FleetAgentTokenRefused.reason


async def test_missing_header_clerk_identity_is_refused() -> None:
    """A correct token with no ``X-Fleet-Clerk-Id`` header still refuses."""
    app = _build_app({_CLERK_ID: _TOKEN})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            _PATH, json=_payload(), headers={"X-Fleet-Agent-Token": _TOKEN}
        )

    assert response.status_code == 403
    assert response.json()["reason"] == FleetAgentTokenRefused.reason


async def test_mismatched_header_and_body_clerk_id_is_refused() -> None:
    """A token valid for one clerk presented alongside another's identity."""
    app = _build_app({_CLERK_ID: _TOKEN, _OTHER_CLERK_ID: "svct_" + "3" * 32})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            _PATH,
            json=_payload(clerk_id=_CLERK_ID),
            headers={"X-Fleet-Clerk-Id": _OTHER_CLERK_ID, "X-Fleet-Agent-Token": _TOKEN},
        )

    assert response.status_code == 403
    assert response.json()["reason"] == FleetAgentTokenRefused.reason


async def test_browser_control_secret_cannot_substitute_for_the_agent_token() -> None:
    """A ``X-Data-Plane-Control-Secret`` header is not this route's currency."""
    app = _build_app({_CLERK_ID: _TOKEN})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            _PATH,
            json=_payload(),
            headers={
                "X-Fleet-Clerk-Id": _CLERK_ID,
                "X-Data-Plane-Control-Secret": "whatever-a-browser-would-send",
            },
        )

    assert response.status_code == 403
    assert response.json()["reason"] == FleetAgentTokenRefused.reason


def test_route_is_excluded_from_the_public_schema() -> None:
    """The internal route stays out of public OpenAPI (``include_in_schema``)."""
    app = _build_app({_CLERK_ID: _TOKEN})
    matching = [route for route in app.routes if getattr(route, "path", None) == _PATH]
    assert matching, "the history-batch route did not mount on a coordinator-shaped app"
    assert all(getattr(route, "include_in_schema", True) is False for route in matching)


@pytest.mark.slow
def test_route_is_absent_outside_the_coordinator_role() -> None:
    """Mounts only under the coordinator role -- keyed on ``FLEET_ROLE``,
    never a service name (dev names the role ``python-service`` with
    ``FLEET_ROLE=fleet_coordinator``; production names it
    ``fleet-coordinator``). A real ``app.main`` import per role, in a
    subprocess: role gating is decided once at module import time (see
    ``test_fleet_role_openapi_agreement.py``'s own probe for why), which is
    also why this is marked ``slow`` rather than living in the fast gate.
    """
    probe = """
import json
import os
import sys

os.environ.setdefault("POLYGON_API_KEY", "contract-schema-placeholder")
os.environ["ALPACA_FAULT_INJECTION_ENABLED"] = "false"
os.environ["FLEET_ROLE"] = sys.argv[1]
os.environ.setdefault("FLEET_CONTROL_DIR", "history-batch-role-probe")
if sys.argv[1] == "clerk_agent":
    os.environ["FLEET_MAX_INFLIGHT_REQUESTS"] = "4"
    os.environ["FLEET_MAX_INFLIGHT_STREAMS"] = "4"

from app.main import app

paths = sorted({route.path for route in app.routes if hasattr(route, "path")})
sys.stdout.write(json.dumps(paths))
"""
    for role, expect_present in (("fleet_coordinator", True), ("clerk_agent", False)):
        result = subprocess.run(
            [sys.executable, "-c", probe, role],
            cwd=_SERVICE_ROOT,
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, f"FLEET_ROLE={role} import failed:\n{result.stderr}"
        paths = json.loads(result.stdout)
        assert (_PATH in paths) is expect_present, (
            f"FLEET_ROLE={role}: expected history-batch route present={expect_present}"
        )


# ---- request validation ------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"timeframe": "5m"},
        {"required_bar_count": 0},
        {"required_bar_count": chart_projection_service.MAX_HISTORY_REQUIRED_BAR_COUNT + 1},
        {"as_of_ms": -1},
        {"as_of_ms": MAX_TIMESTAMP_MS + 1},
        {"clerk_id": ""},
        {"symbol": ""},
    ],
)
async def test_invalid_requests_are_rejected(overrides: dict[str, object]) -> None:
    app = _build_app({_CLERK_ID: _TOKEN})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            _PATH,
            json=_payload(**overrides),
            headers={"X-Fleet-Clerk-Id": _CLERK_ID, "X-Fleet-Agent-Token": _TOKEN},
        )

    assert response.status_code == 422


# ---- the one-hop widening walk ------------------------------------------------


def _complete_1m_bars(*, count: int, now_ms: int) -> list[PolygonBar]:
    return [
        PolygonBar(
            t_ms=now_ms - (i + 1) * 60_000,
            open=1.0,
            high=2.0,
            low=0.5,
            close=1.5,
            volume=10,
            vwap=1.2,
            n=3,
        )
        for i in reversed(range(count))
    ]


async def test_one_clerk_request_causes_exactly_one_http_call_despite_widening(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acceptance criterion (#2204): a fake vendor requiring several widening
    iterations still produces exactly one Clerk -> coordinator HTTP request.

    Drives the real ``RemoteHistoryBatchClient`` against the real
    ``internal_fleet`` router (full auth + validation + walk), with the
    vendor call (``chart_projection_service.fetch_aggregate_bars``) faked to
    require two widening iterations before it satisfies the target count --
    mirroring ``test_history_extends_backward_when_sparse_aggregates_underfill_budget``.
    """
    monkeypatch.setattr(chart_projection_service, "_INDICATOR_WARMUP_BARS", 3)
    monkeypatch.setattr(settings, "POLYGON_API_KEY", "a-real-looking-key")
    supplied = _complete_1m_bars(count=303, now_ms=_NOW)
    vendor_calls: list[tuple[object, object]] = []

    async def fake_fetch_aggregate_bars(symbol, start, end, api_key, *, multiplier, timespan, adjusted=False):
        vendor_calls.append((start, end))
        return supplied[-300:] if len(vendor_calls) == 1 else supplied[:3]

    monkeypatch.setattr(chart_projection_service, "fetch_aggregate_bars", fake_fetch_aggregate_bars)

    app = _build_app({_CLERK_ID: _TOKEN})
    http_calls: list[httpx.Request] = []

    class _RecordingTransport(ASGITransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            http_calls.append(request)
            return await super().handle_async_request(request)

    def _fake_build_internal_client(*, read_timeout_s=None, **_kwargs):
        return httpx.AsyncClient(
            transport=_RecordingTransport(app=app),
            timeout=httpx.Timeout(10.0, read=read_timeout_s),
        )

    monkeypatch.setattr(history_batch_client, "build_internal_client", _fake_build_internal_client)

    client = RemoteHistoryBatchClient(
        base_url="http://127.0.0.1", clerk_id=_CLERK_ID, agent_service_token=_TOKEN
    )
    batch = await client.fetch_batch("ILLQ", "1m", 303, _NOW)

    assert len(http_calls) == 1, "exactly one Clerk -> coordinator HTTP request"
    assert len(vendor_calls) == 2, "the coordinator widened backward more than once"
    assert batch.overlay_notices == []
    assert len(batch.bars) == 303
    starts = [bar.start_ms for bar in batch.bars]
    assert starts == sorted(starts)
    assert all(isinstance(bar.start_ms, int) for bar in batch.bars)
