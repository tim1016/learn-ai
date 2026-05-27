"""Self-test for the data-plane → launcher path.

Walks the layers an operator would hit when ``/api/lean-sidecar/*``
fails with ``LauncherUnreachable``: env config, URL parseability,
shared-secret token resolution, and a live HTTP probe of the
launcher's ``/healthz``. Modelled on
:mod:`app.broker.ibkr.diagnostics` so the operator UX is consistent
across the two host-process integrations.

Important non-side-effects:

* Does **not** send a real ``/launch`` request — only ``/healthz``,
  which is unauthenticated on the launcher side. The token-resolution
  check is local-only (env / on-disk file inspection); it does not
  exercise the auth path against the launcher.
* Does not write to disk. Safe to call as often as the UI wants.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from ipaddress import ip_address
from typing import Literal
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.lean_sidecar.launcher_auth import read_launcher_token
from app.lean_sidecar.launcher_client import DEFAULT_LAUNCHER_URL

logger = logging.getLogger(__name__)


# Probe timeout — short. A healthy launcher returns ``/healthz`` in
# single-digit ms; anything past a couple of seconds means the host
# alias resolved to the wrong network or the launcher is hung.
_HEALTHZ_PROBE_TIMEOUT_S = 2.0


LauncherDiagnosticStatus = Literal["pass", "warn", "fail", "skip"]


class LauncherDiagnosticCheck(BaseModel):
    """One step in the launcher self-test.

    Shape intentionally mirrors :class:`app.broker.ibkr.models.DiagnosticCheck`
    so the frontend can render both with the same component. Kept as a
    separate class (rather than importing the broker model) so the
    LEAN-sidecar surface does not depend on the broker package.
    """

    model_config = ConfigDict(frozen=True)

    name: str = Field(..., description="Stable identifier (e.g. 'launcher_healthz').")
    label: str = Field(..., description="Human-readable check name.")
    status: LauncherDiagnosticStatus
    detail: str
    fix: str | None = None


class LauncherDiagnosticReport(BaseModel):
    """Aggregate result of the launcher self-test."""

    model_config = ConfigDict(frozen=True)

    overall_status: Literal["pass", "warn", "fail"]
    checks: list[LauncherDiagnosticCheck]
    fetched_at_ms: int


def _now_ms() -> int:
    return int(datetime.now(tz=UTC).timestamp() * 1000)


def _resolved_launcher_url() -> str:
    """Match ``launcher_client._launcher_url`` exactly so the
    diagnostics probe and the real call hit the same URL."""
    return os.environ.get("LEAN_LAUNCHER_URL", DEFAULT_LAUNCHER_URL).rstrip("/")


def _private_lan_host(url: str) -> str | None:
    """Return the configured private LAN IP hostname, if any.

    Private LAN IPs work only while the host remains on the same
    network/interface and are a recurring source of Podman reachability
    failures. Loopback is intentionally allowed for non-container dev
    where the data plane and launcher share a namespace.
    """
    try:
        hostname = urlparse(url).hostname
    except ValueError:
        return None
    if hostname is None:
        return None
    try:
        parsed = ip_address(hostname)
    except ValueError:
        return None
    if parsed.is_private and not parsed.is_loopback:
        return hostname
    return None


def _check_url_configured(url: str) -> LauncherDiagnosticCheck:
    env_value = os.environ.get("LEAN_LAUNCHER_URL")
    if env_value:
        private_host = _private_lan_host(url)
        if private_host is not None:
            return LauncherDiagnosticCheck(
                name="launcher_url",
                label="LEAN_LAUNCHER_URL",
                status="warn",
                detail=f"LEAN_LAUNCHER_URL={url} (from env; private LAN IP {private_host})",
                fix=(
                    "Use http://host.containers.internal:8090 for Windows/Linux Podman "
                    "instead of pinning a machine-specific LAN IP."
                ),
            )
        return LauncherDiagnosticCheck(
            name="launcher_url",
            label="LEAN_LAUNCHER_URL",
            status="pass",
            detail=f"LEAN_LAUNCHER_URL={url} (from env)",
        )
    return LauncherDiagnosticCheck(
        name="launcher_url",
        label="LEAN_LAUNCHER_URL",
        status="pass",
        detail=f"LEAN_LAUNCHER_URL unset; using built-in default {url}",
    )


def _check_url_parseable(url: str) -> tuple[LauncherDiagnosticCheck, str | None, int | None]:
    """Verify the configured URL parses to a host + port we can probe."""
    try:
        parsed = urlparse(url)
    except ValueError as exc:
        return (
            LauncherDiagnosticCheck(
                name="launcher_url_parseable",
                label="URL parse",
                status="fail",
                detail=f"could not parse LEAN_LAUNCHER_URL={url!r}: {exc}",
                fix=(
                    "Set LEAN_LAUNCHER_URL to a well-formed http URL like "
                    "http://host.containers.internal:8090."
                ),
            ),
            None,
            None,
        )
    if parsed.scheme not in {"http", "https"}:
        return (
            LauncherDiagnosticCheck(
                name="launcher_url_parseable",
                label="URL parse",
                status="fail",
                detail=f"scheme {parsed.scheme!r} is not http(s); url={url!r}",
                fix="Set LEAN_LAUNCHER_URL to an http:// or https:// URL.",
            ),
            None,
            None,
        )
    if not parsed.hostname:
        return (
            LauncherDiagnosticCheck(
                name="launcher_url_parseable",
                label="URL parse",
                status="fail",
                detail=f"no hostname in LEAN_LAUNCHER_URL={url!r}",
                fix=(
                    "Set LEAN_LAUNCHER_URL to a full URL including hostname, "
                    "e.g. http://host.containers.internal:8090."
                ),
            ),
            None,
            None,
        )
    # ``ParseResult.port`` validates lazily — accessing it on a URL
    # whose port component isn't a valid integer (e.g. ``host:abc``)
    # raises ``ValueError``. Catch here so a malformed env value
    # surfaces as a structured fail row instead of a 500 from /diagnose.
    try:
        parsed_port = parsed.port
    except ValueError as exc:
        return (
            LauncherDiagnosticCheck(
                name="launcher_url_parseable",
                label="URL parse",
                status="fail",
                detail=f"invalid port in LEAN_LAUNCHER_URL={url!r}: {exc}",
                fix=(
                    "Set LEAN_LAUNCHER_URL to a numeric port, e.g. "
                    "http://host.containers.internal:8090."
                ),
            ),
            None,
            None,
        )
    port = parsed_port if parsed_port is not None else (443 if parsed.scheme == "https" else 80)
    return (
        LauncherDiagnosticCheck(
            name="launcher_url_parseable",
            label="URL parse",
            status="pass",
            detail=f"host={parsed.hostname} port={port} scheme={parsed.scheme}",
        ),
        parsed.hostname,
        port,
    )


def _check_token_resolved() -> LauncherDiagnosticCheck:
    """Local-only: env or shared bind-mount file. No launcher call.

    The launcher generates the token at startup and writes it to a
    file the data plane reads through a bind mount. If neither env
    nor file has a token, every ``/launch`` request will 401.
    """
    if os.environ.get("LEAN_LAUNCHER_TOKEN"):
        return LauncherDiagnosticCheck(
            name="launcher_token",
            label="X-Launcher-Token",
            status="pass",
            detail="resolved from LEAN_LAUNCHER_TOKEN env",
        )
    token = read_launcher_token()
    if token:
        return LauncherDiagnosticCheck(
            name="launcher_token",
            label="X-Launcher-Token",
            status="pass",
            detail="resolved from launcher token file on shared artifacts mount",
        )
    return LauncherDiagnosticCheck(
        name="launcher_token",
        label="X-Launcher-Token",
        status="fail",
        detail="no token in env and none on the shared artifacts mount",
        fix=(
            "Start the launcher — it generates a token at startup and "
            "writes it to the artifacts root the container also bind-"
            "mounts. Alternatively, set LEAN_LAUNCHER_TOKEN on both "
            "processes to a shared secret."
        ),
    )


async def _check_healthz(url: str) -> LauncherDiagnosticCheck:
    target = f"{url}/healthz"
    try:
        async with httpx.AsyncClient(timeout=_HEALTHZ_PROBE_TIMEOUT_S) as client:
            response = await client.get(target)
    except httpx.ConnectError as exc:
        return LauncherDiagnosticCheck(
            name="launcher_healthz",
            label=f"GET {target}",
            status="fail",
            detail=f"connect failed: {exc}",
            fix=(
                "Confirm the launcher process is running and listening on "
                "the configured port. From the host: "
                "``uvicorn app.lean_sidecar.launcher.app:app --host 0.0.0.0 --port 8090``. "
                "Loopback-only binds (127.0.0.1) are unreachable from the container."
            ),
        )
    except httpx.TimeoutException:
        return LauncherDiagnosticCheck(
            name="launcher_healthz",
            label=f"GET {target}",
            status="fail",
            detail=f"timeout after {_HEALTHZ_PROBE_TIMEOUT_S:.1f}s",
            fix=(
                "The hostname resolved but no TCP response arrived. Check "
                "that host.containers.internal points at the host's bridge "
                "address and that the host firewall allows inbound from "
                "the container bridge."
            ),
        )
    except httpx.HTTPError as exc:
        return LauncherDiagnosticCheck(
            name="launcher_healthz",
            label=f"GET {target}",
            status="fail",
            detail=f"{type(exc).__name__}: {exc}",
        )
    if response.status_code != 200:
        return LauncherDiagnosticCheck(
            name="launcher_healthz",
            label=f"GET {target}",
            status="fail",
            detail=f"unexpected status {response.status_code}: {response.text[:200]}",
            fix=(
                "The launcher is reachable but /healthz did not return 200 — "
                "check the launcher's logs for startup errors."
            ),
        )
    return LauncherDiagnosticCheck(
        name="launcher_healthz",
        label=f"GET {target}",
        status="pass",
        detail=f"200 OK in <{_HEALTHZ_PROBE_TIMEOUT_S:.1f}s; body={response.text[:120]}",
    )


def _aggregate_status(checks: list[LauncherDiagnosticCheck]) -> Literal["pass", "warn", "fail"]:
    if any(c.status == "fail" for c in checks):
        return "fail"
    if any(c.status == "warn" for c in checks):
        return "warn"
    return "pass"


async def run_launcher_diagnostics() -> LauncherDiagnosticReport:
    """Run every check and return a :class:`LauncherDiagnosticReport`.

    Each check captures its own exception so the report always renders.
    Synchronous checks run inline; the only async check is the
    ``/healthz`` probe, bounded by ``_HEALTHZ_PROBE_TIMEOUT_S``.
    """
    url = _resolved_launcher_url()
    checks: list[LauncherDiagnosticCheck] = []
    checks.append(_check_url_configured(url))
    parse_check, host, _port = _check_url_parseable(url)
    checks.append(parse_check)
    checks.append(_check_token_resolved())
    if host is None:
        checks.append(
            LauncherDiagnosticCheck(
                name="launcher_healthz",
                label="GET /healthz",
                status="skip",
                detail="skipped because LEAN_LAUNCHER_URL did not parse to a usable host",
            )
        )
    else:
        checks.append(await _check_healthz(url))
    return LauncherDiagnosticReport(
        overall_status=_aggregate_status(checks),
        checks=checks,
        fetched_at_ms=_now_ms(),
    )


__all__ = [
    "LauncherDiagnosticCheck",
    "LauncherDiagnosticReport",
    "LauncherDiagnosticStatus",
    "run_launcher_diagnostics",
]
