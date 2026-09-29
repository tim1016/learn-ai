"""Map alpaca-py / HTTP failures to broker-contract errors (spec §9).

The mapping is asserted by tests:

- 401       → :class:`BrokerAuthError`
- 403, order mutation → :class:`BrokerOrderRejected` (Alpaca refusing this
  order: buying power or shares not sufficient, or its wash-trade protection --
  never our credentials; #2621)
- 403, everywhere else → :class:`BrokerAuthError`
- 429       → :class:`BrokerRateLimited` (carries the Retry-After hint)
- 400 / 422 → :class:`BrokerRequestInvalid`
- 409, order mutation → :class:`BrokerOrderRejected` (definitive order conflict)
- 409, everywhere else → :class:`BrokerUnavailable` (``BrokerOrderRejected`` is a
  write-only error per its own contract docstring; a 409 from a read endpoint
  is unexpected, not a rejected order)
- 5xx       → :class:`BrokerUnreachable` (the transient kind of ``BrokerUnavailable``)
- network / timeout → :class:`BrokerUnreachable` (raised by the client itself)
- unknown   → :class:`BrokerUnavailable` (an answer a retry cannot fix)

Every mapped error keeps Alpaca's numeric body ``code`` in its typed
``code`` field. The code is evidence only: Alpaca documents none for its
wash-trade refusal, and one code may cover several refusals, so nothing
classifies an answer by it.

No alpaca-py exception type crosses the router boundary; only contract errors
do. Alpaca's error message is surfaced (it carries no secret), never our keys.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from alpaca.common.exceptions import APIError

from app.broker.contract.errors import (
    BrokerAuthError,
    BrokerError,
    BrokerOrderRejected,
    BrokerRateLimited,
    BrokerRequestInvalid,
    BrokerUnavailable,
    BrokerUnreachable,
)


def status_of(exc: APIError) -> int | None:
    """Best-effort HTTP status from an APIError (None when unavailable)."""
    status = exc.status_code
    return status if isinstance(status, int) else None


@dataclass(frozen=True)
class _AlpacaErrorBody:
    """Alpaca's error message and numeric code, read once from its answer."""

    message: str
    code: int | None


def _error_body(exc: APIError) -> _AlpacaErrorBody:
    """Parse Alpaca's error body once: its ``message`` and integer ``code``.

    alpaca-py's own ``APIError.message`` and ``.code`` re-parse the raw text
    and raise on a body that is not a JSON object (a proxy's HTML page, an
    empty 5xx). Such a body keeps its raw text as the message and has no code,
    as does a JSON object without them.
    """
    raw = str(exc)
    try:
        parsed = json.loads(raw)
    except ValueError:
        return _AlpacaErrorBody(message=raw, code=None)
    if not isinstance(parsed, dict):
        return _AlpacaErrorBody(message=raw, code=None)
    message = parsed.get("message")
    code = parsed.get("code")
    return _AlpacaErrorBody(
        message=raw if message is None else str(message),
        code=code if isinstance(code, int) and not isinstance(code, bool) else None,
    )


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


def map_api_error(exc: APIError, *, broker: str, is_order_mutation: bool = False) -> BrokerError:
    """Translate an alpaca-py ``APIError`` into a broker-contract error.

    ``is_order_mutation`` scopes the 403 and 409 branches: ``BrokerOrderRejected``
    is a write-only error (its own docstring promises read paths never raise
    it), but ``map_api_error`` is invoked from the single shared ``_call`` every
    read AND write goes through. Only ``submit_order`` / ``cancel_order`` pass
    ``is_order_mutation=True``. There a 403 is Alpaca refusing the order and a
    409 is an order conflict; anywhere else a 403 stays a credentials failure
    and a 409 falls through to the generic ``BrokerUnavailable`` catch-all
    below rather than misreporting a broker-read failure as an order rejection.
    """
    status = status_of(exc)
    body = _error_body(exc)
    message = body.message
    detail = f"HTTP {status}" if status is not None else "no HTTP status"

    if status == 403 and is_order_mutation:
        # Alpaca's documented answer to an order it will not place for this
        # account (buying power or shares not sufficient, wash-trade
        # protection). Definitive -- nothing reached the book -- so the Clerk
        # folds it failed, never uncertain.
        return BrokerOrderRejected(
            f"Alpaca refused the order: {message}",
            broker=broker,
            detail=detail,
            code=body.code,
        )
    if status in (401, 403):
        return BrokerAuthError(
            f"Alpaca rejected our credentials: {message}",
            broker=broker,
            detail=detail,
            code=body.code,
        )
    if status == 429:
        return BrokerRateLimited(
            f"Alpaca rate-limited the request: {message}",
            broker=broker,
            detail=detail,
            code=body.code,
            retry_after_ms=_retry_after_ms(exc),
        )
    if status in (400, 422):
        return BrokerRequestInvalid(
            f"Alpaca rejected the request as invalid: {message}",
            broker=broker,
            detail=detail,
            code=body.code,
        )
    if status == 409 and is_order_mutation:
        # A definitive order conflict (duplicate client_order_id, order-state
        # conflict), NOT a transient outage. Kept distinct from
        # BrokerUnavailable so the Clerk classifies it as a clean SUBMIT_FAILED
        # instead of the S5 uncertain-lookup path.
        return BrokerOrderRejected(
            f"Alpaca rejected the order as a conflict: {message}",
            broker=broker,
            detail=detail,
            code=body.code,
        )
    if status is not None and 500 <= status < 600:
        return BrokerUnreachable(
            f"Alpaca returned a server error: {message}",
            broker=broker,
            detail=detail,
            code=body.code,
        )
    return BrokerUnavailable(
        f"Alpaca request failed: {message}",
        broker=broker,
        detail=detail,
        code=body.code,
    )
