"""Phase 3+ parity test harness — pytest options scoped to this package."""

from __future__ import annotations

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--write-recon-report",
        action="store_true",
        default=False,
        help=(
            "Write reconciliation report to "
            "PythonDataService/artifacts/reconciliations/ even when the "
            "parity test passes (default: only on failure)."
        ),
    )


@pytest.fixture
def write_recon_report(request: pytest.FixtureRequest) -> bool:
    return bool(request.config.getoption("--write-recon-report"))
