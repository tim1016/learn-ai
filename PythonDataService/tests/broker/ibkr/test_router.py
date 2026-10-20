"""Broker health, option-read validation, and stream error behavior without a Gateway."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.broker.ibkr.client import BrokerError, set_client
from app.main import app
from app.routers import broker as broker_router

_MIDNIGHT_UTC_EXPIRY_MS = 1_800_057_600_000  # 2027-01-16T00:00:00Z

# ── Phase 1 endpoints ──────────────────────────────────────────────────


def test_read_router_exposes_only_the_retained_feed_reads() -> None:
    assert {(route.path, tuple(sorted(route.methods))) for route in broker_router.read_router.routes} == {
        ("/health", ("GET",)),
        ("/expirations/{symbol}", ("GET",)),
        ("/strikes/{symbol}", ("GET",)),
        ("/option-chain/{symbol}", ("GET",)),
        ("/option-surface/{symbol}", ("GET",)),
    }


@pytest.mark.asyncio
async def test_health_returns_disconnected_when_no_client(monkeypatch) -> None:
    set_client(None)
    monkeypatch.setenv("IBKR_MODE", "paper")
    monkeypatch.setenv("IBKR_PORT", "4002")

    # Force settings reset so monkeypatched env applies.
    from app.broker.ibkr import config as cfg

    cfg.reset_settings_for_testing()
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            resp = await ac.get("/api/broker/health")

        assert resp.status_code == 200
        body = resp.json()
        assert body["connected"] is False
        assert body["mode"] == "paper"
        assert body["port"] == 4002
        assert body["account_id"] is None
    finally:
        # Drop the cached settings so subsequent tests see whatever env
        # the surrounding fixtures set, not the IBKR_PORT=4002 from above.
        cfg.reset_settings_for_testing()


async def test_expirations_returns_the_typed_anchor_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(broker_router, "require_connected_client", lambda: object())

    async def listed(_client: object, _symbol: str) -> list[int]:
        return [1_800_046_800_000]

    monkeypatch.setattr(broker_router.ibkr_contracts, "list_expirations", listed)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get("/api/broker/expirations/SPY")

    assert resp.status_code == 200
    assert resp.json() == {"symbol": "SPY", "expirations_ms": [1_800_046_800_000]}


async def test_chain_refuses_more_lines_than_the_default_ibkr_allocation() -> None:
    set_client(None)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get(
            "/api/broker/option-chain/SPY",
            params=[("expiry_ms", 1_800_046_800_000), *[("strikes", strike) for strike in range(1, 52)]],
        )
    assert resp.status_code == 422


@pytest.mark.parametrize("path", ["expirations", "strikes", "option-chain", "option-surface"])
async def test_invalid_equity_symbol_is_refused_before_gateway_access(path: str) -> None:
    set_client(None)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get(
            f"/api/broker/{path}/spy",
            params=[("expiry_ms", 1_800_046_800_000), ("strikes", 420)],
        )
    assert resp.status_code == 422


@pytest.mark.parametrize("path", ["expirations", "strikes", "option-chain", "option-surface"])
async def test_disabled_ibkr_feed_refuses_option_reads(path: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.broker.ibkr import config

    monkeypatch.setenv("IBKR_BROKER_ENABLED", "false")
    config.reset_settings_for_testing()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get(
                f"/api/broker/{path}/SPY",
                params=[("expiry_ms", 1_800_046_800_000), ("strikes", 420)],
            )
        assert resp.status_code == 503
    finally:
        config.reset_settings_for_testing()


@pytest.mark.parametrize("kind", ["chain", "surface"])
async def test_option_stream_reports_broker_refusal_without_exposing_its_text(
    monkeypatch: pytest.MonkeyPatch, kind: str,
) -> None:
    monkeypatch.setattr(broker_router, "require_connected_client", lambda: object())

    async def refused(*args, **kwargs):
        raise BrokerError("private Gateway detail")
        yield  # pragma: no cover - keeps the stream-shaped fake

    if kind == "chain":
        monkeypatch.setattr(broker_router, "stream_option_chain", refused)
        response = await broker_router.option_chain_stream(
            symbol="SPY", expiry_ms=1_800_046_800_000, strikes=[420], debounce_ms=250,
        )
    else:
        monkeypatch.setattr(broker_router, "stream_option_surface", refused)
        response = await broker_router.option_surface_stream(
            symbol="SPY", expiry_ms=[1_800_046_800_000], strikes=[420], debounce_ms=250,
            max_lines=100,
        )
    chunks = [chunk async for chunk in response.body_iterator]

    assert len(chunks) == 1
    assert "event: error" in chunks[0]
    assert "private Gateway detail" not in chunks[0]


async def test_chain_stream_does_not_describe_a_model_failure_as_a_bad_symbol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(broker_router, "require_connected_client", lambda: object())

    async def broken(*args, **kwargs):
        broker_router.IbkrExpirationsResponse.model_validate({})
        yield  # pragma: no cover - keeps the stream-shaped fake

    monkeypatch.setattr(broker_router, "stream_option_chain", broken)
    response = await broker_router.option_chain_stream(
        symbol="SPY", expiry_ms=1_800_046_800_000, strikes=[420], debounce_ms=250,
    )

    with pytest.raises(ValidationError):
        _ = [chunk async for chunk in response.body_iterator]


async def test_unexpected_stream_failure_propagates_without_a_false_error_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(broker_router, "require_connected_client", lambda: object())

    async def broken(*args, **kwargs):
        raise RuntimeError("unexpected stream failure")
        yield  # pragma: no cover - keeps the stream-shaped fake

    monkeypatch.setattr(broker_router, "stream_option_surface", broken)
    response = await broker_router.option_surface_stream(
        symbol="SPY", expiry_ms=[1_800_046_800_000], strikes=[420], debounce_ms=250,
        max_lines=100,
    )

    with pytest.raises(RuntimeError, match="unexpected stream failure"):
        _ = [chunk async for chunk in response.body_iterator]


# ── Boundary checks: with no client installed, each route answers its status before any Gateway access ──


@pytest.mark.parametrize(
    ("path", "params", "status"),
    [
        pytest.param("/api/broker/expirations/SPY", (), 503, id="expirations_disconnected"),
        # FastAPI rejects the missing required query before the handler runs.
        pytest.param(
            "/api/broker/option-chain/SPY", (("expiry_ms", 1_800_046_800_000),), 422, id="chain_strikes_missing",
        ),
        pytest.param(
            "/api/broker/option-chain/SPY", (("expiry_ms", 1_800_046_800_000), ("strikes", 0)), 400,
            id="chain_strike_non_positive",
        ),
        # Regression (B-13): expiry_ms <= 0 is rejected at the boundary with a 422, not passed into
        # expiry_ms_to_yyyymmdd to produce a 1970 date that silently matches nothing. Query validation
        # (gt=0) runs before the handler / connection check.
        pytest.param("/api/broker/strikes/SPY", (("expiry_ms", 0),), 422, id="b13_strikes_expiry_ms_non_positive"),
        pytest.param(
            "/api/broker/option-chain/SPY", (("expiry_ms", -5), ("strikes", 420)), 422,
            id="b13_chain_expiry_ms_non_positive",
        ),
        pytest.param("/api/broker/strikes/SPY", (("expiry_ms", 1_800_046_800_000),), 503, id="strikes_disconnected"),
        pytest.param("/api/broker/strikes/SPY", (), 422, id="strikes_expiry_ms_missing"),
        pytest.param("/api/broker/option-surface/SPY", (("strikes", 420),), 422, id="surface_expiries_missing"),
        pytest.param(
            "/api/broker/option-surface/SPY", (("expiry_ms", 1_800_046_800_000),), 422, id="surface_strikes_missing",
        ),
        # NaN slips past Pydantic's float coercion; the boundary guard catches it before contract qualification.
        pytest.param(
            "/api/broker/option-surface/SPY", (("expiry_ms", 1_800_046_800_000), ("strikes", "nan")), 400,
            id="surface_strike_nan",
        ),
        pytest.param(
            "/api/broker/option-surface/SPY",
            (("expiry_ms", 1_800_046_800_000), ("strikes", 420), ("strikes", 425)),
            503,
            id="surface_disconnected",
        ),
    ],
)
@pytest.mark.asyncio
async def test_option_routes_answer_their_boundary_status_with_no_client_installed(
    path: str, params: tuple[tuple[str, object], ...], status: int,
) -> None:
    set_client(None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get(path, params=list(params))

    assert resp.status_code == status, resp.text


@pytest.mark.parametrize(
    ("path", "params"),
    [
        ("/api/broker/strikes/SPY", [("expiry_ms", _MIDNIGHT_UTC_EXPIRY_MS)]),
        ("/api/broker/option-chain/SPY", [("expiry_ms", _MIDNIGHT_UTC_EXPIRY_MS), ("strikes", 420)]),
        (
            "/api/broker/option-surface/SPY",
            [("expiry_ms", 1_800_046_800_000), ("expiry_ms", _MIDNIGHT_UTC_EXPIRY_MS), ("strikes", 420)],
        ),
    ],
)
@pytest.mark.asyncio
async def test_an_expiry_that_is_not_16_00_new_york_on_its_date_is_refused(
    path: str, params: list[tuple[str, object]]
) -> None:
    """An expiry is one instant per date. Midnight UTC, how these routes once
    took it, would otherwise name the day before to a reader in New York."""
    set_client(None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get(path, params=params)

    assert resp.status_code == 422
    assert "is not an option expiry" in resp.text
    # It says what the instant is, and proposes no other date to ask for.
    assert "19:00:00.000 New York time on 2027-01-15" in resp.text


# 1969-12-30 at 16:00 New York time: on the anchor, and before the first instant these routes take.
_ANCHOR_BEFORE_1970_MS = -97_200_000


@pytest.mark.parametrize("path", ["strikes", "option-chain", "option-surface"])
@pytest.mark.parametrize(
    ("expiry_ms", "error_type"),
    [
        pytest.param(_ANCHOR_BEFORE_1970_MS, "greater_than", id="an expiry before 1970"),
        pytest.param(10**20, "less_than_equal", id="an instant no calendar holds"),
    ],
)
@pytest.mark.asyncio
async def test_an_expiry_out_of_range_is_refused_as_out_of_range(path: str, expiry_ms: int, error_type: str) -> None:
    """The range is checked before the date is asked for: an absurd value is a 422, never a crash."""
    set_client(None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get(
            f"/api/broker/{path}/SPY",
            params=[("expiry_ms", expiry_ms), ("strike", 420), ("strikes", 420), ("right", "C")],
        )

    assert resp.status_code == 422
    assert [error["type"] for error in resp.json()["detail"]] == [error_type]
