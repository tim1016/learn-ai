"""Tests for live-runs control surface (PRD-A: UI-1, UI-3, UI-4).

Uses httpx.AsyncClient + ASGITransport(app=app) per repo testing rules.
Covers desired-state resolution (absent/PAUSED/STOPPED/corrupt/unknown),
the durable write API (persist + version bump), and the command channel
enqueue -> timeline readback.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.engine.live.desired_state import (
    DesiredState,
    DesiredStateRepo,
    stable_desired_state_path,
)
from app.main import app


@pytest.fixture
def live_runs_root(tmp_path, monkeypatch):
    """Temp live_runs root with IbkrSettings + router caches reset."""
    root = tmp_path / "live_runs"
    root.mkdir(parents=True)

    from app.broker.ibkr import config as ibkr_config

    ibkr_config.reset_settings_for_testing()
    monkeypatch.setenv("IBKR_LIVE_RUNS_ROOT", str(root))
    ibkr_config.reset_settings_for_testing()

    from app.routers import live_runs as lr

    lr._dir_cache.clear()
    lr._status_cache.clear()
    lr._log_tail_states.clear()

    yield root

    ibkr_config.reset_settings_for_testing()


def _ledger(run_id: str, sid: str = "") -> dict:
    now = int(time.time() * 1000)
    return {
        "schema_version": "1.1",
        "run_id": run_id,
        "code_sha": "abc" * 14,
        "strategy_spec_path": "/fake/spec.json",
        "strategy_spec_sha256": "sha" * 21,
        "qc_audit_copy_path": "/fake/qc.py",
        "qc_audit_copy_sha256": "qca" * 21,
        "qc_cloud_backtest_id": "QC-BT-001",
        "account_id": "DU123456",
        "start_date_ms": now - 86_400_000,
        "live_config": {},
        "strategy_instance_id": sid,
        "created_at_ms": now - 3_600_000,
    }


def _make_run(root: Path, run_id: str, sid: str = "") -> Path:
    run_dir = root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run_ledger.json").write_text(json.dumps(_ledger(run_id, sid)), encoding="utf-8")
    return run_dir


def _artifacts_root(live_runs_root: Path) -> Path:
    return live_runs_root.parent


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


_RID = "run-ctrl-" + "a" * 55


# --- UI-1: desired-state path_status resolution ---


async def test_desired_state_absent_effective_running(live_runs_root):
    _make_run(live_runs_root, _RID, sid="inst-absent")
    async with _client() as client:
        resp = await client.get(f"/api/live-runs/{_RID}/desired-state")
    assert resp.status_code == 200
    body = resp.json()
    assert body["path_status"] == "absent"
    assert body["state"] is None


async def test_desired_state_paused(live_runs_root):
    _make_run(live_runs_root, _RID, sid="inst-paused")
    repo = DesiredStateRepo(stable_desired_state_path(_artifacts_root(live_runs_root), "inst-paused"))
    repo.set(DesiredState.PAUSED, updated_by="op", reason="hold", now_ms=1_700_000_500_000)
    async with _client() as client:
        resp = await client.get(f"/api/live-runs/{_RID}/desired-state")
    body = resp.json()
    assert body["path_status"] == "ok"
    assert body["state"] == "PAUSED"
    assert body["updated_at_ms"] == 1_700_000_500_000
    assert isinstance(body["updated_at_ms"], int)
    assert body["version"] == 1


async def test_desired_state_stopped(live_runs_root):
    _make_run(live_runs_root, _RID, sid="inst-stop")
    repo = DesiredStateRepo(stable_desired_state_path(_artifacts_root(live_runs_root), "inst-stop"))
    repo.set(DesiredState.STOPPED, updated_by="op", now_ms=1_700_000_600_000)
    async with _client() as client:
        resp = await client.get(f"/api/live-runs/{_RID}/desired-state")
    body = resp.json()
    assert body["path_status"] == "ok"
    assert body["state"] == "STOPPED"


async def test_desired_state_corrupt(live_runs_root):
    _make_run(live_runs_root, _RID, sid="inst-corrupt")
    path = stable_desired_state_path(_artifacts_root(live_runs_root), "inst-corrupt")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not valid json", encoding="utf-8")
    async with _client() as client:
        resp = await client.get(f"/api/live-runs/{_RID}/desired-state")
    body = resp.json()
    assert body["path_status"] == "corrupt"
    assert body["state"] is None


async def test_desired_state_unknown_no_ledger_binding(live_runs_root):
    _make_run(live_runs_root, _RID, sid="")
    async with _client() as client:
        resp = await client.get(f"/api/live-runs/{_RID}/desired-state")
    body = resp.json()
    assert body["path_status"] == "unknown_no_ledger_binding"
    assert body["state"] is None


async def test_status_includes_controls_fields(live_runs_root):
    _make_run(live_runs_root, _RID, sid="inst-status")
    async with _client() as client:
        resp = await client.get(f"/api/live-runs/{_RID}/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["strategy_instance_id"] == "inst-status"
    assert body["desired_state"]["path_status"] == "absent"
    assert body["command_summary"]["pending_count"] == 0


# --- UI-3: durable desired-state write API ---


async def test_set_desired_state_persists_and_bumps_version(live_runs_root):
    _make_run(live_runs_root, _RID, sid="inst-write")
    async with _client() as client:
        first = await client.post(
            f"/api/live-runs/{_RID}/desired-state",
            json={"action": "pause", "reason": "r1", "updated_by": "op"},
        )
        second = await client.post(
            f"/api/live-runs/{_RID}/desired-state", json={"action": "resume"}
        )
    assert first.status_code == 200
    assert first.json()["state"] == "PAUSED"
    assert first.json()["version"] == 1
    assert isinstance(first.json()["updated_at_ms"], int)
    assert second.status_code == 200
    assert second.json()["state"] == "RUNNING"
    assert second.json()["version"] == 2

    repo = DesiredStateRepo(stable_desired_state_path(_artifacts_root(live_runs_root), "inst-write"))
    record = repo.read()
    assert record is not None
    assert record.desired_state == DesiredState.RUNNING
    assert record.version == 2


async def test_set_desired_state_stop_maps_to_stopped(live_runs_root):
    _make_run(live_runs_root, _RID, sid="inst-stopw")
    async with _client() as client:
        resp = await client.post(
            f"/api/live-runs/{_RID}/desired-state", json={"action": "stop"}
        )
    assert resp.status_code == 200
    assert resp.json()["state"] == "STOPPED"


async def test_set_desired_state_legacy_binding_conflict(live_runs_root):
    _make_run(live_runs_root, _RID, sid="")
    async with _client() as client:
        resp = await client.post(
            f"/api/live-runs/{_RID}/desired-state", json={"action": "pause"}
        )
    assert resp.status_code == 409


# --- UI-4: per-run command-channel API ---


async def test_enqueue_command_writes_pending_and_timeline_reads_it(live_runs_root):
    run_dir = _make_run(live_runs_root, _RID, sid="inst-cmd")
    async with _client() as client:
        enq = await client.post(
            f"/api/live-runs/{_RID}/commands", json={"verb": "FLATTEN"}
        )
        timeline = await client.get(f"/api/live-runs/{_RID}/commands")

    assert enq.status_code == 200
    assert enq.json()["verb"] == "FLATTEN"
    assert isinstance(enq.json()["seq"], int)

    files = list((run_dir / "commands").glob("command.*.pending.json"))
    assert len(files) == 1

    assert timeline.status_code == 200
    tl = timeline.json()
    assert len(tl["pending"]) == 1
    assert tl["pending"][0]["verb"] == "FLATTEN"
    assert tl["pending"][0]["seq"] == enq.json()["seq"]
    assert tl["acks"] == []


async def test_enqueue_command_invalid_verb_rejected(live_runs_root):
    _make_run(live_runs_root, _RID, sid="inst-cmd")
    async with _client() as client:
        resp = await client.post(f"/api/live-runs/{_RID}/commands", json={"verb": "NOPE"})
    assert resp.status_code == 400


async def test_command_summary_in_status(live_runs_root):
    _make_run(live_runs_root, _RID, sid="inst-cmd")
    async with _client() as client:
        await client.post(f"/api/live-runs/{_RID}/commands", json={"verb": "PAUSE"})
        resp = await client.get(f"/api/live-runs/{_RID}/status")
    cs = resp.json()["command_summary"]
    assert cs["pending_count"] == 1
    assert cs["acked_count"] == 0
    assert cs["latest_verb"] == "PAUSE"


async def test_command_timeline_reads_ack_files(live_runs_root):
    from app.engine.live.command_channel import CommandChannel, CommandVerb

    run_dir = _make_run(live_runs_root, _RID, sid="inst-ack")
    channel = CommandChannel(run_dir / "commands")
    cmd = channel.write_from_operator(CommandVerb.STOP)
    channel.ack(cmd, outcome={"status": "ok"})
    async with _client() as client:
        timeline = await client.get(f"/api/live-runs/{_RID}/commands")
    tl = timeline.json()
    assert tl["pending"] == []
    assert len(tl["acks"]) == 1
    assert tl["acks"][0]["verb"] == "STOP"
    assert tl["acks"][0]["outcome"]["status"] == "ok"


async def test_command_summary_latest_survives_ack(live_runs_root):
    """Regression: latest verb/seq must survive an ack (read from ack files)."""
    from app.engine.live.command_channel import CommandChannel, CommandVerb

    run_dir = _make_run(live_runs_root, _RID, sid="inst-cmd-ack")
    channel = CommandChannel(run_dir / "commands")
    cmd = channel.write_from_operator(CommandVerb.PAUSE)
    channel.ack(cmd, outcome={"status": "ok"})
    async with _client() as client:
        resp = await client.get(f"/api/live-runs/{_RID}/status")
    cs = resp.json()["command_summary"]
    assert cs["pending_count"] == 0
    assert cs["acked_count"] == 1
    assert cs["latest_verb"] == "PAUSE"
    assert cs["latest_seq"] == cmd.seq


# --- UI-1: status cache invalidation on control writes ---


async def test_status_cache_busts_on_desired_state_write(live_runs_root):
    """Regression: a desired-state write must invalidate the cached /status."""
    _make_run(live_runs_root, _RID, sid="inst-cache-ds")
    async with _client() as client:
        before = await client.get(f"/api/live-runs/{_RID}/status")
        assert before.json()["desired_state"]["path_status"] == "absent"

        repo = DesiredStateRepo(
            stable_desired_state_path(_artifacts_root(live_runs_root), "inst-cache-ds")
        )
        repo.set(DesiredState.PAUSED, updated_by="op", reason="hold", now_ms=1_700_000_700_000)

        after = await client.get(f"/api/live-runs/{_RID}/status")
    ds = after.json()["desired_state"]
    assert ds["path_status"] == "ok"
    assert ds["state"] == "PAUSED"


async def test_status_cache_busts_on_command_write(live_runs_root):
    """Regression: an enqueued command must invalidate the cached /status."""
    _make_run(live_runs_root, _RID, sid="inst-cache-cmd")
    async with _client() as client:
        before = await client.get(f"/api/live-runs/{_RID}/status")
        assert before.json()["command_summary"]["pending_count"] == 0

        await client.post(f"/api/live-runs/{_RID}/commands", json={"verb": "FLATTEN"})

        after = await client.get(f"/api/live-runs/{_RID}/status")
    cs = after.json()["command_summary"]
    assert cs["pending_count"] == 1
    assert cs["latest_verb"] == "FLATTEN"


async def test_path_traversal_run_id_rejected(live_runs_root):
    """Boundary: traversal/separator run_ids are rejected (never 200)."""
    async with _client() as client:
        for bad in ("..", "a%2Fb", "."):
            resp = await client.get(f"/api/live-runs/{bad}/status")
            assert resp.status_code in (400, 404)
