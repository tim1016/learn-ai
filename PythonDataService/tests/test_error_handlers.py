"""Tests for app.utils.error_handlers."""

from __future__ import annotations

import json

from fastapi import Request
from fastapi.exceptions import RequestValidationError

from app.utils.error_handlers import (
    catalog_schema_not_ready_exception_handler,
    polygon_exception_handler,
    request_validation_exception_handler,
)


def _make_request() -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [],
        "query_string": b"",
    }
    return Request(scope)


async def test_polygon_exception_handler_returns_500_json_response() -> None:
    request = _make_request()
    exc = RuntimeError("boom")

    response = await polygon_exception_handler(request, exc)

    assert response.status_code == 500
    body = json.loads(response.body)
    assert body == {
        "success": False,
        "error": "boom",
        "detail": "An error occurred while processing your request",
    }


async def test_polygon_exception_handler_serializes_exception_message() -> None:
    request = _make_request()
    exc = ValueError("unexpected value: 42")

    response = await polygon_exception_handler(request, exc)

    body = json.loads(response.body)
    assert body["success"] is False
    assert body["error"] == "unexpected value: 42"


async def test_catalog_schema_not_ready_handler_returns_a_typed_503_not_a_500() -> None:
    """A mid-deploy catalog-schema race must not escape as an internal error.

    Mirrors test_app_translates_clerk_errors_to_a_typed_503_not_a_500 in
    tests/broker/alpaca/clerk/sqlite/test_projection_reader_missing_db.py --
    same shape of problem (a transient system state, not an unexpected
    fault), same fix (a typed 503 registered ahead of the catch-all 500).
    """
    from app.data_lake.catalog_client import CatalogSchemaNotReadyError
    from app.main import app

    assert app.exception_handlers.get(CatalogSchemaNotReadyError) is catalog_schema_not_ready_exception_handler

    response = await catalog_schema_not_ready_exception_handler(
        _make_request(),
        CatalogSchemaNotReadyError("ON CONFLICT target matches no constraint/index (Postgres 42P10): boom"),
    )

    assert response.status_code == 503
    body = json.loads(response.body)
    assert body["success"] is False
    assert body["detail"]["reason_code"] == "catalog_schema_not_ready"
    assert body["detail"]["error_type"] == "CatalogSchemaNotReadyError"
    # The stable operator message, not the raw driver error text.
    assert "42P10" not in body["detail"]["message"]
    assert "not yet ready" in body["detail"]["message"]


async def test_request_validation_handler_serializes_nested_non_finite_inputs() -> None:
    exc = RequestValidationError(
        [
            {
                "type": "float_parsing",
                "loc": ("body", "value"),
                "msg": "Input should be a valid number",
                "input": {"nested": float("nan")},
                "ctx": {"input": [float("inf")]},
            }
        ]
    )

    response = await request_validation_exception_handler(_make_request(), exc)

    assert response.status_code == 422
    detail = json.loads(response.body)["detail"][0]
    assert detail["input"]["nested"] == "nan"
    assert detail["ctx"]["input"] == ["inf"]
