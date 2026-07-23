"""Shared fixtures for the Alpaca vendor-layer tests."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Protocol

import pytest
from alpaca.common.exceptions import APIError

# tests/broker/alpaca/conftest.py → tests/fixtures/alpaca/
_ALPACA_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "alpaca"

type AlpacaFixtureLoader = Callable[[str, str], Any]


class ApiErrorFactory(Protocol):
    """Callable shape supplied by the ``make_api_error`` fixture."""

    def __call__(
        self,
        status: int | None,
        *,
        message: str = "denied",
        headers: dict[str, str] | None = None,
    ) -> APIError: ...


@pytest.fixture
def load_alpaca_fixture() -> AlpacaFixtureLoader:
    """Load a committed Alpaca payload fixture: (family, filename) → parsed JSON."""

    def _load(family: str, filename: str) -> Any:
        return json.loads((_ALPACA_FIXTURES / family / filename).read_text())

    return _load


@pytest.fixture
def make_api_error() -> ApiErrorFactory:
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
