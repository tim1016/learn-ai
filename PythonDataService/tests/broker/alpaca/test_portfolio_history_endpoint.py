"""C1 endpoint seam: ASGI router → captured Alpaca SDK request → contract.

``alpaca-py`` uses ``requests``, so ``responses`` is deliberately used instead
of ``respx``: it is the requests-level mock that exercises the production
transport, including the capture hook. The endpoint itself is exercised through
``httpx.AsyncClient`` and ``ASGITransport``.
"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
import responses
from httpx import ASGITransport, AsyncClient, Response

from app.broker.alpaca.broker import AlpacaBroker
from app.broker.alpaca.client import AlpacaTradingClient
from app.broker.alpaca.config import AlpacaSettings
from app.broker.capture.journal import CaptureJournal
from app.broker.contract.registry import (
    get_broker_registry,
    reset_broker_registry_for_testing,
)
from app.config import settings
from app.main import app
from app.security.data_plane_control import CONTROL_SECRET_HEADER

_BASE = "https://paper-api.alpaca.markets"
_PATH = "/v2/account/portfolio/history"


@pytest.fixture(autouse=True)
def _clean_registry() -> Generator[None, None, None]:
    reset_broker_registry_for_testing()
    yield
    reset_broker_registry_for_testing()


def _control_headers() -> dict[str, str]:
    secret = settings.DATA_PLANE_CONTROL_SECRET.strip()
    return {CONTROL_SECRET_HEADER: secret} if secret else {}


async def _get(history_range: str) -> Response:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.get(
            f"/api/brokers/alpaca/portfolio-history?range={history_range}",
            headers=_control_headers(),
        )


@pytest.mark.parametrize(
    ("history_range", "period", "timeframe", "vendor_timestamps", "expected_timestamps"),
    [
        ("1D", "1D", "1Min", [1_700_000_000, 1_700_000_060], [1_700_000_000_000, 1_700_000_060_000]),
        ("30D", "30D", "1D", [1_700_000_000, 1_700_000_060], [1_700_000_000_000, 1_700_000_060_000]),
        ("60D", "60D", "1D", [1_700_000_000, 1_700_000_060], [1_700_000_000_000, 1_700_000_060_000]),
        ("1D", "1D", "1Min", [1_700_000_000_000, 1_700_000_060_000], [1_700_000_000_000, 1_700_000_060_000]),
    ],
)
@responses.activate
async def test_get_portfolio_history_maps_range_and_normalizes_timestamps(
    tmp_path: Path,
    history_range: str,
    period: str,
    timeframe: str,
    vendor_timestamps: list[int],
    expected_timestamps: list[int],
) -> None:
    journal = CaptureJournal(capture_dir=tmp_path, clock=lambda: 1_700_000_000_000)
    client = AlpacaTradingClient(
        settings=AlpacaSettings(api_key_id="key", api_secret_key="secret"),
        journal=journal,
    )
    get_broker_registry().register(AlpacaBroker(client))
    responses.add(
        responses.GET,
        f"{_BASE}{_PATH}",
        json={
            # Alpaca's documented response shape calls this vendor field
            # ``timestamp`` (singular). C1 returns the normalized plural form.
            "timestamp": vendor_timestamps,
            "equity": [100_000.0, 100_125.5],
            "profit_loss": [0.0, 125.5],
            "base_value": 100_000.0,
            "timeframe": timeframe,
        },
        status=200,
    )

    response = await _get(history_range)

    assert response.status_code == 200
    assert response.json() == {
        "timestamps": expected_timestamps,
        "equity": [100_000.0, 100_125.5],
        "profit_loss": [0.0, 125.5],
        "base_value": 100_000.0,
        "timeframe": timeframe,
    }
    query = parse_qs(urlparse(responses.calls[0].request.url).query)
    assert query == {"period": [period], "timeframe": [timeframe]}
    assert journal.records_written == 1


@pytest.mark.parametrize(
    "vendor_body",
    [
        pytest.param(
            {"timestamp": [1_700_000_000], "profit_loss": [0.0], "base_value": 1.0, "timeframe": "1D"},
            id="missing-equity",
        ),
        pytest.param(
            {
                "timestamp": [1_700_000_000, 1_700_000_060],
                "equity": [100_000.0],
                "profit_loss": [0.0, 1.0],
                "base_value": 100_000.0,
                "timeframe": "1D",
            },
            id="misaligned-series",
        ),
        # A null timeframe once became timeframe "None" (#2643).
        pytest.param(
            {"timestamp": [1_700_000_000], "equity": [1.0], "profit_loss": [0.0], "timeframe": None},
            id="null-timeframe",
        ),
        pytest.param(
            {"timestamp": [1_700_000_000], "equity": [1.0], "profit_loss": [0.0], "timeframe": " "},
            id="blank-timeframe",
        ),
    ],
)
@responses.activate
async def test_a_malformed_portfolio_history_reaches_the_owner_as_a_named_503(
    tmp_path: Path,
    vendor_body: dict[str, object],
) -> None:
    """The history read once let the adapter's raw error escape as a 500 (#2643)."""
    journal = CaptureJournal(capture_dir=tmp_path, clock=lambda: 1_700_000_000_000)
    client = AlpacaTradingClient(
        settings=AlpacaSettings(api_key_id="key", api_secret_key="secret"),
        journal=journal,
    )
    get_broker_registry().register(AlpacaBroker(client))
    responses.add(responses.GET, f"{_BASE}{_PATH}", json=vendor_body, status=200)

    response = await _get("30D")

    assert response.status_code == 503
    assert response.json()["detail"] == {
        "broker": "alpaca",
        "message": "Alpaca sent portfolio history data this app could not read.",
        "why": "Part of Alpaca's portfolio history data was missing or in a form this app does not recognize.",
    }
