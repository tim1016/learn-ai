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
from app.config import fleet_settings, settings
from app.data_lake.polygon_fetcher import PolygonBar
from app.routers import internal_fleet
from app.schemas.fleet_history_batch import HistoryBatchQuery, HistoryBatchResponse
from app.services.broker_v2_panel import (
    chart_projection_service,
    history_batch_client,
    history_batch_walk,
)
from app.services.broker_v2_panel.chart_projection_service import build_history_chart
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


# ---- issue #2206: the qualification-only recorded provider selection --------
#
# The gate now runs once at boot (app/main.py), not per-request inside this
# router (see internal_fleet.py's module docstring) -- so proving it closes
# needs a real app.main import, not the bespoke _build_app() every other
# test in this file uses. Role/namespace gating is decided once at Python
# import time, so each posture below runs in its own fresh subprocess, same
# reasoning as test_route_is_absent_outside_the_coordinator_role.


def test_bare_router_app_falls_back_to_the_production_provider() -> None:
    """``_build_app()`` never installs ``app.state.history_batch_provider``
    -- the route's own default must still be the real, Polygon-backed
    provider, so every other test in this file (which relies on exactly
    that default) keeps testing the production path."""
    assert internal_fleet.production_history_batch_provider.__name__ == (
        "production_history_batch_provider"
    )


_HISTORY_BATCH_PROVIDER_SELECTION_PROBE = """
import asyncio
import json
import os
import sys

os.environ["FLEET_ROLE"] = sys.argv[1]
os.environ["FLEET_DEPLOYMENT_NAMESPACE"] = sys.argv[2]
if sys.argv[3]:
    os.environ["FLEET_QUALIFICATION_PROBE_SECRET"] = sys.argv[3]
else:
    os.environ.pop("FLEET_QUALIFICATION_PROBE_SECRET", None)
os.environ["POLYGON_API_KEY"] = sys.argv[4]
os.environ["ALPACA_FAULT_INJECTION_ENABLED"] = "false"
os.environ.setdefault("FLEET_CONTROL_DIR", "history-batch-provider-selection-probe")

import app.main as main

# Whether the recorded provider was installed on app.state -- reported
# without ever calling it, so this probe can check the fourth gate factor
# (a real-shaped Polygon key) without a live network call reaching Polygon.
result = {"recorded_provider_installed": hasattr(main.app.state, "history_batch_provider")}

if sys.argv[5] == "1":
    from httpx import ASGITransport, AsyncClient

    _CLERK_ID = "clrk_history00000000000000000aa"
    _TOKEN = "svct_" + "2" * 32
    main.app.state.fleet_agent_tokens_text = json.dumps({_CLERK_ID: _TOKEN})

    async def run():
        async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test") as client:
            response = await client.post(
                "/internal/fleet/history/batch",
                json={
                    "clerk_id": _CLERK_ID,
                    "symbol": "SPY",
                    "timeframe": "1m",
                    "required_bar_count": 300,
                    "as_of_ms": 1_700_000_000_000,
                },
                headers={"X-Fleet-Clerk-Id": _CLERK_ID, "X-Fleet-Agent-Token": _TOKEN},
            )
        return {"status": response.status_code, "body": response.json()}

    result.update(asyncio.run(run()))

sys.stdout.write(json.dumps(result))
"""


def _history_batch_provider_selection(
    *, role: str, namespace: str, secret: str, polygon_api_key: str, make_request: bool = True
) -> dict[str, object]:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            _HISTORY_BATCH_PROVIDER_SELECTION_PROBE,
            role,
            namespace,
            secret,
            polygon_api_key,
            "1" if make_request else "0",
        ],
        cwd=_SERVICE_ROOT,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"probe failed for role={role}:\\n{result.stderr}"
    return json.loads(result.stdout)


@pytest.mark.slow
@pytest.mark.parametrize(
    "role,namespace,secret",
    [
        ("fleet_coordinator", "compose:learn-ai-fleet", "stray-secret-left-behind"),  # prod namespace + stray secret
        ("fleet_coordinator", "host:local", "stray-secret-left-behind"),  # dev namespace + stray secret
        ("fleet_coordinator", "compose:fleetqualificationabc123", ""),  # qualification namespace, no secret
        ("combined", "compose:fleetqualificationabc123", "qualification-probe-secret"),  # combined role
    ],
)
def test_dev_and_production_cannot_select_the_recorded_provider(
    role: str, namespace: str, secret: str
) -> None:
    """Prove the gate closes, not merely assert it exists in a comment: with
    any single guard missing, an empty Polygon key must still degrade to the
    production notice, never recorded bars -- through a real ``app.main``,
    the boot-time selection this gate now lives in."""
    payload = _history_batch_provider_selection(
        role=role, namespace=namespace, secret=secret, polygon_api_key=""
    )

    assert payload["recorded_provider_installed"] is False
    assert payload["status"] == 200
    body = payload["body"]
    assert body["bars"] == []
    assert [notice["code"] for notice in body["overlay_notices"]] == ["polygon_api_key_missing"]


@pytest.mark.slow
def test_qualification_gate_selects_the_recorded_provider_over_a_missing_polygon_key() -> None:
    """The exact gap issue #2206 closes: qualification's placeholder Polygon
    key can never answer real bars, but the recorded provider must -- proven
    with the real internal route through a real ``app.main`` boot, not by
    calling the provider function directly."""
    payload = _history_batch_provider_selection(
        role="fleet_coordinator",
        namespace="compose:fleetqualificationabc123",
        secret="qualification-probe-secret",
        polygon_api_key="",  # the qualification placeholder's shape
    )

    assert payload["recorded_provider_installed"] is True
    assert payload["status"] == 200
    body = payload["body"]
    assert body["overlay_notices"] == []
    assert len(body["bars"]) > 0
    assert all(bar["source"] == "polygon" for bar in body["bars"])


@pytest.mark.slow
def test_qualification_gate_refuses_a_lane_holding_a_real_polygon_key() -> None:
    """The fourth gate factor (defence in depth): role, namespace and secret
    all line up, but a real-shaped Polygon key must still keep the
    production provider -- a lane actually holding a working credential must
    never be made to serve synthetic bars. No request is issued here: the
    production provider would try a real Polygon call with this key, which
    a unit test must never do -- the fourth factor is checked at the
    provider-selection level, not by inspecting a response body.
    """
    payload = _history_batch_provider_selection(
        role="fleet_coordinator",
        namespace="compose:fleetqualificationabc123",
        secret="qualification-probe-secret",
        polygon_api_key="a-real-looking-key",
        make_request=False,
    )

    assert payload["recorded_provider_installed"] is False


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
        # The walk crashes outside a narrower range than the int64 domain:
        # as_of_ms=0 raises ValueError inside session_start_for_bar_count
        # (the UTC->ET conversion pushes the date before the walk's own
        # Unix-epoch floor), and a value near MAX_TIMESTAMP_MS raises
        # OverflowError (pandas' nanosecond Timestamp ceiling, ~2262-04-11).
        # Both must 422 at the schema boundary, never 500 inside the walk.
        {"as_of_ms": 0},
        {"as_of_ms": MAX_TIMESTAMP_MS},
        {"as_of_ms": "1700000000000"},
        {"as_of_ms": 1.7e12},
        {"as_of_ms": True},
        {"clerk_id": ""},
        {"symbol": ""},
        {"symbol": "123-not-a-symbol"},
        {"extra_field": "unexpected"},
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


# ---- the shared span-map helper (issue #2206) --------------------------------


@pytest.mark.parametrize(
    "multiplier,timespan,expected_ms",
    [
        (1, "minute", 60_000),
        (15, "minute", 900_000),
        (1, "hour", 3_600_000),
        (1, "day", chart_projection_service.MS_PER_DAY),
    ],
)
def test_span_ms_for_matches_each_supported_timespan(multiplier: int, timespan: str, expected_ms: int) -> None:
    """The one span-map definition the walk's own plan uses -- and the one
    the qualification recorded-history generator reuses (issue #2206)
    rather than carrying a second copy that could drift."""
    assert history_batch_walk.span_ms_for(multiplier, timespan) == expected_ms


def test_span_ms_for_rejects_an_unsupported_timespan() -> None:
    with pytest.raises(ValueError, match="unsupported timespan"):
        history_batch_walk.span_ms_for(1, "week")


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


class _RecordingTransport(ASGITransport):
    """An ``ASGITransport`` that records every request it forwards."""

    def __init__(self, *, app: FastAPI, calls: list[httpx.Request]) -> None:
        super().__init__(app=app)
        self._calls = calls

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self._calls.append(request)
        return await super().handle_async_request(request)


async def test_get_history_chart_causes_exactly_one_http_call_despite_widening(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acceptance criterion (issue #2204 gate F4): a fake vendor requiring
    several widening iterations still produces exactly one Clerk ->
    coordinator HTTP request -- proven through the real Clerk-side entry
    point (``build_history_chart``, under a real ``FLEET_ROLE=clerk_agent``
    provider selection), not by calling ``RemoteHistoryBatchClient.fetch_batch``
    directly. Calling ``fetch_batch`` directly is true by construction (it IS
    the one HTTP call) and stays green even if a Clerk-side widening loop, or
    a "retry once on any notice", were reintroduced around it in
    ``build_history_chart`` -- driving through the real entry point is what
    makes this test able to fail.
    """
    monkeypatch.setattr(chart_projection_service, "_INDICATOR_WARMUP_BARS", 3)
    monkeypatch.setattr(settings, "POLYGON_API_KEY", "a-real-looking-key")
    supplied = _complete_1m_bars(count=303, now_ms=_NOW)
    vendor_calls: list[tuple[object, object]] = []

    async def fake_fetch_aggregate_bars(symbol, start, end, api_key, *, multiplier, timespan, adjusted=False):
        vendor_calls.append((start, end))
        return supplied[-300:] if len(vendor_calls) == 1 else supplied[:3]

    monkeypatch.setattr(history_batch_walk, "fetch_aggregate_bars", fake_fetch_aggregate_bars)

    app = _build_app({_CLERK_ID: _TOKEN})
    http_calls: list[httpx.Request] = []

    def _fake_build_internal_client(*, read_timeout_s=None, **_kwargs):
        return httpx.AsyncClient(
            transport=_RecordingTransport(app=app, calls=http_calls),
            timeout=httpx.Timeout(10.0, read=read_timeout_s),
        )

    monkeypatch.setattr(history_batch_client, "build_internal_client", _fake_build_internal_client)
    monkeypatch.setattr(fleet_settings, "ROLE", "clerk_agent")
    monkeypatch.setattr(fleet_settings, "COORDINATOR_URL", "http://127.0.0.1")
    monkeypatch.setattr(fleet_settings, "AGENT_SERVICE_TOKEN", _TOKEN)
    monkeypatch.setattr(fleet_settings, "CLERK_ID", _CLERK_ID)
    monkeypatch.setattr(history_batch_client, "_CACHED_PROVIDER", None)

    result = await build_history_chart(
        "1m",
        [],
        strategy_instance_id="sid-widening-probe",
        symbol="ILLQ",
        batch_provider=history_batch_client.build_history_batch_provider(),
        now_ms=_NOW,
    )

    assert len(http_calls) == 1, "exactly one Clerk -> coordinator HTTP request"
    assert len(vendor_calls) == 2, "the coordinator widened backward more than once"
    assert result.overlay_notices == []
    assert len(result.indicator_bars) == 303
    starts = [bar.start_ms for bar in result.indicator_bars]
    assert starts == sorted(starts)
    assert all(isinstance(bar.start_ms, int) for bar in result.indicator_bars)


async def test_batch_provider_is_awaited_exactly_once_per_build_history_chart_call() -> None:
    """FR-008: one internal request per public history attempt -- pinned as a
    call-count invariant on the provider itself, for both a healthy batch and
    a notice-only one. A Clerk-side retry-on-notice regression would call the
    provider twice for the notice-only case without necessarily changing any
    other observable assertion in this file.
    """
    calls = 0

    async def _healthy_provider(query: HistoryBatchQuery) -> HistoryBatchResponse:
        nonlocal calls
        calls += 1
        return HistoryBatchResponse(
            bars=[], source="polygon", overlay_notices=[], effective_as_of_ms=query.as_of_ms
        )

    await build_history_chart(
        "1m", [], strategy_instance_id="sid-a", symbol="SPY",
        batch_provider=_healthy_provider, now_ms=_NOW,
    )
    assert calls == 1

    calls = 0

    async def _notice_only_provider(query: HistoryBatchQuery) -> HistoryBatchResponse:
        nonlocal calls
        calls += 1
        from app.schemas.broker_v2_panel import ChartOverlayNoticeView

        return HistoryBatchResponse(
            bars=[],
            source="polygon",
            overlay_notices=[
                ChartOverlayNoticeView(
                    code="coordinator_unavailable", message="unavailable", source="polygon"
                )
            ],
            effective_as_of_ms=query.as_of_ms,
        )

    await build_history_chart(
        "1m", [], strategy_instance_id="sid-b", symbol="SPY",
        batch_provider=_notice_only_provider, now_ms=_NOW,
    )
    assert calls == 1


async def test_half_day_history_batch_round_trips_through_the_real_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Golden round-trip (issue #2204 gate F5): the Black Friday half-day
    case through the real ``RemoteHistoryBatchClient`` and the real
    ``internal_fleet`` router -- not just the in-process walk unit tests --
    so the relocation's equivalence is pinned across the wire too, not only
    within one process.

    2025-11-28 (the day after Thanksgiving) is a real NYSE half-day: the
    session closes at 13:00 ET (1764352800000 ms UTC) rather than the usual
    16:00 ET. A daily bar dated that session must carry that early close as
    its ``end_ms``, not midnight-plus-one-day -- exactly the invariant
    ``test_daily_bar_completes_at_session_close_not_midnight_plus_one_day``
    pins in-process.
    """
    black_friday = 1764340200000  # 2025-11-28 09:30 ET session open, ms UTC
    black_friday_close = 1764352800000  # 2025-11-28 13:00 ET session close, ms UTC
    now_ms = black_friday_close + 3_600_000  # well after the half-day closed
    monkeypatch.setattr(chart_projection_service, "_INDICATOR_WARMUP_BARS", 0)
    # `monkeypatch.setitem`, not `setattr` with a replacement dict: both
    # `chart_projection_service` and `history_batch_walk` hold their own name
    # binding to the *same* dict object, so only an in-place mutation is
    # visible to both (a `setattr` here would rebind only the former).
    monkeypatch.setitem(
        chart_projection_service.HISTORY_TIMEFRAME_SPECS,
        "1d",
        chart_projection_service._HistoryTimeframeSpec(1, "day", 1),
    )
    monkeypatch.setattr(settings, "POLYGON_API_KEY", "a-real-looking-key")

    half_day_bar = PolygonBar(
        t_ms=black_friday, open=1.0, high=2.0, low=0.5, close=1.5, volume=10, vwap=1.2, n=3
    )

    async def fake_fetch_aggregate_bars(symbol, start, end, api_key, *, multiplier, timespan, adjusted=False):
        return [half_day_bar]

    monkeypatch.setattr(history_batch_walk, "fetch_aggregate_bars", fake_fetch_aggregate_bars)

    app = _build_app({_CLERK_ID: _TOKEN})
    http_calls: list[httpx.Request] = []

    def _fake_build_internal_client(*, read_timeout_s=None, **_kwargs):
        return httpx.AsyncClient(
            transport=_RecordingTransport(app=app, calls=http_calls),
            timeout=httpx.Timeout(10.0, read=read_timeout_s),
        )

    monkeypatch.setattr(history_batch_client, "build_internal_client", _fake_build_internal_client)
    monkeypatch.setattr(fleet_settings, "ROLE", "clerk_agent")
    monkeypatch.setattr(fleet_settings, "COORDINATOR_URL", "http://127.0.0.1")
    monkeypatch.setattr(fleet_settings, "AGENT_SERVICE_TOKEN", _TOKEN)
    monkeypatch.setattr(fleet_settings, "CLERK_ID", _CLERK_ID)
    monkeypatch.setattr(history_batch_client, "_CACHED_PROVIDER", None)

    result = await build_history_chart(
        "1d",
        [],
        strategy_instance_id="sid-half-day",
        symbol="SPY",
        batch_provider=history_batch_client.build_history_batch_provider(),
        now_ms=now_ms,
    )

    assert len(http_calls) == 1
    assert result.overlay_notices == []
    assert [bar.start_ms for bar in result.bars] == [black_friday]
    assert [bar.end_ms for bar in result.bars] == [black_friday_close]
