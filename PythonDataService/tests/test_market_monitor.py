"""Tests for market monitor endpoints and PolygonMarketMonitor class"""
import pytest
from unittest.mock import patch, MagicMock
from app.services.market_monitor import PolygonMarketMonitor


# ---------------------------------------------------------------------------
# Unit tests for PolygonMarketMonitor class
# ---------------------------------------------------------------------------

class TestPolygonMarketMonitor:
    """Tests for the PolygonMarketMonitor service class"""

    def setup_method(self):
        self.monitor = PolygonMarketMonitor(polygon_api_key="test-key")

    @patch.object(PolygonMarketMonitor, "__init__", lambda self, **kwargs: None)
    def _make_monitor(self):
        m = PolygonMarketMonitor.__new__(PolygonMarketMonitor)
        m.client = MagicMock()
        return m

    def test_get_current_market_state_open(self):
        monitor = self._make_monitor()
        monitor.client.get_market_status.return_value = {
            "market": "open",
            "exchanges": {"nyse": "open", "nasdaq": "open", "otc": "closed"},
            "earlyHours": False,
            "afterHours": False,
            "serverTime": "2026-02-17T10:30:00-05:00",
        }

        result = monitor.get_current_market_state()

        assert result["market"] == "open"
        assert result["exchanges"]["nyse"] == "open"
        assert result["exchanges"]["nasdaq"] == "open"
        assert result["early_hours"] is False
        assert result["after_hours"] is False
        assert "error" not in result

    def test_get_current_market_state_extended_hours(self):
        monitor = self._make_monitor()
        monitor.client.get_market_status.return_value = {
            "market": "extended-hours",
            "exchanges": {"nyse": "extended-hours", "nasdaq": "extended-hours", "otc": "closed"},
            "earlyHours": False,
            "afterHours": True,
            "serverTime": "2026-02-17T17:30:00-05:00",
        }

        result = monitor.get_current_market_state()

        assert result["market"] == "extended-hours"
        assert result["after_hours"] is True

    def test_get_current_market_state_handles_timeout(self):
        monitor = self._make_monitor()
        monitor.client.get_market_status.side_effect = TimeoutError("Connection timed out")

        result = monitor.get_current_market_state()

        assert result["market"] == "unknown"
        assert "error" in result
        assert "timed out" in result["error"].lower()

    def test_get_current_market_state_handles_connection_error(self):
        monitor = self._make_monitor()
        monitor.client.get_market_status.side_effect = ConnectionError("No route to host")

        result = monitor.get_current_market_state()

        assert result["market"] == "unknown"
        assert "error" in result

    def test_get_upcoming_events_deduplicates(self):
        monitor = self._make_monitor()
        monitor.client.get_market_holidays.return_value = [
            {"date": "2026-05-25", "name": "Memorial Day", "status": "closed", "exchange": "NYSE"},
            {"date": "2026-05-25", "name": "Memorial Day", "status": "closed", "exchange": "NASDAQ"},
            {"date": "2026-05-25", "name": "Memorial Day", "status": "closed", "exchange": "OTC"},
            {"date": "2026-07-03", "name": "Independence Day", "status": "early-close",
             "exchange": "NYSE", "open": "2026-07-03T09:30:00-04:00", "close": "2026-07-03T13:00:00-04:00"},
            {"date": "2026-07-03", "name": "Independence Day", "status": "early-close",
             "exchange": "NASDAQ", "open": "2026-07-03T09:30:00-04:00", "close": "2026-07-03T13:00:00-04:00"},
        ]

        result = monitor.get_upcoming_events(limit=5)

        assert len(result) == 2
        assert result[0]["name"] == "Memorial Day"
        assert result[0]["status"] == "Closed"
        assert "NYSE" in result[0]["exchanges"]
        assert "NASDAQ" in result[0]["exchanges"]
        assert result[1]["name"] == "Independence Day"
        assert result[1]["status"] == "Early Close"

    def test_get_upcoming_events_respects_limit(self):
        monitor = self._make_monitor()
        monitor.client.get_market_holidays.return_value = [
            {"date": f"2026-0{i+1}-01", "name": f"Holiday {i+1}", "status": "closed", "exchange": "NYSE"}
            for i in range(10)
        ]

        result = monitor.get_upcoming_events(limit=3)
        assert len(result) == 3

    def test_get_upcoming_events_handles_empty(self):
        monitor = self._make_monitor()
        monitor.client.get_market_holidays.return_value = []

        result = monitor.get_upcoming_events()
        assert result == []

    def test_get_upcoming_events_handles_timeout(self):
        monitor = self._make_monitor()
        monitor.client.get_market_holidays.side_effect = TimeoutError("timeout")

        result = monitor.get_upcoming_events()
        assert result == []

    def test_format_server_time_valid(self):
        readable = PolygonMarketMonitor._format_server_time("2026-02-17T10:30:00-05:00")
        assert "2026" in readable
        assert "Feb" in readable

    def test_format_server_time_empty(self):
        assert PolygonMarketMonitor._format_server_time("") == "N/A"

    def test_format_server_time_invalid(self):
        result = PolygonMarketMonitor._format_server_time("not-a-date")
        assert result == "not-a-date"

    def test_normalize_status(self):
        assert PolygonMarketMonitor._normalize_status("closed") == "Closed"
        assert PolygonMarketMonitor._normalize_status("early-close") == "Early Close"
        assert PolygonMarketMonitor._normalize_status("CLOSED") == "Closed"
        assert PolygonMarketMonitor._normalize_status("something-else") == "Something-Else"


# ---------------------------------------------------------------------------
# Integration tests for FastAPI endpoints
# ---------------------------------------------------------------------------

MOCK_MARKET_STATE = {
    "market": "open",
    "exchanges": {"nyse": "open", "nasdaq": "open", "otc": "closed"},
    "early_hours": False,
    "after_hours": False,
    "server_time": "2026-02-17T10:30:00-05:00",
    "server_time_readable": "Tue Feb 17 2026, 10:30 AM EST",
}

MOCK_HOLIDAYS = [
    {
        "date": "2026-05-25",
        "name": "Memorial Day",
        "status": "Closed",
        "open": None,
        "close": None,
        "exchanges": ["NYSE", "NASDAQ", "OTC"],
    },
    {
        "date": "2026-07-03",
        "name": "Independence Day",
        "status": "Early Close",
        "open": "2026-07-03T09:30:00-04:00",
        "close": "2026-07-03T13:00:00-04:00",
        "exchanges": ["NYSE", "NASDAQ"],
    },
]


@pytest.mark.anyio
async def test_market_status_endpoint(client):
    """GET /api/market/status should return exchange statuses"""
    with patch(
        "app.routers.market_monitor.monitor.get_current_market_state",
        return_value=MOCK_MARKET_STATE,
    ):
        response = await client.get("/api/market/status")

    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["market"] == "open"
    assert data["exchanges"]["nyse"] == "open"
    assert data["exchanges"]["nasdaq"] == "open"


@pytest.mark.anyio
async def test_market_holidays_endpoint(client):
    """GET /api/market/holidays should return upcoming events"""
    with patch(
        "app.routers.market_monitor.monitor.get_upcoming_events",
        return_value=MOCK_HOLIDAYS,
    ):
        response = await client.get("/api/market/holidays")

    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["count"] == 2
    assert data["events"][0]["name"] == "Memorial Day"
    assert data["events"][0]["status"] == "Closed"


@pytest.mark.anyio
async def test_market_holidays_limit_param(client):
    """GET /api/market/holidays?limit=1 should forward limit"""
    with patch(
        "app.routers.market_monitor.monitor.get_upcoming_events",
        return_value=MOCK_HOLIDAYS[:1],
    ) as mock_fn:
        response = await client.get("/api/market/holidays?limit=1")

    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    mock_fn.assert_called_once_with(limit=1)


@pytest.mark.anyio
async def test_market_dashboard_endpoint(client):
    """GET /api/market/dashboard should return combined status + holidays"""
    with patch(
        "app.routers.market_monitor.monitor.get_current_market_state",
        return_value=MOCK_MARKET_STATE,
    ), patch(
        "app.routers.market_monitor.monitor.get_upcoming_events",
        return_value=MOCK_HOLIDAYS,
    ):
        response = await client.get("/api/market/dashboard")

    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["status"]["market"] == "open"
    assert data["holidays"]["count"] == 2


@pytest.mark.anyio
async def test_market_status_handles_error(client):
    """GET /api/market/status should handle monitor errors gracefully"""
    error_state = {
        "market": "unknown",
        "exchanges": {},
        "early_hours": False,
        "after_hours": False,
        "server_time": "",
        "server_time_readable": "N/A",
        "error": "API timeout",
    }
    with patch(
        "app.routers.market_monitor.monitor.get_current_market_state",
        return_value=error_state,
    ):
        response = await client.get("/api/market/status")

    assert response.status_code == 200
    data = response.json()
    assert data["success"] is False
    assert data["error"] == "API timeout"
