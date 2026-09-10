"""Error handling utilities"""

import logging
import math
from typing import Any

from fastapi import Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


async def request_validation_exception_handler(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    """Return a JSON-safe 422 even when rejected input is NaN/Infinity."""
    del request
    content = jsonable_encoder({"detail": exc.errors()})
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content=_replace_non_finite(content),
    )


def _replace_non_finite(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {key: _replace_non_finite(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_non_finite(item) for item in value]
    return value


async def polygon_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Global exception handler for the application"""
    logger.error(f"Unhandled exception: {exc!s}", exc_info=True)

    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"success": False, "error": str(exc), "detail": "An error occurred while processing your request"},
    )


async def clerk_sqlite_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Answer an unusable Clerk authority in the Clerk's own vocabulary.

    ``ClerkSqliteError`` means the SQLite authority cannot serve this request --
    absent, wrong identity, wrong schema, lease held elsewhere. That is a
    *state of the system*, not an unexpected fault, so it must not fall through
    to the catch-all 500: an operator staring at a post-reset account was being
    shown ``sqlite3.OperationalError: unable to open database file`` on the very
    endpoint that exists to explain the situation. 503 with a typed
    ``reason_code`` says "this authority is not currently usable", which is both
    true and actionable.
    """
    logger.warning(
        "Clerk SQLite authority unusable for this request",
        extra={
            "action": "clerk_authority_unusable",
            "error_type": type(exc).__name__,
            # The detail stays server-side. Some members of this family
            # interpolate the raw driver error and the database path --
            # ``IntegrityCheckFailed(f"{db_path} is corrupt: {exc}")`` -- so
            # returning ``str(exc)`` re-published the very
            # ``sqlite3.OperationalError`` text this handler exists to stop
            # showing, plus a filesystem path (#1865 review).
            "error_detail": str(exc),
        },
    )
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={
            "success": False,
            "detail": {
                "reason_code": "clerk_authority_unusable",
                "error_type": type(exc).__name__,
                "message": (
                    "This account's Clerk authority is not currently usable. "
                    "Check the data plane logs for the specific cause."
                ),
            },
        },
    )


async def catalog_schema_not_ready_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Answer a mid-deploy catalog-schema race with a retryable 503 instead
    of the catch-all 500.

    ``CatalogSchemaNotReadyError`` (app.data_lake.catalog_client) means a
    ``claim_*`` call's ``ON CONFLICT`` target doesn't match the database's
    current indexes -- almost always python-service having started serving
    ``/ensure-data`` traffic before Backend's EF Core migration finished
    applying (compose.yaml health-gates Backend on python-service, not the
    reverse). That is a transient deploy-ordering state, not an unexpected
    fault, so -- same reasoning as ``clerk_sqlite_exception_handler`` above
    -- it must not fall through to the catch-all 500 as an opaque asyncpg
    stack trace.
    """
    logger.warning(
        "Catalog schema not ready for this request",
        extra={
            "action": "catalog_schema_not_ready",
            "error_type": type(exc).__name__,
            "error_detail": str(exc),
        },
    )
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={
            "success": False,
            "detail": {
                "reason_code": "catalog_schema_not_ready",
                "error_type": type(exc).__name__,
                "message": (
                    "The data lake catalog's database schema is not yet ready for this "
                    "request -- likely a migration still applying during a deploy. Retry "
                    "after a short delay."
                ),
            },
        },
    )


async def broker_unbound_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Answer a worker with no broker binding in the configuration's vocabulary.

    ``BrokerUnbound`` means this process resolved no effective broker profile —
    nothing applied, an unreadable profiles database, a revision that will not
    load, or credentials reaching an account the revision is not approved for.
    Every one of those is a *state the contract has words for* (§6), and the
    whole point of the refusal vocabulary is that an operator sees which one.

    Without this it fell through to the catch-all 500, so precisely when the
    Broker Desk should have said "the gate is closed, here is what to do", it
    showed a generic fault instead — and worse than before the migration, where
    a missing credential produced a typed contract error.
    """
    from app.broker.alpaca.active_binding import BrokerUnbound

    if not isinstance(exc, BrokerUnbound):  # pragma: no cover - registration is exact
        raise exc
    logger.warning(
        "Broker request refused: no broker binding is installed",
        extra={"action": "broker_unbound_request", "reason_code": exc.reason},
    )
    return JSONResponse(status_code=exc.http_status, content={"detail": exc.as_detail()})


async def broker_profile_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Answer a credential/verification refusal in the configuration's vocabulary.

    ``BrokerProfileError`` is the second refusal family on the configuration
    surface: same contract §6 ``reason`` strings as ``BrokerConfigurationError``
    but a separate base class, raised by the account ceremonies behind
    ``/profiles/{id}/revisions/{n}/verify-account`` and ``/account-pin`` when a
    slot is unavailable, a revision will not resolve, or the observed account
    contradicts the revision's mode.

    Without this it reached the catch-all ``Exception`` handler, so the two
    routes that exist to say *which* credential is missing answered with a
    generic 500 — the same gap ``broker_unbound_exception_handler`` above
    closed for its own family.
    """
    from app.broker.alpaca.profile import BrokerProfileError

    if not isinstance(exc, BrokerProfileError):  # pragma: no cover - registration is exact
        raise exc
    logger.warning(
        "Broker configuration ceremony refused",
        extra={"action": "broker_profile_refusal", "reason_code": exc.reason},
    )
    return JSONResponse(status_code=exc.http_status, content={"detail": exc.as_detail()})
