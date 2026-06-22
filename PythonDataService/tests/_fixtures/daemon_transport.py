"""Test helpers for the typed daemon transport (PRD #619-C).

Most caller-site tests mock ``host_daemon_client.fetch_*`` functions that now
return ``tuple[DaemonResult, dict | None]``. These helpers spell that shape
out without forcing every test fixture to know the ``DaemonResult`` API
verbatim.
"""

from __future__ import annotations

import httpx

from app.engine.live.daemon_transport import DaemonResult


def as_typed_get(payload: dict | None) -> tuple[DaemonResult, dict | None]:
    """Convenience for test mocks of typed GETs.

    Returns ``(CONNECTED, payload)`` when ``payload`` is a dict, mirroring the
    happy path; otherwise returns ``(UNREACHABLE, None)`` shaped as a
    connection-refused failure so the test exercises the fail-closed branch
    that callers used to hit on ``daemon is None``.
    """
    if payload is None:
        return (
            DaemonResult.from_httpx_exception(httpx.ConnectError("test:unreachable")),
            None,
        )
    return DaemonResult.connected(), payload
