"""Recency snapshot persistence transport.

Deliberately different failure semantics from the legacy
``persist_via_dotnet``/``persist_engine_run`` helpers, which swallow HTTP
failures and return ``None`` (persistence is best-effort there because the
in-memory/on-disk artifact remains authoritative). For the Recency Chart,
the persisted snapshot IS the sole source of truth (design spec §4.1) — a
failed persist must raise so the caller (``run_recency``) counts it as a
failed run rather than silently succeeding with nothing durable written.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from app.research.recency.persist_client import persist_recency_snapshot
from app.research.recency.runner import RecencyRunSnapshot, RecencyTradeSnapshot


def _snapshot() -> RecencyRunSnapshot:
    return RecencyRunSnapshot(
        launch_id="launch-1",
        symbol="SPY",
        strategy_key="ema_crossover_2_bps",
        params={"gap_bps": 2.0},
        params_hash="hash1",
        total_pnl=20.0,
        sharpe=None,
        trades=[
            RecencyTradeSnapshot(
                fingerprint="fp1",
                entry_ms=100,
                exit_ms=200,
                pnl_pts=2.0,
                pnl_pct=0.02,
                quantity=10,
                pnl=20.0,
                holding_sessions=1,
            )
        ],
    )


@respx.mock
def test_posts_the_snapshot_and_returns_the_assigned_run_id() -> None:
    route = respx.post("http://backend.test/api/recency/snapshots").mock(
        return_value=httpx.Response(200, json={"recency_run_id": 42})
    )

    result = persist_recency_snapshot(_snapshot(), base_url="http://backend.test")

    assert result == 42
    assert route.called
    sent_body = json.loads(route.calls.last.request.content)
    assert sent_body["launch_id"] == "launch-1"
    assert sent_body["params_hash"] == "hash1"
    assert sent_body["trades"][0]["fingerprint"] == "fp1"


@respx.mock
def test_raises_on_server_error_instead_of_swallowing() -> None:
    respx.post("http://backend.test/api/recency/snapshots").mock(return_value=httpx.Response(500))

    with pytest.raises(httpx.HTTPStatusError):
        persist_recency_snapshot(_snapshot(), base_url="http://backend.test")


@respx.mock
def test_raises_on_network_failure_instead_of_swallowing() -> None:
    respx.post("http://backend.test/api/recency/snapshots").mock(side_effect=httpx.ConnectError("refused"))

    with pytest.raises(httpx.HTTPError):
        persist_recency_snapshot(_snapshot(), base_url="http://backend.test")
