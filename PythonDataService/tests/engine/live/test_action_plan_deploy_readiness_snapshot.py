"""Cross-stack snapshot test for deploy action-plan readiness."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from app.engine.live.deploy import _STRATEGY_REGISTRY, action_plan_deploy_readiness

SNAPSHOT_PATH = (
    Path(__file__).resolve().parents[3]
    / "app"
    / "engine"
    / "live"
    / "action_plan_deploy_readiness.snapshot.json"
)
FRONTEND_SNAPSHOT_PATH = (
    Path(__file__).resolve().parents[4]
    / "Frontend"
    / "src"
    / "app"
    / "components"
    / "broker"
    / "broker-deploy-form"
    / "action-plan-deploy-readiness.snapshot.json"
)


def _snapshot() -> dict[str, object]:
    return json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))


def _cases() -> list[dict[str, object]]:
    return list(_snapshot()["cases"])  # type: ignore[index]


def test_action_plan_deploy_readiness_snapshot_file_exists() -> None:
    assert SNAPSHOT_PATH.exists(), (
        f"snapshot not found at {SNAPSHOT_PATH} - regenerate via "
        "PythonDataService/scripts/regenerate_action_plan_deploy_readiness_snapshot.py"
    )


@pytest.mark.parametrize("case", _cases(), ids=lambda case: str(case["id"]))
def test_action_plan_deploy_readiness_snapshot_matches_backend(case: dict[str, object]) -> None:
    action_plan = case["action_plan"]
    live_config = {} if action_plan is None else {"action": action_plan}
    readiness = action_plan_deploy_readiness(
        strategy_key=str(case["strategy_key"]),
        live_config=live_config,
    )

    assert readiness.can_deploy is case["can_deploy"]
    assert readiness.reason_code == case["reason_code"]
    assert readiness.message == case["message"]


def test_action_plan_deploy_readiness_snapshots_are_byte_identical() -> None:
    if not FRONTEND_SNAPSHOT_PATH.exists():
        pytest.skip(f"Frontend snapshot not visible at {FRONTEND_SNAPSHOT_PATH}")
    assert SNAPSHOT_PATH.read_bytes() == FRONTEND_SNAPSHOT_PATH.read_bytes()


def test_ema_crossover_signal_requires_a_stock_action_plan() -> None:
    readiness = action_plan_deploy_readiness(
        strategy_key="ema_crossover_signal",
        live_config={},
    )

    assert readiness.can_deploy is False
    assert readiness.reason_code == "ACTION_PLAN_EMPTY"
    assert readiness.message.startswith("EMA Crossover Signal requires an action plan")


def test_action_plan_readiness_uses_the_registry_display_name(monkeypatch: pytest.MonkeyPatch) -> None:
    key = "ema_crossover_signal"
    original = _STRATEGY_REGISTRY[key]
    monkeypatch.setitem(_STRATEGY_REGISTRY, key, replace(original, display_name="Registry-Owned Label"))

    readiness = action_plan_deploy_readiness(strategy_key=key, live_config={})

    assert readiness.message.startswith("Registry-Owned Label requires an action plan")
