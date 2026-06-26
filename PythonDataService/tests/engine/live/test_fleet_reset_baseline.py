from __future__ import annotations

import json
from pathlib import Path

from app.engine.live.fleet_reset_baseline import (
    baseline_path,
    read_applicable_baseline,
)


def test_read_applicable_baseline_accepts_flat_listed_instance(tmp_path: Path) -> None:
    path = baseline_path(tmp_path, "DUM284968")
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "account_id": "DUM284968",
                "baseline_at_ms": 1_700_000_000_000,
                "positions": [],
                "open_orders": [],
                "applies_to_strategy_instance_ids": ["DEPVAL-AAPL-20260626"],
            }
        ),
        encoding="utf-8",
    )

    baseline = read_applicable_baseline(
        live_runs_root=tmp_path,
        account_id="DUM284968",
        strategy_instance_id="DEPVAL-AAPL-20260626",
    )

    assert baseline is not None
    assert baseline.baseline_at_ms == 1_700_000_000_000


def test_read_applicable_baseline_rejects_non_flat_snapshot(tmp_path: Path) -> None:
    path = baseline_path(tmp_path, "DUM284968")
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "account_id": "DUM284968",
                "baseline_at_ms": 1_700_000_000_000,
                "positions": [{"symbol": "TSLA", "quantity": 1}],
                "open_orders": [],
                "applies_to_strategy_instance_ids": ["DEPVAL-AAPL-20260626"],
            }
        ),
        encoding="utf-8",
    )

    baseline = read_applicable_baseline(
        live_runs_root=tmp_path,
        account_id="DUM284968",
        strategy_instance_id="DEPVAL-AAPL-20260626",
    )

    assert baseline is None


def test_read_applicable_baseline_rejects_unlisted_instance(tmp_path: Path) -> None:
    path = baseline_path(tmp_path, "DUM284968")
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "account_id": "DUM284968",
                "baseline_at_ms": 1_700_000_000_000,
                "positions": [],
                "open_orders": [],
                "applies_to_strategy_instance_ids": ["DEPVAL-AAPL-20260626"],
            }
        ),
        encoding="utf-8",
    )

    baseline = read_applicable_baseline(
        live_runs_root=tmp_path,
        account_id="DUM284968",
        strategy_instance_id="DEPVAL-SPY-20260626",
    )

    assert baseline is None
