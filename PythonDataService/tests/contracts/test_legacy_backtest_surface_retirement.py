"""Structural guard for the retired pre-Engine-Lab strategy surface."""

from __future__ import annotations

from pathlib import Path


def test_legacy_strategy_service_package_is_retired() -> None:
    service_root = Path(__file__).resolve().parents[2] / "app" / "services" / "strategies"

    assert not list(service_root.glob("*.py"))


def test_legacy_live_instance_control_projection_is_retired() -> None:
    """Was a scan of `routers/live_instances.py` for the control-projection
    route literals it must no longer declare. PR-B of #1813 (2026-08-27)
    retired the router itself, so the guarantee is now the stronger "no
    live-instance router exists to declare them"."""
    routers = Path(__file__).resolve().parents[2] / "app" / "routers"

    for filename in ("live_instances.py", "live_runs.py", "bot_events.py"):
        assert not (routers / filename).exists(), filename


def test_legacy_live_instance_surface_assembler_is_retired() -> None:
    service = (
        Path(__file__).resolve().parents[2]
        / "app"
        / "services"
        / "live_instance_surface_assembler.py"
    )

    assert not service.exists()


def test_run_evidence_api_is_retired() -> None:
    """Was a pin that the two surviving `/api/live-runs` routes were
    read-only. PR-B of #1813 (2026-08-27) retired the whole run-evidence
    surface (`routers/live_runs.py`, `routers/bot_events.py`), so the
    guarantee is now "no `/api/live-runs` route is registered at all" —
    a route that does not exist cannot grow a mutation verb."""
    from app.main import app

    live_run_paths = {
        route.path
        for route in app.routes
        if getattr(route, "path", "").startswith("/api/live-runs")
    }

    assert live_run_paths == set()
