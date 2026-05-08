# Intentionally minimal — prevents pytest from loading the parent
# tests/conftest.py (which imports the full FastAPI app) when only
# running fixture validation tests that have no app dependency.
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

_SVC_ROOT = Path(__file__).parent.parent.parent
_ARTIFACT_PATH = _SVC_ROOT / "artifacts" / "fixture-validation" / "latest.json"


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Write a validation summary to artifacts/fixture-validation/latest.json."""
    try:
        terminalreporter = session.config.pluginmanager.get_plugin("terminalreporter")
        if terminalreporter:
            stats = terminalreporter.stats
            n_passed = len(stats.get("passed", []))
            n_failed = len(stats.get("failed", []))
            n_error = len(stats.get("error", []))
        else:
            n_passed = 0
            n_failed = 0
            n_error = 0

        artifact = {
            "generated_at": datetime.now(UTC).isoformat(),
            "exit_status": int(exitstatus),
            "passed": n_passed,
            "failed": n_failed,
            "errors": n_error,
            "status": "ok" if exitstatus == 0 else "fail",
        }
        _ARTIFACT_PATH.parent.mkdir(parents=True, exist_ok=True)
        _ARTIFACT_PATH.write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")
    except Exception:
        pass  # Never let artifact writing break test results
