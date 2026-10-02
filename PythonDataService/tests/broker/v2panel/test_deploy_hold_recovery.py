"""The deploy page names the fix for what actually blocks it.

When IB Gateway logged out overnight, the page showed two blockers whose fixes
pointed nowhere: "Resolve the Clerk hold from the Operator panel" (no such
action exists for a stream-health hold, which lifts by itself) and "Restore
both Clerk channels". Both now name the one thing the owner can do.
"""

from __future__ import annotations

import httpx
import pytest
from httpx import ASGITransport

from app.broker.alpaca.clerk.models import ChannelHealth, ClerkStatus, HoldState
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE,
    STREAM_HEALTH_HOLD_REASON_CODE,
)
from app.services.broker_v2_panel import panel_deploy
from app.utils.timestamps import now_ms_utc
from tests.broker.v2panel.fixtures import ACCT


def _install_clerk_status(
    monkeypatch: pytest.MonkeyPatch,
    *,
    hold: HoldState,
    market_data_connected: bool = True,
    market_data_reason: str = "",
) -> None:
    async def clerk_status(*, symbol: str | None = None) -> ClerkStatus:
        observed_at_ms = now_ms_utc()
        return ClerkStatus(
            broker="alpaca",
            account_id=ACCT,
            hold=hold,
            outstanding_intents=0,
            observed_at_ms=observed_at_ms,
            channel_healths=[
                ChannelHealth(
                    stream="market_data",
                    healthy=market_data_connected and not market_data_reason,
                    connected=market_data_connected,
                    reason=market_data_reason,
                    observed_at_ms=observed_at_ms,
                ),
                ChannelHealth(stream="execution", healthy=True, connected=True, observed_at_ms=observed_at_ms),
            ],
        )

    monkeypatch.setattr(panel_deploy, "clerk_status", clerk_status)


async def _deploy_view(fast_app, **params: str) -> dict:
    async with httpx.AsyncClient(transport=ASGITransport(app=fast_app), base_url="http://test") as client:
        resp = await client.get(f"/api/brokers/alpaca/accounts/{ACCT}/bots/deploy", params=params)
    assert resp.status_code == 200
    return resp.json()


def _check(body: dict, gate_id: str) -> dict:
    return next(check for check in body["readiness_checks"] if check["gate_id"] == gate_id)


_STREAM_HOLD = HoldState(
    active=True,
    reason_code=STREAM_HEALTH_HOLD_REASON_CODE,
    reason="A market-data or execution channel is unhealthy.",
    since_ms=1_790_916_315_993,
)


@pytest.mark.asyncio
async def test_a_logged_out_gateway_names_ib_gateway_on_both_blockers(
    deploy_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    fast_app, _registry = deploy_app
    _install_clerk_status(
        monkeypatch, hold=_STREAM_HOLD, market_data_connected=False, market_data_reason="IBKR connection lost"
    )

    body = await _deploy_view(fast_app)

    hold, channels = _check(body, "clerk.exposure_hold"), _check(body, "clerk.channel_health")
    assert "Log in to IB Gateway" in hold["recovery"]
    assert "Log in to IB Gateway" in channels["recovery"]
    assert channels["headline"] == "Deployment is blocked: IBKR market data is disconnected."
    assert "Operator panel" not in str(body)


@pytest.mark.asyncio
async def test_a_warming_symbol_is_not_blamed_on_ib_gateway(
    deploy_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Connected but not yet ready is warm-up, which logging in cannot fix."""
    fast_app, _registry = deploy_app
    _install_clerk_status(
        monkeypatch,
        hold=HoldState(active=False),
        market_data_reason="Active IBKR feed for SPY has not produced its first closed bar",
    )

    channels = _check(await _deploy_view(fast_app, symbol="SPY"), "clerk.channel_health")

    assert channels["ready"] is False
    assert "IB Gateway" not in channels["recovery"]
    assert channels["headline"] != "Deployment is blocked: IBKR market data is disconnected."


@pytest.mark.asyncio
async def test_a_stream_hold_that_outlives_its_outage_says_it_will_lift(
    deploy_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hold sync releases on its next tick; the page says so instead of sending the owner looking."""
    fast_app, _registry = deploy_app
    _install_clerk_status(monkeypatch, hold=_STREAM_HOLD)

    hold = _check(await _deploy_view(fast_app), "clerk.exposure_hold")

    assert hold["ready"] is False
    assert "lifts on its next connection check" in hold["recovery"]


@pytest.mark.asyncio
async def test_a_loss_hold_points_to_settings(deploy_app, monkeypatch: pytest.MonkeyPatch) -> None:
    fast_app, _registry = deploy_app
    _install_clerk_status(
        monkeypatch,
        hold=HoldState(
            active=True,
            reason_code=LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE,
            reason="Today's loss reached the account's limit.",
            since_ms=1_790_916_315_993,
        ),
    )

    hold = _check(await _deploy_view(fast_app), "clerk.exposure_hold")

    assert hold["recovery"] == "Review and clear the loss hold in this account's Settings, then refresh this page."
