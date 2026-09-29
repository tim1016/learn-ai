"""Broker-neutral error taxonomy (Broker System v2, Layer 3).

Vendor layers translate their SDK/HTTP failures into these contract errors
(see ``app/broker/alpaca/errors.py``); the router translates contract errors
into HTTP responses carrying a *what / why* detail per the error-authoring
standard. No vendor exception type crosses the router boundary.

Each error declares the ``http_status`` the router should surface. The values
describe the failure honestly from the caller's perspective: an upstream auth
failure is *our* misconfiguration (``502``), not the caller's, so it is never
reported as ``401``.
"""

from __future__ import annotations

from typing import ClassVar


class BrokerError(Exception):
    """Base class for every broker-contract error.

    ``message`` is a caller-facing *what* (neutral, specific, no blame).
    ``detail`` is an optional *why* the router may append. ``broker`` names
    the vendor for logs and multi-broker surfaces. ``code`` is the vendor's
    own numeric error code when its answer carried one (Alpaca's body
    ``code``), kept as evidence; ``None`` when it gave none.
    """

    # Default HTTP status the router surfaces for this error family.
    http_status: ClassVar[int] = 502

    def __init__(
        self,
        message: str,
        *,
        broker: str | None = None,
        detail: str | None = None,
        code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.broker = broker
        self.detail = detail
        self.code = code


class UnknownBrokerError(BrokerError):
    """The requested ``{broker}`` path segment has no registered port."""

    http_status: ClassVar[int] = 404


class BrokerAuthError(BrokerError):
    """Vendor rejected our credentials (HTTP 401, or a 403 on anything but an order submission).

    Surfaced as ``502`` — a broker misconfiguration on our side, not a client
    authorization problem. A 403 on an order submission is the vendor refusing
    that order, which is :class:`BrokerOrderNotPermitted`.
    """

    http_status: ClassVar[int] = 502


class BrokerRateLimited(BrokerError):
    """Vendor throttled us (HTTP 429).

    Carries ``retry_after_ms`` when the vendor supplied a Retry-After hint so
    the router can echo it. Surfaced as ``503``.
    """

    http_status: ClassVar[int] = 503

    def __init__(
        self,
        message: str,
        *,
        broker: str | None = None,
        detail: str | None = None,
        code: int | None = None,
        retry_after_ms: int | None = None,
    ) -> None:
        super().__init__(message, broker=broker, detail=detail, code=code)
        self.retry_after_ms = retry_after_ms


class BrokerRequestInvalid(BrokerError):
    """Vendor rejected the request as malformed (HTTP 422). Surfaced as ``400``."""

    http_status: ClassVar[int] = 400


class BrokerOrderRejected(BrokerError):
    """Vendor definitively refused an order mutation. Surfaced as ``409``.

    Raised only on the write path (submit / cancel), for an order conflict
    (a duplicate client order id, an order-state conflict). The vendor
    answered, so it is never the uncertain :class:`BrokerUnavailable`. Read
    paths never raise it.
    """

    http_status: ClassVar[int] = 409


class BrokerOrderNotPermitted(BrokerOrderRejected):
    """Vendor will not place this new order for this account.

    Raised only on order submission, never on a cancel or a read: Alpaca's 403
    on ``POST /v2/orders`` -- buying power or shares not sufficient, or its
    wash-trade protection refusing an order that could trade against an
    opposite-side order open on the same symbol in the account. The new order
    never reached the book.
    """


class BrokerUnavailable(BrokerError):
    """Vendor is unreachable or returned a server error (5xx / network).

    Surfaced as ``503``. Also the vendor mapping's catch-all for an answer no
    other error names (an unexpected 404 or 409 on a read), which a retry
    cannot fix; :class:`BrokerUnreachable` is the kind that can pass.
    """

    http_status: ClassVar[int] = 503


class BrokerUnreachable(BrokerUnavailable):
    """Vendor did not answer: a network failure, a timeout, or its own server error (5xx).

    The transient kind of ``BrokerUnavailable`` -- the same request can
    succeed once the vendor answers again -- so a caller that retries on its
    own may retry exactly this, never the catch-all above (#2582).
    """


class BrokerEvidenceUnavailable(BrokerUnavailable):
    """Broker answered, but its evidence cannot support a safety verdict.

    This remains a 503 at the HTTP boundary while allowing safety consumers to
    distinguish malformed or contradictory evidence from a transient outage.
    """


class BrokerSubmissionHeld(BrokerError):
    """New submission is refused by the account-level exposure hold (phase-2 S6).

    Raised by the Clerk when a submit is attempted while an unexplained-order
    hold is active — a safety posture, not a vendor rejection. Surfaced as
    ``409`` with the ``reason_code`` the router echoes so the UI can flag it and
    offer the operator the clear-hold exit. Cancels are never held (reducing
    exposure is always allowed), so this only guards ``submit``.
    """

    http_status: ClassVar[int] = 409

    def __init__(
        self,
        message: str,
        *,
        reason_code: str,
        broker: str | None = None,
        detail: str | None = None,
    ) -> None:
        super().__init__(message, broker=broker, detail=detail)
        self.reason_code = reason_code


class BrokerAccountModeDisagreement(BrokerError):
    """The configured mode and the broker-observed account disagree (ADR 0059 D1).

    Raised at the ingestion boundary when an account-number shape contradicts
    the mode that selected the endpoint. Shape never *grants* a mode; it only
    refuses one. Surfaced as ``409`` with ``reason_code`` so the desk can name
    the disagreement rather than a generic broker error.
    """

    http_status: ClassVar[int] = 409
    reason_code: ClassVar[str] = "LIVE_MODE_DISAGREEMENT"
