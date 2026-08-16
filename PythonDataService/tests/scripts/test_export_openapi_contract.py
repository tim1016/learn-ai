"""Regression tests for deterministic OpenAPI contract generation."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def test_export_excludes_dev_fault_routes_when_caller_environment_enables_them(
    tmp_path: Path,
) -> None:
    service_root = Path(__file__).resolve().parents[2]
    output = tmp_path / "openapi.json"
    environment = os.environ.copy()
    environment["ALPACA_FAULT_INJECTION_ENABLED"] = "true"
    environment.setdefault("POLYGON_API_KEY", "contract-schema-placeholder")

    subprocess.run(
        [
            sys.executable,
            str(service_root / "scripts" / "export_openapi_contract.py"),
            "--output",
            str(output),
        ],
        cwd=service_root,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    contract = json.loads(output.read_text(encoding="utf-8"))
    assert "/api/brokers/alpaca/fault-injection/arm" not in contract["paths"]
    for schema_name in ("ManualOrderPreviewRequest", "ManualOrderSubmitRequest"):
        legs = contract["components"]["schemas"][schema_name]["properties"]["legs"]
        assert legs["minItems"] == 1
        assert legs["maxItems"] == 8

    run_ledger = contract["components"]["schemas"]["RunLedger"]
    assert run_ledger["type"] == "object"
    assert {"run_id", "start_ms", "end_ms", "warmup_start_ms"} <= set(run_ledger["properties"])
