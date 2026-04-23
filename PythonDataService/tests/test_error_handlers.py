"""Tests for app.utils.error_handlers."""

from __future__ import annotations

import json

from fastapi import Request

from app.utils.error_handlers import polygon_exception_handler


def _make_request() -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [],
        "query_string": b"",
    }
    return Request(scope)


async def test_polygon_exception_handler_returns_500_json_response():
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


async def test_polygon_exception_handler_serializes_exception_message():
    request = _make_request()
    exc = ValueError("unexpected value: 42")

    response = await polygon_exception_handler(request, exc)

    body = json.loads(response.body)
    assert body["success"] is False
    assert body["error"] == "unexpected value: 42"
