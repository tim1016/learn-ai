"""Slice 1A — ``/status`` surfaces ``action_plan`` and ``instrument_surface``.

PRD #593 §"API contracts": ``GET /api/live-instances/{id}/status`` exposes
the bound run's declared ``live_config.action`` plan and the strategy's
registered ``instrument_surface`` value.

Slice 1A only ships the empty-plan case from the ledger side. Stock and
option legs (#595 / #596) extend the ledger payload but the response
plumbing is identical — this test pins the empty-plan attestation today
so the response shape is fixed before the leg variants arrive.

Prior art: ``tests/routers/test_live_instances.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.engine.live import host_daemon_client
from app.routers import live_instances
from tests._fixtures.daemon_transport import as_typed_get


def _write_ledger_with_action(
    root: Path,
    *,
    run_id: str,
    sid: str,
    strategy_key: str,
    action_plan: dict | None,
) -> None:
    """Mirror the prod ledger shape (``live_config`` carries ``action`` when
    present). Mirrors ``tests/routers/test_live_instances._write_ledger``
    but with the extra fields Slice 1A reads."""

    run_dir = root / run_id
    run_dir.mkdir(parents=True)
    live_config: dict = {"sizing": {"kind": "FixedShares", "value": 1}}
    if action_plan is not None:
        live_config["action"] = action_plan
    payload: dict = {
        "run_id": run_id,
        "strategy_instance_id": sid,
        "strategy_key": strategy_key,
        "created_at_ms": 100,
        "live_config": live_config,
    }
    (run_dir / "run_ledger.json").write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture
def app_with_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[FastAPI, Path]:
    root = tmp_path / "live_runs"
    root.mkdir()
    stub = SimpleNamespace(
        live_runs_root=str(root),
        live_runner_daemon_url="http://daemon",
        fleet_dirty_blocks_starts=False,
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


async def test_status_surfaces_empty_action_plan_from_ledger(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    _write_ledger_with_action(
        root,
        run_id="run-aaa",
        sid="spy_ema_paper",
        strategy_key="spy_ema_crossover",
        action_plan={"on_enter": [], "on_exit": []},
    )
    _set_daemon(
        monkeypatch,
        process={"state": "running", "run_id": "run-aaa", "pid": 1, "started_at_ms": 100},
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_ema_paper/status")

    assert response.status_code == 200
    body = response.json()
    assert body["action_plan"] == {"on_enter": [], "on_exit": []}


async def test_status_surfaces_registered_explicit_instrument_surface(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    _write_ledger_with_action(
        root,
        run_id="run-bbb",
        sid="spy_ema_paper",
        strategy_key="spy_ema_crossover",
        action_plan={"on_enter": [], "on_exit": []},
    )
    _set_daemon(
        monkeypatch,
        process={"state": "running", "run_id": "run-bbb", "pid": 1, "started_at_ms": 100},
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_ema_paper/status")

    assert response.status_code == 200
    body = response.json()
    assert body["instrument_surface"] == "explicit"


async def test_status_action_plan_is_null_when_ledger_omits_action(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Legacy / pre-Slice-1A ledgers carry no ``action`` key. The status
    payload must surface ``None`` rather than fabricating an empty plan,
    so the cockpit can distinguish "operator declared an empty plan" from
    "the ledger pre-dates the field". """

    app, root = app_with_root
    _write_ledger_with_action(
        root,
        run_id="run-ccc",
        sid="spy_ema_paper",
        strategy_key="spy_ema_crossover",
        action_plan=None,
    )
    _set_daemon(
        monkeypatch,
        process={"state": "running", "run_id": "run-ccc", "pid": 1, "started_at_ms": 100},
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_ema_paper/status")

    assert response.status_code == 200
    body = response.json()
    assert body["action_plan"] is None


# ---------------------------------------------------------------------------
# Slice 1B (#595) — stock entry leg + close_leg exit persistence.

_STOCK_PLAN: dict = {
    "on_enter": [
        {
            "leg_id": "spy_long",
            "instrument": {"kind": "stock", "underlying": "SPY"},
            "position": "long",
            "qty_ratio": 1,
        }
    ],
    "on_exit": [{"kind": "close_leg", "entry_leg_id": "spy_long"}],
}


async def test_status_surfaces_lineage_block_from_ledger(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slice 1E (#598) — ``/status`` exposes the redeploy lineage
    (parent_run_id, redeploy_reason, redeployed_at_ms) from the
    ledger's ``lineage`` block. Persistence lives outside ``live_config``
    so the fields stay unhashed; reading them is a read of the same
    on-disk file the status helper already opens."""

    app, root = app_with_root
    run_dir = root / "run-lineage"
    run_dir.mkdir(parents=True)
    import json

    (run_dir / "run_ledger.json").write_text(
        json.dumps(
            {
                "run_id": "run-lineage",
                "strategy_instance_id": "spy_ema_paper",
                "strategy_key": "spy_ema_crossover",
                "created_at_ms": 100,
                "live_config": {"sizing": {"kind": "FixedShares", "value": 1}},
                "lineage": {
                    "parent_run_id": "run-parent",
                    "redeploy_reason": "quantity bump after live read",
                    "redeployed_at_ms": 200,
                },
            }
        ),
        encoding="utf-8",
    )
    _set_daemon(
        monkeypatch,
        process={"state": "running", "run_id": "run-lineage", "pid": 1, "started_at_ms": 100},
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_ema_paper/status")

    assert response.status_code == 200
    body = response.json()
    assert body["lineage"] == {
        "parent_run_id": "run-parent",
        "redeploy_reason": "quantity bump after live read",
        "redeployed_at_ms": 200,
    }


async def test_status_lineage_is_null_when_ledger_omits_it(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Legacy / pre-Slice-1E ledgers carry no lineage block — surface
    ``None``, not a fabricated empty record."""

    app, root = app_with_root
    _write_ledger_with_action(
        root,
        run_id="run-no-lineage",
        sid="spy_ema_paper",
        strategy_key="spy_ema_crossover",
        action_plan=None,
    )
    _set_daemon(
        monkeypatch,
        process={"state": "running", "run_id": "run-no-lineage", "pid": 1, "started_at_ms": 100},
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_ema_paper/status")

    assert response.status_code == 200
    assert response.json()["lineage"] is None


async def test_status_surfaces_stock_plan_from_ledger(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end demo from PRD #593 §"The operator workflow": an
    operator builds 'buy 100 SPY on ENTER / sell 100 SPY on EXIT' from
    the UI, submits, sees it persisted in the ledger, and sees it
    rendered in the cockpit card. Slice 1B closes the persistence +
    surfacing half (the picker UI is the other half)."""

    app, root = app_with_root
    _write_ledger_with_action(
        root,
        run_id="run-stock",
        sid="spy_ema_paper",
        strategy_key="spy_ema_crossover",
        action_plan=_STOCK_PLAN,
    )
    _set_daemon(
        monkeypatch,
        process={"state": "running", "run_id": "run-stock", "pid": 1, "started_at_ms": 100},
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_ema_paper/status")

    assert response.status_code == 200
    body = response.json()
    assert body["action_plan"] == _STOCK_PLAN
