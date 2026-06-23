"""Contract tests for the instance-addressed operator console API (ADR 0004).

The host daemon is faked at the client seam (no network); liveness is resolved
server-side and the serialized response carries both `live_binding` and
`evidence_binding` so the client cannot confuse them.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from app.engine.live import host_daemon_client
from app.routers import live_instances
from tests._fixtures.daemon_transport import as_typed_get


def _write_ledger(
    root: Path, run_id: str, sid: str, created_at_ms: int, spec_path: Path | None = None
) -> None:
    run_dir = root / run_id
    run_dir.mkdir(parents=True)
    payload: dict = {"run_id": run_id, "strategy_instance_id": sid, "created_at_ms": created_at_ms}
    if spec_path is not None:
        payload["strategy_spec_path"] = str(spec_path)
    (run_dir / "run_ledger.json").write_text(json.dumps(payload), encoding="utf-8")


def _write_live_state(root: Path, sid: str, run_id: str, positions: dict[str, int]) -> None:
    live_state_dir = root.parent / "live_state" / sid
    live_state_dir.mkdir(parents=True, exist_ok=True)
    (live_state_dir / "live_state.json").write_text(
        json.dumps(
            {
                "strategy_instance_id": sid,
                "run_id": run_id,
                "bot_order_namespace": f"{sid}_ns",
                "ib_client_id": 42,
                "expected_position_by_symbol": positions,
                "last_processed_bar_ms": 1,
                "last_artifact_flush_ms": 1,
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture
def app_with_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "live_runs"
    root.mkdir()
    stub = SimpleNamespace(
        live_runs_root=str(root),
        live_runner_daemon_url="http://daemon",
        live_runner_host_start_command="",
        fleet_dirty_blocks_starts=False,
        # Mirror the real default env (IBKR_MODE=paper, IBKR_READONLY=false) so
        # start_defaults resolves to place-orders; dedicated tests override.
        mode="paper",
        readonly=False,
    )
    monkeypatch.setattr(live_instances, "get_settings", lambda: stub)
    from app.main import app

    return app, root


def _set_daemon(
    monkeypatch: pytest.MonkeyPatch, *, instances: dict | None = None, process: dict | None = None
) -> None:
    async def fake_instances(_base_url: str):
        return as_typed_get(instances)

    async def fake_process(_base_url: str, _sid: str):
        return as_typed_get(process)

    monkeypatch.setattr(host_daemon_client, "fetch_instances", fake_instances)
    monkeypatch.setattr(host_daemon_client, "fetch_instance_process", fake_process)


async def test_instance_status_running_exposes_live_binding(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-live-aaa", "spy_ema_paper", 100)
    _set_daemon(
        monkeypatch,
        process={"state": "running", "run_id": "run-live-aaa", "pid": 99, "started_at_ms": 100},
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_ema_paper/status")

    assert response.status_code == 200
    body = response.json()
    assert body["process"]["state"] == "running"
    assert body["process"]["bound_run_id"] == "run-live-aaa"
    assert body["live_binding"]["run_id"] == "run-live-aaa"
    assert body["live_binding"]["source"] == "registry"
    assert body["evidence_binding"]["run_id"] == "run-live-aaa"
    assert body["evidence_binding"]["is_live"] is False
    assert body["desired_state"] is not None


async def test_instance_status_dead_is_evidence_only(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-old-bbb", "spy_ema_paper", 50)
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_ema_paper/status")

    assert response.status_code == 200
    body = response.json()
    assert body["process"]["state"] == "idle"
    assert body["live_binding"] is None
    assert body["evidence_binding"]["run_id"] == "run-old-bbb"


async def test_status_start_defaults_seed_strategy_from_ledger_key(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#416: the Start-card defaults seed ``strategy`` from the run ledger's
    ``strategy_key`` so the console never starts from a blank/hardcoded field."""
    app, root = app_with_root
    run_dir = root / "run-keyed"
    run_dir.mkdir(parents=True)
    (run_dir / "run_ledger.json").write_text(
        json.dumps(
            {
                "run_id": "run-keyed",
                "strategy_instance_id": "spy_ema_paper",
                "created_at_ms": 100,
                "strategy_key": "spy_ema_crossover",
            }
        ),
        encoding="utf-8",
    )
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_ema_paper/status")

    assert response.status_code == 200
    defaults = response.json()["start_defaults"]
    assert defaults["strategy"] == "spy_ema_crossover"
    # readonly now defaults to False in paper mode (the fixture stub has no
    # explicit mode → treated as paper); see the dedicated paper/live tests.
    assert defaults["readonly"] is False
    assert defaults["hydrate_policy"] == "require"
    assert defaults["max_orders_per_day"] == 50_000
    assert defaults["ibkr_host"] == "127.0.0.1"


async def test_status_start_defaults_empty_strategy_for_legacy_ledger(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A legacy ledger without ``strategy_key`` yields an empty ``strategy`` for
    the operator to supply — the field is present, just unseeded."""
    app, root = app_with_root
    _write_ledger(root, "run-legacy", "spy_ema_paper", 50)
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_ema_paper/status")

    assert response.status_code == 200
    assert response.json()["start_defaults"]["strategy"] == ""


async def test_status_start_defaults_carry_redeploy_identity_from_ledger(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Start-card defaults also carry the ledger's deploy identity (spec
    path, qc audit copy, qc backtest id, account) so the console can deep-link a
    one-click re-deploy (fresh run_id) to recover a poisoned/halted instance
    without the operator re-typing the deploy form."""
    app, root = app_with_root
    run_dir = root / "run-redeploy"
    run_dir.mkdir(parents=True)
    (run_dir / "run_ledger.json").write_text(
        json.dumps(
            {
                "run_id": "run-redeploy",
                "strategy_instance_id": "spy_ema_paper",
                "created_at_ms": 100,
                "strategy_key": "spy_ema_crossover",
                "strategy_spec_path": "PythonDataService/app/engine/strategy/spec/fixtures/spy_ema_crossover.spec.json",
                "qc_audit_copy_path": "references/qc-shadow/SpyEmaCrossoverAlgorithm.py",
                "qc_cloud_backtest_id": "d2fe45a7142e88575f6fbd75229f8681",
                "account_id": "DU1234567",
            }
        ),
        encoding="utf-8",
    )
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_ema_paper/status")

    assert response.status_code == 200
    defaults = response.json()["start_defaults"]
    assert defaults["strategy_spec_path"].endswith("spy_ema_crossover.spec.json")
    assert defaults["qc_audit_copy_path"] == "references/qc-shadow/SpyEmaCrossoverAlgorithm.py"
    assert defaults["qc_cloud_backtest_id"] == "d2fe45a7142e88575f6fbd75229f8681"
    assert defaults["account_id"] == "DU1234567"


async def test_chart_snapshot_today_returns_bars_and_runs(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slice 5: ``GET /chart-snapshot`` returns the day's bars + every run
    of the instance that touched the day. ``has_bars`` is True iff the
    response carries at least one bar."""
    app, root = app_with_root

    # Run with sidecar started_at_ms so it counts as "active today".
    # VCR-P3-I: ``today`` here must match the endpoint's _today_ny() — the
    # trading-day date in America/New_York, NOT UTC. Otherwise this test
    # flakes in the ~5h window every day where the two calendars disagree.
    from datetime import datetime
    from zoneinfo import ZoneInfo

    ny_tz = ZoneInfo("America/New_York")
    today = datetime.now(ny_tz).date()
    today_start_ms = int(
        datetime(today.year, today.month, today.day, tzinfo=ny_tz).timestamp() * 1000
    )

    run_dir = root / "run-chart"
    run_dir.mkdir(parents=True)
    (run_dir / "run_ledger.json").write_text(
        json.dumps(
            {
                "run_id": "run-chart",
                "strategy_instance_id": "spy_chart",
                "created_at_ms": today_start_ms,
                "live_config": {"symbol": "SPY"},
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "run_status.json").write_text(
        json.dumps(
            {
                "run_id": "run-chart",
                "started_at_ms": today_start_ms + 1_000,
                "last_update_ms": today_start_ms + 60_000,
                "ended_at_ms": None,
                "exit_code": None,
                "exit_reason": None,
                "host_pid": 7,
            }
        ),
        encoding="utf-8",
    )
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_chart/chart-snapshot")

    assert response.status_code == 200
    body = response.json()
    assert body["date"] == today.isoformat()
    assert body["symbol"] == "SPY"
    assert body["resolution"] == "1m"
    assert body["has_bars"] is False  # no live aggregator data in this test
    assert isinstance(body["now_ms"], int)
    assert len(body["runs"]) == 1
    run = body["runs"][0]
    assert run["run_id"] == "run-chart"
    assert run["started_at_ms"] == today_start_ms + 1_000
    assert run["is_current"] is False
    assert run["color_index"] == 0


async def test_chart_snapshot_rejects_invalid_resolution(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slice 5: only ``1m`` and ``5s`` resolutions are accepted; anything else
    is a 400, not a silent default."""
    app, _root = app_with_root
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/api/live-instances/spy_chart/chart-snapshot", params={"resolution": "15m"}
        )
    assert response.status_code == 400


async def test_chart_snapshot_past_date_omits_live_buffer(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slice 5: a past-date request ignores the live aggregator buffer. With
    no persistence data and no runs touching that day, ``has_bars`` is
    False and ``runs`` is empty — the frontend renders the "bars
    unavailable" badge from this state."""
    app, _root = app_with_root
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/api/live-instances/spy_chart/chart-snapshot", params={"date": "2025-01-01"}
        )
    assert response.status_code == 200
    body = response.json()
    assert body["date"] == "2025-01-01"
    assert body["has_bars"] is False
    assert body["runs"] == []


async def test_chart_snapshot_rejects_malformed_date(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slice 5: a malformed date string is a 400; never silently coerced."""
    app, _root = app_with_root
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/api/live-instances/spy_chart/chart-snapshot", params={"date": "not-a-date"}
        )
    assert response.status_code == 400


async def test_active_dates_returns_run_dates_with_no_bars_marker(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slice 6: dates the instance ran on but pre-date persistence still
    appear in the picker with ``has_bars=False``."""
    from app.engine.live.run_status import write_run_status
    from app.schemas.live_runs import RunStatusSidecar

    app, root = app_with_root
    run_dir = root / "run-day1"
    run_dir.mkdir(parents=True)
    started_ms = int(datetime(2026, 1, 5, tzinfo=UTC).timestamp() * 1000)
    (run_dir / "run_ledger.json").write_text(
        json.dumps(
            {
                "run_id": "run-day1",
                "strategy_instance_id": "spy_dates",
                "created_at_ms": started_ms,
            }
        ),
        encoding="utf-8",
    )
    write_run_status(
        run_dir,
        RunStatusSidecar(
            run_id="run-day1",
            started_at_ms=started_ms,
            last_update_ms=started_ms + 60_000,
            ended_at_ms=started_ms + 3_600_000,
            exit_code=0,
            host_pid=11,
        ),
    )
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_dates/active-dates")

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    entry = body[0]
    assert entry["date"] == "2026-01-05"
    assert entry["run_count"] == 1
    assert entry["has_bars"] is False


async def test_active_dates_counts_every_utc_day_a_run_overlaps(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slice 6 (PR #483 review): a run spanning midnight UTC must appear on
    BOTH dates the picker shows, not just its start day. Anchoring solely
    on started_at_ms previously hid the later day."""
    from app.engine.live.run_status import write_run_status
    from app.schemas.live_runs import RunStatusSidecar

    app, root = app_with_root
    run_dir = root / "run-overnight"
    run_dir.mkdir(parents=True)
    started_ms = int(datetime(2026, 1, 5, 22, 0, tzinfo=UTC).timestamp() * 1000)
    ended_ms = int(datetime(2026, 1, 7, 4, 0, tzinfo=UTC).timestamp() * 1000)
    (run_dir / "run_ledger.json").write_text(
        json.dumps(
            {
                "run_id": "run-overnight",
                "strategy_instance_id": "spy_overnight",
                "created_at_ms": started_ms,
            }
        ),
        encoding="utf-8",
    )
    write_run_status(
        run_dir,
        RunStatusSidecar(
            run_id="run-overnight",
            started_at_ms=started_ms,
            last_update_ms=ended_ms,
            ended_at_ms=ended_ms,
            exit_code=0,
            host_pid=12,
        ),
    )
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_overnight/active-dates")

    assert response.status_code == 200
    body = response.json()
    dates = [entry["date"] for entry in body]
    # Spans 2026-01-05 22:00 UTC → 2026-01-07 04:00 UTC, so all three UTC
    # days must appear.
    assert dates == ["2026-01-05", "2026-01-06", "2026-01-07"]
    for entry in body:
        assert entry["run_count"] == 1


async def test_chart_snapshot_filters_trades_to_requested_utc_day(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slice 5 (PR #483 review): a multi-day run's trades from other UTC
    days must NOT project onto a per-date /chart-snapshot response."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    app, root = app_with_root
    run_dir = root / "run-spans"
    run_dir.mkdir(parents=True)
    day_a_ms = int(datetime(2026, 1, 5, 14, 30, tzinfo=UTC).timestamp() * 1000)
    day_b_ms = int(datetime(2026, 1, 6, 14, 30, tzinfo=UTC).timestamp() * 1000)
    (run_dir / "run_ledger.json").write_text(
        json.dumps(
            {
                "run_id": "run-spans",
                "strategy_instance_id": "spy_spans",
                "created_at_ms": day_a_ms,
                "live_config": {"symbol": "SPY"},
            }
        ),
        encoding="utf-8",
    )
    from app.engine.live.run_status import write_run_status
    from app.schemas.live_runs import RunStatusSidecar

    write_run_status(
        run_dir,
        RunStatusSidecar(
            run_id="run-spans",
            started_at_ms=day_a_ms,
            last_update_ms=day_b_ms,
            ended_at_ms=day_b_ms + 3_600_000,
            exit_code=0,
            host_pid=14,
        ),
    )
    # Trades from two different UTC days under the same run.
    table = pa.table(
        {
            "entry_time_ms": pa.array([day_a_ms, day_b_ms], type=pa.int64()),
            "exit_time_ms": pa.array([day_a_ms + 60_000, day_b_ms + 60_000], type=pa.int64()),
            "entry_price": pa.array([100.0, 200.0], type=pa.float64()),
            "exit_price": pa.array([101.0, 201.0], type=pa.float64()),
            "pnl_points": pa.array([1.0, 1.0], type=pa.float64()),
        }
    )
    pq.write_table(table, run_dir / "trades.parquet")
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/api/live-instances/spy_spans/chart-snapshot", params={"date": "2026-01-05"}
        )

    assert response.status_code == 200
    runs = response.json()["runs"]
    assert len(runs) == 1
    trades = runs[0]["trades"]
    assert len(trades) == 1
    assert trades[0]["entry_time_ms"] == day_a_ms


async def test_active_dates_rejects_invalid_resolution(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slice 6: only 1m / 5s accepted at the boundary."""
    app, _root = app_with_root
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/api/live-instances/spy_dates/active-dates", params={"resolution": "10s"}
        )
    assert response.status_code == 400


async def test_status_provenance_attests_the_run_identity(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The status carries what the run's content-addressed identity attests to —
    the hashed inputs (commit, spec+SHA, QC audit copy+SHA, backtest id, account)
    — so the console can explain the hashes ("what this proves") not dump them."""
    app, root = app_with_root
    run_dir = root / "run-prov"
    run_dir.mkdir(parents=True)
    (run_dir / "run_ledger.json").write_text(
        json.dumps(
            {
                "run_id": "run-prov",
                "schema_version": "1.2",
                "strategy_instance_id": "spy_ema_paper",
                "strategy_key": "spy_ema_crossover",
                "code_sha": "c0ffee1234deadbeef",
                "strategy_spec_path": "PythonDataService/app/engine/strategy/spec/fixtures/spy_ema_crossover.spec.json",
                "strategy_spec_sha256": "aaaaspec",
                "qc_audit_copy_path": "references/qc-shadow/SpyEmaCrossoverAlgorithm.py",
                "qc_audit_copy_sha256": "bbbbaudit",
                "qc_cloud_backtest_id": "d2fe45a7142e88575f6fbd75229f8681",
                "account_id": "DU1234567",
                "start_date_ms": 1714838400000,
                "created_at_ms": 1714838400500,
                "live_config": {"symbol": "SPY", "consolidator_period_min": 15},
            }
        ),
        encoding="utf-8",
    )
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_ema_paper/status")

    assert response.status_code == 200
    prov = response.json()["provenance"]
    assert prov["run_id"] == "run-prov"
    assert prov["code_sha"] == "c0ffee1234deadbeef"
    assert prov["strategy_spec_sha256"] == "aaaaspec"
    assert prov["qc_audit_copy_sha256"] == "bbbbaudit"
    assert prov["qc_cloud_backtest_id"] == "d2fe45a7142e88575f6fbd75229f8681"
    assert prov["account_id"] == "DU1234567"
    assert prov["start_date_ms"] == 1714838400000
    # live_config is part of the identity hash, so it must be in the provenance.
    assert prov["live_config"] == {"symbol": "SPY", "consolidator_period_min": 15}


async def test_status_exposes_symbol_from_ledger_live_config(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slice 2: the chart card needs the traded symbol to drop its 'SPY' default.
    Symbol is sourced from the ledger's ``live_config.symbol`` so two strategies
    that differ only in symbol don't have to plumb it through the URL."""
    app, root = app_with_root
    run_dir = root / "run-sym"
    run_dir.mkdir(parents=True)
    (run_dir / "run_ledger.json").write_text(
        json.dumps(
            {
                "run_id": "run-sym",
                "strategy_instance_id": "qqq_strategy",
                "created_at_ms": 1714838400500,
                "live_config": {"symbol": "QQQ", "consolidator_period_min": 1},
            }
        ),
        encoding="utf-8",
    )
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/qqq_strategy/status")

    assert response.status_code == 200
    body = response.json()
    assert body["symbol"] == "QQQ"


async def test_status_symbol_is_null_when_nothing_deployed(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ledger → no symbol. The frontend must treat ``null`` as 'unknown' and
    not fall back to a hardcoded ticker — the prior 'SPY' default was the bug
    Slice 2 closes."""
    app, _root = app_with_root
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/ghost_instance/status")

    assert response.status_code == 200
    assert response.json()["symbol"] is None


async def test_status_symbol_is_null_when_live_config_missing_symbol(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A legacy ledger that predates the symbol field must not crash — the field
    surfaces ``null`` and the UI handles that explicitly."""
    app, root = app_with_root
    run_dir = root / "run-legacy"
    run_dir.mkdir(parents=True)
    (run_dir / "run_ledger.json").write_text(
        json.dumps(
            {
                "run_id": "run-legacy",
                "strategy_instance_id": "legacy_strategy",
                "created_at_ms": 1714838400500,
                # No live_config — pre-symbol ledger.
            }
        ),
        encoding="utf-8",
    )
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/legacy_strategy/status")

    assert response.status_code == 200
    assert response.json()["symbol"] is None


async def test_status_provenance_none_when_nothing_deployed(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _root = app_with_root
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/ghost_instance/status")

    assert response.status_code == 200
    assert response.json()["provenance"] is None


async def test_status_last_exit_surfaces_the_specific_halt_trigger(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A halted run leaves a poisoned.flag carrying the SPECIFIC safety trigger.
    The status surfaces it (+ forensic details) so the console can explain *what*
    the engine detected, not just a generic 'Safety halt'."""
    from app.engine.live.halt import (
        PoisonedHaltReason,
        PoisonedHaltTrigger,
        write_poisoned_flag,
    )
    from app.engine.live.run_status import write_run_status
    from app.schemas.live_runs import ExitReason, RunStatusSidecar

    app, root = app_with_root
    _write_ledger(root, "run-halt", "spy_ema_paper", 100)
    run_dir = root / "run-halt"
    write_run_status(
        run_dir,
        RunStatusSidecar(
            run_id="run-halt",
            started_at_ms=1,
            last_update_ms=2,
            ended_at_ms=3,
            exit_code=1,
            exit_reason=ExitReason.fatal_halt,
            host_pid=7,
        ),
    )
    write_poisoned_flag(
        run_dir,
        PoisonedHaltReason(
            trigger=PoisonedHaltTrigger.OUTSIDE_MUTATION,
            halted_at_ms=1_700_000_000_000,
            last_clean_bar_close_ms=0,
            details={"client_order_id": "live-42", "symbol": "SPY"},
        ),
    )
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_ema_paper/status")

    assert response.status_code == 200
    last_exit = response.json()["last_exit"]
    assert last_exit["exit_reason"] == "fatal_halt"
    assert last_exit["halt_trigger"] == "outside_mutation"
    assert last_exit["halt_at_ms"] == 1_700_000_000_000
    assert last_exit["halt_detail"]["symbol"] == "SPY"


async def test_emergency_flatten_works_without_live_binding(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The account-wide flatten reaches the latest run's daemon emergency-flatten
    even with NO live binding (the binding-gated console FLATTEN command can't) —
    exactly the post-halt/poison case where flattening matters most."""
    app, root = app_with_root
    _write_ledger(root, "run-flat", "spy_ema_paper", 100)
    _set_daemon(monkeypatch, process={"state": "idle"})  # not running -> no live binding

    captured: dict = {}

    async def fake_flatten(base_url: str, run_id: str, payload: dict) -> dict:
        captured["run_id"] = run_id
        captured["payload"] = payload
        return {"accepted": True, "process": {"state": "idle"}}

    monkeypatch.setattr(host_daemon_client, "emergency_flatten_run", fake_flatten)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/live-instances/spy_ema_paper/emergency-flatten",
            json={"account": "DU123", "confirm": True},
        )

    assert response.status_code == 200
    assert response.json()["accepted"] is True
    assert captured["run_id"] == "run-flat"
    assert captured["payload"] == {"account": "DU123", "confirm": True}


async def test_emergency_flatten_requires_confirm(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-flat2", "spy_ema_paper", 100)
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/live-instances/spy_ema_paper/emergency-flatten",
            json={"account": "DU123", "confirm": False},
        )

    assert response.status_code == 400


async def test_emergency_flatten_404_when_instance_has_no_run(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _root = app_with_root
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/live-instances/ghost_instance/emergency-flatten",
            json={"account": "DU123", "confirm": True},
        )

    assert response.status_code == 404


async def test_status_start_defaults_redeploy_fields_empty_for_legacy_ledger(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Legacy ledgers missing the deploy fields yield empty strings (the deploy
    form then asks for them) rather than erroring."""
    app, root = app_with_root
    _write_ledger(root, "run-legacy-redeploy", "spy_ema_paper", 50)
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_ema_paper/status")

    defaults = response.json()["start_defaults"]
    assert defaults["strategy_spec_path"] == ""
    assert defaults["qc_cloud_backtest_id"] == ""
    assert defaults["account_id"] == ""


async def test_instance_status_unreachable_daemon_is_not_guessed(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-x", "spy_ema_paper", 10)
    _set_daemon(monkeypatch, process=None)  # daemon unreachable -> None

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_ema_paper/status")

    assert response.status_code == 200
    body = response.json()
    assert body["process"]["state"] == "unreachable"
    assert body["live_binding"] is None
    assert body["evidence_binding"]["run_id"] == "run-x"


async def test_list_instances_merges_daemon_and_disk(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-ema-1", "spy_ema_paper", 100)
    _write_ledger(root, "run-vwap-1", "spy_vwap_shadow", 100)
    _set_daemon(
        monkeypatch,
        instances={
            "instances": [
                {
                    "strategy_instance_id": "spy_ema_paper",
                    "run_id": "run-ema-1",
                    "run_dir": str(root / "run-ema-1"),
                    "process": {"state": "running", "run_id": "run-ema-1"},
                }
            ],
            "fetched_at_ms": 1,
        },
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances")

    assert response.status_code == 200
    rows = {row["strategy_instance_id"]: row for row in response.json()}
    assert set(rows) == {"spy_ema_paper", "spy_vwap_shadow"}
    assert rows["spy_ema_paper"]["process_state"] == "running"
    assert rows["spy_ema_paper"]["bound_run_id"] == "run-ema-1"
    # Disk-only instance: daemon reachable but not managing it -> offline, no bound run.
    assert rows["spy_vwap_shadow"]["process_state"] == "offline"
    assert rows["spy_vwap_shadow"]["bound_run_id"] is None
    assert rows["spy_vwap_shadow"]["latest_run_id"] == "run-vwap-1"


async def test_instance_status_rejects_invalid_id(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _root = app_with_root
    _set_daemon(monkeypatch, process=None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/evil$/status")

    assert response.status_code == 400


async def test_status_includes_namespace_attributed_broker_slice(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-brk", "spy_ema_paper", 100)
    live_state_dir = root.parent / "live_state" / "spy_ema_paper"
    live_state_dir.mkdir(parents=True)
    (live_state_dir / "live_state.json").write_text(
        json.dumps(
            {
                "strategy_instance_id": "spy_ema_paper",
                "run_id": "run-brk",
                "bot_order_namespace": "spy_ema_ns",
                "ib_client_id": 42,
                "expected_position_by_symbol": {"SPY": 100},
                "pending_intents": [{"symbol": "SPY"}],
                "last_processed_bar_ms": 1,
                "last_artifact_flush_ms": 1,
            }
        ),
        encoding="utf-8",
    )
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_ema_paper/status")

    broker = response.json()["broker"]
    assert broker["bot_order_namespace"] == "spy_ema_ns"
    assert broker["owned_positions"] == {"SPY": 100}  # engine's own namespace tally
    assert broker["pending_order_count"] == 1


async def test_status_broker_absent_without_sidecar(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-nobrk", "spy_ema_paper", 100)
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_ema_paper/status")

    assert response.json()["broker"] is None


async def test_account_fleet_flags_residual_contamination(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-ema", "spy_ema", 100)
    _write_live_state(root, "spy_ema", "run-ema", {"SPY": 100})

    async def fake_net() -> dict[str, int]:
        return {"SPY": 137}  # 37 unexplained

    monkeypatch.setattr(live_instances, "_fetch_net_positions", fake_net)
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/account")

    body = response.json()
    assert body["verdict"] == "contaminated"
    assert body["residual"] == {"SPY": 37}
    assert body["explained_total"] == {"SPY": 100}
    assert any(b["strategy_instance_id"] == "spy_ema" for b in body["explained_by_instance"])


async def test_account_fleet_unknown_without_broker(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-ema", "spy_ema", 100)
    _write_live_state(root, "spy_ema", "run-ema", {"SPY": 100})

    async def fake_net() -> None:
        return None

    monkeypatch.setattr(live_instances, "_fetch_net_positions", fake_net)
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/account")

    assert response.json()["verdict"] == "unknown"


async def test_instance_commands_returns_bound_run_timeline(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-cmd", "spy_ema_paper", 100)
    commands = root / "run-cmd" / "commands"
    commands.mkdir()
    (commands / "command.1.RECONCILE.pending.json").write_text(
        json.dumps({"seq": 1, "verb": "RECONCILE", "payload": {}}), encoding="utf-8"
    )
    _set_daemon(monkeypatch, process={"state": "running", "run_id": "run-cmd", "pid": 1})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_ema_paper/commands")

    body = response.json()
    assert body["poll_interval_ms"] == 1000  # server-provided
    assert "pending" not in body and "acks" not in body  # canonical entries[] shape
    assert [e["seq"] for e in body["entries"]] == [1]
    assert body["entries"][0]["status"] == "queued"


async def test_instance_commands_empty_without_live_binding(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _root = app_with_root
    _set_daemon(monkeypatch, process=None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_ema_paper/commands")

    assert response.json() == {"entries": [], "poll_interval_ms": 1000}


async def test_issue_one_shot_command_queues_on_bound_run(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-os", "spy_ema_paper", 100)
    _set_daemon(monkeypatch, process={"state": "running", "run_id": "run-os", "pid": 1})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/live-instances/spy_ema_paper/commands", json={"verb": "RECONCILE"}
        )

    assert response.status_code == 200
    assert response.json()["verb"] == "RECONCILE"
    queued = list((root / "run-os" / "commands").glob("command.*.RECONCILE.pending.json"))
    assert len(queued) == 1


async def test_issue_command_rejects_intent_verbs(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-os2", "spy_ema_paper", 100)
    _set_daemon(monkeypatch, process={"state": "running", "run_id": "run-os2", "pid": 1})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/live-instances/spy_ema_paper/commands", json={"verb": "PAUSE"}
        )

    assert response.status_code == 400  # PAUSE is the intent knob, not a one-shot command


async def test_issue_command_without_live_binding_conflicts(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _root = app_with_root
    _set_daemon(monkeypatch, process=None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/live-instances/spy_ema_paper/commands", json={"verb": "FLATTEN"}
        )

    assert response.status_code == 409


async def test_status_transports_engine_readiness_when_live(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-live-rdy", "spy_ema_paper", 100)
    (root / "run-live-rdy" / "readiness.json").write_text(
        json.dumps(
            {
                "kind": "live_readiness",
                "as_of_ms": 5,
                "source": "engine",
                "verdict": "READY",
                "summary": "ready",
                "gates": [{"name": "desired_state", "status": "pass", "severity": "hard", "detail": "RUNNING"}],
            }
        ),
        encoding="utf-8",
    )
    _set_daemon(monkeypatch, process={"state": "running", "run_id": "run-live-rdy", "pid": 1})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_ema_paper/status")

    readiness = response.json()["readiness"]
    assert readiness["kind"] == "live_readiness"
    assert readiness["source"] == "engine"  # engine-authored, transported verbatim
    assert readiness["verdict"] == "READY"


async def test_status_derives_start_readiness_when_dead(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-dead-rdy", "spy_ema_paper", 50)
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_ema_paper/status")

    readiness = response.json()["readiness"]
    assert readiness["kind"] == "start_readiness"
    assert readiness["source"] == "backend_derived"
    assert readiness["live_readiness_available"] is False


async def test_status_includes_spec_derived_decision_column_descriptors(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.engine.strategy.spec import schema as spec_schema

    fixture = Path(spec_schema.__file__).parent / "fixtures" / "spy_ema_crossover.spec.json"
    app, root = app_with_root
    _write_ledger(root, "run-desc", "spy_ema_paper", 100, spec_path=fixture)
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_ema_paper/status")

    body = response.json()
    cols = {c["name"]: c for c in body["decision_columns"]}
    assert {"ema5", "ema10", "rsi"} <= set(cols)
    assert cols["rsi"]["label"] == "RSI"
    assert cols["ema5"]["label"] == "EMA 5"
    assert cols["ema5"]["format"] == "decimal"
    # No decisions.parquet written -> latest_decision is None, descriptors still resolve.
    assert body["latest_decision"] is None


async def test_set_desired_state_actuates_live_binding(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-live-ccc", "spy_ema_paper", 100)
    _set_daemon(monkeypatch, process={"state": "running", "run_id": "run-live-ccc", "pid": 7})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/live-instances/spy_ema_paper/desired-state",
            json={"action": "pause", "updated_by": "operator", "reason": "risk"},
        )

    assert response.status_code == 200
    body = response.json()
    # 1. durable intent written first
    assert body["durable"]["state"] == "PAUSED"
    # 2. live actuation queued on the bound run
    assert body["actuation"]["actuated"] is True
    assert body["actuation"]["run_id"] == "run-live-ccc"
    assert body["actuation"]["command_seq"] is not None
    queued = list((root / "run-live-ccc" / "commands").glob("command.*.PAUSE.pending.json"))
    assert len(queued) == 1


async def test_set_desired_state_without_live_binding_is_durable_only(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _root = app_with_root
    _set_daemon(monkeypatch, process=None)  # no live process

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/live-instances/spy_ema_paper/desired-state",
            json={"action": "stop", "updated_by": "op"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["durable"]["state"] == "STOPPED"
    assert body["actuation"]["actuated"] is False
    assert "durable only" in body["actuation"]["detail"]


async def test_set_desired_state_live_but_run_dir_not_visible_is_durable_only(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Daemon reports a live process but its run dir is not visible under this
    service's root: never claim a phantom actuation (a command written here
    would never be seen by the engine polling its real dir)."""
    app, _root = app_with_root
    _set_daemon(monkeypatch, process={"state": "running", "run_id": "run-ghost", "pid": 5})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/live-instances/spy_ema_paper/desired-state",
            json={"action": "pause"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["durable"]["state"] == "PAUSED"
    assert body["actuation"]["actuated"] is False
    assert "not visible locally" in body["actuation"]["detail"]


async def test_set_desired_state_enqueue_failure_is_durable_only(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-live-ddd", "spy_ema_paper", 100)
    _set_daemon(monkeypatch, process={"state": "running", "run_id": "run-live-ddd", "pid": 8})

    def fail_write(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(live_instances.CommandChannel, "write_from_operator", fail_write)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/live-instances/spy_ema_paper/desired-state",
            json={"action": "pause"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["durable"]["state"] == "PAUSED"
    assert body["actuation"]["actuated"] is False
    assert body["actuation"]["run_id"] == "run-live-ddd"
    assert "failed to enqueue live command" in body["actuation"]["detail"]


# ── deploy / create (ADR 0006) ───────────────────────────────────────


def _deploy_body() -> dict:
    return {
        "strategy_spec_path": "PythonDataService/spec.json",
        "qc_audit_copy_path": "references/qc-shadow/A.py",
        "qc_cloud_backtest_id": "bt-1",
        "account_id": "DU111",
        "start_date_ms": 1700000000000,
        "strategy_instance_id": "spy_ema_paper",
        # VCR-0001 / Phase 1 — explicit sizing is required at the deploy
        # boundary. Safe canary is the deploy-form default.
        "live_config": {
            "symbol": "SPY",
            "sizing": {"kind": "FixedShares", "value": 1},
        },
    }


async def test_deploy_instance_created_returns_201(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _ = app_with_root

    async def fake_deploy(_base_url: str, _payload: dict) -> dict:
        return {"run_id": "run-new", "run_dir": "/runs/run-new", "created": True, "start": None}

    monkeypatch.setattr(host_daemon_client, "deploy", fake_deploy)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/live-instances", json=_deploy_body())

    assert response.status_code == 201
    body = response.json()
    assert body["run_id"] == "run-new"
    assert body["created"] is True


async def test_deploy_instance_idempotent_returns_200(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _ = app_with_root

    async def fake_deploy(_base_url: str, _payload: dict) -> dict:
        return {"run_id": "run-existing", "run_dir": "/runs/run-existing", "created": False, "start": None}

    monkeypatch.setattr(host_daemon_client, "deploy", fake_deploy)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/live-instances", json=_deploy_body())

    assert response.status_code == 200
    assert response.json()["created"] is False


async def test_deploy_instance_dirty_tree_propagates_409(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _ = app_with_root

    async def fake_deploy(_base_url: str, _payload: dict) -> dict:
        raise host_daemon_client.HostDaemonError(409, "Working tree is dirty; commit or stash before deploying.")

    monkeypatch.setattr(host_daemon_client, "deploy", fake_deploy)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/live-instances", json=_deploy_body())

    assert response.status_code == 409
    assert "dirty" in response.json()["detail"].lower()


async def test_deploy_instance_daemon_unreachable_propagates_503(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _ = app_with_root

    async def fake_deploy(_base_url: str, _payload: dict) -> dict:
        raise host_daemon_client.HostDaemonError(503, "host daemon unreachable: connection refused")

    monkeypatch.setattr(host_daemon_client, "deploy", fake_deploy)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/live-instances", json=_deploy_body())

    assert response.status_code == 503


async def test_deploy_instance_invalid_payload_returns_502(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A schema-invalid deploy payload from the daemon is an upstream contract
    failure → 502, not a 500 that makes the data plane look broken."""
    app, _ = app_with_root

    async def fake_deploy(_base_url: str, _payload: dict) -> dict:
        return {"unexpected": "shape"}  # missing run_id/run_dir/created

    monkeypatch.setattr(host_daemon_client, "deploy", fake_deploy)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/live-instances", json=_deploy_body())

    assert response.status_code == 502


async def test_qc_audit_copies_invalid_payload_returns_502(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A malformed (non-None) listing from the daemon must not 500 or silently
    read as an empty list — surface it as a gateway error."""
    app, _ = app_with_root

    async def fake_fetch(_base_url: str):
        return as_typed_get({"scope_root": 123, "entries": "not-a-list"})  # wrong types

    monkeypatch.setattr(host_daemon_client, "fetch_qc_audit_copies", fake_fetch)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/qc-audit-copies")

    assert response.status_code == 502


async def test_qc_audit_copies_passthrough(app_with_root, monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = app_with_root

    async def fake_fetch(_base_url: str):
        return as_typed_get(
            {"scope_root": "references/qc-shadow", "entries": ["references/qc-shadow/A.py"]}
        )

    monkeypatch.setattr(host_daemon_client, "fetch_qc_audit_copies", fake_fetch)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/qc-audit-copies")

    assert response.status_code == 200
    assert response.json()["entries"] == ["references/qc-shadow/A.py"]


async def test_qc_audit_copies_failclosed_to_empty(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _ = app_with_root

    async def fake_fetch(_base_url: str):
        return as_typed_get(None)  # daemon unreachable

    monkeypatch.setattr(host_daemon_client, "fetch_qc_audit_copies", fake_fetch)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/qc-audit-copies")

    assert response.status_code == 200
    assert response.json()["entries"] == []


# ── daemon-health proxy (PRD #619-C P2 — /health is auth-gated) ──────


def _idle_health() -> dict:
    """A minimal HostRunnerHealth payload shaped for the schema validator."""
    return {
        "ok": True,
        "repo_root": "/repo",
        "live_runs_root": "/repo/artifacts/live_runs",
        "fetched_at_ms": 1700000000000,
        "process": {
            "state": "idle",
            "run_id": None,
            "pid": None,
            "started_at_ms": None,
            "ended_at_ms": None,
            "exit_code": None,
            "command": [],
            "log_path": None,
            "message": None,
        },
        "daemon_boot_id": "boot-abc",
    }


async def test_daemon_health_forwards_envelope(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The browser cannot send X-Live-Runner-Token. The data plane probes
    the daemon and forwards the parsed envelope so the cockpit / deploy
    form can render Daemon up = OK."""
    from app.engine.live.daemon_transport import DaemonResult
    from app.schemas.live_runs import HostRunnerHealth

    app, _ = app_with_root
    payload = _idle_health()

    async def fake_fetch(_base_url: str):
        return DaemonResult.connected(daemon_boot_id="boot-abc"), HostRunnerHealth.model_validate(payload)

    monkeypatch.setattr(host_daemon_client, "fetch_health", fake_fetch)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/daemon-health")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["daemon_boot_id"] == "boot-abc"


async def test_daemon_health_auth_failed_surfaces_as_502(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale/rotated token would have silently shown the deploy form
    "Live engine unavailable" before this route existed. Surfacing 502
    lets the connectivity strip distinguish auth from unreachable."""
    from app.engine.live.daemon_transport import DaemonResult

    app, _ = app_with_root

    async def fake_fetch(_base_url: str):
        return DaemonResult.auth_failed(status=401, detail="bad token"), None

    monkeypatch.setattr(host_daemon_client, "fetch_health", fake_fetch)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/daemon-health")

    assert response.status_code == 502
    assert "token" in response.json()["detail"].lower()


async def test_daemon_health_unreachable_surfaces_as_503(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Daemon process down → 503, matching the existing operation-error
    map's remediation copy for "live engine unavailable"."""
    import httpx

    from app.engine.live.daemon_transport import DaemonResult

    app, _ = app_with_root

    async def fake_fetch(_base_url: str):
        return (
            DaemonResult.from_httpx_exception(httpx.ConnectError("connection refused")),
            None,
        )

    monkeypatch.setattr(host_daemon_client, "fetch_health", fake_fetch)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/daemon-health")

    assert response.status_code == 503


# ── start / stop proxy (ADR 0007 — token forwarded server-side) ──────


def _running_process(run_id: str) -> dict:
    return {
        "state": "running",
        "run_id": run_id,
        "strategy_instance_id": "spy_ema_paper",
        "pid": 4242,
        "started_at_ms": 1700000000000,
        "ended_at_ms": None,
        "exit_code": None,
        "command": ["python", "-m", "app.engine.live.run", "start"],
        "log_path": "/runs/host_daemon.log",
        "message": "Host runner process is active.",
    }


async def test_start_run_forwards_and_returns_action(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _ = app_with_root
    seen: dict = {}

    async def fake_start(_base_url: str, run_id: str, payload: dict) -> dict:
        seen["run_id"] = run_id
        seen["payload"] = payload
        return {"accepted": True, "process": _running_process(run_id)}

    monkeypatch.setattr(host_daemon_client, "start_run", fake_start)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/live-instances/runs/run-abc/start",
            json={"readonly": False, "hydrate_policy": "optional", "strategy": "spy_ema_crossover"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] is True
    assert body["process"]["state"] == "running"
    # The proxy forwards the run_id and the start knobs verbatim to the daemon.
    assert seen["run_id"] == "run-abc"
    assert seen["payload"]["readonly"] is False
    assert seen["payload"]["hydrate_policy"] == "optional"


async def test_stop_run_forwards_and_returns_action(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _ = app_with_root

    async def fake_stop(_base_url: str, run_id: str, _payload: dict) -> dict:
        proc = _running_process(run_id)
        proc["state"] = "stopping"
        return {"accepted": True, "process": proc}

    monkeypatch.setattr(host_daemon_client, "stop_run", fake_stop)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/live-instances/runs/run-abc/stop", json={"force": False})

    assert response.status_code == 200
    assert response.json()["process"]["state"] == "stopping"


async def test_start_run_propagates_daemon_404(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _ = app_with_root

    async def fake_start(_base_url: str, _run_id: str, _payload: dict) -> dict:
        raise host_daemon_client.HostDaemonError(404, "Run 'run-missing' not found")

    monkeypatch.setattr(host_daemon_client, "start_run", fake_start)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/live-instances/runs/run-missing/start", json={})

    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


async def test_start_run_propagates_daemon_unreachable_503(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _ = app_with_root

    async def fake_start(_base_url: str, _run_id: str, _payload: dict) -> dict:
        raise host_daemon_client.HostDaemonError(503, "host daemon unreachable: connection refused")

    monkeypatch.setattr(host_daemon_client, "start_run", fake_start)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/live-instances/runs/run-abc/start", json={})

    assert response.status_code == 503


async def test_start_run_invalid_daemon_payload_returns_502(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _ = app_with_root

    async def fake_start(_base_url: str, _run_id: str, _payload: dict) -> dict:
        return {"unexpected": "shape"}  # missing accepted/process

    monkeypatch.setattr(host_daemon_client, "start_run", fake_start)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/live-instances/runs/run-abc/start", json={})

    assert response.status_code == 502


# ---------------------------------------------------------------------------
# PRD #619-C5 — single-shot mutation OUTCOME_UNKNOWN surfacing
# ---------------------------------------------------------------------------


def _outcome_unknown_exc(
    *, category: str = "read_timeout", detail: str = "response lost"
) -> host_daemon_client.HostDaemonOutcomeUnknownError:
    return host_daemon_client.HostDaemonOutcomeUnknownError(
        error_category=category, detail=detail
    )


def _assert_outcome_unknown_body(
    body: dict, *, endpoint: str, category: str = "read_timeout"
) -> None:
    """Shared assertions for the 619-C5 typed 409 response body."""
    assert body["outcome"] == "UNKNOWN"
    assert body["reason_code"] == "OUTCOME_UNKNOWN"
    assert body["error_category"] == category
    assert body["endpoint"] == endpoint
    assert isinstance(body["occurred_at_ms"], int)
    assert body["occurred_at_ms"] > 0
    assert isinstance(body["runbook_hint"], str)
    assert body["runbook_hint"]  # non-empty


async def test_deploy_outcome_unknown_returns_typed_409(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ReadTimeout-after-send during deploy must surface as 409 +
    OUTCOME_UNKNOWN — the run may or may not have been created on the
    daemon side."""
    app, _ = app_with_root

    async def fake_deploy(_base_url: str, _payload: dict) -> dict:
        raise _outcome_unknown_exc()

    monkeypatch.setattr(host_daemon_client, "deploy", fake_deploy)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/live-instances", json=_deploy_body())

    assert response.status_code == 409
    _assert_outcome_unknown_body(response.json()["detail"], endpoint="deploy")


async def test_start_run_outcome_unknown_returns_typed_409(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _ = app_with_root

    async def fake_start(_base_url: str, _run_id: str, _payload: dict) -> dict:
        raise _outcome_unknown_exc(category="write_timeout")

    monkeypatch.setattr(host_daemon_client, "start_run", fake_start)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/live-instances/runs/run-abc/start", json={})

    assert response.status_code == 409
    _assert_outcome_unknown_body(
        response.json()["detail"], endpoint="start_run", category="write_timeout"
    )


async def test_stop_run_outcome_unknown_returns_typed_409(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _ = app_with_root

    async def fake_stop(_base_url: str, _run_id: str, _payload: dict) -> dict:
        raise _outcome_unknown_exc()

    monkeypatch.setattr(host_daemon_client, "stop_run", fake_stop)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/live-instances/runs/run-abc/stop", json={})

    assert response.status_code == 409
    _assert_outcome_unknown_body(response.json()["detail"], endpoint="stop_run")


async def test_emergency_flatten_outcome_unknown_returns_typed_409(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Emergency-flatten has a 130s timeout; an ambiguous outcome here
    means broker positions may be in an intermediate state — the highest
    stakes case for 619-C5."""
    app, root = app_with_root
    sid = "strategy-of-flatten"
    _write_ledger(root, "run-flatten", sid, created_at_ms=1_700_000_000_000)

    async def fake_flatten(_base_url: str, _run_id: str, _payload: dict) -> dict:
        raise _outcome_unknown_exc(category="remote_protocol_error")

    monkeypatch.setattr(host_daemon_client, "emergency_flatten_run", fake_flatten)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"/api/live-instances/{sid}/emergency-flatten",
            json={"account": "DU123", "confirm": True},
        )

    assert response.status_code == 409
    _assert_outcome_unknown_body(
        response.json()["detail"],
        endpoint="emergency_flatten",
        category="remote_protocol_error",
    )


def test_outcome_unknown_reason_code_is_in_documented_vocabulary() -> None:
    """The reason code must be present in the closed REASON_CODES set
    so the Frontend's typed lookup ships the operator copy alongside C5."""
    from app.services.operator_capability import REASON_CODES

    assert "OUTCOME_UNKNOWN" in REASON_CODES


async def test_start_run_rejects_unsafe_run_id_400(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unsafe run_id is rejected at the boundary before any forward."""
    app, _ = app_with_root
    called = False

    async def fake_start(_base_url: str, _run_id: str, _payload: dict) -> dict:
        nonlocal called
        called = True
        return {"accepted": True, "process": _running_process("x")}

    monkeypatch.setattr(host_daemon_client, "start_run", fake_start)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Leading whitespace reaches the handler as a single segment and is
        # rejected by _validate_path_segment; the daemon is never called.
        response = await client.post("/api/live-instances/runs/ bad/start", json={})

    assert response.status_code == 400
    assert called is False


def _write_run_status(
    root: Path,
    run_id: str,
    *,
    ended_at_ms: int | None,
    exit_code: int | None,
    exit_reason: str | None,
) -> None:
    (root / run_id / "run_status.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "run_id": run_id,
                "started_at_ms": 1,
                "last_update_ms": 2,
                "ended_at_ms": ended_at_ms,
                "exit_code": exit_code,
                "exit_reason": exit_reason,
                "host_pid": 4242,
            }
        ),
        encoding="utf-8",
    )


def _write_hydration(root: Path, run_id: str, *, accepted: bool, failure_reason: str) -> None:
    (root / run_id / "indicator_state_hydration.json").write_text(
        json.dumps(
            {"schema_version": 1, "accepted": accepted, "validation": {"failure_reason": failure_reason}}
        ),
        encoding="utf-8",
    )


async def test_status_last_exit_surfaces_cold_start_hydration_failure(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A STOPPED instance must explain *why* it stopped. A cold start that exits 4
    under hydrate_policy=require carries the hydration receipt's failure_reason so
    the console can render seed-day guidance instead of a bare 'STOPPED'."""
    app, root = app_with_root
    _write_ledger(root, "run-coldstart", "spy_ema_paper", 100)
    _write_run_status(root, "run-coldstart", ended_at_ms=200, exit_code=4, exit_reason="exception")
    _write_hydration(root, "run-coldstart", accepted=False, failure_reason="missing")
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_ema_paper/status")

    assert response.status_code == 200
    last_exit = response.json()["last_exit"]
    assert last_exit is not None
    assert last_exit["exit_code"] == 4
    assert last_exit["exit_reason"] == "exception"
    assert last_exit["hydration_accepted"] is False
    assert last_exit["hydration_failure_reason"] == "missing"


async def test_status_last_exit_absent_while_run_is_live(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live run (no terminal ended_at_ms) must not surface a stale last_exit —
    that would contradict the RUNNING badge."""
    app, root = app_with_root
    _write_ledger(root, "run-live-ccc", "spy_ema_paper", 100)
    _write_run_status(root, "run-live-ccc", ended_at_ms=None, exit_code=None, exit_reason=None)
    _set_daemon(
        monkeypatch,
        process={"state": "running", "run_id": "run-live-ccc", "pid": 7, "started_at_ms": 100},
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_ema_paper/status")

    assert response.status_code == 200
    assert response.json()["last_exit"] is None


async def test_status_last_exit_tolerates_malformed_hydration_receipt(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A corrupt/hand-edited receipt (non-bool ``accepted``, non-str
    ``failure_reason``) must not 500 the status endpoint — the hydration fields
    degrade to None while the run's exit is still reported."""
    app, root = app_with_root
    _write_ledger(root, "run-badreceipt", "spy_ema_paper", 100)
    _write_run_status(root, "run-badreceipt", ended_at_ms=200, exit_code=4, exit_reason="exception")
    (root / "run-badreceipt" / "indicator_state_hydration.json").write_text(
        json.dumps({"accepted": "nope", "validation": {"failure_reason": 123}}), encoding="utf-8"
    )
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_ema_paper/status")

    assert response.status_code == 200
    last_exit = response.json()["last_exit"]
    assert last_exit["exit_code"] == 4
    assert last_exit["hydration_accepted"] is None
    assert last_exit["hydration_failure_reason"] is None


async def test_start_defaults_readonly_false_in_paper_mode(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Paper mode with orders allowed defaults the Start card to place (paper)
    orders — readonly=False — so the operator doesn't re-enable trading on every
    start. Orders are paper, so trading-by-default is safe."""
    app, root = app_with_root
    _write_ledger(root, "run-paper", "spy_ema_paper", 100)
    stub = SimpleNamespace(
        live_runs_root=str(root),
        live_runner_daemon_url="http://daemon",
        live_runner_host_start_command="",
        fleet_dirty_blocks_starts=False,
        mode="paper",
        readonly=False,
    )
    monkeypatch.setattr(live_instances, "get_settings", lambda: stub)
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_ema_paper/status")

    assert response.status_code == 200
    assert response.json()["start_defaults"]["readonly"] is False


async def test_start_defaults_readonly_true_in_live_mode(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live mode keeps the Start card in shadow (no-orders) by default — a
    real-money run never auto-trades from a server-authored default."""
    app, root = app_with_root
    _write_ledger(root, "run-live-mode", "spy_ema_paper", 100)
    stub = SimpleNamespace(
        live_runs_root=str(root),
        live_runner_daemon_url="http://daemon",
        live_runner_host_start_command="",
        fleet_dirty_blocks_starts=False,
        mode="live",
        readonly=False,
    )
    monkeypatch.setattr(live_instances, "get_settings", lambda: stub)
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_ema_paper/status")

    assert response.status_code == 200
    assert response.json()["start_defaults"]["readonly"] is True


async def test_start_defaults_honors_ibkr_readonly_in_paper_mode(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IBKR_READONLY=true keeps the Start card in shadow even in paper mode — the
    engine refuses orders under operator lockdown, so the UI must not promise
    them. (CodeRabbit #436.)"""
    app, root = app_with_root
    _write_ledger(root, "run-lockdown", "spy_ema_paper", 100)
    stub = SimpleNamespace(
        live_runs_root=str(root),
        live_runner_daemon_url="http://daemon",
        live_runner_host_start_command="",
        fleet_dirty_blocks_starts=False,
        mode="paper",
        readonly=True,
    )
    monkeypatch.setattr(live_instances, "get_settings", lambda: stub)
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_ema_paper/status")

    assert response.status_code == 200
    assert response.json()["start_defaults"]["readonly"] is True


async def test_start_defaults_fail_closed_when_mode_missing(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing/unknown ``mode`` (config drift, partial rollout) must fail closed
    to shadow — never default to placing orders on a possibly-live account.
    (CodeRabbit #436.)"""
    app, root = app_with_root
    _write_ledger(root, "run-nomode", "spy_ema_paper", 100)
    stub = SimpleNamespace(
        live_runs_root=str(root),
        live_runner_daemon_url="http://daemon",
        live_runner_host_start_command="",
        fleet_dirty_blocks_starts=False,
        readonly=False,  # even with orders allowed, an absent mode stays shadow
    )
    monkeypatch.setattr(live_instances, "get_settings", lambda: stub)
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_ema_paper/status")

    assert response.status_code == 200
    assert response.json()["start_defaults"]["readonly"] is True


# ─────────────────────── VCR-P3-I — NY-tz trading day ───────────────────


def test_today_ny_uses_america_new_york_not_utc(monkeypatch: pytest.MonkeyPatch) -> None:
    """VCR-P3-I — ``_today_ny()`` returns the trading-day date in
    ``America/New_York``, NOT the UTC calendar date. At the UTC
    boundary (~00:00 UTC = ~19:00 ET winter / ~20:00 ET summer) these
    two dates differ, and the chart-snapshot ``today`` reference must
    follow the trading day, not the UTC day."""
    from datetime import UTC, date, datetime

    fixed_utc_instant = datetime(2026, 3, 6, 2, 30, tzinfo=UTC)
    # 2026-03-06 02:30 UTC == 2026-03-05 21:30 America/New_York (EST).
    # Trading day is 2026-03-05; UTC calendar says 2026-03-06.

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            if tz is None:
                return fixed_utc_instant.replace(tzinfo=None)
            return fixed_utc_instant.astimezone(tz)

    monkeypatch.setattr(live_instances, "datetime", _FixedDatetime)
    assert live_instances._today_ny() == date(2026, 3, 5)
    # Sanity: the NY-tz "today" is NOT the same as the UTC "today" at
    # this instant — otherwise the test isn't actually exercising the
    # bug it covers.
    assert live_instances._today_ny() != fixed_utc_instant.date()
