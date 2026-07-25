from __future__ import annotations

import json
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
RETIRED_RUNTIME_PATHS = (
    "PythonDataService/app/routers/lifecycle_projection.py",
    "PythonDataService/app/schemas/lifecycle_projection.py",
    "PythonDataService/app/services/lifecycle_projection_schema.py",
    "PythonDataService/app/services/lifecycle_projection_store.py",
    "PythonDataService/app/services/lifecycle_projection_replay.py",
    "PythonDataService/app/services/lifecycle_projection_tailer.py",
)


def test_legacy_lifecycle_projection_runtime_and_contract_are_retired() -> None:
    for relative_path in RETIRED_RUNTIME_PATHS:
        assert not (REPOSITORY_ROOT / relative_path).exists(), relative_path

    main_source = (REPOSITORY_ROOT / "PythonDataService/app/main.py").read_text(encoding="utf-8")
    config_source = (REPOSITORY_ROOT / "PythonDataService/app/config.py").read_text(encoding="utf-8")
    assert "lifecycle_projection" not in main_source
    assert "LIFECYCLE_PROJECTION_ENABLED" not in config_source

    openapi = json.loads(
        (REPOSITORY_ROOT / "contracts/openapi/python-data-service.openapi.json").read_text(encoding="utf-8")
    )
    assert "/api/lifecycle-projection/timeline" not in openapi["paths"]
    assert "/api/lifecycle-projection/safety-triage" not in openapi["paths"]
    assert "LifecycleProjectionEventRow" not in openapi["components"]["schemas"]


def test_transaction_history_read_path_remains_bounded_and_projection_only() -> None:
    router_source = (
        REPOSITORY_ROOT / "PythonDataService/app/routers/clerk_transactions.py"
    ).read_text(encoding="utf-8")
    service_source = (
        REPOSITORY_ROOT / "PythonDataService/app/services/clerk_transaction_projection.py"
    ).read_text(encoding="utf-8")

    assert "Query(default=50, ge=1, le=100)" in router_source
    assert "app.services.lifecycle_projection" not in router_source
    assert "app.broker." not in router_source
    assert "account_truth" not in router_source.lower()

    history_source = service_source.split("async def transaction_history", maxsplit=1)[1]
    history_source = history_source.split("class PostgresClerkTransactionProjectionStore", maxsplit=1)[0]
    assert "store.history_page(" in history_source
    assert "read_appended_clerk_journal" not in history_source
    assert "read_appended_alpaca_journal" not in history_source
