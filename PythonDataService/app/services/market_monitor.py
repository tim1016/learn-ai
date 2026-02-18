"""Market status and holiday monitoring using Polygon.io Reference Data APIs"""
from polygon import RESTClient
from polygon.exceptions import AuthError, BadResponse
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone
import logging

logger = logging.getLogger(__name__)


class PolygonMarketMonitor:
    """Monitors exchange status and upcoming market holidays via Polygon.io.

    Wraps the Polygon Reference Data endpoints:
    - GET /v1/marketstatus/now    (current exchange statuses)
    - GET /v1/marketstatus/upcoming (upcoming holidays / early closes)
    """

    def __init__(self, polygon_api_key: str):
        self.client = RESTClient(api_key=polygon_api_key)

    # ------------------------------------------------------------------
    # Current market state
    # ------------------------------------------------------------------
    def get_current_market_state(self) -> Dict[str, Any]:
        """Return the live status of NYSE, NASDAQ, and the overall market.

        Returns:
            {
                "market": "open" | "closed" | "extended-hours",
                "exchanges": {
                    "nyse":   "open" | "closed" | "extended-hours",
                    "nasdaq": "open" | "closed" | "extended-hours",
                    "otc":    "open" | "closed" | "extended-hours"
                },
                "early_hours": bool,
                "after_hours": bool,
                "server_time": "2026-02-17T12:00:00-05:00",
                "server_time_readable": "Mon Feb 17 2026, 12:00 PM EST"
            }
        """
        try:
            logger.info("[MarketMonitor] Fetching current market status")
            raw = self.client.get_market_status()

            server_time_str = raw.get("serverTime", "")
            readable = self._format_server_time(server_time_str)

            result = {
                "market": raw.get("market", "unknown"),
                "exchanges": raw.get("exchanges", {}),
                "early_hours": raw.get("earlyHours", False),
                "after_hours": raw.get("afterHours", False),
                "server_time": server_time_str,
                "server_time_readable": readable,
            }
            logger.info(f"[MarketMonitor] Market={result['market']}, "
                        f"NYSE={result['exchanges'].get('nyse')}, "
                        f"NASDAQ={result['exchanges'].get('nasdaq')}")
            return result

        except AuthError:
            logger.error("[MarketMonitor] Invalid Polygon API key")
            return self._error_response("Invalid API key — check your POLYGON_API_KEY")
        except (BadResponse, TimeoutError, ConnectionError) as e:
            logger.error(f"[MarketMonitor] API error fetching market status: {e}")
            return self._error_response(f"Polygon API error: {e}")
        except Exception as e:
            logger.error(f"[MarketMonitor] Unexpected error: {e}", exc_info=True)
            return self._error_response(f"Unexpected error: {e}")

    # ------------------------------------------------------------------
    # Upcoming holidays / events
    # ------------------------------------------------------------------
    def get_upcoming_events(self, limit: int = 5) -> List[Dict[str, Any]]:
        """Return the next *limit* unique market holiday events.

        Polygon returns one entry per exchange per holiday (NYSE, NASDAQ, OTC).
        This method de-duplicates by (date, name) so each event appears once,
        keeping the most restrictive status (closed > early-close).

        Returns a list of dicts:
            {
                "date": "2026-05-25",
                "name": "Memorial Day",
                "status": "closed",
                "open": null,
                "close": null,
                "exchanges": ["NYSE", "NASDAQ", "OTC"]
            }
        """
        try:
            logger.info(f"[MarketMonitor] Fetching upcoming holidays (limit={limit})")
            raw_holidays = self.client.get_market_holidays()

            if not isinstance(raw_holidays, list):
                logger.warning("[MarketMonitor] Unexpected holidays response type")
                return []

            # De-duplicate across exchanges
            events_map: Dict[str, Dict[str, Any]] = {}
            for h in raw_holidays:
                key = f"{h.get('date')}|{h.get('name')}"
                exchange = h.get("exchange", "")

                if key not in events_map:
                    events_map[key] = {
                        "date": h.get("date"),
                        "name": h.get("name"),
                        "status": self._normalize_status(h.get("status", "")),
                        "open": h.get("open"),
                        "close": h.get("close"),
                        "exchanges": [exchange],
                    }
                else:
                    events_map[key]["exchanges"].append(exchange)
                    # Prefer "closed" over "early-close"
                    if h.get("status", "").lower() == "closed":
                        events_map[key]["status"] = "Closed"
                        events_map[key]["open"] = None
                        events_map[key]["close"] = None

            # Sort by date, take first `limit`
            sorted_events = sorted(events_map.values(), key=lambda e: e["date"] or "")
            result = sorted_events[:limit]

            logger.info(f"[MarketMonitor] Returning {len(result)} upcoming events")
            return result

        except AuthError:
            logger.error("[MarketMonitor] Invalid Polygon API key")
            return []
        except (BadResponse, TimeoutError, ConnectionError) as e:
            logger.error(f"[MarketMonitor] API error fetching holidays: {e}")
            return []
        except Exception as e:
            logger.error(f"[MarketMonitor] Unexpected error: {e}", exc_info=True)
            return []

    # ------------------------------------------------------------------
    # Console dashboard (standalone usage)
    # ------------------------------------------------------------------
    def display_dashboard(self) -> None:
        """Print a formatted market status dashboard to the console."""
        status = self.get_current_market_state()
        events = self.get_upcoming_events()

        width = 60
        print("=" * width)
        print("  MARKET STATUS DASHBOARD".center(width))
        print("=" * width)

        if "error" in status:
            print(f"  Error: {status['error']}")
        else:
            print(f"  Server Time : {status['server_time_readable']}")
            print(f"  Overall     : {status['market'].upper()}")
            print("-" * width)
            exchanges = status.get("exchanges", {})
            for name in ("nyse", "nasdaq", "otc"):
                label = name.upper().ljust(8)
                val = exchanges.get(name, "unknown").upper()
                print(f"  {label} : {val}")
            print(f"  Pre-Market  : {'Yes' if status['early_hours'] else 'No'}")
            print(f"  After-Hours : {'Yes' if status['after_hours'] else 'No'}")

        print()
        print("-" * width)
        print("  UPCOMING MARKET HOLIDAYS".center(width))
        print("-" * width)

        if not events:
            print("  No upcoming events available.")
        else:
            header = f"  {'Date':<14} {'Event':<24} {'Status':<12}"
            print(header)
            print("  " + "-" * (width - 4))
            for ev in events:
                date_str = ev["date"] or "N/A"
                name = (ev["name"] or "Unknown")[:23]
                ev_status = ev["status"] or "N/A"
                hours = ""
                if ev.get("open") and ev.get("close"):
                    hours = f" ({ev['open']} - {ev['close']})"
                print(f"  {date_str:<14} {name:<24} {ev_status}{hours}")

        print("=" * width)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _format_server_time(iso_str: str) -> str:
        """Convert RFC-3339 server time to a human-readable string."""
        if not iso_str:
            return "N/A"
        try:
            dt = datetime.fromisoformat(iso_str)
            return dt.strftime("%a %b %d %Y, %I:%M %p %Z").strip()
        except (ValueError, TypeError):
            return iso_str

    @staticmethod
    def _normalize_status(raw: str) -> str:
        """Capitalise status labels consistently."""
        mapping = {
            "closed": "Closed",
            "early-close": "Early Close",
        }
        return mapping.get(raw.lower(), raw.title())

    @staticmethod
    def _error_response(message: str) -> Dict[str, Any]:
        return {
            "market": "unknown",
            "exchanges": {},
            "early_hours": False,
            "after_hours": False,
            "server_time": "",
            "server_time_readable": "N/A",
            "error": message,
        }
