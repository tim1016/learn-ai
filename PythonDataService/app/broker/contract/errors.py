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
    the vendor for logs and multi-broker surfaces.
    """

    # Default HTTP status the router surfaces for this error family.
    http_status: ClassVar[int] = 502

    def __init__(
        self,
        message: str,
        *,
        broker: str | None = None,
        detail: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.broker = broker
        self.detail = detail


class UnknownBrokerError(BrokerError):
    """The requested ``{broker}`` path segment has no registered port."""

    http_status: ClassVar[int] = 404


class BrokerAuthError(BrokerError):
    """Vendor rejected our credentials (HTTP 401/403).

    Surfaced as ``502`` — a broker misconfiguration on our side, not a client
    authorization problem.
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
        retry_after_ms: int | None = None,
    ) -> None:
        super().__init__(message, broker=broker, detail=detail)
        self.retry_after_ms = retry_after_ms


class BrokerRequestInvalid(BrokerError):
    """Vendor rejected the request as malformed (HTTP 422). Surfaced as ``400``."""

    http_status: ClassVar[int] = 400


class BrokerOrderRejected(BrokerError):
    """Vendor rejected an order (phase-2 write path). Surfaced as ``409``.

    Declared now so the contract error set is complete; the phase-1 read paths
    never raise it.
    """

    http_status: ClassVar[int] = 409


class BrokerUnavailable(BrokerError):
    """Vendor is unreachable or returned a server error (5xx / network).

    Surfaced as ``503``.
    """

    http_status: ClassVar[int] = 503


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
