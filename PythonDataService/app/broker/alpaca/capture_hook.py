"""Verbatim raw-capture hook for the alpaca-py session (spec §6, D4).

alpaca-py drives HTTP through a ``requests.Session``. We append a ``response``
hook to that session so every response — success *or* error — is journaled
verbatim (the exact bytes on the wire) **before** the SDK parses or raises.
The endpoint family is derived from the URL path.

Safety:

- The hook reads only the response body and the request's URL/method/query. It
  never touches auth headers, so no key material can reach the journal; the
  journal redacts secret-like query keys as a second line of defence.
- The hook never raises into the SDK: a capture failure is logged and the
  response is passed through untouched (phase-1 read-path failure policy).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from urllib.parse import parse_qs, urlsplit

from requests import Response
from requests.sessions import Session

from app.broker.capture.journal import CaptureEndpoint, CaptureJournal

logger = logging.getLogger(__name__)


def _family_for_path(path: str) -> CaptureEndpoint | None:
    """Map an Alpaca URL path to its capture endpoint family, or ``None``."""
    normalized = path.rstrip("/")
    # Activities is nested under /account, so it must be checked first.
    if "/account/activities" in normalized:
        return CaptureEndpoint.ACTIVITIES
    if normalized.endswith("/account"):
        return CaptureEndpoint.ACCOUNT
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


def _make_hook(journal: CaptureJournal, broker: str) -> Callable[..., Response]:
    def _hook(response: Response, *args: object, **kwargs: object) -> Response:
        try:
            family = _family_for_path(urlsplit(response.url).path)
            if family is not None:
                request = response.request
                journal.record(
                    broker=broker,
                    endpoint=family,
                    method=request.method or "GET",
                    params=_query_params(response.url),
                    status=response.status_code,
                    raw_body=response.content,
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
) -> Callable[..., Response]:
    """Append the verbatim-capture response hook to ``session``.

    The journal records response bytes exactly as delivered to ``requests``.
    Request identity encoding so those bytes are not transparently decompressed
    before the hook sees them. Returns the installed hook (useful for tests).
    Idempotent per session is the caller's responsibility — install once per
    client.
    """
    session.headers["Accept-Encoding"] = "identity"
    session.hooks.setdefault("response", [])
    hook = _make_hook(journal, broker)
    session.hooks["response"].append(hook)
    return hook
