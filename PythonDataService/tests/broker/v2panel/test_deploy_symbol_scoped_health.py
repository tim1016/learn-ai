"""Symbol-scoped deploy-health wire contract (#1777 WP3, finding S6).

The deploy view asked for channel health *without* a symbol, so the
account-level market-data fact aggregated every subscribed symbol: one
symbol still warming up its first closed bar marked the whole feed
unhealthy and refused **every** deploy on the account — while the per-bot
admission gate underneath already evaluated health per symbol, correctly.

These tests pin the split decided in #1777:

* the generic view reports channel presence and connectivity only,
* ``?symbol=`` evaluates that symbol's warm-up,
* POST evaluates its own request's symbol rather than inheriting the
  generic verdict.
"""

from __future__ import annotations

import httpx
import pytest
from httpx import ASGITransport

from app.broker.alpaca.clerk.models import (
    ChannelHealth,
    ClerkStatus,
    HoldState,
)
from app.services.broker_v2_panel import panel_data_source
from app.utils.timestamps import now_ms_utc
from tests.broker.v2panel.conftest import _BODY, _HEALTHY_POSTURE
from tests.broker.v2panel.fixtures import ACCT

_WARMING = "AAPL"
_READY = "SPY"
_ALLOW_BODY_STRATEGY = frozenset({("ema_crossover_signal", ACCT)})


def _install_warming_symbol(monkeypatch: pytest.MonkeyPatch) -> None:
    """One symbol warming, one ready, feed connected throughout.

    Mirrors ``ibkr_feed.health``: the unscoped call aggregates every active
    symbol, so it reports unhealthy-but-connected while ``_WARMING`` has
    not produced its first closed bar.
    """

    async def clerk_status(*, symbol: str | None = None) -> ClerkStatus:
        observed_at_ms = now_ms_utc()
        warming = symbol is None or symbol == _WARMING
        reason = (
            f"Active IBKR feed for {_WARMING} has not produced its first closed bar"
            if warming
            else ""
        )
        return ClerkStatus(
            broker="alpaca",
            account_id=ACCT,
            hold=HoldState(active=False),
            outstanding_intents=0,
            observed_at_ms=observed_at_ms,
            channel_healths=[
                ChannelHealth(
                    stream="market_data",
                    healthy=not warming,
                    connected=True,
                    reason=reason,
                    observed_at_ms=observed_at_ms,
                ),
                ChannelHealth(
                    stream="execution",
                    healthy=True,
                    connected=True,
                    observed_at_ms=observed_at_ms,
                ),
            ],
            operator_posture=_HEALTHY_POSTURE,
        )

    monkeypatch.setattr(panel_data_source, "_clerk_status", clerk_status)


def _channel_gate(body: dict) -> dict:
    return next(
        check
        for check in body["readiness_checks"]
        if check["gate_id"] == "clerk.channel_health"
    )


@pytest.mark.asyncio
async def test_generic_view_stays_eligible_while_one_symbol_warms(
    deploy_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S6: warm-up of one symbol must not refuse deploys account-wide."""
    fast_app, _registry = deploy_app
    _install_warming_symbol(monkeypatch)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=fast_app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/api/brokers/alpaca/accounts/{ACCT}/bots/deploy")

    assert resp.status_code == 200
    assert _channel_gate(resp.json())["ready"] is True


@pytest.mark.asyncio
async def test_symbol_scoped_view_refuses_the_warming_symbol(
    deploy_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal names the symbol, from the health sample's own reason."""
    fast_app, _registry = deploy_app
    _install_warming_symbol(monkeypatch)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=fast_app), base_url="http://test"
    ) as client:
        resp = await client.get(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots/deploy",
            params={"symbol": _WARMING},
        )

    assert resp.status_code == 200
    gate = _channel_gate(resp.json())
    assert gate["ready"] is False
    assert _WARMING in gate["explanation"]


@pytest.mark.asyncio
async def test_symbol_scoped_view_admits_the_ready_symbol(
    deploy_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    fast_app, _registry = deploy_app
    _install_warming_symbol(monkeypatch)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=fast_app), base_url="http://test"
    ) as client:
        resp = await client.get(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots/deploy",
            params={"symbol": _READY},
        )

    assert resp.status_code == 200
    assert _channel_gate(resp.json())["ready"] is True


@pytest.mark.asyncio
async def test_deploy_evaluates_its_own_symbol_while_another_warms(
    deploy_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST must not inherit the generic view's channel verdict."""
    fast_app, _registry = deploy_app
    monkeypatch.setattr(
        "app.services.canary_admission.CANARY_ADMITTED_PROGRAM_ACCOUNT_PAIRS",
        _ALLOW_BODY_STRATEGY,
    )
    _install_warming_symbol(monkeypatch)
    assert _BODY["symbol"] == _READY, "fixture must deploy the ready symbol"

    async with httpx.AsyncClient(
        transport=ASGITransport(app=fast_app), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots", json=_BODY
        )

    assert resp.status_code == 201, resp.text
    assert resp.json()["outcome"] == "success"
