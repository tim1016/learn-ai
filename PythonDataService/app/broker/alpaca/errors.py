"""Map alpaca-py / HTTP failures to broker-contract errors (spec §9).

The mapping is asserted by tests:

- 401 / 403 → :class:`BrokerAuthError`
- 429       → :class:`BrokerRateLimited` (carries the Retry-After hint)
- 400 / 422 → :class:`BrokerRequestInvalid`
- 5xx       → :class:`BrokerUnavailable`
- network / unknown → :class:`BrokerUnavailable`

No alpaca-py exception type crosses the router boundary; only contract errors
do. Alpaca's error message is surfaced (it carries no secret), never our keys.
"""

from __future__ import annotations

import json

from alpaca.common.exceptions import APIError

from app.broker.contract.errors import (
    BrokerAuthError,
    BrokerError,
    BrokerRateLimited,
    BrokerRequestInvalid,
    BrokerUnavailable,
)


def status_of(exc: APIError) -> int | None:
    """Best-effort HTTP status from an APIError (None when unavailable)."""
    status = exc.status_code
    return status if isinstance(status, int) else None


def _message_of(exc: APIError) -> str:
    """Alpaca's error message, falling back to the raw error text."""
    try:
        return str(exc.message)
    except Exception:
        return str(exc)


def _retry_after_ms(exc: APIError) -> int | None:
    """Parse a Retry-After header (seconds) into ms, when present and numeric."""
    response = getattr(exc, "response", None)
    header = getattr(response, "headers", {}) if response is not None else {}
    raw = header.get("Retry-After") if hasattr(header, "get") else None
    if raw is None:
        return None
    try:
        return int(float(raw) * 1000)
    except (TypeError, ValueError):
        return None


def map_api_error(exc: APIError, *, broker: str) -> BrokerError:
    """Translate an alpaca-py ``APIError`` into a broker-contract error."""
    status = status_of(exc)
    message = _message_of(exc)
    detail = f"HTTP {status}" if status is not None else "no HTTP status"

    if status in (401, 403):
        return BrokerAuthError(
            f"Alpaca rejected our credentials: {message}",
            broker=broker,
            detail=detail,
        )
    if status == 429:
        return BrokerRateLimited(
            f"Alpaca rate-limited the request: {message}",
            broker=broker,
            detail=detail,
            retry_after_ms=_retry_after_ms(exc),
        )
    if status in (400, 422):
        return BrokerRequestInvalid(
            f"Alpaca rejected the request as invalid: {message}",
            broker=broker,
            detail=detail,
        )
    if status is not None and 500 <= status < 600:
        return BrokerUnavailable(
            f"Alpaca returned a server error: {message}",
            broker=broker,
            detail=detail,
        )
    return BrokerUnavailable(
        f"Alpaca request failed: {message}",
        broker=broker,
        detail=detail,
    )


def parse_alpaca_error_body(raw: str) -> dict:
    """Parse an Alpaca error body to a dict; ``{}`` when it is not JSON."""
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}
