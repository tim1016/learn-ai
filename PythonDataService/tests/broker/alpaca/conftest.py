"""Shared fixtures for the Alpaca vendor-layer tests."""

from __future__ import annotations

import json
from collections.abc import Callable
from types import SimpleNamespace

import pytest
from alpaca.common.exceptions import APIError


@pytest.fixture
def make_api_error() -> Callable[..., APIError]:
    """Build an alpaca-py ``APIError`` with a chosen HTTP status and headers."""

    def _make(
        status: int | None,
        *,
        message: str = "denied",
        headers: dict[str, str] | None = None,
    ) -> APIError:
        body = json.dumps({"code": 40010000, "message": message})
        response = SimpleNamespace(status_code=status, headers=headers or {})
        http_error = SimpleNamespace(response=response, request=None)
        return APIError(body, http_error=http_error)

    return _make
