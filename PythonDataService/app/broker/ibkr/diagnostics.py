"""Broker self-test for the ``GET /api/broker/diagnose`` endpoint.

Walks the layers an operator would hit when the connection is broken —
settings, host resolution, TCP reachability, client lifecycle, the DU
sentinel — and reports each as a structured :class:`DiagnosticCheck`.
The endpoint never raises; if a check itself fails to run, that becomes
its own ``fail`` row with the exception message in ``detail``.

Important non-side-effects:

* Does **not** call ``IbkrClient.connect``. If the connection is down,
  reconnecting is the operator's job — we report what we observe.
* Does not write to disk and does not place orders; the account-fetch
  check is read-only.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
from datetime import UTC, datetime

from app.broker.ibkr import account as ibkr_account
from app.broker.ibkr.client import (
    BrokerError,
    NotConnectedError,
    _resolve_host,
    get_client,
)
from app.broker.ibkr.config import LIVE_PORTS, PAPER_PORTS, get_settings
from app.broker.ibkr.models import DiagnosticCheck, DiagnosticReport

logger = logging.getLogger(__name__)


TCP_PROBE_TIMEOUT_S = 2.0


def _now_ms() -> int:
    return int(datetime.now(tz=UTC).timestamp() * 1000)


def _check_settings_mode() -> DiagnosticCheck:
    s = get_settings()
    if s.mode in {"paper", "live"}:
        return DiagnosticCheck(
            name="settings_mode",
            label="IBKR_MODE",
            status="pass",
            detail=f"IBKR_MODE={s.mode}",
        )
    return DiagnosticCheck(
        name="settings_mode",
        label="IBKR_MODE",
        status="fail",
        detail=f"unexpected mode: {s.mode!r}",
        fix="Set IBKR_MODE=paper (or IBKR_MODE=live with explicit operator approval) in .env.",
    )


def _check_settings_port() -> DiagnosticCheck:
    s = get_settings()
    if s.mode == "paper" and s.port in PAPER_PORTS:
        return DiagnosticCheck(
            name="settings_port",
            label="IBKR_PORT",
            status="pass",
            detail=f"port {s.port} is a paper port (paper ports = {sorted(PAPER_PORTS)})",
        )
    if s.mode == "live" and s.port in LIVE_PORTS:
        return DiagnosticCheck(
            name="settings_port",
            label="IBKR_PORT",
            status="pass",
            detail=f"port {s.port} is a live port (live ports = {sorted(LIVE_PORTS)})",
        )
    return DiagnosticCheck(
        name="settings_port",
        label="IBKR_PORT",
        status="fail",
        detail=f"port {s.port} disagrees with mode={s.mode!r}",
        fix=(
            "Set IBKR_PORT=4002 for paper Gateway / 7497 for paper TWS, "
            "or IBKR_PORT=4001 / 7496 for live."
        ),
    )


def _check_host_resolution() -> tuple[DiagnosticCheck, str]:
    s = get_settings()
    resolved = _resolve_host(s.host)
    if not resolved:
        return (
            DiagnosticCheck(
                name="host_resolution",
                label="IBKR_HOST",
                status="fail",
                detail=f"resolved IBKR_HOST={s.host!r} to empty string",
                fix=(
                    "Set IBKR_HOST to a literal IP (e.g. the Windows host's IP "
                    "from ipconfig) or 'auto' inside containers with a default gateway."
                ),
            ),
            resolved,
        )
    if s.host == "auto" and resolved == "auto":
        return (
            DiagnosticCheck(
                name="host_resolution",
                label="IBKR_HOST",
                status="warn",
                detail="IBKR_HOST=auto but the container's default gateway could not be detected; using literal 'auto' will fail at the wire.",
                fix=(
                    "Set IBKR_HOST to the Windows host IP explicitly. From PowerShell: "
                    "Get-NetIPAddress -AddressFamily IPv4 | "
                    "Where-Object { $_.InterfaceAlias -match 'WSL|vEthernet' }."
                ),
            ),
            resolved,
        )
    if s.host == "auto":
        return (
            DiagnosticCheck(
                name="host_resolution",
                label="IBKR_HOST",
                status="pass",
                detail=f"IBKR_HOST=auto resolved to default gateway {resolved}",
            ),
            resolved,
        )
    return (
        DiagnosticCheck(
            name="host_resolution",
            label="IBKR_HOST",
            status="pass",
            detail=f"IBKR_HOST={resolved} (literal)",
        ),
        resolved,
    )


async def _check_tcp_reachable(host: str, port: int) -> DiagnosticCheck:
    if not host or host == "auto":
        return DiagnosticCheck(
            name="tcp_reachable",
            label=f"TCP {host}:{port}",
            status="skip",
            detail="skipped because host did not resolve to a usable address",
        )
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=TCP_PROBE_TIMEOUT_S,
        )
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        return DiagnosticCheck(
            name="tcp_reachable",
            label=f"TCP {host}:{port}",
            status="pass",
            detail=f"connected to {host}:{port} within {TCP_PROBE_TIMEOUT_S:.0f}s",
        )
    except TimeoutError:
        return DiagnosticCheck(
            name="tcp_reachable",
            label=f"TCP {host}:{port}",
            status="fail",
            detail=f"timeout after {TCP_PROBE_TIMEOUT_S:.0f}s connecting to {host}:{port}",
            fix=(
                "Check that IB Gateway / TWS is running on the host and listening "
                "on the configured port, that the Windows firewall allows inbound "
                "from the WSL/Podman bridge, and that Gateway's API tab has the "
                "container IP under 'Trusted IPs'."
            ),
        )
    except OSError as exc:
        if isinstance(exc, ConnectionRefusedError) or exc.errno in {111, 10061}:
            return DiagnosticCheck(
                name="tcp_reachable",
                label=f"TCP {host}:{port}",
                status="fail",
                detail=f"connection refused at {host}:{port}: {exc}",
                fix=(
                    "Gateway is not listening on this port, or it is rejecting the "
                    "client. Confirm Gateway is logged in to the paper account, the "
                    "API > Settings socket port matches IBKR_PORT, and 'Read-Only API' "
                    "is OFF."
                ),
            )
        if isinstance(exc, socket.gaierror):
            return DiagnosticCheck(
                name="tcp_reachable",
                label=f"TCP {host}:{port}",
                status="fail",
                detail=f"DNS resolution failed for {host!r}: {exc}",
                fix="Set IBKR_HOST to a literal IP or a name resolvable from the container.",
            )
        return DiagnosticCheck(
            name="tcp_reachable",
            label=f"TCP {host}:{port}",
            status="fail",
            detail=f"socket error connecting to {host}:{port}: {exc}",
        )


def _check_client_initialized() -> DiagnosticCheck:
    try:
        get_client()
    except NotConnectedError as exc:
        return DiagnosticCheck(
            name="client_initialized",
            label="IbkrClient lifespan",
            status="fail",
            detail=str(exc),
            fix=(
                "Restart the polygon-data-service container. The FastAPI lifespan "
                "event constructs the IbkrClient at startup; if it failed, the "
                "container logs (podman logs polygon-data-service) carry the reason."
            ),
        )
    return DiagnosticCheck(
        name="client_initialized",
        label="IbkrClient lifespan",
        status="pass",
        detail="process-wide IbkrClient is constructed",
    )


def _check_client_connected() -> DiagnosticCheck:
    try:
        client = get_client()
    except NotConnectedError:
        return DiagnosticCheck(
            name="client_connected",
            label="ib_async session",
            status="skip",
            detail="skipped because the IbkrClient is not initialized",
        )
    if client.is_connected():
        return DiagnosticCheck(
            name="client_connected",
            label="ib_async session",
            status="pass",
            detail="ib_async reports an open session",
        )
    return DiagnosticCheck(
        name="client_connected",
        label="ib_async session",
        status="fail",
        detail="ib_async reports the session is closed",
        fix=(
            "Either Gateway dropped the client or the initial connect failed. "
            "Check the container logs for 'IBKR connect' lines and confirm "
            "Gateway is up before restarting the service."
        ),
    )


def _check_account_sentinel() -> DiagnosticCheck:
    s = get_settings()
    try:
        client = get_client()
    except NotConnectedError:
        return DiagnosticCheck(
            name="account_sentinel",
            label="DU prefix sentinel",
            status="skip",
            detail="skipped because the IbkrClient is not initialized",
        )
    account_id = client.connected_account
    if account_id is None:
        return DiagnosticCheck(
            name="account_sentinel",
            label="DU prefix sentinel",
            status="skip",
            detail="skipped because no account has been resolved on the connection",
        )
    is_paper = account_id.upper().startswith("DU")
    if s.mode == "paper" and is_paper:
        return DiagnosticCheck(
            name="account_sentinel",
            label="DU prefix sentinel",
            status="pass",
            detail=f"connected account {account_id} has DU prefix and IBKR_MODE=paper",
        )
    if s.mode == "live" and not is_paper:
        return DiagnosticCheck(
            name="account_sentinel",
            label="DU prefix sentinel",
            status="pass",
            detail=f"connected account {account_id} is non-paper and IBKR_MODE=live",
        )
    return DiagnosticCheck(
        name="account_sentinel",
        label="DU prefix sentinel",
        status="fail",
        detail=f"sentinel mismatch: IBKR_MODE={s.mode} but connected account is {account_id}",
        fix=(
            "This should not happen — the connect()-time sentinel would have refused. "
            "If you see this, restart the service to force a reconnect."
        ),
    )


async def _check_account_fetch() -> DiagnosticCheck:
    try:
        client = get_client()
    except NotConnectedError:
        return DiagnosticCheck(
            name="account_fetch",
            label="fetch_account_summary",
            status="skip",
            detail="skipped because the IbkrClient is not initialized",
        )
    if not client.is_connected():
        return DiagnosticCheck(
            name="account_fetch",
            label="fetch_account_summary",
            status="skip",
            detail="skipped because the ib_async session is closed",
        )
    try:
        snapshot = await ibkr_account.fetch_account_summary(client)
    except BrokerError as exc:
        return DiagnosticCheck(
            name="account_fetch",
            label="fetch_account_summary",
            status="fail",
            detail=f"BrokerError: {exc}",
            fix=(
                "Account summary requires the connection to be established and the "
                "account to have been resolved. If TCP reachability passes but this "
                "fails, the issue is likely on the Gateway side — check API > "
                "Settings > 'Master API client ID' and 'Trusted IPs'."
            ),
        )
    except Exception as exc:
        return DiagnosticCheck(
            name="account_fetch",
            label="fetch_account_summary",
            status="fail",
            detail=f"{type(exc).__name__}: {exc}",
        )
    return DiagnosticCheck(
        name="account_fetch",
        label="fetch_account_summary",
        status="pass",
        detail=(
            f"account={snapshot.account_id} is_paper={snapshot.is_paper} "
            f"cash={snapshot.cash_balance} nlv={snapshot.net_liquidation}"
        ),
    )


def _aggregate_status(checks: list[DiagnosticCheck]) -> str:
    if any(c.status == "fail" for c in checks):
        return "fail"
    if any(c.status == "warn" for c in checks):
        return "warn"
    return "pass"


async def run_diagnostics() -> DiagnosticReport:
    """Run every check and return a :class:`DiagnosticReport`.

    Each check captures its own exception so the report always renders;
    a check that itself fails to run shows up as a ``fail`` row whose
    ``detail`` carries the exception text. Synchronous checks run inline;
    the only async checks are TCP probe and the account fetch, both of
    which are bounded with timeouts.
    """
    checks: list[DiagnosticCheck] = []
    s = get_settings()

    checks.append(_check_settings_mode())
    checks.append(_check_settings_port())
    host_check, resolved_host = _check_host_resolution()
    checks.append(host_check)
    checks.append(await _check_tcp_reachable(resolved_host, s.port))
    checks.append(_check_client_initialized())
    checks.append(_check_client_connected())
    checks.append(_check_account_sentinel())
    checks.append(await _check_account_fetch())

    return DiagnosticReport(
        overall_status=_aggregate_status(checks),  # type: ignore[arg-type]
        checks=checks,
        fetched_at_ms=_now_ms(),
    )


__all__ = ["run_diagnostics"]
