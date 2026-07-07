"""Integration tests for GET /api/live-runs/*.

Uses httpx.AsyncClient + ASGITransport(app=app) per repo testing rules.
Each test creates its own tmp live_runs root via a scoped fixture that
monkeypatches IBKR_LIVE_RUNS_ROOT and resets the IbkrSettings cache.

The router also has module-level LRU / TTL caches (_dir_cache, _status_cache,
_log_tail_states); these are cleared before each test via the fixture to
prevent cross-test pollution.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from httpx import ASGITransport, AsyncClient

from app.engine.live.halt import PoisonedHaltReason, PoisonedHaltTrigger
from app.main import app
from app.operator.incidents.safety_halt_notices import build_safety_halt_incident
from app.operator.incidents.store import IncidentStore
from app.schemas.live_runs import ExitReason, RunStatusSidecar

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ledger(run_id: str, account_id: str = "DU123456") -> dict:
    now = int(time.time() * 1000)
    return {
        "schema_version": "1.0",
        "run_id": run_id,
        "code_sha": "abc" * 14,
        "strategy_spec_path": "/fake/spec.json",
        "strategy_spec_sha256": "sha" * 21,
        "qc_audit_copy_path": "/fake/qc.py",
        "qc_audit_copy_sha256": "qca" * 21,
        "qc_cloud_backtest_id": "QC-BT-001",
        "account_id": account_id,
        "start_date_ms": now - 86_400_000,
        "live_config": {},
        "created_at_ms": now - 3600_000,
    }


def _sidecar(
    run_id: str,
    *,
    started_offset_s: float = 30.0,
    ended: bool = False,
    exit_reason: ExitReason | None = None,
) -> RunStatusSidecar:
    now = int(time.time() * 1000)
    started = now - int(started_offset_s * 1000)
    return RunStatusSidecar(
        run_id=run_id,
        started_at_ms=started,
        last_update_ms=now,
        ended_at_ms=now if ended else None,
        exit_code=0 if ended else None,
        exit_reason=exit_reason,
        host_pid=42,
    )


def _write_ledger(run_dir: Path, run_id: str, account_id: str = "DU123456") -> None:
    (run_dir / "run_ledger.json").write_text(json.dumps(_ledger(run_id, account_id)), encoding="utf-8")


def _write_sidecar(run_dir: Path, sc: RunStatusSidecar) -> None:
    (run_dir / "run_status.json").write_text(json.dumps(sc.model_dump()), encoding="utf-8")


def _write_decisions(run_dir: Path, n: int) -> None:
    t = pa.table({"signal": ["ENTER"] * n})
    pq.write_table(t, run_dir / "decisions.parquet")


def _write_decisions_dataset(run_dir: Path, values: list[str]) -> None:
    dataset_dir = run_dir / "decisions.parquet"
    dataset_dir.mkdir()
    pq.write_table(pa.table({"signal": values[:1]}), dataset_dir / "part-000001.parquet")
    if len(values) > 1:
        pq.write_table(pa.table({"signal": values[1:]}), dataset_dir / "part-000002.parquet")


def _write_executions_dataset(run_dir: Path) -> None:
    dataset_dir = run_dir / "executions.parquet"
    dataset_dir.mkdir()
    pq.write_table(
        pa.table({"ts_ms": [1_700_000_000_000], "exec_id": ["exec-1"]}),
        dataset_dir / "part-000001.parquet",
    )
    pq.write_table(
        pa.table({"ts_ms": [1_700_000_060_000], "exec_id": ["exec-2"]}),
        dataset_dir / "part-000002.parquet",
    )


def _write_log(run_dir: Path, content: str, *, mtime_offset_s: float = 0.0) -> None:
    log = run_dir / "live.log"
    log.write_text(content, encoding="utf-8")
    if mtime_offset_s != 0:
        target = time.time() - mtime_offset_s
        os.utime(log, (target, target))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def live_runs_root(tmp_path, monkeypatch):
    """Temp live_runs root with IbkrSettings cache reset."""
    root = tmp_path / "live_runs"
    root.mkdir()

    from app.broker.ibkr import config as ibkr_config

    ibkr_config.reset_settings_for_testing()
    monkeypatch.setenv("IBKR_LIVE_RUNS_ROOT", str(root))
    ibkr_config.reset_settings_for_testing()

    # Clear router-level caches to prevent cross-test contamination
    from app.routers import live_runs as lr

    lr._dir_cache.clear()
    lr._status_cache.clear()
    lr._log_tail_states.clear()

    yield root

    ibkr_config.reset_settings_for_testing()


# ---------------------------------------------------------------------------
# Individual state run-dir factories
# ---------------------------------------------------------------------------


def _make_running_run(root: Path) -> str:
    run_id = "run-running-" + "a" * 52
    run_dir = root / run_id
    run_dir.mkdir()
    sc = _sidecar(run_id, started_offset_s=30)
    _write_ledger(run_dir, run_id)
    _write_sidecar(run_dir, sc)
    bar_line = "2026-01-01T09:35:00+00:00 INFO [BAR] 2026-01-01T09:35:00+00:00 consolidator_emitted=1 snapshot=set\n"
    _write_log(run_dir, bar_line)
    _write_decisions(run_dir, 1)
    return run_id


def _make_warming_up_run(root: Path) -> str:
    run_id = "run-warmup-" + "b" * 53
    run_dir = root / run_id
    run_dir.mkdir()
    sc = _sidecar(run_id, started_offset_s=20)
    _write_ledger(run_dir, run_id)
    _write_sidecar(run_dir, sc)
    bar_line = "2026-01-01T09:35:00+00:00 INFO [BAR] 2026-01-01T09:35:00+00:00 consolidator_emitted=1 snapshot=set\n"
    _write_log(run_dir, bar_line)
    # No decisions.parquet
    return run_id


def _make_waiting_for_bars_run(root: Path) -> str:
    run_id = "run-waiting-" + "c" * 52
    run_dir = root / run_id
    run_dir.mkdir()
    sc = _sidecar(run_id, started_offset_s=5)
    _write_ledger(run_dir, run_id)
    _write_sidecar(run_dir, sc)
    _write_log(run_dir, "INFO startup\n")
    return run_id


def _make_halted_run(root: Path) -> str:
    run_id = "run-halted-" + "d" * 53
    run_dir = root / run_id
    run_dir.mkdir()
    _write_ledger(run_dir, run_id)
    (run_dir / "halt.flag").write_text('{"reason": "operator"}', encoding="utf-8")
    return run_id


def _make_poisoned_run(root: Path) -> str:
    run_id = "run-poisoned-" + "e" * 51
    run_dir = root / run_id
    run_dir.mkdir()
    _write_ledger(run_dir, run_id)
    (run_dir / "poisoned.flag").write_text('{"trigger": "OUTSIDE_MUTATION"}', encoding="utf-8")
    return run_id


def _make_complete_run(root: Path) -> str:
    run_id = "run-complete-" + "f" * 51
    run_dir = root / run_id
    run_dir.mkdir()
    sc = _sidecar(run_id, started_offset_s=3600, ended=True, exit_reason=ExitReason.normal)
    _write_ledger(run_dir, run_id)
    _write_sidecar(run_dir, sc)
    return run_id


def _make_stopped_run(root: Path) -> str:
    run_id = "run-stopped-" + "g" * 52
    run_dir = root / run_id
    run_dir.mkdir()
    sc = _sidecar(run_id, started_offset_s=3600, ended=True, exit_reason=ExitReason.keyboard_interrupt)
    _write_ledger(run_dir, run_id)
    _write_sidecar(run_dir, sc)
    return run_id


def _make_stale_run(root: Path) -> str:
    run_id = "run-stale-" + "h" * 54
    run_dir = root / run_id
    run_dir.mkdir()
    sc = _sidecar(run_id, started_offset_s=120)
    _write_ledger(run_dir, run_id)
    _write_sidecar(run_dir, sc)
    bar_line = "2026-01-01T09:35:00+00:00 INFO [BAR] 2026-01-01T09:35:00+00:00 consolidator_emitted=1 snapshot=set\n"
    _write_log(run_dir, bar_line, mtime_offset_s=120)  # log 120 s old
    return run_id


def _make_legacy_run(root: Path) -> str:
    run_id = "run-legacy-" + "i" * 53
    run_dir = root / run_id
    run_dir.mkdir()
    _write_ledger(run_dir, run_id)
    _write_log(run_dir, "INFO [START] run completed cleanly\n")
    return run_id


# ---------------------------------------------------------------------------
# GET /api/live-runs — list endpoint
# ---------------------------------------------------------------------------


async def test_list_runs_empty_root(live_runs_root):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-runs")
    assert response.status_code == 200
    assert response.json() == []


async def test_list_runs_returns_all_states(live_runs_root):
    _make_running_run(live_runs_root)
    _make_complete_run(live_runs_root)
    _make_halted_run(live_runs_root)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-runs")

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 3
    states = {item["state"] for item in data}
    assert "running" in states
    assert "complete" in states
    assert "halted" in states


async def test_list_runs_status_filter(live_runs_root):
    _make_running_run(live_runs_root)
    _make_complete_run(live_runs_root)
    _make_halted_run(live_runs_root)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-runs", params={"status": "running"})

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["state"] == "running"


async def test_list_runs_summary_fields(live_runs_root):
    _make_running_run(live_runs_root)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-runs")

    assert response.status_code == 200
    item = response.json()[0]
    # Verify required summary fields are present
    assert "run_id" in item
    assert "account_id" in item
    assert "state" in item
    assert "decision_count" in item
    assert "execution_count" in item
    assert "halt_flag_set" in item
    assert "poisoned_flag_set" in item
    assert item["account_id"] == "DU123456"


# ---------------------------------------------------------------------------
# GET /api/live-runs/{run_id}/status — per-run status
# ---------------------------------------------------------------------------


async def test_status_404_unknown_run(live_runs_root):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-runs/nonexistent-run-id/status")
    assert response.status_code == 404


@pytest.mark.parametrize(
    "factory, expected_state",
    [
        (_make_running_run, "running"),
        (_make_warming_up_run, "warming_up"),
        (_make_waiting_for_bars_run, "waiting_for_bars"),
        (_make_halted_run, "halted"),
        (_make_poisoned_run, "poisoned"),
        (_make_complete_run, "complete"),
        (_make_stopped_run, "stopped"),
        (_make_stale_run, "stale"),
        (_make_legacy_run, "complete"),
    ],
    ids=[
        "running",
        "warming_up",
        "waiting_for_bars",
        "halted",
        "poisoned",
        "complete",
        "stopped",
        "stale",
        "legacy_complete",
    ],
)
async def test_status_per_state(live_runs_root, factory, expected_state):
    run_id = factory(live_runs_root)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/live-runs/{run_id}/status")

    assert response.status_code == 200
    data = response.json()
    assert data["run_id"] == run_id
    assert data["state"] == expected_state


async def test_status_has_required_sub_models(live_runs_root):
    run_id = _make_running_run(live_runs_root)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/live-runs/{run_id}/status")

    assert response.status_code == 200
    data = response.json()
    # All sub-models must be present
    assert "decisions" in data
    assert "executions" in data
    assert "trades" in data
    assert "flags" in data
    assert "artifacts" in data
    assert "reconcile" in data
    assert "fetched_at_ms" in data


async def test_status_decisions_count(live_runs_root):
    run_id = _make_running_run(live_runs_root)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/live-runs/{run_id}/status")

    assert response.status_code == 200
    data = response.json()
    assert data["decisions"]["row_count"] == 1


async def test_executions_endpoint_reads_segmented_dataset(live_runs_root):
    run_id = "run-exec-dataset-" + "x" * 47
    run_dir = live_runs_root / run_id
    run_dir.mkdir()
    _write_ledger(run_dir, run_id)
    _write_executions_dataset(run_dir)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/live-runs/{run_id}/executions?since_ms=1700000000000")

    assert response.status_code == 200
    assert response.json() == [{"ts_ms": 1_700_000_060_000, "exec_id": "exec-2"}]


async def test_status_reads_segmented_decisions_dataset(live_runs_root):
    run_id = "run-dataset-" + "s" * 52
    run_dir = live_runs_root / run_id
    run_dir.mkdir()
    _write_ledger(run_dir, run_id)
    _write_sidecar(run_dir, _sidecar(run_id, started_offset_s=30))
    _write_log(
        run_dir,
        "2026-01-01T09:35:00+00:00 INFO [BAR] 2026-01-01T09:35:00+00:00 consolidator_emitted=1 snapshot=set\n",
    )
    _write_decisions_dataset(run_dir, ["HOLD", "ENTER"])

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/live-runs/{run_id}/status")

    assert response.status_code == 200
    data = response.json()
    assert data["state"] == "running"
    assert data["decisions"]["row_count"] == 2
    assert data["decisions"]["latest_decision"]["signal"] == "ENTER"
    artifacts = {artifact["name"]: artifact for artifact in data["artifacts"]["files"]}
    assert artifacts["decisions.parquet"]["row_count"] == 2
    assert artifacts["decisions.parquet"]["size_bytes"] > 0


async def test_status_halt_flag_populated(live_runs_root):
    run_id = _make_halted_run(live_runs_root)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/live-runs/{run_id}/status")

    assert response.status_code == 200
    data = response.json()
    assert data["flags"]["halt_flag"] is not None


async def test_status_poisoned_flag_populated(live_runs_root):
    run_id = _make_poisoned_run(live_runs_root)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/live-runs/{run_id}/status")

    assert response.status_code == 200
    data = response.json()
    assert data["flags"]["poisoned_flag"] is not None


# ---------------------------------------------------------------------------
# GET /api/live-runs/{run_id}/log-tail — log tail endpoint
# ---------------------------------------------------------------------------


async def test_log_tail_404_unknown_run(live_runs_root):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-runs/nonexistent-run-id/log-tail")
    assert response.status_code == 404


async def test_log_tail_empty_when_no_log(live_runs_root):
    run_id = _make_halted_run(live_runs_root)  # no live.log

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/live-runs/{run_id}/log-tail")

    assert response.status_code == 200
    assert response.json() == []


async def test_log_tail_returns_bar_events(live_runs_root):
    run_id = _make_running_run(live_runs_root)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/live-runs/{run_id}/log-tail")

    assert response.status_code == 200
    lines = response.json()
    assert len(lines) >= 1
    bar_events = [ln for ln in lines if ln["event_type"] == "bar"]
    assert len(bar_events) >= 1
    # Bar event must carry ts_ms and consolidator_emitted
    assert bar_events[0]["ts_ms"] is not None
    assert bar_events[0]["consolidator_emitted"] is not None


async def test_log_tail_lines_param(live_runs_root):
    """lines= query param limits the returned count."""
    run_id = "run-multiline-" + "j" * 50
    run_dir = live_runs_root / run_id
    run_dir.mkdir()
    _write_ledger(run_dir, run_id)
    # Write 5 [BAR] lines
    content = ""
    for i in range(5):
        content += (
            f"2026-01-01T09:3{i}:00+00:00 INFO [BAR] 2026-01-01T09:3{i}:00+00:00 consolidator_emitted=1 snapshot=set\n"
        )
    _write_log(run_dir, content)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/live-runs/{run_id}/log-tail", params={"lines": 2})

    assert response.status_code == 200
    lines = response.json()
    assert len(lines) <= 2


async def test_log_tail_raw_lines_have_event_type_raw(live_runs_root):
    run_id = _make_waiting_for_bars_run(live_runs_root)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/live-runs/{run_id}/log-tail")

    assert response.status_code == 200
    lines = response.json()
    raw_lines = [ln for ln in lines if ln["event_type"] == "raw"]
    assert len(raw_lines) >= 1


# ---------------------------------------------------------------------------
# GET /api/live-runs/{run_id}/incidents — PR 6 of #565
# ---------------------------------------------------------------------------


async def test_incidents_404_unknown_run(live_runs_root):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-runs/nonexistent-run-id/incidents")
    assert response.status_code == 404


async def test_incidents_empty_when_no_log(live_runs_root):
    # _make_halted_run writes no live.log; the endpoint must return an
    # empty list rather than 404 / 500 in this case.
    run_id = _make_halted_run(live_runs_root)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/live-runs/{run_id}/incidents")

    assert response.status_code == 200
    assert response.json() == []


async def test_incidents_returns_safety_halt_operator_incident_without_log(live_runs_root):
    run_id = "run-safety-halt-" + "s" * 48
    run_dir = live_runs_root / run_id
    run_dir.mkdir()
    _write_ledger(run_dir, run_id)
    IncidentStore(run_dir).append(
        build_safety_halt_incident(
            strategy_instance_id="spy_ema_paper",
            run_id=run_id,
            halt_reason=PoisonedHaltReason(
                trigger=PoisonedHaltTrigger.COLD_START_DIVERGENCE,
                halted_at_ms=1_781_014_378_021,
                last_clean_bar_close_ms=1_781_014_300_000,
                details={"reason": "foreign_perm_id", "source": "reconciliation_orchestrator"},
            ),
            artifact_path=run_dir / "poisoned.flag",
            log_path=run_dir / "live.log",
        )
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/live-runs/{run_id}/incidents")

    assert response.status_code == 200
    rows = response.json()
    assert len(rows) == 1
    assert rows[0]["incident_category"] == "cold_start_divergence"
    assert rows[0]["incident_source"] == "app"
    assert rows[0]["dynamic_facts"]["run_id"] == run_id
    assert rows[0]["dynamic_facts"]["halt_trigger"] == "cold_start_divergence"
    assert rows[0]["dynamic_facts"]["artifact_path"].endswith("poisoned.flag")


async def test_incidents_dedupes_safety_halt_operator_incident_against_log(live_runs_root):
    run_id = "run-safety-dedupe-" + "d" * 46
    run_dir = live_runs_root / run_id
    run_dir.mkdir()
    _write_ledger(run_dir, run_id)
    halted_at_ms = 1_781_014_378_021
    IncidentStore(run_dir).append(
        build_safety_halt_incident(
            strategy_instance_id="spy_ema_paper",
            run_id=run_id,
            halt_reason=PoisonedHaltReason(
                trigger=PoisonedHaltTrigger.COLD_START_DIVERGENCE,
                halted_at_ms=halted_at_ms,
                last_clean_bar_close_ms=1_781_014_300_000,
                details={"reason": "foreign_perm_id", "source": "reconciliation_orchestrator"},
            ),
            artifact_path=run_dir / "poisoned.flag",
            log_path=run_dir / "live.log",
        )
    )
    _write_log(
        run_dir,
        "2026-06-09 14:12:58,021 CRITICAL app poison_sentinel.cold_start_divergence foreign_perm_id\n",
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/live-runs/{run_id}/incidents")

    assert response.status_code == 200
    rows = response.json()
    assert len([row for row in rows if row["incident_category"] == "cold_start_divergence"]) == 1
    assert rows[0]["dynamic_facts"]["run_id"] == run_id


async def test_incidents_preserves_distinct_same_time_safety_halt_log_rows(live_runs_root):
    run_id = "run-safety-halt-logs-" + "y" * 42
    run_dir = live_runs_root / run_id
    run_dir.mkdir()
    _write_ledger(run_dir, run_id)
    IncidentStore(run_dir).append(
        build_safety_halt_incident(
            strategy_instance_id="spy_ema_paper",
            run_id=run_id,
            halt_reason=PoisonedHaltReason(
                trigger=PoisonedHaltTrigger.COLD_START_DIVERGENCE,
                halted_at_ms=1_767_279_600_000,
                last_clean_bar_close_ms=1_767_279_540_000,
                details={"reason": "first path", "source": "reconciliation_orchestrator"},
            ),
            artifact_path=run_dir / "poisoned.flag",
            log_path=run_dir / "live.log",
        )
    )
    _write_log(
        run_dir,
        (
            "2026-01-01 15:00:00,000 ERROR app poison_sentinel.cold_start_divergence first path\n"
            "2026-01-01 15:00:00,000 ERROR app poison_sentinel.cold_start_divergence second path\n"
        ),
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/live-runs/{run_id}/incidents")

    assert response.status_code == 200
    rows = response.json()
    assert len(rows) == 3
    assert [row["message"] for row in rows[1:]] == [
        "poison_sentinel.cold_start_divergence first path",
        "poison_sentinel.cold_start_divergence second path",
    ]


async def test_incidents_returns_warning_error_critical_with_categories(live_runs_root):
    # Endpoint widens the legacy /failures shape (ERROR/CRITICAL only) to
    # include WARNING-level events, and tags each row with the backend-
    # classified incident_category the frontend's INCIDENT_COPY map keys on.
    run_id = "run-incidents-" + "k" * 50
    run_dir = live_runs_root / run_id
    run_dir.mkdir()
    _write_ledger(run_dir, run_id)
    content = (
        "2026-06-09 13:47:27,074 INFO __main__ [STEP 0] starting\n"
        # WARNING-level broker disconnect — only the incidents shape sees it.
        "2026-06-09 14:12:58,021 WARNING ib_async.wrapper Error 1100, reqId -1: lost\n"
        # ERROR with no recognised classifier rule → UNKNOWN fallback.
        "2026-06-09 14:13:00,000 ERROR my.module something unhelpful\n"
        # CRITICAL engine halt — classifies as ENGINE_FATAL.
        "2026-06-09 14:13:01,000 CRITICAL __main__ Unhandled exception in engine.run\n"
        "Traceback (most recent call last):\n"
        '  File "/app/run.py", line 1, in main\n'
        "    raise RuntimeError('boom')\n"
        "RuntimeError: boom\n"
    )
    _write_log(run_dir, content)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/live-runs/{run_id}/incidents")

    assert response.status_code == 200
    rows = response.json()
    assert len(rows) == 3

    # WARNING-level broker disconnect — present only on /incidents, not /failures.
    assert rows[0]["level"] == "WARNING"
    assert rows[0]["incident_category"] == "broker_disconnect"

    # Unrecognised message → UNKNOWN fallback (frontend INCIDENT_COPY uses this).
    assert rows[1]["level"] == "ERROR"
    assert rows[1]["incident_category"] == "unknown"

    # Engine fatal carries its traceback intact for the raw-log drawer.
    assert rows[2]["level"] == "CRITICAL"
    assert rows[2]["incident_category"] == "engine_fatal"
    assert rows[2]["traceback"] is not None
    assert "RuntimeError: boom" in rows[2]["traceback"]


async def test_incidents_since_ms_cursor_filters_to_newer_rows(live_runs_root):
    # The since_ms cursor lets the frontend poll incrementally without
    # re-shipping the full window every tick.
    run_id = "run-since-" + "m" * 54
    run_dir = live_runs_root / run_id
    run_dir.mkdir()
    _write_ledger(run_dir, run_id)
    content = (
        "2026-06-09 14:12:58,021 WARNING ib_async.wrapper Error 1100: lost\n"
        "2026-06-09 14:13:00,000 ERROR my.module unrecognised\n"
    )
    _write_log(run_dir, content)

    # Cursor matches the first row's ts_ms; only strictly-newer rows return.
    first_ts = 1781014378021  # 2026-06-09 14:12:58.021 parsed as UTC

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            f"/api/live-runs/{run_id}/incidents", params={"since_ms": first_ts}
        )

    assert response.status_code == 200
    rows = response.json()
    assert len(rows) == 1
    assert rows[0]["level"] == "ERROR"
