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
RETIRED_OPENAPI_SCHEMAS = (
    "LifecycleProjectionEventRow",
    "LifecycleSafetyProjectionEventRow",
    "LifecycleSafetyTriageResponse",
    "LifecycleTimelineResponse",
)
RETIRED_RUNTIME_IMPORTS = (
    "app.routers.lifecycle_projection",
    "app.schemas.lifecycle_projection",
    "app.services.lifecycle_projection_",
    "LIFECYCLE_PROJECTION_ENABLED",
)


def test_legacy_lifecycle_projection_runtime_and_contract_are_retired() -> None:
    for relative_path in RETIRED_RUNTIME_PATHS:
        assert not (REPOSITORY_ROOT / relative_path).exists(), relative_path

    main_source = (REPOSITORY_ROOT / "PythonDataService/app/main.py").read_text(encoding="utf-8")
    config_source = (REPOSITORY_ROOT / "PythonDataService/app/config.py").read_text(encoding="utf-8")
    assert "lifecycle_projection" not in main_source
    assert "LIFECYCLE_PROJECTION_ENABLED" not in config_source

    application_sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (REPOSITORY_ROOT / "PythonDataService/app").rglob("*.py")
    )
    for retired_import in RETIRED_RUNTIME_IMPORTS:
        assert retired_import not in application_sources

    openapi = json.loads(
        (REPOSITORY_ROOT / "contracts/openapi/python-data-service.openapi.json").read_text(encoding="utf-8")
    )
    assert "/api/lifecycle-projection/timeline" not in openapi["paths"]
    assert "/api/lifecycle-projection/safety-triage" not in openapi["paths"]
    for schema_name in RETIRED_OPENAPI_SCHEMAS:
        assert schema_name not in openapi["components"]["schemas"]

    generated_client = (
        REPOSITORY_ROOT / "Frontend/src/app/api/broker.types.ts"
    ).read_text(encoding="utf-8")
    frontend_sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (REPOSITORY_ROOT / "Frontend/src").rglob("*.ts")
    )
    assert "/api/lifecycle-projection" not in generated_client
    assert "lifecycle-projection.types" not in frontend_sources
    for schema_name in RETIRED_OPENAPI_SCHEMAS:
        assert schema_name not in generated_client


def test_transaction_history_read_path_remains_bounded_and_projection_only() -> None:
    """The IBKR/Postgres projection half of this guard (transaction_history /
    transaction_detail in clerk_transaction_projection.py) retired with PR-A
    of #1813 (2026-08-26) — that file now holds only the shared
    ClerkTransactionProjectionUnavailable exception. The router-level bounded/
    projection-only invariants below still hold for the Alpaca/SQLite path.
    """
    router_source = (
        REPOSITORY_ROOT / "PythonDataService/app/routers/clerk_transactions.py"
    ).read_text(encoding="utf-8")

    assert "Query(default=50, ge=1, le=100)" in router_source
    assert "app.services.lifecycle_projection" not in router_source
    assert "app.broker." not in router_source
    assert "account_truth" not in router_source.lower()
