"""The Clerk's history-batch provider selection and transport (issue #2204).

Covers ``app.services.broker_v2_panel.history_batch_client``: role-based
provider selection, the ``RemoteHistoryBatchClient``'s absorption of every
transport failure mode into ``coordinator_unavailable``, that its inner read
timeout matches the pinned constant, and that a construction-time
misconfiguration (issue #2204 gate F7) degrades every request rather than
crashing boot or a request.
"""

from __future__ import annotations

import logging

import httpx
import pytest

from app.config import fleet_settings, settings
from app.schemas.fleet_history_batch import HistoryBatchQuery, HistoryBatchResponse
from app.services.broker_v2_panel import history_batch_client
from app.services.broker_v2_panel.history_batch_client import (
    HISTORY_BATCH_INNER_TIMEOUT_S,
    HistoryClientMisconfigured,
    RemoteHistoryBatchClient,
    build_history_batch_provider,
)

_NOW = 1_700_000_000_000


def _query(**overrides: object) -> HistoryBatchQuery:
    fields: dict[str, object] = {
        "symbol": "SPY",
        "timeframe": "1m",
        "required_bar_count": 300,
        "as_of_ms": _NOW,
    }
    fields.update(overrides)
    return HistoryBatchQuery(**fields)


@pytest.fixture(autouse=True)
def _reset_fleet_role(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test pins its own role rather than trusting process defaults.

    Also clears the module-level provider cache (issue #2204 gate F7):
    production builds the provider once per process, but each test must see
    a fresh construction against its own role/monkeypatch state, not
    whatever a previous test's role happened to cache.
    """
    monkeypatch.setattr(fleet_settings, "ROLE", "combined")
    monkeypatch.setattr(fleet_settings, "COORDINATOR_URL", None)
    monkeypatch.setattr(fleet_settings, "AGENT_SERVICE_TOKEN", None)
    monkeypatch.setattr(fleet_settings, "CLERK_ID", None)
    monkeypatch.setattr(history_batch_client, "_CACHED_PROVIDER", None)


# ---- provider selection ------------------------------------------------------


def test_combined_role_selects_the_local_in_process_provider() -> None:
    provider = build_history_batch_provider()
    assert provider is history_batch_client._local_batch_provider


def test_fleet_coordinator_role_also_selects_the_local_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #2204 gate F5: a positive role match, not ``!= "clerk_agent"``."""
    monkeypatch.setattr(fleet_settings, "ROLE", "fleet_coordinator")

    provider = build_history_batch_provider()

    assert provider is history_batch_client._local_batch_provider


def test_clerk_agent_role_selects_the_remote_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fleet_settings, "ROLE", "clerk_agent")
    monkeypatch.setattr(fleet_settings, "COORDINATOR_URL", "http://127.0.0.1:8000")
    monkeypatch.setattr(fleet_settings, "AGENT_SERVICE_TOKEN", "svct_" + "1" * 32)
    monkeypatch.setattr(fleet_settings, "CLERK_ID", "clrk_probe")

    provider = build_history_batch_provider()

    assert provider.__self__.__class__ is RemoteHistoryBatchClient


@pytest.mark.parametrize(
    "missing",
    ["COORDINATOR_URL", "AGENT_SERVICE_TOKEN", "CLERK_ID"],
)
def test_construct_provider_still_raises_directly_for_an_unconfigured_clerk_agent(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    """The underlying construction logic still raises loudly in isolation --
    it is only ``build_history_batch_provider`` (the public seam) that
    catches it and converts it into a degrading provider (issue #2204 gate
    F7). Both are tested: this pins the raise; the next test pins the catch.
    """
    monkeypatch.setattr(fleet_settings, "ROLE", "clerk_agent")
    monkeypatch.setattr(fleet_settings, "COORDINATOR_URL", "http://127.0.0.1:8000")
    monkeypatch.setattr(fleet_settings, "AGENT_SERVICE_TOKEN", "svct_" + "1" * 32)
    monkeypatch.setattr(fleet_settings, "CLERK_ID", "clrk_probe")
    monkeypatch.setattr(fleet_settings, missing, None)

    with pytest.raises(HistoryClientMisconfigured):
        history_batch_client._construct_provider()


@pytest.mark.parametrize(
    "missing",
    ["COORDINATOR_URL", "AGENT_SERVICE_TOKEN", "CLERK_ID"],
)
async def test_clerk_agent_role_without_full_configuration_degrades_every_request(
    monkeypatch: pytest.MonkeyPatch, missing: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Issue #2204 gate F7: a misconfigured clerk_agent never crashes boot or
    a request -- it degrades every history request to
    ``coordinator_unavailable``, with one ERROR log at construction."""
    monkeypatch.setattr(fleet_settings, "ROLE", "clerk_agent")
    monkeypatch.setattr(fleet_settings, "COORDINATOR_URL", "http://127.0.0.1:8000")
    monkeypatch.setattr(fleet_settings, "AGENT_SERVICE_TOKEN", "svct_" + "1" * 32)
    monkeypatch.setattr(fleet_settings, "CLERK_ID", "clrk_probe")
    monkeypatch.setattr(fleet_settings, missing, None)

    with caplog.at_level(logging.ERROR):
        provider = build_history_batch_provider()
        batch = await provider(_query())

    assert batch.bars == []
    assert [n.code for n in batch.overlay_notices] == ["coordinator_unavailable"]
    assert any(
        record.levelno == logging.ERROR
        and getattr(record, "action", None) == "history_batch_provider_misconfigured"
        for record in caplog.records
    )


def test_a_misconfigured_provider_is_constructed_and_logged_exactly_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The cache (issue #2204 gate F7) means the ERROR log fires once, not
    once per request."""
    monkeypatch.setattr(fleet_settings, "ROLE", "clerk_agent")
    monkeypatch.setattr(fleet_settings, "COORDINATOR_URL", None)

    with caplog.at_level(logging.ERROR):
        first = build_history_batch_provider()
        second = build_history_batch_provider()

    assert first is second
    matches = [
        record
        for record in caplog.records
        if getattr(record, "action", None) == "history_batch_provider_misconfigured"
    ]
    assert len(matches) == 1


async def test_local_provider_never_leaves_process_without_calling_coordinator_builder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``combined``-posture provider is a direct call, never HTTP."""
    captured: dict[str, object] = {}

    async def fake_build(*, symbol, timeframe, required_bar_count, as_of_ms, polygon_api_key):
        captured.update(
            symbol=symbol,
            timeframe=timeframe,
            required_bar_count=required_bar_count,
            as_of_ms=as_of_ms,
            polygon_api_key=polygon_api_key,
        )
        return HistoryBatchResponse(
            bars=[], source="polygon", overlay_notices=[], effective_as_of_ms=as_of_ms
        )

    monkeypatch.setattr(history_batch_client, "build_coordinator_history_batch", fake_build)
    monkeypatch.setattr(settings, "POLYGON_API_KEY", "local-key")

    result = await history_batch_client._local_batch_provider(_query())

    assert captured == {
        "symbol": "SPY",
        "timeframe": "1m",
        "required_bar_count": 300,
        "as_of_ms": _NOW,
        "polygon_api_key": "local-key",
    }
    assert result.bars == []


# ---- RemoteHistoryBatchClient transport failure absorption -------------------


async def _run_with_transport(monkeypatch: pytest.MonkeyPatch, handler) -> HistoryBatchResponse:
    def _fake_build_internal_client(*, read_timeout_s=None, **_kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(history_batch_client, "build_internal_client", _fake_build_internal_client)
    client = RemoteHistoryBatchClient(
        base_url="http://127.0.0.1", clerk_id="clrk_x", agent_service_token="tok"
    )
    return await client.fetch_batch(_query())


async def test_connection_failure_degrades_to_coordinator_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    batch = await _run_with_transport(monkeypatch, _handler)

    assert batch.bars == []
    assert [n.code for n in batch.overlay_notices] == ["coordinator_unavailable"]
    message = batch.overlay_notices[0].message
    assert "127.0.0.1" not in message
    assert "tok" not in message


async def test_timeout_degrades_to_coordinator_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    batch = await _run_with_transport(monkeypatch, _handler)

    assert batch.bars == []
    assert [n.code for n in batch.overlay_notices] == ["coordinator_unavailable"]


async def test_non_200_status_degrades_to_coordinator_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"reason": "fleet_presence_unavailable"})

    batch = await _run_with_transport(monkeypatch, _handler)

    assert batch.bars == []
    assert [n.code for n in batch.overlay_notices] == ["coordinator_unavailable"]


async def test_a_4xx_and_a_5xx_refusal_log_distinct_actions(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Issue #2204 gate F8: a 4xx (the coordinator reached and refused) and a
    5xx (the coordinator failed) are distinguishable in the log, even though
    the public notice stays the generic ``coordinator_unavailable`` either
    way -- and neither log line claims the coordinator "could not be
    reached" when it plainly was."""

    def _handler_422(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": "invalid"})

    def _handler_500(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "boom"})

    with caplog.at_level(logging.WARNING):
        await _run_with_transport(monkeypatch, _handler_422)
        await _run_with_transport(monkeypatch, _handler_500)

    actions_by_level = {
        (record.levelno, getattr(record, "action", None)) for record in caplog.records
    }
    assert (logging.ERROR, "history_batch_refused_4xx") in actions_by_level
    assert (logging.WARNING, "history_batch_refused_5xx") in actions_by_level


async def test_malformed_success_body_degrades_to_coordinator_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json at all")

    batch = await _run_with_transport(monkeypatch, _handler)

    assert batch.bars == []
    assert [n.code for n in batch.overlay_notices] == ["coordinator_unavailable"]


async def test_unexpected_success_shape_degrades_to_coordinator_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"bars": "not-a-list", "source": "polygon", "effective_as_of_ms": _NOW},
        )

    batch = await _run_with_transport(monkeypatch, _handler)

    assert batch.bars == []
    assert [n.code for n in batch.overlay_notices] == ["coordinator_unavailable"]


async def test_a_mismatched_effective_as_of_ms_degrades_to_coordinator_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #2204 gate F2: the Clerk itself checks ``effective_as_of_ms``
    agrees with what it asked for -- a coordinator that silently answered a
    different instant is exactly as unusable as one that never answered."""

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"bars": [], "source": "polygon", "overlay_notices": [], "effective_as_of_ms": _NOW + 1},
        )

    batch = await _run_with_transport(monkeypatch, _handler)

    assert batch.bars == []
    assert [n.code for n in batch.overlay_notices] == ["coordinator_unavailable"]


async def test_healthy_response_round_trips_bars_and_notices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "bars": [
                    {
                        "start_ms": _NOW - 60_000,
                        "end_ms": _NOW,
                        "open": "1.0",
                        "high": "2.0",
                        "low": "0.5",
                        "close": "1.5",
                        "volume": 10,
                        "source": "polygon",
                    }
                ],
                "source": "polygon",
                "overlay_notices": [],
                "effective_as_of_ms": _NOW,
            },
        )

    batch = await _run_with_transport(monkeypatch, _handler)

    assert len(batch.bars) == 1
    assert batch.bars[0].start_ms == _NOW - 60_000
    assert batch.overlay_notices == []
    assert batch.effective_as_of_ms == _NOW


async def test_fetch_batch_builds_its_client_with_the_inner_read_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def _fake_build_internal_client(*, read_timeout_s=None, **_kwargs):
        captured["read_timeout_s"] = read_timeout_s
        return httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={
                        "bars": [],
                        "source": "polygon",
                        "overlay_notices": [],
                        "effective_as_of_ms": _NOW,
                    },
                )
            )
        )

    monkeypatch.setattr(history_batch_client, "build_internal_client", _fake_build_internal_client)
    client = RemoteHistoryBatchClient(
        base_url="http://127.0.0.1", clerk_id="clrk_x", agent_service_token="tok"
    )

    await client.fetch_batch(_query())

    assert captured["read_timeout_s"] == HISTORY_BATCH_INNER_TIMEOUT_S


async def test_fetch_batch_sends_no_token_in_the_request_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No key or token appears in the payload (FR-010) -- only the identity
    headers carry the token; the JSON body carries none of it."""
    seen_bodies: list[bytes] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen_bodies.append(request.content)
        return httpx.Response(
            200,
            json={"bars": [], "source": "polygon", "overlay_notices": [], "effective_as_of_ms": _NOW},
        )

    await _run_with_transport(monkeypatch, _handler)

    assert seen_bodies
    assert b"tok" not in seen_bodies[0]
