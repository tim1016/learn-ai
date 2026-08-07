"""Structural guard for the retired pre-Engine-Lab strategy surface."""

from __future__ import annotations

from pathlib import Path


def test_legacy_strategy_service_package_is_retired() -> None:
    service_root = Path(__file__).resolve().parents[2] / "app" / "services" / "strategies"

    assert not list(service_root.glob("*.py"))


def test_legacy_live_instance_control_projection_is_retired() -> None:
    router = (
        Path(__file__).resolve().parents[2] / "app" / "routers" / "live_instances.py"
    ).read_text(encoding="utf-8")

    for fragment in (
        '"/catalog"',
        '"/catalog/page"',
        '"/roll-call"',
        '"/{strategy_instance_id}/status"',
        '"/{strategy_instance_id}/operator-surface/stream"',
        '"/{strategy_instance_id}/desired-state"',
        '"/{strategy_instance_id}/commands"',
        '"/{strategy_instance_id}/activity"',
        '"/{strategy_instance_id}/chart-snapshot"',
    ):
        assert fragment not in router


def test_legacy_live_instance_surface_assembler_is_retired() -> None:
    service = (
        Path(__file__).resolve().parents[2]
        / "app"
        / "services"
        / "live_instance_surface_assembler.py"
    )

    assert not service.exists()


def test_run_evidence_api_has_no_mutation_routes() -> None:
    from app.main import app

    methods_by_path = {
        route.path: route.methods
        for route in app.routes
        if route.path
        in {
            "/api/live-runs/{run_id}/commands",
            "/api/live-runs/{run_id}/desired-state",
        }
    }

    assert methods_by_path == {
        "/api/live-runs/{run_id}/commands": {"GET"},
        "/api/live-runs/{run_id}/desired-state": {"GET"},
    }
