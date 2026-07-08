"""Regenerate the cross-stack deploy action-plan readiness snapshot.

The backend deploy gate in ``app.engine.live.deploy`` is authoritative. The
Angular deploy form has a preflight mirror so operators see the block before
submitting. This snapshot pins representative reason-code/message scenarios on
both sides so copy or branch drift fails tests.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Final

from app.engine.live.deploy import action_plan_deploy_readiness

logger = logging.getLogger(__name__)

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]

_PYTHON_SNAPSHOT_PATH: Final[Path] = (
    _REPO_ROOT
    / "PythonDataService"
    / "app"
    / "engine"
    / "live"
    / "action_plan_deploy_readiness.snapshot.json"
)
_FRONTEND_SNAPSHOT_PATH: Final[Path] = (
    _REPO_ROOT
    / "Frontend"
    / "src"
    / "app"
    / "components"
    / "broker"
    / "broker-deploy-form"
    / "action-plan-deploy-readiness.snapshot.json"
)

_SNAPSHOT_COMMENT: Final[str] = (
    "Snapshot of the backend deploy action-plan readiness contract. "
    "PythonDataService/tests/engine/live/test_action_plan_deploy_readiness_snapshot.py "
    "asserts these cases match app.engine.live.deploy.action_plan_deploy_readiness. "
    "Frontend/src/app/components/broker/broker-deploy-form/deploy-readiness.spec.ts "
    "asserts the Angular preflight mirror returns the same reason_code/message "
    "for each case. Regenerate both copies with "
    "PythonDataService/scripts/regenerate_action_plan_deploy_readiness_snapshot.py."
)

_ENTRY_ONLY_STOCK_PLAN: Final[dict[str, object]] = {
    "on_enter": [
        {
            "leg_id": "spy_long",
            "instrument": {"kind": "stock", "underlying": "SPY"},
            "position": "long",
            "qty_ratio": 1,
        }
    ],
    "on_exit": [],
}

_READY_STOCK_PLAN: Final[dict[str, object]] = {
    **_ENTRY_ONLY_STOCK_PLAN,
    "on_exit": [{"kind": "close_leg", "entry_leg_id": "spy_long"}],
}

_CASES: Final[list[dict[str, object]]] = [
    {
        "id": "other_strategy_entry_only_is_ready",
        "strategy_key": "spy_ema_crossover",
        "action_plan": _ENTRY_ONLY_STOCK_PLAN,
    },
    {
        "id": "missing_action_envelope",
        "strategy_key": "deployment_validation",
        "action_plan": None,
    },
    {
        "id": "empty_action_plan",
        "strategy_key": "deployment_validation",
        "action_plan": {"on_enter": [], "on_exit": []},
    },
    {
        "id": "missing_entry_leg",
        "strategy_key": "deployment_validation",
        "action_plan": {"on_enter": [], "on_exit": [{"kind": "close_leg", "entry_leg_id": "spy_long"}]},
    },
    {
        "id": "malformed_entry_shape",
        "strategy_key": "deployment_validation",
        "action_plan": {"on_enter": [{}], "on_exit": [{"kind": "close_leg", "entry_leg_id": "spy_long"}]},
    },
    {
        "id": "unsupported_short_stock",
        "strategy_key": "deployment_validation",
        "action_plan": {
            "on_enter": [
                {
                    "leg_id": "spy_short",
                    "instrument": {"kind": "stock", "underlying": "SPY"},
                    "position": "short",
                    "qty_ratio": 1,
                }
            ],
            "on_exit": [{"kind": "close_leg", "entry_leg_id": "spy_short"}],
        },
    },
    {
        "id": "missing_close_leg",
        "strategy_key": "deployment_validation",
        "action_plan": _ENTRY_ONLY_STOCK_PLAN,
    },
    {
        "id": "ready_deployment_validation_stock",
        "strategy_key": "deployment_validation",
        "action_plan": _READY_STOCK_PLAN,
    },
]


def build_snapshot() -> dict[str, object]:
    cases: list[dict[str, object]] = []
    for case in _CASES:
        action_plan = case["action_plan"]
        live_config = {} if action_plan is None else {"action": action_plan}
        readiness = action_plan_deploy_readiness(
            strategy_key=str(case["strategy_key"]),
            live_config=live_config,
        )
        cases.append(
            {
                **case,
                "can_deploy": readiness.can_deploy,
                "reason_code": readiness.reason_code,
                "message": readiness.message,
            }
        )
    return {
        "$comment": _SNAPSHOT_COMMENT,
        "generated_by": "PythonDataService/scripts/regenerate_action_plan_deploy_readiness_snapshot.py",
        "source_files": ["PythonDataService/app/engine/live/deploy.py (action_plan_deploy_readiness)"],
        "cases": cases,
    }


def _write(path: Path, snapshot: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snapshot, indent=2, sort_keys=False) + "\n", encoding="utf-8")


def write_snapshots() -> tuple[Path, Path]:
    snapshot = build_snapshot()
    _write(_PYTHON_SNAPSHOT_PATH, snapshot)
    _write(_FRONTEND_SNAPSHOT_PATH, snapshot)
    return _PYTHON_SNAPSHOT_PATH, _FRONTEND_SNAPSHOT_PATH


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    py_path, fe_path = write_snapshots()
    logger.info("wrote snapshot", extra={"path": str(py_path)})
    logger.info("wrote snapshot", extra={"path": str(fe_path)})
    return 0


if __name__ == "__main__":
    sys.exit(main())
