"""Tests for app.utils.error_handlers."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic_core import PydanticUndefined

from app.utils.error_handlers import (
    _ECHO_DEPTH_LIMIT,
    _ECHO_TRUNCATED,
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


async def test_a_required_list_parameter_that_was_not_sent_is_a_422_not_a_500() -> None:
    """FastAPI reports a list query parameter that was not sent with pydantic's "undefined" as its input."""
    exc = RequestValidationError([{"type": "list_type", "loc": ("query", "params_hash"), "msg": "Input should be a valid list", "input": PydanticUndefined}])

    response = await request_validation_exception_handler(_make_request(), exc)

    assert response.status_code == 422
    assert json.loads(response.body)["detail"] == [{"type": "list_type", "loc": ["query", "params_hash"], "msg": "Input should be a valid list", "input": None}]


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


async def test_request_validation_handler_binds_the_echo_of_deeply_nested_input() -> None:
    """#2969: input nested past the recursion limit is truncated, not walked to a crash."""
    deep: Any = 1
    for _ in range(2000):
        deep = {"a": deep}
    exc = RequestValidationError(
        [{"type": "missing", "loc": ("body", "method"), "msg": "Field required", "input": deep}]
    )

    response = await request_validation_exception_handler(_make_request(), exc)

    assert response.status_code == 422
    echoed: Any = json.loads(response.body)["detail"][0]["input"]
    steps = 0
    while isinstance(echoed, dict):
        steps += 1
        echoed = echoed["a"]
    assert echoed == _ECHO_TRUNCATED
    assert steps <= _ECHO_DEPTH_LIMIT + 1


async def test_a_route_posted_a_deeply_nested_json_body_answers_422_not_500() -> None:
    """#2969 done-when: the nested body posted to one route gets the 422 it is due."""
    import httpx

    from app.main import app

    body = '{"a":' * 2000 + "1" + "}" * 2000
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/research/strategy-runs/baselines",
            content=body,
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 422
    assert isinstance(json.loads(response.content)["detail"], list)


# ── A string no UTF-8 can write (a lone surrogate, JSON "\ud800") ───────────────────────────────


async def test_a_lone_surrogate_in_the_echoed_input_is_written_as_a_question_mark_in_a_value_and_in_a_key() -> None:
    # "\ud800" is half of a UTF-16 pair and has no UTF-8 form: encoding it with errors="replace" gives the one byte
    # 0x3F, a question mark. The value "a\ud800b" is read back as "a?b", the key "\ud800" as "?", and the surrogate in
    # the error's own location ("loc") the same way.
    exc = RequestValidationError(
        [
            {
                "type": "extra_forbidden",
                "loc": ("body", "\ud800"),
                "msg": "Extra inputs are not permitted",
                "input": {"\ud800": 1, "ok": "a\ud800b"},
            }
        ]
    )

    response = await request_validation_exception_handler(_make_request(), exc)

    assert response.status_code == 422
    assert json.loads(response.body)["detail"] == [
        {
            "type": "extra_forbidden",
            "loc": ["body", "?"],
            "msg": "Extra inputs are not permitted",
            "input": {"?": 1, "ok": "a?b"},
        }
    ]


async def test_an_answer_with_no_lone_surrogate_in_it_is_written_exactly_as_it_was() -> None:
    # Strings UTF-8 can write (accents, Japanese, an emoji outside the basic plane) are untouched, and so is the rest:
    # numbers, null, a boolean, nested lists and a key, and a NaN is the word "nan" as before.
    echoed = {
        "é": "日本語",
        "pair": "\U0001f600",
        "numbers": [1, 2.5, None, True],
        "nested": {"k": ["v"]},
        "x": float("nan"),
    }
    exc = RequestValidationError([{"type": "missing", "loc": ("body", "é"), "msg": "Field required", "input": echoed}])

    response = await request_validation_exception_handler(_make_request(), exc)

    expected = {
        "detail": [
            {
                "type": "missing",
                "loc": ["body", "é"],
                "msg": "Field required",
                "input": {
                    "é": "日本語",
                    "pair": "\U0001f600",
                    "numbers": [1, 2.5, None, True],
                    "nested": {"k": ["v"]},
                    "x": "nan",
                },
            }
        ]
    }
    assert response.status_code == 422
    assert response.body == JSONResponse(content=expected).body


# Two job routes of other features that take a JSON body and refuse this one at validation, so nothing starts (the
# options backtest's own two routes are held in its route tests). The body is sent as ASCII-escaped JSON bytes, since
# httpx's json= will not encode a lone surrogate on the client side.
JOB_ROUTES = [
    "/api/jobs-internal/grid-search",
    "/api/jobs-internal/golden-search",
]


@pytest.mark.parametrize("route", JOB_ROUTES)
@pytest.mark.parametrize(
    ("payload", "echoed"),
    [
        # The body lacks the fields the route needs, so each "missing" error echoes the body it was given.
        ({"job_id": "\ud800"}, '"job_id":"?"'),
        ({"job_id": "job-1", "x": {"\ud800": 1}}, '"x":{"?":1}'),
    ],
    ids=["in a value", "in a key"],
)
async def test_a_job_route_refuses_a_body_with_a_lone_surrogate_in_it_with_a_422_and_not_a_500(
    route: str, payload: dict[str, Any], echoed: str
) -> None:
    from app.main import app

    body = json.dumps(payload, ensure_ascii=True).encode("ascii")
    assert b"\\ud800" in body
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(route, content=body, headers={"Content-Type": "application/json"})

    assert response.status_code == 422
    assert echoed in response.text
