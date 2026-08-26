from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = REPOSITORY_ROOT / "PythonDataService"
FAST_TEST_COMMAND_SOURCES = (
    REPOSITORY_ROOT / ".github/workflows/ci.yml",
    REPOSITORY_ROOT / ".claude/CLAUDE.md",
    REPOSITORY_ROOT / ".claude/commands/test-all.md",
    SERVICE_ROOT / "CLAUDE.md",
    SERVICE_ROOT / "pytest.ini",
)


def test_root_conftest_defers_fastapi_app_import() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import runpy, sys; "
                "runpy.run_path('tests/conftest.py'); "
                "raise SystemExit('app.main' in sys.modules)"
            ),
        ],
        cwd=SERVICE_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_fast_test_commands_filter_by_marker_not_name() -> None:
    incorrect_sources: list[str] = []
    missing_sources: list[str] = []

    for path in FAST_TEST_COMMAND_SOURCES:
        contents = path.read_text(encoding="utf-8")
        relative_path = str(path.relative_to(REPOSITORY_ROOT))
        if '-k "not slow"' in contents:
            incorrect_sources.append(relative_path)
        if '-m "not slow"' not in contents:
            missing_sources.append(relative_path)

    assert incorrect_sources == []
    assert missing_sources == []
