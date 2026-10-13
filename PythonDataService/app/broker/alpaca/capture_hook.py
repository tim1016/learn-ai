"""Verbatim raw-capture hook for the alpaca-py session (ADR 0032 D4).

alpaca-py drives HTTP through a ``requests.Session``. We append a ``response``
hook to that session so every response — success *or* error — is journaled
verbatim (the exact bytes on the wire) **before** the SDK parses or raises.
The endpoint family is derived from the URL path.

Each line also carries when the request began and when the answer had been
read whole, and the body sent when the request is an option order's submit
(ADR 0032, amendment 2).

Safety:

- The hook reads only the response body and the request's URL/method/query,
  and the body of an option order's submit. It never touches auth headers, so
  no key material can reach the journal; the journal redacts secret-like keys
  of a query and of a body as a second line of defence.
- The hook never raises into the SDK: a capture failure is logged and the
  response is passed through untouched (phase-1 read-path failure policy).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from time import monotonic
from urllib.parse import parse_qs, urlsplit

from requests import PreparedRequest, Response
from requests.sessions import Session

from app.broker.capture.journal import CaptureEndpoint, CaptureJournal
from app.options.contract import OptionContract
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

# Alpaca's order class for an order of several legs; only options have one.
_MULTI_LEG_ORDER_CLASS = "mleg"


def _family_for_path(path: str) -> CaptureEndpoint | None:
    """Map an Alpaca URL path to its capture endpoint family, or ``None``."""
    normalized = path.rstrip("/")
    # Nested account routes must be checked before the general account route.
    if "/account/portfolio/history" in normalized:
        return CaptureEndpoint.PORTFOLIO_HISTORY
    if "/account/activities" in normalized:
        return CaptureEndpoint.ACTIVITIES
    if normalized.endswith("/account"):
        return CaptureEndpoint.ACCOUNT
    if "/options/contracts" in normalized:
        return CaptureEndpoint.OPTION_CONTRACTS
    if "/positions" in normalized:
        return CaptureEndpoint.POSITIONS
    if "/orders" in normalized:
        return CaptureEndpoint.ORDERS
    if "/assets" in normalized:
        return CaptureEndpoint.ASSETS
    if normalized.endswith("/clock") or normalized.endswith("/calendar"):
        return CaptureEndpoint.CLOCK
    return None


def _query_params(url: str) -> dict[str, object]:
    """Query parameters only — never headers, never key material."""
    query = urlsplit(url).query
    if not query:
        return {}
    return {
        key: (values[0] if len(values) == 1 else values)
        for key, values in parse_qs(query).items()
    }


def _is_option_order(body: object) -> bool:
    """Whether a submitted order body is an option order's: of several legs, or on a contract's own symbol."""
    if not isinstance(body, dict):
        return False
    if body.get("order_class") == _MULTI_LEG_ORDER_CLASS:
        return True
    symbol = body.get("symbol")
    if not isinstance(symbol, str):
        return False
    try:
        OptionContract.from_occ_symbol(symbol)
    except ValueError:
        return False
    return True


def _option_order_body(request: PreparedRequest | None, path: str) -> bytes | None:
    """The body of an option order's submit as it was sent, or ``None`` for any other request.

    Only a POST to the orders path with a JSON body of an option order. A
    stock order's body, a cancel and every read are left out: the journal
    keeps a request's body for the one case where the answer alone does not
    show the order's own terms. Whether the body may be kept is the journal's
    to say, and it keeps only UTF-8 JSON: ``json.loads`` here also reads other
    encodings, which the journal then drops.
    """
    if request is None or (request.method or "").upper() != "POST" or not path.rstrip("/").endswith("/orders"):
        return None
    body = request.body
    if isinstance(body, str):
        body = body.encode("utf-8")
    if not isinstance(body, bytes):
        return None
    try:
        parsed = json.loads(body)
    except ValueError:
        # Not a body this hook can tell an order from; the answer's own line is journaled all the same.
        return None
    return body if _is_option_order(parsed) else None


def _make_hook(journal: CaptureJournal, broker: str, clock: Callable[[], int]) -> Callable[..., Response]:
    def _hook(response: Response, *args: object, **kwargs: object) -> Response:
        try:
            path = urlsplit(response.url).path
            family = _family_for_path(path)
            if family is not None:
                request = response.request
                # ``requests`` ran this hook when the answer's headers arrived, ``elapsed`` after the request
                # was sent. Reading the body is where the answer ends. One wall-clock reading and monotonic
                # durations from it, so the two times are in order whatever the wall clock does.
                arrived_ms = clock()
                read_from = monotonic()
                raw_body = response.content
                read_ms = round((monotonic() - read_from) * 1000)
                waited_ms = round(response.elapsed.total_seconds() * 1000)
                journal.record(
                    broker=broker,
                    endpoint=family,
                    method=request.method or "GET",
                    params=_query_params(response.url),
                    status=response.status_code,
                    raw_body=raw_body,
                    began_at_ms=arrived_ms - waited_ms,
                    ended_at_ms=arrived_ms + read_ms,
                    request_body=_option_order_body(request, path),
                )
        except Exception:  # capture is best-effort; never break the response.
            logger.error("alpaca capture hook failed", exc_info=True)
        return response

    return _hook


def install_capture_hook(
    session: Session,
    journal: CaptureJournal,
    *,
    broker: str = "alpaca",
    clock: Callable[[], int] = now_ms_utc,
) -> Callable[..., Response]:
    """Append the verbatim-capture response hook to ``session``.

    The journal records response bytes exactly as delivered to ``requests``.
    Request identity encoding so those bytes are not transparently decompressed
    before the hook sees them. Returns the installed hook (useful for tests).
    Idempotent per session is the caller's responsibility — install once per
    client. ``clock`` is the wall clock the exchange's two times are read from.
    """
    session.headers["Accept-Encoding"] = "identity"
    session.hooks.setdefault("response", [])
    hook = _make_hook(journal, broker, clock)
    session.hooks["response"].append(hook)
    return hook
