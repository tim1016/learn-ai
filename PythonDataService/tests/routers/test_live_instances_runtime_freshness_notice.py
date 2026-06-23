"""Regression test for #657 — runtime_freshness.headline + additional_reasons wired.

Verifies that ``OperatorSurfaceRuntimeFreshness`` surfaces ``headline``
and ``additional_reasons`` populated by ``compose_runtime_freshness_notices``
when the engine-runtime snapshot is missing (ENGINE_RUNTIME_MISSING code
active).

Wire path under test:
    _project_runtime_freshness  →  compose_runtime_freshness_notices
    →  OperatorSurfaceRuntimeFreshness.{headline, additional_reasons}
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from app.engine.live import host_daemon_client
from app.routers import live_instances
from app.services.operator_surface import _project_runtime_freshness
from app.services.runtime_freshness import DomainFreshness, RuntimeFreshness
from tests._fixtures.daemon_transport import as_typed_get


def _write_ledger(root: Path, run_id: str, sid: str, created_at_ms: int) -> None:
    import json

    run_dir = root / run_id
    run_dir.mkdir(parents=True)
    payload: dict = {
        "run_id": run_id,
        "strategy_instance_id": sid,
        "created_at_ms": created_at_ms,
    }
    (run_dir / "run_ledger.json").write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture
def app_with_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "live_runs"
    root.mkdir()
    stub = SimpleNamespace(
        live_runs_root=str(root),
        live_runner_daemon_url="http://daemon",
        live_runner_host_start_command="",
        fleet_dirty_blocks_starts=False,
        mode="paper",
        readonly=False,
    )
    monkeypatch.setattr(live_instances, "get_settings", lambda: stub)
    from app.main import app

    return app, root


def _set_daemon(monkeypatch: pytest.MonkeyPatch, *, process: dict | None = None) -> None:
    async def fake_instances(_base_url: str):
        return as_typed_get(None)

    async def fake_process(_base_url: str, _sid: str):
        return as_typed_get(process)

    monkeypatch.setattr(host_daemon_client, "fetch_instances", fake_instances)
    monkeypatch.setattr(host_daemon_client, "fetch_instance_process", fake_process)


# ---------------------------------------------------------------------------
# Regression: #657 — headline + additional_reasons populated when runtime stale
# ---------------------------------------------------------------------------


async def test_runtime_freshness_notice_headline_present_when_engine_runtime_missing(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When there is no engine-runtime snapshot, ENGINE_RUNTIME_MISSING is
    active and the composer must produce a non-None ``headline`` notice
    with ``code == "runtime.engine_runtime_incompatible"`` and an empty
    ``additional_reasons`` list (single matched rule became the headline).

    Before the Task-5 wiring this test fails with:
        AssertionError: headline is None / additional_reasons is []
    """
    app, root = app_with_root
    _write_ledger(root, "run-notice-1", "spy_ema_paper", 100)
    _set_daemon(
        monkeypatch,
        process={"state": "running", "run_id": "run-notice-1", "pid": 1, "started_at_ms": 100},
    )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/live-instances/spy_ema_paper/status")

    assert response.status_code == 200
    rf = response.json()["operator_surface"]["runtime_freshness"]

    # Shape: new fields must exist.
    assert "headline" in rf, "headline field missing from runtime_freshness"
    assert "additional_reasons" in rf, "additional_reasons field missing from runtime_freshness"

    # With no engine-runtime snapshot, ENGINE_RUNTIME_MISSING is active.
    assert "ENGINE_RUNTIME_MISSING" in rf["stale_reason_codes"]

    # Composer must surface a non-None headline.
    headline = rf["headline"]
    assert headline is not None, "Expected headline notice but got None"
    assert headline["code"] == "runtime.engine_runtime_incompatible"
    assert headline["tier"] == "critical"
    assert isinstance(headline["title"], str) and headline["title"]
    assert isinstance(headline["message"], str) and headline["message"]
    assert "ENGINE_RUNTIME_MISSING" in headline["source_codes"]

    # additional_reasons must be a list; the single matched rule became the
    # headline, so there are no additional reasons in this scenario.
    additional_reasons = rf["additional_reasons"]
    assert isinstance(additional_reasons, list)
    assert additional_reasons == []


async def test_runtime_freshness_notice_headline_none_when_runtime_fresh(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the engine runtime is fully fresh, ``headline`` must be ``None``
    and ``additional_reasons`` must be an empty list."""
    from app.engine.live.engine_runtime import (
        BarLoopBlock,
        BrokerBlock,
        CommandLoopBlock,
        ControlPlaneBlock,
        EngineRuntimeSnapshot,
        write_engine_runtime_snapshot,
    )

    app, root = app_with_root
    now_ms = 1_772_463_600_000  # 2026-03-02 10:00:00 America/New_York
    _write_ledger(root, "run-notice-2", "spy_ema_paper", 100)
    write_engine_runtime_snapshot(
        root / "run-notice-2",
        EngineRuntimeSnapshot(
            strategy_instance_id="spy_ema_paper",
            run_id="run-notice-2",
            pid=123,
            process_start_identity="child-1",
            expected_daemon_boot_id="boot-1",
            snapshot_seq=1,
            written_at_ms=now_ms,
            command_loop=CommandLoopBlock(
                heartbeat_at_ms=now_ms,
                state="PAUSED",
            ),
            broker=BrokerBlock(
                identity="PAPER_VERIFIED",
                submission_capability="PAPER_ORDERS_ENABLED",
                effective_posture="PAPER_EXECUTION",
                connection_state="connected",
                connection_epoch=1,
                connected_account="DU123",
                port_class="paper_port",
                observation_at_ms=now_ms,
                probe_completed_at_ms=now_ms,
                reconnect_attempt=0,
            ),
            bar_loop=BarLoopBlock(
                heartbeat_at_ms=now_ms,
                latest_source_bar_ms=now_ms,
                expected_interval_ms=60_000,
            ),
            control_plane=ControlPlaneBlock(
                lease_observed_at_ms=now_ms,
                observed_daemon_boot_id="boot-1",
            ),
        ),
    )
    _set_daemon(
        monkeypatch,
        process={"state": "running", "run_id": "run-notice-2", "pid": 123, "started_at_ms": 100},
    )
    monkeypatch.setattr(live_instances, "_now_ms", lambda: now_ms)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/live-instances/spy_ema_paper/status")

    assert response.status_code == 200
    rf = response.json()["operator_surface"]["runtime_freshness"]

    assert rf["stale_reason_codes"] == []
    assert rf["headline"] is None
    assert rf["additional_reasons"] == []


# ---------------------------------------------------------------------------
# Unit tests for _project_runtime_freshness — suppress_banner logic
# ---------------------------------------------------------------------------


def _fresh() -> DomainFreshness:
    """Helper: create a fresh DomainFreshness with no stale codes."""
    return DomainFreshness(state="FRESH", age_ms=0, stale_reason_codes=[])


def _stale(code: str, age_ms: int = 99_000) -> DomainFreshness:
    """Helper: create a stale DomainFreshness with the given code and age."""
    return DomainFreshness(state="STALE", age_ms=age_ms, stale_reason_codes=[code])


def test_runtime_freshness_projection_session_closed_suppresses_headline_only():
    """When BAR_LOOP_SESSION_CLOSED is the only stale reason, headline must
    be None (suppressed), but additional_reasons must still include the
    runtime.market_closed notice.

    This test validates that suppress_banner works: the headline is excluded
    from the surface while the additional_reasons list still captures the info
    tier notice for operator awareness.
    """
    runtime = RuntimeFreshness(
        posture_demoted=False,
        command_loop=_fresh(),
        broker=_fresh(),
        bar_loop=_stale("BAR_LOOP_SESSION_CLOSED", age_ms=0),
        control_plane=_fresh(),
    )

    surface = _project_runtime_freshness(runtime)

    assert surface is not None
    assert surface.headline is None
    assert len(surface.additional_reasons) == 1
    assert surface.additional_reasons[0].code == "runtime.market_closed"
    assert surface.additional_reasons[0].tier == "info"
