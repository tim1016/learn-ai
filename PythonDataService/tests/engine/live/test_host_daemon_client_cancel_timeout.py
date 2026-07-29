"""Operator cancel forwards must give the outer HTTP hop a budget that covers
the inner Clerk RPC.

``cancel_exact_order`` and ``cancel_pending_a0`` acquire the Clerk's broker-write
lock, so their inner Unix-socket RPC budget is the submit-sized 240s. If the
outer container -> host-daemon HTTP read is shorter, it expires first and the
operator still sees OUTCOME_UNKNOWN behind even one 25s broker write. The outer
timeout must therefore be >= the inner budget.
"""

from __future__ import annotations

from app.engine.live import host_daemon_client
from app.engine.live.account_clerk_rpc_protocol import ACCOUNT_CLERK_RPC_SUBMIT_TIMEOUT_S


async def test_operator_exact_cancel_uses_timeout_covering_inner_clerk_budget(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_post_action(url, payload, *, timeout=host_daemon_client._TIMEOUT):
        captured["timeout"] = timeout
        return {}

    monkeypatch.setattr(host_daemon_client, "_post_action", fake_post_action)

    await host_daemon_client.operator_exact_cancel("http://d", "DU1", {})

    timeout = captured["timeout"]
    assert timeout.read >= ACCOUNT_CLERK_RPC_SUBMIT_TIMEOUT_S


async def test_operator_pending_cancel_uses_timeout_covering_inner_clerk_budget(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_post_action(url, payload, *, timeout=host_daemon_client._TIMEOUT):
        captured["timeout"] = timeout
        return {}

    monkeypatch.setattr(host_daemon_client, "_post_action", fake_post_action)

    await host_daemon_client.operator_pending_cancel("http://d", "DU1", {})

    timeout = captured["timeout"]
    assert timeout.read >= ACCOUNT_CLERK_RPC_SUBMIT_TIMEOUT_S
