from __future__ import annotations

import json

import app.engine.live.run_ledger as run_ledger


def test_legacy_run_ledger_is_read_only_parser_only(tmp_path) -> None:
    path = tmp_path / "run_ledger.json"
    path.write_text(
        json.dumps(
            {
                "run_id": "legacy-run",
                "code_sha": "abc123",
                "strategy_instance_id": "legacy-bot",
                "strategy_spec_path": "spec.json",
                "strategy_spec_sha256": "spec-sha",
                "qc_audit_copy_path": "qc.py",
                "qc_audit_copy_sha256": "qc-sha",
                "qc_cloud_backtest_id": "qc-1",
                "account_id": "DU123",
                "start_date_ms": 1_780_000_000_000,
                "live_config": {},
            }
        ),
        encoding="utf-8",
    )

    parsed = run_ledger.read_ledger(path)

    assert parsed.run_id == "legacy-run"
    assert parsed.strategy_instance_id == "legacy-bot"
    assert not hasattr(run_ledger, "build_ledger")
    assert not hasattr(run_ledger, "write_ledger")
    assert not hasattr(run_ledger, "compute_run_id")
